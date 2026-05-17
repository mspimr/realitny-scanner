"""
=============================================================
  REALITNÝ SCANNER v2 — Multi-profil edition
=============================================================
Každý "profil" = samostatný vyhľadávací dotaz so svojimi
kritériami a vlastnou izolovanou databázou výsledkov.

Profily spravuješ cez web dashboard (pridaj / uprav / zmaž).
Scanner beží nepretržite a každý profil skenuje zvlášť.
Zmena kritérií = nová prázdna sada výsledkov pre daný profil.

Inštalácia:
  pip install requests beautifulsoup4 lxml flask

Spustenie (lokálne):
  python scanner.py

Spustenie (produkcia / Railway):
  gunicorn scanner:app --bind 0.0.0.0:$PORT --workers 1 --threads 4
=============================================================
"""

import hashlib
import json
import os
import re
import smtplib
import sqlite3
import threading
import time
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup
from flask import Flask, abort, jsonify, render_template_string, request

# ============================================================
#  GLOBÁLNA KONFIGURÁCIA (env premenné alebo priamo tu)
# ============================================================

ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY",  "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD",  "liptov2025")
EMAIL_ODOSIELATEL  = os.getenv("EMAIL_ODOSIELATEL",   "")
EMAIL_PRIJEMCA     = os.getenv("EMAIL_PRIJEMCA",      "")
EMAIL_HESLO        = os.getenv("EMAIL_HESLO",         "")

DB_FILE = "scanner.db"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "sk-SK,sk;q=0.9",
}

# ============================================================
#  DATABÁZA
# ============================================================

def get_db():
    con = sqlite3.connect(DB_FILE)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = get_db()
    con.executescript("""
        CREATE TABLE IF NOT EXISTS profiles (
            id            TEXT PRIMARY KEY,
            nazov         TEXT NOT NULL,
            kriteria      TEXT NOT NULL,
            zdroje        TEXT NOT NULL,
            aktivny       INTEGER DEFAULT 1,
            interval_min  INTEGER DEFAULT 10,
            tg_min_skore  INTEGER DEFAULT 70,
            vytvoreny     TEXT,
            posledny_scan TEXT
        );

        CREATE TABLE IF NOT EXISTS leads (
            id        TEXT    NOT NULL,
            profil_id TEXT    NOT NULL,
            zdroj     TEXT,
            nazov     TEXT,
            cena      INTEGER DEFAULT 0,
            plocha    INTEGER DEFAULT 0,
            popis     TEXT,
            url       TEXT,
            skore     INTEGER DEFAULT 0,
            seen_at   TEXT,
            PRIMARY KEY (id, profil_id),
            FOREIGN KEY (profil_id) REFERENCES profiles(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS scan_log (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            profil_id TEXT,
            cas       TEXT,
            naskenov  INTEGER DEFAULT 0,
            nove      INTEGER DEFAULT 0,
            leady     INTEGER DEFAULT 0
        );
    """)
    con.commit()
    existing = con.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]
    if existing == 0:
        _vytvor_ukazkovy_profil(con)
    con.close()


def _vytvor_ukazkovy_profil(con):
    pid = _genuj_id("Byty Liptov")
    kriteria = {
        "typ": "byt", "lokalita": "Liptov",
        "max_cena": 200000, "min_cena": 0,
        "min_plocha": 50, "max_plocha": 200, "min_izby": 2,
        "prefer_slova": ["rekonštrukci", "novostavba", "záhrada", "garáž"],
        "vyluc_slova": ["suterén", "dražba"],
        "ai_pokyn": "Uprednostni ponuky po rekonštrukcii alebo novostavby s parkovaním.",
    }
    con.execute(
        "INSERT INTO profiles (id,nazov,kriteria,zdroje,aktivny,interval_min,tg_min_skore,vytvoreny)"
        " VALUES (?,?,?,?,1,10,70,?)",
        (pid, "Byty Liptov",
         json.dumps(kriteria, ensure_ascii=False),
         json.dumps(["nehnutelnosti", "topreality"]),
         datetime.now().isoformat()),
    )
    con.commit()


def _genuj_id(text):
    return hashlib.md5(f"{text}{time.time()}".encode()).hexdigest()[:10]


# ── Profily ──────────────────────────────────────────────────

def db_vsetky_profily():
    con = get_db()
    rows = con.execute("SELECT * FROM profiles ORDER BY vytvoreny").fetchall()
    con.close()
    return [_row_profil(r) for r in rows]


def db_profil(pid):
    con = get_db()
    r = con.execute("SELECT * FROM profiles WHERE id=?", (pid,)).fetchone()
    con.close()
    return _row_profil(r) if r else None


def _row_profil(r):
    d = dict(r)
    d["kriteria"] = json.loads(d["kriteria"])
    d["zdroje"]   = json.loads(d["zdroje"])
    return d


def db_uloz_profil(pid, nazov, kriteria, zdroje, interval_min, tg_min_skore):
    con = get_db()
    existuje = con.execute("SELECT id FROM profiles WHERE id=?", (pid,)).fetchone()
    if existuje:
        con.execute(
            "UPDATE profiles SET nazov=?,kriteria=?,zdroje=?,interval_min=?,tg_min_skore=? WHERE id=?",
            (nazov, json.dumps(kriteria, ensure_ascii=False),
             json.dumps(zdroje), interval_min, tg_min_skore, pid),
        )
        # Zmaž staré leady — nové kritériá = nová sada výsledkov
        con.execute("DELETE FROM leads WHERE profil_id=?", (pid,))
    else:
        con.execute(
            "INSERT INTO profiles (id,nazov,kriteria,zdroje,aktivny,interval_min,tg_min_skore,vytvoreny)"
            " VALUES (?,?,?,?,1,?,?,?)",
            (pid, nazov,
             json.dumps(kriteria, ensure_ascii=False),
             json.dumps(zdroje), interval_min, tg_min_skore,
             datetime.now().isoformat()),
        )
    con.commit()
    con.close()


def db_zmazat_profil(pid):
    con = get_db()
    con.execute("DELETE FROM leads WHERE profil_id=?", (pid,))
    con.execute("DELETE FROM scan_log WHERE profil_id=?", (pid,))
    con.execute("DELETE FROM profiles WHERE id=?", (pid,))
    con.commit()
    con.close()


def db_toggle_profil(pid):
    con = get_db()
    con.execute("UPDATE profiles SET aktivny = 1 - aktivny WHERE id=?", (pid,))
    con.commit()
    stav = con.execute("SELECT aktivny FROM profiles WHERE id=?", (pid,)).fetchone()[0]
    con.close()
    return stav


# ── Leady ────────────────────────────────────────────────────

def db_uloz_lead(lead, profil_id):
    con = get_db()
    con.execute(
        "INSERT OR REPLACE INTO leads"
        " (id,profil_id,zdroj,nazov,cena,plocha,popis,url,skore,seen_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (lead["id"], profil_id,
         lead.get("zdroj", ""), lead.get("nazov", ""),
         lead.get("cena", 0), lead.get("plocha", 0),
         lead.get("popis", ""), lead.get("url", ""),
         lead.get("skore", 0),
         datetime.now().strftime("%Y-%m-%d %H:%M")),
    )
    con.commit()
    con.close()


def db_leady(profil_id, min_skore=0, sort="skore", limit=200):
    sort_sql = {"skore": "skore DESC", "cena": "cena ASC", "datum": "seen_at DESC"}.get(sort, "skore DESC")
    con = get_db()
    rows = con.execute(
        f"SELECT * FROM leads WHERE profil_id=? AND skore>=? ORDER BY {sort_sql} LIMIT ?",
        (profil_id, min_skore, limit),
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


def db_stats(profil_id):
    con = get_db()
    total    = con.execute("SELECT COUNT(*) FROM leads WHERE profil_id=?",            (profil_id,)).fetchone()[0]
    relevant = con.execute("SELECT COUNT(*) FROM leads WHERE profil_id=? AND skore>=60", (profil_id,)).fetchone()[0]
    last     = con.execute(
        "SELECT cas,naskenov,nove,leady FROM scan_log WHERE profil_id=? ORDER BY id DESC LIMIT 1",
        (profil_id,),
    ).fetchone()
    con.close()
    return {
        "total":        total,
        "relevant":     relevant,
        "last_scan":    last["cas"]      if last else "—",
        "last_scanned": last["naskenov"] if last else 0,
        "last_new":     last["nove"]     if last else 0,
        "last_leady":   last["leady"]    if last else 0,
    }


def db_uloz_log(profil_id, naskenov, nove, leady_count):
    con = get_db()
    con.execute(
        "INSERT INTO scan_log (profil_id,cas,naskenov,nove,leady) VALUES (?,?,?,?,?)",
        (profil_id, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), naskenov, nove, leady_count),
    )
    con.execute(
        "UPDATE profiles SET posledny_scan=? WHERE id=?",
        (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), profil_id),
    )
    con.commit()
    con.close()


# ============================================================
#  PARSERY
# ============================================================

def _url_zdroja(profil, zdroj_key):
    k   = profil["kriteria"]
    lok = k.get("lokalita", "").replace(" ", "-").lower()
    typ = k.get("typ", "any")
    tbl = {
        "nehnutelnosti": {
            "byt":     f"https://www.nehnutelnosti.sk/vysledky/byty/{lok}/predaj",
            "dom":     f"https://www.nehnutelnosti.sk/vysledky/domy/{lok}/predaj",
            "pozemok": f"https://www.nehnutelnosti.sk/vysledky/pozemky/{lok}/predaj",
            "any":     f"https://www.nehnutelnosti.sk/vysledky/byty/{lok}/predaj",
        },
        "topreality": {
            "byt":     f"https://www.topreality.sk/vyhladavanie-nehnutelnosti.html?form=1&type%5B%5D=101&type%5B%5D=102&type%5B%5D=103&location={lok}&transaction=1",
            "dom":     f"https://www.topreality.sk/vyhladavanie-nehnutelnosti.html?form=1&type%5B%5D=111&type%5B%5D=112&location={lok}&transaction=1",
            "pozemok": f"https://www.topreality.sk/vyhladavanie-nehnutelnosti.html?form=1&type%5B%5D=301&location={lok}&transaction=1",
            "any":     f"https://www.topreality.sk/vyhladavanie-nehnutelnosti.html?form=1&location={lok}&transaction=1",
        },
        "bazos": {
            "byt":  f"https://reality.bazos.sk/predaj/byt/?hledat={lok}",
            "dom":  f"https://reality.bazos.sk/predaj/dom/?hledat={lok}",
            "any":  f"https://reality.bazos.sk/predaj/?hledat={lok}",
        },
    }
    src = tbl.get(zdroj_key, {})
    return src.get(typ) or src.get("any", "")


def _cislo(el):
    if not el:
        return 0
    text = el.get_text() if hasattr(el, "get_text") else str(el)
    for c in re.findall(r"[\d\s\xa0]+", text):
        c = c.replace(" ", "").replace("\xa0", "")
        if c.isdigit() and len(c) >= 3:
            return int(c)
    return 0


def parse_nehnutelnosti(html, zdroj_nazov):
    soup   = BeautifulSoup(html, "lxml")
    karty  = soup.select("article.advertisement-item, div.advertisement-item, li.advertisement-item") \
             or soup.select("[class*='advertisement-item']")
    result = []
    for k in karty[:40]:
        try:
            a = k.select_one("h2 a,h3 a,.advertisement-item__title a,a[class*='title']")
            if not a: continue
            url = a.get("href", "")
            if url and not url.startswith("http"): url = "https://www.nehnutelnosti.sk" + url
            popis_el = k.select_one("[class*='location'],[class*='locality']")
            result.append({"zdroj": zdroj_nazov, "nazov": a.get_text(strip=True),
                "cena": _cislo(k.select_one("[class*='price']")),
                "plocha": _cislo(k.select_one("[class*='area']")),
                "popis": popis_el.get_text(strip=True)[:300] if popis_el else "",
                "url": url, "id": url.split("/")[-2] if url.count("/") > 3 else url[-16:]})
        except Exception: continue
    return result


def parse_topreality(html, zdroj_nazov):
    soup   = BeautifulSoup(html, "lxml")
    karty  = soup.select(".item,.property-item,article[class*='item']")
    result = []
    for k in karty[:40]:
        try:
            a = k.select_one("h2 a,h3 a,.title a,a.name")
            if not a: continue
            url = a.get("href", "")
            if url and not url.startswith("http"): url = "https://www.topreality.sk" + url
            lok = k.select_one("[class*='location'],.locality,.address")
            result.append({"zdroj": zdroj_nazov, "nazov": a.get_text(strip=True),
                "cena": _cislo(k.select_one(".price,[class*='price']")),
                "plocha": _cislo(k.select_one("[class*='area']")),
                "popis": lok.get_text(strip=True)[:300] if lok else "",
                "url": url, "id": url.split("/")[-1][:20] if url else ""})
        except Exception: continue
    return result


def parse_bazos(html, zdroj_nazov):
    soup   = BeautifulSoup(html, "lxml")
    karty  = soup.select(".inzerat,div[class*='inzerat'],.oglas")
    result = []
    for k in karty[:40]:
        try:
            a = k.select_one("h2 a,.nadpis a")
            if not a: continue
            url = a.get("href", "")
            if url and not url.startswith("http"): url = "https://reality.bazos.sk" + url
            p = k.select_one(".popis,p")
            result.append({"zdroj": zdroj_nazov, "nazov": a.get_text(strip=True),
                "cena": _cislo(k.select_one(".cena,[class*='cena']")),
                "plocha": 0,
                "popis": p.get_text(strip=True)[:300] if p else "",
                "url": url, "id": url.split("/")[-2] if url.count("/") > 3 else url[-16:]})
        except Exception: continue
    return result


PARSERY = {
    "nehnutelnosti": (parse_nehnutelnosti, "nehnutelnosti.sk"),
    "topreality":    (parse_topreality,    "topreality.sk"),
    "bazos":         (parse_bazos,         "bazos.sk"),
}

# ============================================================
#  FILTER & SKÓRE
# ============================================================

def zakladny_filter(p, k):
    cena, plocha = p.get("cena", 0), p.get("plocha", 0)
    if cena   and cena   > k.get("max_cena", 9e9):   return False
    if cena   and cena   < k.get("min_cena", 0):     return False
    if plocha and plocha < k.get("min_plocha", 0):   return False
    if plocha and plocha > k.get("max_plocha", 9e9): return False
    text = (p.get("nazov","") + " " + p.get("popis","")).lower()
    lok  = k.get("lokalita","").lower()
    if lok and lok not in text:                       return False
    for sl in k.get("vyluc_slova", []):
        if sl.lower() in text:                        return False
    return True


def skore_bez_ai(p, k):
    skore = 50
    text  = (p.get("nazov","") + " " + p.get("popis","")).lower()
    for sl in k.get("prefer_slova", []):
        if sl.lower() in text: skore += 8
    if p.get("cena") and p["cena"] < k.get("max_cena", 9e9) * 0.75:
        skore += 12
    return min(skore, 99)


def ai_hodnot(ponuky, k):
    if not ANTHROPIC_API_KEY or not ponuky:
        return {p["id"]: skore_bez_ai(p, k) for p in ponuky}
    zoznam = "\n".join(
        f"[{i+1}] {p['nazov']} | {p.get('cena',0)}€ | {p.get('plocha',0)}m² | {p.get('popis','')[:100]}"
        for i, p in enumerate(ponuky)
    )
    prompt = (f"Si realitný analytik. Ohodnoť ponuky (0-100).\n"
              f"Kritériá: max {k.get('max_cena',0)}€, lokalita {k.get('lokalita','')}, "
              f"pokyn: {k.get('ai_pokyn','')}\n\nPonuky:\n{zoznam}\n\n"
              f"Odpovedz IBA JSON: {{\"1\":85,\"2\":40,...}}")
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 300,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=20,
        )
        mapa = json.loads(r.json()["content"][0]["text"].strip())
        return {ponuky[int(ki)-1]["id"]: int(v) for ki, v in mapa.items() if int(ki)-1 < len(ponuky)}
    except Exception as e:
        _log(f"AI chyba: {e}", "warn")
        return {p["id"]: skore_bez_ai(p, k) for p in ponuky}


# ============================================================
#  NOTIFIKÁCIE
# ============================================================

def posli_telegram(lead, profil_nazov, min_skore):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    if lead.get("skore", 0) < min_skore: return
    skore = lead.get("skore", 0)
    cena  = f"{lead['cena']:,} €" if lead.get("cena") else "cena neuvedená"
    plocha = f"  ·  📐 {lead['plocha']} m²" if lead.get("plocha") else ""
    txt = (f"{'🔥' if skore >= 85 else '✅'} *{lead['nazov']}*\n"
           f"💶 {cena}{plocha}\n"
           f"📁 _{profil_nazov}_  |  Skóre: {skore}%\n"
           f"📍 {lead.get('zdroj','')}\n")
    if lead.get("popis"):   txt += f"\n_{lead['popis'][:180]}_\n"
    if lead.get("url"):     txt += f"\n[Zobraziť →]({lead['url']})"
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": txt,
                  "parse_mode": "Markdown", "disable_web_page_preview": False}, timeout=10)
    except Exception: pass


def posli_email(leady, profil_nazov):
    if not EMAIL_ODOSIELATEL or not EMAIL_HESLO or not leady: return
    telo = f"<h2>🔥 Nové leady — {profil_nazov}</h2><hr>"
    for l in leady:
        telo += (f"<div style='margin:14px 0;padding:12px;border:1px solid #eee;border-radius:8px'>"
                 f"<h3>{l['nazov']}</h3><p>{l.get('cena',0):,}€ · {l.get('plocha',0)}m² · {l['skore']}%</p>"
                 f"<p>{l.get('popis','')[:200]}</p><a href='{l['url']}'>Zobraziť →</a></div>")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🏠 {len(leady)} leadov [{profil_nazov}] {datetime.now().strftime('%d.%m %H:%M')}"
    msg["From"] = EMAIL_ODOSIELATEL; msg["To"] = EMAIL_PRIJEMCA
    msg.attach(MIMEText(telo, "html", "utf-8"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as s:
            s.starttls(); s.login(EMAIL_ODOSIELATEL, EMAIL_HESLO); s.send_message(msg)
    except Exception as e:
        _log(f"Email chyba: {e}", "warn")


# ============================================================
#  SCAN JEDNÉHO PROFILU
# ============================================================

_seen_cache: dict = {}   # profil_id → set of seen IDs


def _seen(profil_id):
    if profil_id not in _seen_cache:
        con = get_db()
        ids = {r[0] for r in con.execute("SELECT id FROM leads WHERE profil_id=?", (profil_id,)).fetchall()}
        con.close()
        _seen_cache[profil_id] = ids
    return _seen_cache[profil_id]


def scan_profil(profil):
    pid   = profil["id"]
    nazov = profil["nazov"]
    k     = profil["kriteria"]
    seen  = _seen(pid)

    _log(f"[{nazov}] Štart")
    vsetky_nove = []

    for zdroj_key in profil["zdroje"]:
        if zdroj_key not in PARSERY: continue
        parse_fn, zdroj_nazov = PARSERY[zdroj_key]
        url = _url_zdroja(profil, zdroj_key)
        if not url: continue
        try:
            resp   = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            ponuky = parse_fn(resp.text, zdroj_nazov)
            nove   = [p for p in ponuky if p["id"] not in seen]
            _log(f"  [{nazov}] {zdroj_nazov}: {len(ponuky)} celkom, {len(nove)} nových")
            vsetky_nove.extend(nove)
            for p in ponuky: seen.add(p["id"])
        except Exception as e:
            _log(f"  [{nazov}] Chyba {zdroj_key}: {e}", "warn")
        time.sleep(1.2)

    if not vsetky_nove:
        db_uloz_log(pid, 0, 0, 0)
        return

    prefilter = [p for p in vsetky_nove if zakladny_filter(p, k)]
    _log(f"[{nazov}] Filter: {len(vsetky_nove)} → {len(prefilter)}")

    skore_map = ai_hodnot(prefilter, k)

    leady = []
    for p in prefilter:
        p["skore"] = skore_map.get(p["id"], skore_bez_ai(p, k))
        db_uloz_lead(p, pid)
        if p["skore"] >= 60: leady.append(p)

    leady.sort(key=lambda x: x["skore"], reverse=True)
    db_uloz_log(pid, len(vsetky_nove), len(prefilter), len(leady))
    _log(f"[{nazov}] ✅ {len(leady)} leadov (skóre ≥ 60)")

    for lead in leady:
        posli_telegram(lead, nazov, profil.get("tg_min_skore", 70))
        time.sleep(0.2)
    posli_email([l for l in leady if l["skore"] >= 70], nazov)


# ============================================================
#  SCHEDULER
# ============================================================

_next_scan: dict = {}


def scheduler_loop():
    while True:
        now = time.time()
        for p in db_vsetky_profily():
            if not p["aktivny"]: continue
            pid = p["id"]
            if pid not in _next_scan: _next_scan[pid] = now
            if now >= _next_scan[pid]:
                try:    scan_profil(p)
                except Exception as e: _log(f"[{p['nazov']}] Chyba: {e}", "warn")
                _next_scan[pid] = time.time() + p["interval_min"] * 60
        time.sleep(15)


def _log(msg, typ=""):
    ikona = {"ok": "✅", "warn": "⚠️", "err": "❌"}.get(typ, "·")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {ikona} {msg}")


# ============================================================
#  FLASK APP + HTML
# ============================================================

app = Flask(__name__)


def check_auth():
    t = request.args.get("token") or request.cookies.get("token", "")
    if t != DASHBOARD_PASSWORD: abort(401)


HTML = r"""<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Realitný scanner</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f3f0;color:#1a1a18;font-size:15px;min-height:100vh}
header{background:#fff;border-bottom:1px solid #e0dfd8;padding:0 20px;display:flex;align-items:center;height:52px;gap:10px}
header h1{font-size:17px;font-weight:600;margin-right:auto}
.hdr-info{font-size:12px;color:#aaa}

/* Tab bar */
.tab-bar{display:flex;background:#fff;border-bottom:1px solid #e0dfd8;padding:0 16px;overflow-x:auto;gap:2px;align-items:stretch}
.tab-btn{padding:10px 16px;font-size:13px;cursor:pointer;border:none;background:none;color:#888;border-bottom:2px solid transparent;white-space:nowrap;font-weight:500;display:flex;align-items:center;gap:6px}
.tab-btn.active{color:#111;border-bottom-color:#111}
.tab-btn .cnt{font-size:10px;background:#eee;border-radius:10px;padding:1px 6px;min-width:20px;text-align:center}
.tab-btn.active .cnt{background:#111;color:#fff}
.tab-btn.paused{opacity:.5}
.add-tab{padding:10px 14px;font-size:20px;cursor:pointer;border:none;background:none;color:#bbb;line-height:1;margin-left:4px}
.add-tab:hover{color:#333}

/* Pane */
.pane{display:none;padding:20px 20px 60px;max-width:900px;margin:0 auto}
.pane.active{display:block}

/* Stats */
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;margin-bottom:18px}
.stat{background:#fff;border:1px solid #e0dfd8;border-radius:10px;padding:12px;text-align:center}
.stat .n{font-size:22px;font-weight:600}
.stat .l{font-size:11px;color:#aaa;margin-top:2px}

/* Controls */
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
select,input[type=number]{font-size:13px;padding:5px 9px;border:1px solid #ddd;border-radius:7px;background:#fff}
.btn{font-size:13px;padding:6px 14px;border:1px solid #ddd;border-radius:7px;background:#fff;cursor:pointer;white-space:nowrap}
.btn:hover{background:#f0ede8}
.btn-danger{border-color:#f5c6c6;color:#b00}
.btn-danger:hover{background:#fff5f5}
.btn-ok{border-color:#b8e6cc;color:#085}
.btn-ok:hover{background:#f0fff8}

/* Cards */
.leads{display:grid;gap:10px}
.card{background:#fff;border:1px solid #e0dfd8;border-radius:10px;padding:14px 16px}
.card-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:5px}
.card-title{font-size:15px;font-weight:600;line-height:1.3;flex:1}
.card-price{font-size:15px;font-weight:600;color:#1a4f8a;white-space:nowrap}
.card-meta{font-size:12px;color:#aaa;margin-bottom:5px}
.card-desc{font-size:13px;color:#666;line-height:1.5;margin-bottom:8px}
.card-foot{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.badge{font-size:11px;padding:2px 8px;border-radius:20px;font-weight:600}
.badge-top{background:#fff0d0;color:#7a3800}
.badge-ok{background:#e0f5ea;color:#0a5a30}
.src{font-size:10px;border:1px solid #ddd;border-radius:20px;padding:1px 7px;color:#aaa}
.bar-wrap{flex:1;min-width:40px;height:4px;background:#eee;border-radius:2px}
.bar{height:100%;border-radius:2px;background:#1D9E75}
a.ext{font-size:12px;color:#1a4f8a;text-decoration:none;margin-left:auto}
a.ext:hover{text-decoration:underline}
.empty{text-align:center;padding:40px;color:#ccc;font-size:14px;line-height:2}

/* Modal */
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:100;overflow-y:auto;padding:20px}
.modal-box{background:#fff;border-radius:14px;max-width:600px;margin:0 auto;padding:24px}
.modal-box h2{font-size:17px;margin-bottom:16px}
.form-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:8px}
.form-group{display:flex;flex-direction:column;gap:4px}
.form-group label{font-size:12px;color:#888;font-weight:500}
.form-group input,.form-group select,.form-group textarea{
  font-size:13px;padding:7px 10px;border:1px solid #ddd;border-radius:7px;
  background:#fff;font-family:inherit;width:100%}
.form-group textarea{resize:vertical;min-height:62px}
.form-full{grid-column:1/-1}
.sec{font-size:12px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:.05em;
     margin:14px 0 6px;padding-top:12px;border-top:1px solid #f0ede8}
.chip-row{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}
.chip{font-size:12px;padding:4px 11px;border:1px solid #ddd;border-radius:20px;
      background:#f8f7f4;cursor:pointer;user-select:none;transition:all .12s}
.chip.on{background:#111;color:#fff;border-color:#111}
.form-actions{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap}
@media(max-width:580px){.form-grid{grid-template-columns:1fr}.card-top{flex-direction:column}}
</style>
</head>
<body>

<header>
  <span style="font-size:20px">🏠</span>
  <h1>Realitný scanner</h1>
  <span class="hdr-info" id="hdr-info"></span>
</header>

<div class="tab-bar" id="tab-bar">
  <button class="add-tab" title="Nový profil" onclick="openModal()">＋</button>
</div>

<div id="panes"></div>

<!-- Modal -->
<div class="overlay" id="overlay" onclick="if(event.target===this)closeModal()">
  <div class="modal-box">
    <h2 id="modal-title">Nový profil</h2>
    <input type="hidden" id="f-pid">

    <div class="form-grid">
      <div class="form-group form-full">
        <label>Názov profilu *</label>
        <input id="f-nazov" placeholder="napr. Byty Liptov, Domy Ružomberok...">
      </div>
      <div class="form-group">
        <label>Typ nehnuteľnosti</label>
        <select id="f-typ">
          <option value="byt">Byt</option>
          <option value="dom">Rodinný dom</option>
          <option value="pozemok">Pozemok</option>
          <option value="any">Akýkoľvek</option>
        </select>
      </div>
      <div class="form-group">
        <label>Lokalita *</label>
        <input id="f-lokalita" placeholder="napr. Liptov, Ružomberok">
      </div>
      <div class="form-group">
        <label>Min. cena (€)</label>
        <input id="f-mincena" type="number" placeholder="0">
      </div>
      <div class="form-group">
        <label>Max. cena (€)</label>
        <input id="f-maxcena" type="number" placeholder="200000">
      </div>
      <div class="form-group">
        <label>Min. plocha (m²)</label>
        <input id="f-minplocha" type="number" placeholder="50">
      </div>
      <div class="form-group">
        <label>Max. plocha (m²)</label>
        <input id="f-maxplocha" type="number" placeholder="200">
      </div>
      <div class="form-group">
        <label>Min. počet izieb</label>
        <select id="f-izby">
          <option value="1">1+</option><option value="2" selected>2+</option>
          <option value="3">3+</option><option value="4">4+</option>
        </select>
      </div>
      <div class="form-group">
        <label>Interval skenovania</label>
        <select id="f-interval">
          <option value="5">každých 5 minút</option>
          <option value="10" selected>každých 10 minút</option>
          <option value="15">každých 15 minút</option>
          <option value="30">každých 30 minút</option>
          <option value="60">raz za hodinu</option>
        </select>
      </div>
    </div>

    <div class="sec">Zdroje</div>
    <div class="chip-row" id="f-zdroje">
      <span class="chip on" data-v="nehnutelnosti">nehnutelnosti.sk</span>
      <span class="chip on" data-v="topreality">topreality.sk</span>
      <span class="chip"    data-v="bazos">bazos.sk</span>
    </div>

    <div class="sec">Kľúčové slová</div>
    <div class="form-grid">
      <div class="form-group">
        <label>Preferované (čiarkami)</label>
        <input id="f-prefer" placeholder="rekonštrukci,novostavba,záhrada">
      </div>
      <div class="form-group">
        <label>Vylúčiť (čiarkami)</label>
        <input id="f-vyluc" placeholder="suterén,dražba">
      </div>
      <div class="form-group form-full">
        <label>AI pokyn (voľný text)</label>
        <textarea id="f-ai" placeholder="Uprednostni novostavby s garážou a blízkosťou prírody..."></textarea>
      </div>
    </div>

    <div class="sec">Notifikácie</div>
    <div class="form-grid">
      <div class="form-group">
        <label>Telegram — min. skóre (%)</label>
        <input id="f-tgskore" type="number" value="70" min="0" max="100">
      </div>
    </div>

    <div class="form-actions">
      <button class="btn" onclick="closeModal()">Zrušiť</button>
      <button class="btn btn-ok"     onclick="ulozProfil()">💾 Uložiť</button>
      <button class="btn btn-danger" id="btn-delete" style="display:none" onclick="zmazatProfil()">🗑 Zmazať profil</button>
    </div>
  </div>
</div>

<script>
const TOKEN = new URLSearchParams(location.search).get('token') || '';
const api  = u => fetch(u + (u.includes('?') ? '&' : '?') + 'token=' + TOKEN).then(r => r.json());
const post = (u, b) => fetch(u + '?token=' + TOKEN, {method:'POST',
  headers:{'Content-Type':'application/json'}, body:JSON.stringify(b)}).then(r => r.json());

let profily = [], activePid = null;

// ── Load & render tabs ──────────────────────────────────────
async function reload() {
  profily = await api('/api/profily');
  renderTabs();
  if (!activePid && profily.length) activePid = profily[0].id;
  if (activePid) switchTab(activePid, false);
}

function renderTabs() {
  const bar  = document.getElementById('tab-bar');
  bar.querySelectorAll('.tab-btn').forEach(b => b.remove());
  const add  = bar.querySelector('.add-tab');
  profily.forEach(p => {
    const b = document.createElement('button');
    b.className = 'tab-btn' + (p.id===activePid?' active':'') + (!p.aktivny?' paused':'');
    b.dataset.pid = p.id;
    b.innerHTML = `${p.nazov}<span class="cnt" id="cnt-${p.id}">…</span>`;
    b.onclick = () => switchTab(p.id, true);
    bar.insertBefore(b, add);
  });
  // Profil count in header
  document.getElementById('hdr-info').textContent =
    profily.length + (profily.length===1?' profil':' profily');
}

// ── Switch tab ──────────────────────────────────────────────
async function switchTab(pid, doRefresh=true) {
  activePid = pid;
  document.querySelectorAll('.tab-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.pid === pid));
  // Build pane if missing
  if (!document.getElementById('pane-'+pid)) {
    const d = document.createElement('div');
    d.className = 'pane'; d.id = 'pane-'+pid;
    d.innerHTML = buildPane(pid);
    document.getElementById('panes').appendChild(d);
  }
  document.querySelectorAll('.pane').forEach(p => p.classList.remove('active'));
  document.getElementById('pane-'+pid).classList.add('active');
  if (doRefresh) await refreshPane(pid);
}

// ── Build pane HTML ─────────────────────────────────────────
function buildPane(pid) {
  return `
  <div class="stats">
    <div class="stat"><div class="n" id="s-total-${pid}">—</div><div class="l">ponúk celkom</div></div>
    <div class="stat"><div class="n" id="s-rel-${pid}">—</div><div class="l">relevantných (≥60%)</div></div>
    <div class="stat"><div class="n" id="s-last-${pid}">—</div><div class="l">posledný scan</div></div>
    <div class="stat"><div class="n" id="s-new-${pid}">—</div><div class="l">nových naposledy</div></div>
  </div>
  <div class="controls">
    <select id="sort-${pid}" onchange="refreshPane('${pid}')">
      <option value="skore">Skóre ↓</option>
      <option value="cena">Cena ↑</option>
      <option value="datum">Najnovšie</option>
    </select>
    <select id="min-${pid}" onchange="refreshPane('${pid}')">
      <option value="0">Všetky</option>
      <option value="50">50%+</option>
      <option value="60" selected>60%+</option>
      <option value="75">75%+</option>
      <option value="90">90%+</option>
    </select>
    <button class="btn" onclick="refreshPane('${pid}')">🔄 Obnoviť</button>
    <button class="btn" onclick="editProfil('${pid}')">⚙️ Upraviť</button>
    <button class="btn" id="tog-${pid}" onclick="toggleProfil('${pid}')">⏸ Pozastaviť</button>
  </div>
  <div class="leads" id="leads-${pid}"><div class="empty">Načítavam…</div></div>`;
}

// ── Refresh pane data ───────────────────────────────────────
async function refreshPane(pid) {
  const sort = document.getElementById('sort-'+pid)?.value || 'skore';
  const min  = document.getElementById('min-'+pid)?.value  || 60;
  const [stats, leads] = await Promise.all([
    api(`/api/stats/${pid}`),
    api(`/api/leads/${pid}?sort=${sort}&min_skore=${min}`),
  ]);
  // Fill stats
  document.getElementById('s-total-'+pid).textContent = stats.total;
  document.getElementById('s-rel-'+pid).textContent   = stats.relevant;
  document.getElementById('s-last-'+pid).textContent  = (stats.last_scan||'—').slice(11,16) || '—';
  document.getElementById('s-new-'+pid).textContent   = stats.last_new;
  // Tab badge
  const cnt = document.getElementById('cnt-'+pid);
  if (cnt) cnt.textContent = stats.relevant > 0 ? stats.relevant : '—';
  // Toggle btn
  const p = profily.find(x => x.id===pid);
  const tog = document.getElementById('tog-'+pid);
  if (p && tog) tog.textContent = p.aktivny ? '⏸ Pozastaviť' : '▶ Spustiť';
  // Leads
  renderLeads(pid, leads);
}

function renderLeads(pid, leads) {
  const el = document.getElementById('leads-'+pid);
  if (!leads.length) {
    el.innerHTML = '<div class="empty">Žiadne leady pre tieto kritériá.<br><small style="color:#ddd">Scanner zbiera výsledky podľa nastaveného intervalu.</small></div>';
    return;
  }
  el.innerHTML = leads.map(l => {
    const cena  = l.cena ? l.cena.toLocaleString('sk-SK')+' €' : 'cena neuvedená';
    const plocha = l.plocha ? ` · ${l.plocha} m²` : '';
    const badge  = l.skore>=85
      ? '<span class="badge badge-top">🔥 Top lead</span>'
      : '<span class="badge badge-ok">✓ Relevantné</span>';
    return `<div class="card">
      <div class="card-top">
        <span class="card-title">${l.nazov}</span>
        <span class="card-price">${cena}</span>
      </div>
      <div class="card-meta">${l.seen_at||''}${plocha}</div>
      ${l.popis?`<div class="card-desc">${l.popis.slice(0,220)}</div>`:''}
      <div class="card-foot">
        ${badge}
        <span class="src">${l.zdroj}</span>
        <div class="bar-wrap"><div class="bar" style="width:${l.skore}%"></div></div>
        <span style="font-size:11px;color:#bbb">${l.skore}%</span>
        <a class="ext" href="${l.url}" target="_blank">Zobraziť →</a>
      </div>
    </div>`;
  }).join('');
}

// ── Modal ───────────────────────────────────────────────────
function openModal(p=null) {
  document.getElementById('modal-title').textContent = p ? 'Upraviť profil' : 'Nový profil';
  document.getElementById('f-pid').value      = p?.id || '';
  document.getElementById('f-nazov').value    = p?.nazov || '';
  document.getElementById('f-typ').value      = p?.kriteria?.typ || 'byt';
  document.getElementById('f-lokalita').value = p?.kriteria?.lokalita || '';
  document.getElementById('f-mincena').value  = p?.kriteria?.min_cena || '';
  document.getElementById('f-maxcena').value  = p?.kriteria?.max_cena || 200000;
  document.getElementById('f-minplocha').value= p?.kriteria?.min_plocha || 50;
  document.getElementById('f-maxplocha').value= p?.kriteria?.max_plocha || 200;
  document.getElementById('f-izby').value     = p?.kriteria?.min_izby || 2;
  document.getElementById('f-interval').value = p?.interval_min || 10;
  document.getElementById('f-prefer').value   = (p?.kriteria?.prefer_slova||['rekonštrukci','novostavba','záhrada','garáž']).join(',');
  document.getElementById('f-vyluc').value    = (p?.kriteria?.vyluc_slova||['suterén','dražba']).join(',');
  document.getElementById('f-ai').value       = p?.kriteria?.ai_pokyn || '';
  document.getElementById('f-tgskore').value  = p?.tg_min_skore || 70;
  document.querySelectorAll('#f-zdroje .chip').forEach(c =>
    c.classList.toggle('on', p ? (p.zdroje||[]).includes(c.dataset.v) : ['nehnutelnosti','topreality'].includes(c.dataset.v)));
  document.getElementById('btn-delete').style.display = p ? '' : 'none';
  document.getElementById('overlay').style.display = 'block';
}

function editProfil(pid) { openModal(profily.find(p => p.id===pid)); }
function closeModal()     { document.getElementById('overlay').style.display = 'none'; }

document.addEventListener('click', e => {
  if (e.target.closest('#f-zdroje')?.contains(e.target) && e.target.classList.contains('chip'))
    e.target.classList.toggle('on');
});

async function ulozProfil() {
  const nazov = document.getElementById('f-nazov').value.trim();
  const lok   = document.getElementById('f-lokalita').value.trim();
  if (!nazov) { alert('Zadaj názov profilu'); return; }
  if (!lok)   { alert('Zadaj lokalitu'); return; }
  const zdroje = [...document.querySelectorAll('#f-zdroje .chip.on')].map(c=>c.dataset.v);
  if (!zdroje.length) { alert('Vyber aspoň jeden zdroj'); return; }
  const pid = document.getElementById('f-pid').value || null;
  const body = {
    pid, nazov,
    interval_min: +document.getElementById('f-interval').value,
    tg_min_skore: +document.getElementById('f-tgskore').value,
    zdroje,
    kriteria: {
      typ:          document.getElementById('f-typ').value,
      lokalita:     lok,
      min_cena:     +document.getElementById('f-mincena').value || 0,
      max_cena:     +document.getElementById('f-maxcena').value || 999999,
      min_plocha:   +document.getElementById('f-minplocha').value || 0,
      max_plocha:   +document.getElementById('f-maxplocha').value || 9999,
      min_izby:     +document.getElementById('f-izby').value || 1,
      prefer_slova: document.getElementById('f-prefer').value.split(',').map(s=>s.trim()).filter(Boolean),
      vyluc_slova:  document.getElementById('f-vyluc').value.split(',').map(s=>s.trim()).filter(Boolean),
      ai_pokyn:     document.getElementById('f-ai').value.trim(),
    },
  };
  const res = await post('/api/profil/uloz', body);
  if (res.ok) {
    closeModal();
    activePid = res.pid;
    // Zmaž starý pane — kritériá sa zmenili, výsledky sa resetujú
    const old = document.getElementById('pane-'+res.pid);
    if (old) old.remove();
    await reload();
  } else alert('Chyba: '+(res.error||'neznáma'));
}

async function zmazatProfil() {
  const pid = document.getElementById('f-pid').value;
  if (!pid || !confirm('Zmazať profil aj so všetkými leadmi?')) return;
  await post('/api/profil/zmazat', {pid});
  closeModal();
  const old = document.getElementById('pane-'+pid);
  if (old) old.remove();
  activePid = null;
  await reload();
}

async function toggleProfil(pid) {
  const res = await post('/api/profil/toggle', {pid});
  if (res.ok) {
    const p = profily.find(x=>x.id===pid);
    if (p) { p.aktivny = res.aktivny; renderTabs(); }
    const tog = document.getElementById('tog-'+pid);
    if (tog) tog.textContent = res.aktivny ? '⏸ Pozastaviť' : '▶ Spustiť';
  }
}

// ── Boot & auto-refresh ─────────────────────────────────────
reload();
setInterval(async () => { if (activePid) await refreshPane(activePid); }, 60000);
</script>
</body>
</html>"""


# ── API ──────────────────────────────────────────────────────

@app.route("/")
def index():
    if request.args.get("token", "") != DASHBOARD_PASSWORD:
        return """<html><body style='font-family:sans-serif;text-align:center;padding:80px'>
        <h2>🔒 Realitný scanner</h2>
        <form><input name='token' type='password' placeholder='Heslo'
          style='padding:8px;font-size:15px;border:1px solid #ddd;border-radius:6px;margin-right:8px'>
        <button type='submit' style='padding:8px 16px;border:1px solid #ddd;border-radius:6px;cursor:pointer'>
        Prihlásiť</button></form></body></html>"""
    return render_template_string(HTML)


@app.route("/api/profily")
def api_profily():
    check_auth(); return jsonify(db_vsetky_profily())


@app.route("/api/profil/uloz", methods=["POST"])
def api_uloz():
    check_auth()
    d   = request.json
    pid = d.get("pid") or _genuj_id(d.get("nazov","profil"))
    db_uloz_profil(pid, d["nazov"], d["kriteria"], d["zdroje"],
                   d.get("interval_min", 10), d.get("tg_min_skore", 70))
    _seen_cache.pop(pid, None)   # reset seen cache
    _next_scan[pid] = 0          # scan hneď
    return jsonify({"ok": True, "pid": pid})


@app.route("/api/profil/zmazat", methods=["POST"])
def api_zmazat():
    check_auth()
    pid = request.json.get("pid")
    if pid:
        db_zmazat_profil(pid)
        _seen_cache.pop(pid, None)
        _next_scan.pop(pid, None)
    return jsonify({"ok": True})


@app.route("/api/profil/toggle", methods=["POST"])
def api_toggle():
    check_auth()
    pid  = request.json.get("pid")
    stav = db_toggle_profil(pid) if pid else 0
    return jsonify({"ok": True, "aktivny": stav})


@app.route("/api/leads/<pid>")
def api_leads(pid):
    check_auth()
    return jsonify(db_leady(pid,
        min_skore=int(request.args.get("min_skore", 60)),
        sort=request.args.get("sort", "skore")))


@app.route("/api/stats/<pid>")
def api_stats(pid):
    check_auth(); return jsonify(db_stats(pid))


# ============================================================
#  ŠTART
# ============================================================

if __name__ == "__main__":
    init_db()
    threading.Thread(target=scheduler_loop, daemon=True).start()
    _log("=" * 52)
    _log("REALITNÝ SCANNER v2 — Multi-profil")
    _log(f"Dashboard : http://localhost:5000?token={DASHBOARD_PASSWORD}")
    _log(f"AI filter : {'áno (Claude)' if ANTHROPIC_API_KEY else 'nie (základný)'}")
    _log(f"Telegram  : {'áno' if TELEGRAM_BOT_TOKEN else 'nie'}")
    _log("=" * 52)
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)), debug=False)
