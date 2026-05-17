"""
=============================================================
  REALITNÝ SCANNER v2 — Multi-profil
=============================================================
Railway: pridaj PostgreSQL plugin → DATABASE_URL sa nastaví auto.
Lokálne: beží na SQLite automaticky.

pip install flask requests beautifulsoup4 lxml psycopg2-binary gunicorn
python scanner.py
=============================================================
"""

import hashlib, json, os, re, smtplib, threading, time
from contextlib import contextmanager
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from bs4 import BeautifulSoup
from flask import Flask, abort, jsonify, render_template_string, request

# ============================================================
#  KONFIGURÁCIA
# ============================================================
ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY",  "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID",   "")
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD",  "liptov2025")
EMAIL_ODOSIELATEL  = os.getenv("EMAIL_ODOSIELATEL",   "")
EMAIL_PRIJEMCA     = os.getenv("EMAIL_PRIJEMCA",      "")
EMAIL_HESLO        = os.getenv("EMAIL_HESLO",         "")
DATABASE_URL       = os.getenv("DATABASE_URL",        "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "sk-SK,sk;q=0.9",
}

# ============================================================
#  DB VRSTVA — PostgreSQL (Railway) alebo SQLite (lokálne)
# ============================================================
USE_PG = bool(DATABASE_URL)
PH = "%s" if USE_PG else "?"

if USE_PG:
    import psycopg2, psycopg2.extras
    _pg_url = DATABASE_URL.replace("postgres://", "postgresql://", 1)

    @contextmanager
    def get_db():
        con = psycopg2.connect(_pg_url, cursor_factory=psycopg2.extras.RealDictCursor)
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback(); raise
        finally:
            con.close()

else:
    import sqlite3

    @contextmanager
    def get_db():
        con = sqlite3.connect("scanner.db")
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback(); raise
        finally:
            con.close()


def _r(row):
    return dict(row) if row else None


def init_db():
    serial = "SERIAL" if USE_PG else "INTEGER"
    autoincrement = "" if USE_PG else "AUTOINCREMENT"
    conflict_leads = (
        "ON CONFLICT (id,profil_id) DO UPDATE SET skore=EXCLUDED.skore,seen_at=EXCLUDED.seen_at"
        if USE_PG else "OR REPLACE"
    )

    stmts = [
        """CREATE TABLE IF NOT EXISTS profiles (
            id TEXT PRIMARY KEY, nazov TEXT NOT NULL,
            kriteria TEXT NOT NULL, zdroje TEXT NOT NULL,
            aktivny INTEGER DEFAULT 1, interval_min INTEGER DEFAULT 10,
            tg_min_skore INTEGER DEFAULT 70, vytvoreny TEXT, posledny_scan TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS leads (
            id TEXT NOT NULL, profil_id TEXT NOT NULL, zdroj TEXT,
            nazov TEXT, cena INTEGER DEFAULT 0, plocha INTEGER DEFAULT 0,
            popis TEXT, url TEXT, skore INTEGER DEFAULT 0, seen_at TEXT,
            PRIMARY KEY (id, profil_id)
        )""",
        f"""CREATE TABLE IF NOT EXISTS scan_log (
            id {serial} PRIMARY KEY {autoincrement},
            profil_id TEXT, cas TEXT,
            naskenov INTEGER DEFAULT 0, nove INTEGER DEFAULT 0, leady INTEGER DEFAULT 0
        )""",
    ]
    with get_db() as con:
        cur = con.cursor()
        for s in stmts:
            cur.execute(s)
        cur.execute("SELECT COUNT(*) as c FROM profiles")
        row = cur.fetchone()
        cnt = row["c"] if USE_PG else row[0]
        if cnt == 0:
            pid = _gid("Byty Liptov")
            k = json.dumps({"typ":"byt","lokalita":"Liptov","max_cena":200000,"min_cena":0,
                "min_plocha":50,"max_plocha":200,"min_izby":2,
                "prefer_slova":["rekonštrukci","novostavba","záhrada","garáž"],
                "vyluc_slova":["suterén","dražba"],
                "ai_pokyn":"Uprednostni ponuky po rekonštrukcii alebo novostavby s parkovaním."},
                ensure_ascii=False)
            z = json.dumps(["nehnutelnosti","topreality"])
            cur.execute(
                f"INSERT INTO profiles (id,nazov,kriteria,zdroje,aktivny,interval_min,tg_min_skore,vytvoreny)"
                f" VALUES ({PH},{PH},{PH},{PH},1,10,70,{PH})",
                (pid,"Byty Liptov",k,z,datetime.now().isoformat()))


def _gid(t):
    return hashlib.md5(f"{t}{time.time()}".encode()).hexdigest()[:10]


# ── Profily ──────────────────────────────────────────────────

def _pp(d):
    d["kriteria"] = json.loads(d["kriteria"])
    d["zdroje"]   = json.loads(d["zdroje"])
    return d


def db_vsetky_profily():
    with get_db() as con:
        cur = con.cursor()
        cur.execute("SELECT * FROM profiles ORDER BY vytvoreny")
        return [_pp(_r(r)) for r in cur.fetchall()]


def db_uloz_profil(pid, nazov, kriteria, zdroje, interval_min, tg_min_skore):
    kj = json.dumps(kriteria, ensure_ascii=False)
    zj = json.dumps(zdroje)
    with get_db() as con:
        cur = con.cursor()
        cur.execute(f"SELECT id FROM profiles WHERE id={PH}", (pid,))
        if cur.fetchone():
            cur.execute(
                f"UPDATE profiles SET nazov={PH},kriteria={PH},zdroje={PH},"
                f"interval_min={PH},tg_min_skore={PH} WHERE id={PH}",
                (nazov,kj,zj,interval_min,tg_min_skore,pid))
            cur.execute(f"DELETE FROM leads WHERE profil_id={PH}", (pid,))
        else:
            cur.execute(
                f"INSERT INTO profiles (id,nazov,kriteria,zdroje,aktivny,interval_min,tg_min_skore,vytvoreny)"
                f" VALUES ({PH},{PH},{PH},{PH},1,{PH},{PH},{PH})",
                (pid,nazov,kj,zj,interval_min,tg_min_skore,datetime.now().isoformat()))


def db_zmazat_profil(pid):
    with get_db() as con:
        cur = con.cursor()
        for tbl,col in [("leads","profil_id"),("scan_log","profil_id"),("profiles","id")]:
            cur.execute(f"DELETE FROM {tbl} WHERE {col}={PH}", (pid,))


def db_profil(pid):
    with get_db() as con:
        cur = con.cursor()
        cur.execute(f"SELECT * FROM profiles WHERE id={PH}", (pid,))
        r = cur.fetchone()
    return _pp(_r(r)) if r else None


def db_toggle_profil(pid):
    with get_db() as con:
        cur = con.cursor()
        cur.execute(f"UPDATE profiles SET aktivny=1-aktivny WHERE id={PH}", (pid,))
        cur.execute(f"SELECT aktivny FROM profiles WHERE id={PH}", (pid,))
        row = cur.fetchone()
        return row["aktivny"] if USE_PG else row[0]


# ── Leady ────────────────────────────────────────────────────

def db_uloz_lead(lead, profil_id):
    with get_db() as con:
        cur = con.cursor()
        if USE_PG:
            cur.execute(
                "INSERT INTO leads (id,profil_id,zdroj,nazov,cena,plocha,popis,url,skore,seen_at)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)"
                " ON CONFLICT (id,profil_id) DO UPDATE SET skore=EXCLUDED.skore,seen_at=EXCLUDED.seen_at",
                (lead["id"],profil_id,lead.get("zdroj",""),lead.get("nazov",""),
                 lead.get("cena",0),lead.get("plocha",0),lead.get("popis",""),
                 lead.get("url",""),lead.get("skore",0),datetime.now().strftime("%Y-%m-%d %H:%M")))
        else:
            cur.execute(
                "INSERT OR REPLACE INTO leads (id,profil_id,zdroj,nazov,cena,plocha,popis,url,skore,seen_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (lead["id"],profil_id,lead.get("zdroj",""),lead.get("nazov",""),
                 lead.get("cena",0),lead.get("plocha",0),lead.get("popis",""),
                 lead.get("url",""),lead.get("skore",0),datetime.now().strftime("%Y-%m-%d %H:%M")))


def db_leady(profil_id, min_skore=0, sort="skore", limit=200):
    sc = {"skore":"skore DESC","cena":"cena ASC","datum":"seen_at DESC"}.get(sort,"skore DESC")
    with get_db() as con:
        cur = con.cursor()
        cur.execute(f"SELECT * FROM leads WHERE profil_id={PH} AND skore>={PH} ORDER BY {sc} LIMIT {PH}",
                    (profil_id,min_skore,limit))
        return [_r(r) for r in cur.fetchall()]


def db_stats(profil_id):
    with get_db() as con:
        cur = con.cursor()
        def cnt(sql, args):
            cur.execute(sql, args)
            row = cur.fetchone()
            return row["c"] if USE_PG else row[0]
        total    = cnt(f"SELECT COUNT(*) as c FROM leads WHERE profil_id={PH}", (profil_id,))
        relevant = cnt(f"SELECT COUNT(*) as c FROM leads WHERE profil_id={PH} AND skore>=60", (profil_id,))
        cur.execute(f"SELECT cas,naskenov,nove,leady FROM scan_log WHERE profil_id={PH} ORDER BY id DESC LIMIT 1", (profil_id,))
        last = _r(cur.fetchone())
    return {"total":total,"relevant":relevant,
            "last_scan":last["cas"] if last else "—",
            "last_scanned":last["naskenov"] if last else 0,
            "last_new":last["nove"] if last else 0}


def db_uloz_log(profil_id, naskenov, nove, leady_count):
    with get_db() as con:
        cur = con.cursor()
        cur.execute(f"INSERT INTO scan_log (profil_id,cas,naskenov,nove,leady) VALUES ({PH},{PH},{PH},{PH},{PH})",
                    (profil_id,datetime.now().strftime("%Y-%m-%d %H:%M:%S"),naskenov,nove,leady_count))
        cur.execute(f"UPDATE profiles SET posledny_scan={PH} WHERE id={PH}",
                    (datetime.now().strftime("%Y-%m-%d %H:%M:%S"),profil_id))


# ============================================================
#  PARSERY
# ============================================================

# Mapa lokalít → nehnutelnosti.sk URL slug
_LOK_SLUG = {
    "liptov":"liptovsky-mikulas","liptovsky mikulas":"liptovsky-mikulas",
    "liptovský mikuláš":"liptovsky-mikulas","liptovska osada":"liptovska-osada",
    "ruzomberok":"ruzomberok","ružomberok":"ruzomberok",
    "martin":"martin","zilina":"zilina","žilina":"zilina",
    "bratislava":"bratislava","kosice":"kosice","košice":"kosice",
    "poprad":"poprad","banska bystrica":"banska-bystrica",
    "banská bystrica":"banska-bystrica","trencin":"trencin","trenčín":"trencin",
    "nitra":"nitra","presov":"presov","prešov":"presov","trnava":"trnava",
    "zvolen":"zvolen","prievidza":"prievidza","nove zamky":"nove-zamky",
}

def _slug(lok):
    l = lok.lower().strip()
    if l in _LOK_SLUG: return _LOK_SLUG[l]
    for k,v in _LOK_SLUG.items():
        if k in l or l in k: return v
    tr = str.maketrans("áäčďéíľĺňóôŕšťúůýž ","aacdeillnoorstuuyz-")
    return l.translate(tr)

_TYP_NH = {"byt":"byty","dom":"domy","pozemok":"pozemky","any":"byty"}
_TYP_TR = {
    "byt":"type%5B%5D=101&type%5B%5D=102&type%5B%5D=103&type%5B%5D=104",
    "dom":"type%5B%5D=111&type%5B%5D=112&type%5B%5D=113",
    "pozemok":"type%5B%5D=301&type%5B%5D=302",
    "any":"type%5B%5D=101&type%5B%5D=102&type%5B%5D=111&type%5B%5D=112",
}

def _url_zdroja(profil, zk):
    k      = profil["kriteria"]
    typ    = k.get("typ", "any")
    ponuka = k.get("ponuka", "predaj")
    lok    = k.get("lokalita", "")
    slug   = _slug(lok)

    # Mapovanie typov na nehnutelnosti.sk URL segmenty
    _NH_TYP = {
        "byt":"byty","1-izbovy-byt":"1-izbove-byty","2-izbovy-byt":"2-izbove-byty",
        "3-izbovy-byt":"3-izbove-byty","4-izbovy-byt":"4-izbove-byty",
        "5-a-viac-izbovy-byt":"5-a-viac-izbove-byty",
        "dom":"domy","vila":"vily","chalupa":"chaty-chalupy",
        "pozemok":"pozemky","stavebny-pozemok":"stavebne-pozemky",
        "kancelarsky-priestor":"kancelarske-priestory",
        "obchodny-priestor":"obchodne-priestory","any":"byty",
    }
    # Mapovanie ponuky
    _NH_PONUKA = {"predaj":"predaj","prenajom":"prenajom","dopyt":"dopyt"}

    if zk == "nehnutelnosti":
        kat    = _NH_TYP.get(typ, "byty")
        pon    = _NH_PONUKA.get(ponuka, "predaj")
        return f"https://www.nehnutelnosti.sk/vysledky/{kat}/{slug}/{pon}"

    if zk == "topreality":
        typy = _TYP_TR.get(typ if typ in _TYP_TR else ("byt" if "byt" in typ else ("dom" if "dom" in typ else "any")), _TYP_TR["any"])
        tr_ponuka = "1" if ponuka == "predaj" else "2"
        return f"https://www.topreality.sk/vyhladavanie-nehnutelnosti.html?form=1&{typy}&location={slug}&transaction={tr_ponuka}"

    if zk == "bazos":
        if "byt" in typ: return f"https://reality.bazos.sk/predaj/byt/?hledat={slug}"
        if "dom" in typ or "vila" in typ or "chalupa" in typ: return f"https://reality.bazos.sk/predaj/dom/?hledat={slug}"
        return f"https://reality.bazos.sk/predaj/?hledat={slug}"

    return ""


def _ext_cislo(text):
    for c in re.findall(r"[\d\s\xa0]+", str(text)):
        c = c.replace(" ","").replace("\xa0","")
        if c.isdigit() and len(c)>=2: return int(c)
    return 0


def parse_nehnutelnosti(html, src):
    """
    nehnutelnosti.sk — Next.js, inzeráty sú <a href='/detail/ID/nazov'>.
    Cena a plocha sa ťahajú regex-om z okolitého textu.
    """
    soup = BeautifulSoup(html, "lxml")
    out, seen = [], set()
    for a in soup.select("a[href*='/detail/']")[:80]:
        try:
            href = a.get("href","")
            if not href or href in seen: continue
            seen.add(href)
            url = href if href.startswith("http") else "https://www.nehnutelnosti.sk"+href
            parts = url.rstrip("/").split("/")
            uid = parts[4] if len(parts)>4 else url[-16:]

            # Nadpis — h2/h3 v najbližšom rodičovskom bloku
            nazov = ""
            par = a.find_parent(["article","section","li","div"])
            if par:
                h = par.find(["h2","h3"])
                if h: nazov = h.get_text(strip=True)
            if not nazov: nazov = a.get_text(strip=True)
            if len(nazov) < 6: continue

            # Cena a plocha z textu rodiča
            ptxt = par.get_text(" ", strip=True) if par else ""
            cena = 0
            mc = re.search(r"([\d][\d\s\xa0]{2,})\s*€", ptxt)
            if mc: cena = _ext_cislo(mc.group(1))
            plocha = 0
            ma = re.search(r"(\d{2,4})\s*m²", ptxt)
            if ma: plocha = int(ma.group(1))

            # Popis — lokalita alebo prvý krátky odsek
            popis = ""
            if par:
                for el in par.select("p,[class*='locat'],[class*='address'],[class*='region']"):
                    t = el.get_text(strip=True)
                    if 5 < len(t) < 200 and "€" not in t and "m²" not in t:
                        popis = t; break

            out.append({"zdroj":src,"nazov":nazov,"cena":cena,
                        "plocha":plocha,"popis":popis,"url":url,"id":uid})
        except: continue
    return out


def parse_topreality(html, src):
    soup = BeautifulSoup(html,"lxml")
    karty = soup.select(".item,.property-item,article[class*='item'],div[class*='list-item'],li[class*='item']")
    out = []
    for k in karty[:40]:
        try:
            a = k.select_one("h2 a,h3 a,.title a,a.name,a[href*='/nehnutelnost/']")
            if not a: continue
            url = a.get("href","")
            if url and not url.startswith("http"): url = "https://www.topreality.sk"+url
            ptxt = k.get_text(" ", strip=True)
            cena = 0
            mc = re.search(r"([\d][\d\s\xa0]{2,})\s*€", ptxt)
            if mc: cena = _ext_cislo(mc.group(1))
            plocha = 0
            ma = re.search(r"(\d{2,4})\s*m²", ptxt)
            if ma: plocha = int(ma.group(1))
            lok = k.select_one("[class*='location'],.locality,.address,p")
            popis = lok.get_text(strip=True)[:200] if lok else ""
            uid = url.rstrip("/").split("/")[-1][:20]
            nazov = a.get_text(strip=True)
            if len(nazov) < 5: continue
            out.append({"zdroj":src,"nazov":nazov,"cena":cena,
                        "plocha":plocha,"popis":popis,"url":url,"id":uid})
        except: continue
    return out


def parse_bazos(html, src):
    soup = BeautifulSoup(html,"lxml")
    karty = soup.select(".inzerat,div[class*='inzerat'],.oglas")
    out = []
    for k in karty[:40]:
        try:
            a = k.select_one("h2 a,.nadpis a,h3 a")
            if not a: continue
            url = a.get("href","")
            if url and not url.startswith("http"): url = "https://reality.bazos.sk"+url
            ptxt = k.get_text(" ", strip=True)
            cena = 0
            mc = re.search(r"([\d][\d\s\xa0]{2,})\s*€", ptxt)
            if mc: cena = _ext_cislo(mc.group(1))
            p = k.select_one(".popis,p")
            popis = p.get_text(strip=True)[:200] if p else ""
            uid = url.rstrip("/").split("/")[-2] if url.count("/")>3 else url[-16:]
            out.append({"zdroj":src,"nazov":a.get_text(strip=True),
                        "cena":cena,"plocha":0,"popis":popis,"url":url,"id":uid})
        except: continue
    return out


PARSERY = {
    "nehnutelnosti": (parse_nehnutelnosti, "nehnutelnosti.sk"),
    "topreality":    (parse_topreality,    "topreality.sk"),
    "bazos":         (parse_bazos,         "bazos.sk"),
}


# ============================================================
#  FILTER & SKÓRE
# ============================================================

def ok_filter(p, k):
    c, a = p.get("cena",0), p.get("plocha",0)
    if c and c > k.get("max_cena", 9e9): return False
    if c and c < k.get("min_cena", 0):   return False
    if a and a < k.get("min_plocha", 0): return False
    if a and a > k.get("max_plocha", 9e9): return False
    # Lokalitu NEkontrolujeme v texte — URL ju už filtruje
    txt = (p.get("nazov","") + " " + p.get("popis","")).lower()
    for sl in k.get("vyluc_slova", []):
        if sl.lower() in txt: return False
    return True


def skore(p, k):
    s = 50
    txt = (p.get("nazov","")+p.get("popis","")).lower()
    for sl in k.get("prefer_slova",[]): 
        if sl.lower() in txt: s += 8
    if p.get("cena") and p["cena"] < k.get("max_cena",9e9)*0.75: s += 12
    return min(s, 99)


def ai_hodnot(ponuky, k):
    if not ANTHROPIC_API_KEY or not ponuky:
        return {p["id"]: skore(p,k) for p in ponuky}
    zoznam = "\n".join(f"[{i+1}] {p['nazov']} | {p.get('cena',0)}€ | {p.get('plocha',0)}m² | {p.get('popis','')[:100]}"
                       for i,p in enumerate(ponuky))
    prompt = (f"Ohodnoť realitné ponuky 0-100 pre investora.\n"
              f"Max cena: {k.get('max_cena',0)}€, lokalita: {k.get('lokalita','')}\n"
              f"Pokyn: {k.get('ai_pokyn','')}\n\nPonuky:\n{zoznam}\n\n"
              f"Odpovedz IBA JSON: {{\"1\":85,\"2\":40,...}}")
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
            json={"model":"claude-haiku-4-5-20251001","max_tokens":300,"messages":[{"role":"user","content":prompt}]},
            timeout=20)
        mapa = json.loads(r.json()["content"][0]["text"].strip())
        return {ponuky[int(ki)-1]["id"]: int(v) for ki,v in mapa.items() if int(ki)-1 < len(ponuky)}
    except Exception as e:
        _log(f"AI chyba: {e}","warn")
        return {p["id"]: skore(p,k) for p in ponuky}


# ============================================================
#  NOTIFIKÁCIE
# ============================================================

def posli_telegram(lead, profil_nazov, min_skore):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID: return
    if lead.get("skore",0) < min_skore: return
    s = lead.get("skore",0)
    cena = f"{lead['cena']:,} €" if lead.get("cena") else "cena neuvedená"
    plocha = f"  ·  📐 {lead['plocha']} m²" if lead.get("plocha") else ""
    txt = (f"{'🔥' if s>=85 else '✅'} *{lead['nazov']}*\n"
           f"💶 {cena}{plocha}\n📁 _{profil_nazov}_  |  {s}%\n📍 {lead.get('zdroj','')}\n")
    if lead.get("popis"): txt += f"\n_{lead['popis'][:180]}_\n"
    if lead.get("url"):   txt += f"\n[Zobraziť →]({lead['url']})"
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id":TELEGRAM_CHAT_ID,"text":txt,"parse_mode":"Markdown","disable_web_page_preview":False},
            timeout=10)
    except: pass


# ============================================================
#  SCAN
# ============================================================

_seen_cache: dict = {}
_next_scan:  dict = {}


def _seen(pid):
    if pid not in _seen_cache:
        with get_db() as con:
            cur = con.cursor()
            cur.execute(f"SELECT id FROM leads WHERE profil_id={PH}", (pid,))
            _seen_cache[pid] = {r["id"] if USE_PG else r[0] for r in cur.fetchall()}
    return _seen_cache[pid]


def scan_profil(profil):
    pid, nazov, k = profil["id"], profil["nazov"], profil["kriteria"]
    seen = _seen(pid)
    _log(f"[{nazov}] Štart")
    nove_all = []

    for zk in profil["zdroje"]:
        if zk not in PARSERY: continue
        fn, src = PARSERY[zk]
        url = _url_zdroja(profil, zk)
        if not url: continue
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            ponuky = fn(resp.text, src)
            nove = [p for p in ponuky if p["id"] not in seen]
            _log(f"  [{nazov}] {src}: {len(ponuky)} celkom, {len(nove)} nových")
            nove_all.extend(nove)
            for p in ponuky: seen.add(p["id"])
        except Exception as e:
            _log(f"  [{nazov}] Chyba {zk}: {e}","warn")
        time.sleep(1.2)

    if not nove_all:
        db_uloz_log(pid,0,0,0); return

    pref = [p for p in nove_all if ok_filter(p,k)]
    _log(f"[{nazov}] Filter: {len(nove_all)} → {len(pref)}")

    sm = ai_hodnot(pref, k)
    leady = []
    for p in pref:
        p["skore"] = sm.get(p["id"], skore(p,k))
        db_uloz_lead(p, pid)
        if p["skore"] >= 60: leady.append(p)

    leady.sort(key=lambda x: x["skore"], reverse=True)
    db_uloz_log(pid, len(nove_all), len(pref), len(leady))
    _log(f"[{nazov}] ✅ {len(leady)} leadov")

    for lead in leady:
        posli_telegram(lead, nazov, profil.get("tg_min_skore",70))
        time.sleep(0.2)


def scheduler_loop():
    while True:
        now = time.time()
        try:
            profily = db_vsetky_profily()
        except Exception as e:
            _log(f"Scheduler DB chyba: {e}","warn")
            time.sleep(30); continue
        for p in profily:
            if not p["aktivny"]: continue
            pid = p["id"]
            if pid not in _next_scan: _next_scan[pid] = now
            if now >= _next_scan[pid]:
                try: scan_profil(p)
                except Exception as e: _log(f"[{p['nazov']}] Scan chyba: {e}","warn")
                _next_scan[pid] = time.time() + p["interval_min"]*60
        time.sleep(15)


def _log(msg, typ=""):
    ikona = {"ok":"✅","warn":"⚠️","err":"❌"}.get(typ,"·")
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {ikona} {msg}", flush=True)


# ============================================================
#  FLASK
# ============================================================

app = Flask(__name__)


@app.before_request
def ensure_db():
    try: init_db()
    except Exception as e: _log(f"init_db chyba: {e}","err")


def check_auth():
    t = request.args.get("token") or request.cookies.get("token","")
    if t != DASHBOARD_PASSWORD: abort(401)


HTML = r"""<!DOCTYPE html>
<html lang="sk">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Realitný scanner</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f4f3f0;color:#1a1a18;font-size:15px}
header{background:#fff;border-bottom:1px solid #e0dfd8;padding:0 20px;display:flex;align-items:center;height:52px;gap:10px}
header h1{font-size:17px;font-weight:600;margin-right:auto}
.tab-bar{display:flex;background:#fff;border-bottom:1px solid #e0dfd8;padding:0 12px;overflow-x:auto;align-items:stretch}
.tab-btn{padding:10px 16px;font-size:13px;cursor:pointer;border:none;background:none;color:#888;border-bottom:2px solid transparent;white-space:nowrap;font-weight:500;display:flex;align-items:center;gap:6px}
.tab-btn.active{color:#111;border-bottom-color:#111}
.tab-btn.paused{opacity:.5}
.tab-btn .cnt{font-size:10px;background:#eee;border-radius:10px;padding:1px 6px}
.tab-btn.active .cnt{background:#111;color:#fff}
.add-tab{padding:10px 14px;font-size:20px;cursor:pointer;border:none;background:none;color:#bbb}
.add-tab:hover{color:#333}
.pane{display:none;padding:18px 18px 60px;max-width:860px;margin:0 auto}
.pane.active{display:block}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:10px;margin-bottom:16px}
.stat{background:#fff;border:1px solid #e0dfd8;border-radius:10px;padding:12px;text-align:center}
.stat .n{font-size:22px;font-weight:600}.stat .l{font-size:11px;color:#aaa;margin-top:2px}
.controls{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px;align-items:center}
select{font-size:13px;padding:5px 9px;border:1px solid #ddd;border-radius:7px;background:#fff}
.btn{font-size:13px;padding:6px 14px;border:1px solid #ddd;border-radius:7px;background:#fff;cursor:pointer;white-space:nowrap}
.btn:hover{background:#f0ede8}
.btn-danger{border-color:#f5c6c6;color:#b00}.btn-danger:hover{background:#fff5f5}
.btn-ok{border-color:#b8e6cc;color:#085}.btn-ok:hover{background:#f0fff8}
.leads{display:grid;gap:10px}
.card{background:#fff;border:1px solid #e0dfd8;border-radius:10px;padding:14px 16px}
.card-top{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:5px}
.card-title{font-size:15px;font-weight:600;line-height:1.3;flex:1}
.card-price{font-size:15px;font-weight:600;color:#1a4f8a;white-space:nowrap}
.card-meta{font-size:12px;color:#aaa;margin-bottom:5px}
.card-desc{font-size:13px;color:#666;line-height:1.5;margin-bottom:8px}
.card-foot{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.badge{font-size:11px;padding:2px 8px;border-radius:20px;font-weight:600}
.badge-top{background:#fff0d0;color:#7a3800}.badge-ok{background:#e0f5ea;color:#0a5a30}
.src{font-size:10px;border:1px solid #ddd;border-radius:20px;padding:1px 7px;color:#aaa}
.bar-wrap{flex:1;min-width:40px;height:4px;background:#eee;border-radius:2px}
.bar{height:100%;border-radius:2px;background:#1D9E75}
a.ext{font-size:12px;color:#1a4f8a;text-decoration:none;margin-left:auto}
a.ext:hover{text-decoration:underline}
.empty{text-align:center;padding:40px;color:#ccc;font-size:14px;line-height:2}
.toast{position:fixed;bottom:20px;right:20px;background:#222;color:#fff;padding:10px 18px;border-radius:8px;font-size:13px;display:none;z-index:999}
.overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:100;overflow-y:auto;padding:20px}
.mbox{background:#fff;border-radius:14px;max-width:600px;margin:0 auto;padding:24px}
.mbox h2{font-size:17px;margin-bottom:16px}
.fg{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:8px}
.fi{display:flex;flex-direction:column;gap:4px}
.fi label{font-size:12px;color:#888;font-weight:500}
.fi input,.fi select,.fi textarea{font-size:13px;padding:7px 10px;border:1px solid #ddd;border-radius:7px;background:#fff;font-family:inherit;width:100%}
.fi textarea{resize:vertical;min-height:60px}
.fw{grid-column:1/-1}
.sec{font-size:12px;font-weight:600;color:#888;text-transform:uppercase;letter-spacing:.05em;margin:14px 0 6px;padding-top:12px;border-top:1px solid #f0ede8}
.chip-row{display:flex;flex-wrap:wrap;gap:6px;margin-top:2px}
.chip{font-size:12px;padding:4px 11px;border:1px solid #ddd;border-radius:20px;background:#f8f7f4;cursor:pointer;user-select:none}
.chip.on{background:#111;color:#fff;border-color:#111}
.fa{display:flex;gap:8px;margin-top:16px;flex-wrap:wrap}
.tab-btn .tab-x{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;font-size:14px;line-height:1;margin-left:5px;color:transparent;transition:background .15s,color .15s;vertical-align:middle}
.tab-btn:hover .tab-x{color:#888;background:rgba(0,0,0,.08)}
.tab-btn.active .tab-x{color:#555}
.tab-btn .tab-x:hover{color:#c00 !important;background:rgba(200,0,0,.12) !important}
.confirm-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:200;align-items:center;justify-content:center}
.confirm-overlay.show{display:flex}
.confirm-box{background:var(--color-background-primary,#fff);border-radius:12px;padding:24px;max-width:340px;width:90%;box-shadow:0 8px 32px rgba(0,0,0,.18)}
.confirm-box h3{font-size:16px;font-weight:500;margin-bottom:8px;color:var(--color-text-primary,#111)}
.confirm-box p{font-size:13px;color:var(--color-text-secondary,#666);margin-bottom:20px;line-height:1.5}
.confirm-actions{display:flex;gap:8px;justify-content:flex-end}
@media(max-width:580px){.fg{grid-template-columns:1fr}.card-top{flex-direction:column}}
</style>
</head>
<body>
<header><span style="font-size:20px">🏠</span><h1>Realitný scanner</h1></header>
<div class="tab-bar" id="tab-bar">
  <button class="add-tab" title="Nový profil" onclick="openModal()">＋</button>
</div>
<div id="panes"></div>
<div class="toast" id="toast"></div>

<div class="confirm-overlay" id="confirm-overlay">
  <div class="confirm-box">
    <h3>Zmazať profil?</h3>
    <p id="confirm-msg">Toto vymaže profil aj všetky jeho leady. Akcia sa nedá vrátiť späť.</p>
    <div class="confirm-actions">
      <button class="btn" onclick="closeConfirm()">Zrušiť</button>
      <button class="btn btn-danger" id="confirm-ok">🗑 Zmazať</button>
    </div>
  </div>
</div>

<div class="overlay" id="overlay" onclick="if(event.target===this)closeModal()">
  <div class="mbox">
    <h2 id="mt">Nový profil</h2>
    <input type="hidden" id="f-pid">

    <div class="fg">
      <div class="fi fw"><label>Názov profilu *</label>
        <input id="f-nazov" placeholder="napr. Byty Liptov, Domy Ružomberok…">
      </div>
    </div>

    <div class="sec">Kde · Čo · Ponuka</div>
    <div class="fg">
      <div class="fi fw"><label>Lokalita * (mesto, okres alebo kraj)</label>
        <input id="f-lok" placeholder="napr. Liptovský Mikuláš, Ružomberok, Liptov">
      </div>
      <div class="fi"><label>Typ nehnuteľnosti</label>
        <select id="f-typ">
          <option value="byt">Byt (všetky)</option>
          <option value="1-izbovy-byt">1-izbový byt</option>
          <option value="2-izbovy-byt">2-izbový byt</option>
          <option value="3-izbovy-byt">3-izbový byt</option>
          <option value="4-izbovy-byt">4-izbový byt</option>
          <option value="5-a-viac-izbovy-byt">5+ izbový byt</option>
          <option value="dom">Rodinný dom</option>
          <option value="vila">Vila</option>
          <option value="chalupa">Chalupa / chata</option>
          <option value="pozemok">Pozemok</option>
          <option value="stavebny-pozemok">Stavebný pozemok</option>
          <option value="kancelarsky-priestor">Kancelársky priestor</option>
          <option value="obchodny-priestor">Obchodný priestor</option>
          <option value="any">Akýkoľvek</option>
        </select>
      </div>
      <div class="fi"><label>Ponuka</label>
        <select id="f-ponuka">
          <option value="predaj">Predaj</option>
          <option value="prenajom">Prenájom</option>
          <option value="dopyt">Dopyt</option>
        </select>
      </div>
    </div>

    <div class="sec">Cena a výmera</div>
    <div class="fg">
      <div class="fi"><label>Cena od (€)</label><input id="f-minc" type="number" placeholder="0" min="0"></div>
      <div class="fi"><label>Cena do (€)</label><input id="f-maxc" type="number" placeholder="200000" min="0"></div>
      <div class="fi"><label>Plocha od (m²)</label><input id="f-mina" type="number" placeholder="0" min="0"></div>
      <div class="fi"><label>Plocha do (m²)</label><input id="f-maxa" type="number" placeholder="500" min="0"></div>
    </div>

    <div class="sec">Stav nehnuteľnosti</div>
    <div class="chip-row" id="f-stav">
      <span class="chip" data-v="novostavba">Novostavba</span>
      <span class="chip" data-v="kompletna-rekonstrukcia">Kompletná rekonštrukcia</span>
      <span class="chip" data-v="castocna-rekonstrukcia">Čiastočná rekonštrukcia</span>
      <span class="chip" data-v="povodny-stav">Pôvodný stav</span>
      <span class="chip" data-v="holodom">Holodom / holobyt</span>
    </div>

    <div class="sec">Počet izieb (min)</div>
    <div class="chip-row" id="f-izby-chips">
      <span class="chip" data-v="1">1+</span>
      <span class="chip on" data-v="2">2+</span>
      <span class="chip" data-v="3">3+</span>
      <span class="chip" data-v="4">4+</span>
      <span class="chip" data-v="5">5+</span>
    </div>

    <div class="sec">Vlastnosti</div>
    <div class="chip-row" id="f-vlastnosti">
      <span class="chip" data-v="balkón">Balkón</span>
      <span class="chip" data-v="terasa">Terasa</span>
      <span class="chip" data-v="záhrada">Záhrada</span>
      <span class="chip" data-v="garáž">Garáž</span>
      <span class="chip" data-v="parking">Parkovanie</span>
      <span class="chip" data-v="výťah">Výťah</span>
      <span class="chip" data-v="pivnica">Pivnica</span>
      <span class="chip" data-v="klimatizácia">Klimatizácia</span>
    </div>

    <div class="sec">Zdroje</div>
    <div class="chip-row" id="f-zdroje">
      <span class="chip on" data-v="nehnutelnosti">nehnutelnosti.sk</span>
      <span class="chip on" data-v="topreality">topreality.sk</span>
      <span class="chip"    data-v="bazos">bazos.sk</span>
    </div>

    <div class="sec">Ďalšie nastavenia</div>
    <div class="fg">
      <div class="fi"><label>Vylúčiť slová (čiarkami)</label>
        <input id="f-vyl" placeholder="suterén, dražba, exekúcia">
      </div>
      <div class="fi"><label>AI pokyn (voľný text)</label>
        <input id="f-ai" placeholder="Uprednostni novostavby pri prírode…">
      </div>
      <div class="fi"><label>Interval skenovania</label>
        <select id="f-int">
          <option value="5">5 minút</option>
          <option value="10" selected>10 minút</option>
          <option value="15">15 minút</option>
          <option value="30">30 minút</option>
          <option value="60">1 hodina</option>
        </select>
      </div>
      <div class="fi"><label>Telegram min. skóre (%)</label>
        <input id="f-tg" type="number" value="70" min="0" max="100">
      </div>
    </div>

    <div class="fa">
      <button class="btn" onclick="closeModal()">Zrušiť</button>
      <button class="btn btn-ok" id="btn-save" onclick="uloz()">💾 Uložiť profil</button>
      <button class="btn btn-danger" id="btn-del" style="display:none" onclick="zmazat()">🗑 Zmazať</button>
    </div>
  </div>
</div>

<script>
const TOKEN = new URLSearchParams(location.search).get('token')||'';
const G = id => document.getElementById(id);
const api  = u => fetch(u+(u.includes('?')?'&':'?')+'token='+TOKEN).then(r=>r.json());
const post = (u,b) => fetch(u+'?token='+TOKEN,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)}).then(r=>r.json());

let profily=[], activePid=null;

function toast(msg,ok=true){const t=G('toast');t.textContent=msg;t.style.background=ok?'#1D9E75':'#c00';t.style.display='block';setTimeout(()=>t.style.display='none',3500)}

async function reload(){
  profily=await api('/api/profily');
  renderTabs();
  if(!activePid&&profily.length) activePid=profily[0].id;
  if(activePid) switchTab(activePid,false);
}

function renderTabs(){
  const bar=G('tab-bar'),add=bar.querySelector('.add-tab');
  bar.querySelectorAll('.tab-btn').forEach(b=>b.remove());
  profily.forEach(p=>{
    const b=document.createElement('button');
    b.className='tab-btn'+(p.id===activePid?' active':'')+(!p.aktivny?' paused':'');
    b.dataset.pid=p.id;
    b.innerHTML=`${p.nazov}<span class="cnt" id="cnt-${p.id}">…</span><span class="tab-x" title="Zmazať" onclick="zmazatTab(event,'${p.id}','${p.nazov.replace(/'/g,"\\'")}')">×</span>`;
    b.onclick=(e)=>{ if(!e.target.classList.contains('tab-x')) switchTab(p.id,true); };
    bar.insertBefore(b,add);
  });
}

async function switchTab(pid,doRef=true){
  activePid=pid;
  document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('active',b.dataset.pid===pid));
  if(!G('pane-'+pid)){
    const d=document.createElement('div');d.className='pane';d.id='pane-'+pid;
    d.innerHTML=buildPane(pid);G('panes').appendChild(d);
  }
  document.querySelectorAll('.pane').forEach(p=>p.classList.remove('active'));
  G('pane-'+pid).classList.add('active');
  if(doRef) await refreshPane(pid);
}

function buildPane(pid){return`
  <div class="stats">
    <div class="stat"><div class="n" id="st-${pid}">—</div><div class="l">celkom</div></div>
    <div class="stat"><div class="n" id="sr-${pid}">—</div><div class="l">relevantných</div></div>
    <div class="stat"><div class="n" id="sl-${pid}">—</div><div class="l">posledný scan</div></div>
    <div class="stat"><div class="n" id="sn-${pid}">—</div><div class="l">nových naposledy</div></div>
  </div>
  <div class="controls">
    <select id="srt-${pid}" onchange="refreshPane('${pid}')">
      <option value="skore">Skóre ↓</option><option value="cena">Cena ↑</option><option value="datum">Najnovšie</option>
    </select>
    <select id="min-${pid}" onchange="refreshPane('${pid}')">
      <option value="0">Všetky</option><option value="50">50%+</option>
      <option value="60" selected>60%+</option><option value="75">75%+</option><option value="90">90%+</option>
    </select>
    <button class="btn" onclick="refreshPane('${pid}')">🔄 Obnoviť</button>
    <button class="btn" onclick="editProfil('${pid}')">⚙️ Upraviť</button>
    <button class="btn" id="tog-${pid}" onclick="toggle('${pid}')">⏸ Pozastaviť</button>
  </div>
  <div class="leads" id="leads-${pid}"><div class="empty">Načítavam…</div></div>`;}

async function refreshPane(pid){
  const srt=G('srt-'+pid)?.value||'skore', min=G('min-'+pid)?.value||60;
  const [stats,leads]=await Promise.all([api('/api/stats/'+pid),api(`/api/leads/${pid}?sort=${srt}&min_skore=${min}`)]);
  G('st-'+pid).textContent=stats.total;
  G('sr-'+pid).textContent=stats.relevant;
  G('sl-'+pid).textContent=(stats.last_scan||'—').slice(11,16)||'—';
  G('sn-'+pid).textContent=stats.last_new;
  const cnt=G('cnt-'+pid); if(cnt) cnt.textContent=stats.relevant||'—';
  const p=profily.find(x=>x.id===pid), tog=G('tog-'+pid);
  if(p&&tog) tog.textContent=p.aktivny?'⏸ Pozastaviť':'▶ Spustiť';
  renderLeads(pid,leads);
}

function renderLeads(pid,leads){
  const el=G('leads-'+pid);
  if(!leads.length){el.innerHTML='<div class="empty">Žiadne leady.<br><small style="color:#ddd">Scanner zbiera výsledky.</small></div>';return;}
  el.innerHTML=leads.map(l=>{
    const cena=l.cena?l.cena.toLocaleString('sk-SK')+' €':'—';
    const plocha=l.plocha?` · ${l.plocha} m²`:'';
    const badge=l.skore>=85?'<span class="badge badge-top">🔥 Top</span>':'<span class="badge badge-ok">✓ OK</span>';
    return`<div class="card">
      <div class="card-top"><span class="card-title">${l.nazov}</span><span class="card-price">${cena}</span></div>
      <div class="card-meta">${l.seen_at||''}${plocha}</div>
      ${l.popis?`<div class="card-desc">${l.popis.slice(0,220)}</div>`:''}
      <div class="card-foot">${badge}<span class="src">${l.zdroj}</span>
        <div class="bar-wrap"><div class="bar" style="width:${l.skore}%"></div></div>
        <span style="font-size:11px;color:#bbb">${l.skore}%</span>
        <a class="ext" href="${l.url}" target="_blank">Zobraziť →</a>
      </div></div>`;
  }).join('');
}

// Chip toggling — izby = single select, ostatné = multi
document.addEventListener('click',e=>{
  const chip=e.target.closest('.chip');
  if(!chip) return;
  const row=chip.closest('.chip-row');
  if(!row) return;
  if(row.id==='f-izby-chips'){
    row.querySelectorAll('.chip').forEach(c=>c.classList.remove('on'));
    chip.classList.add('on');
  } else {
    chip.classList.toggle('on');
  }
});

function _chips(id){return[...document.querySelectorAll(`#${id} .chip.on`)].map(c=>c.dataset.v);}
function _setChips(id,vals){document.querySelectorAll(`#${id} .chip`).forEach(c=>c.classList.toggle('on',vals.includes(c.dataset.v)));}

function openModal(p=null){
  G('mt').textContent=p?'Upraviť profil':'Nový profil';
  G('f-pid').value=p?.id||'';
  G('f-nazov').value=p?.nazov||'';
  G('f-typ').value=p?.kriteria?.typ||'byt';
  G('f-ponuka').value=p?.kriteria?.ponuka||'predaj';
  G('f-lok').value=p?.kriteria?.lokalita||'';
  G('f-minc').value=p?.kriteria?.min_cena||'';
  G('f-maxc').value=p?.kriteria?.max_cena||'';
  G('f-mina').value=p?.kriteria?.min_plocha||'';
  G('f-maxa').value=p?.kriteria?.max_plocha||'';
  G('f-vyl').value=(p?.kriteria?.vyluc_slova||['suterén','dražba','exekúcia']).join(', ');
  G('f-ai').value=p?.kriteria?.ai_pokyn||'';
  G('f-int').value=p?.interval_min||10;
  G('f-tg').value=p?.tg_min_skore||70;
  _setChips('f-stav', p?.kriteria?.stav||[]);
  const izby=String(p?.kriteria?.min_izby||2);
  document.querySelectorAll('#f-izby-chips .chip').forEach(c=>c.classList.toggle('on',c.dataset.v===izby));
  _setChips('f-vlastnosti', p?.kriteria?.prefer_slova||[]);
  _setChips('f-zdroje', p?.zdroje||['nehnutelnosti','topreality']);
  G('btn-del').style.display=p?'':'none';
  G('overlay').style.display='block';
}

function editProfil(pid){openModal(profily.find(p=>p.id===pid));}
function closeModal(){G('overlay').style.display='none';}

async function uloz(){
  const nazov=G('f-nazov').value.trim(), lok=G('f-lok').value.trim();
  if(!nazov){toast('Zadaj názov profilu',false);return;}
  if(!lok){toast('Zadaj lokalitu',false);return;}
  const zdroje=_chips('f-zdroje');
  if(!zdroje.length){toast('Vyber aspoň jeden zdroj',false);return;}
  const izbyChip=document.querySelector('#f-izby-chips .chip.on');
  const pid=G('f-pid').value||null;
  const body={pid,nazov,
    interval_min:+G('f-int').value||10,
    tg_min_skore:+G('f-tg').value||70,
    zdroje,
    kriteria:{
      typ:G('f-typ').value,
      ponuka:G('f-ponuka').value,
      lokalita:lok,
      min_cena:+G('f-minc').value||0,
      max_cena:+G('f-maxc').value||999999,
      min_plocha:+G('f-mina').value||0,
      max_plocha:+G('f-maxa').value||9999,
      min_izby:izbyChip?+izbyChip.dataset.v:1,
      stav:_chips('f-stav'),
      prefer_slova:_chips('f-vlastnosti'),
      vyluc_slova:G('f-vyl').value.split(/[,;]+/).map(s=>s.trim()).filter(Boolean),
      ai_pokyn:G('f-ai').value.trim(),
    }};
  G('btn-save').textContent='Ukladám…';G('btn-save').disabled=true;
  try{
    const res=await post('/api/profil/uloz',body);
    if(res.ok){closeModal();activePid=res.pid;const old=G('pane-'+res.pid);if(old)old.remove();await reload();toast('Profil uložený ✓');}
    else toast('Chyba: '+(res.error||'neznáma'),false);
  }catch(e){toast('Sieťová chyba: '+e.message,false);}
  G('btn-save').textContent='💾 Uložiť profil';G('btn-save').disabled=false;
}

function closeConfirm(){G('confirm-overlay').classList.remove('show');}

function _confirmDelete(pid,nazov,onOk){
  G('confirm-msg').textContent=`Zmazať profil "${nazov}" aj so všetkými leadmi? Akcia sa nedá vrátiť späť.`;
  G('confirm-overlay').classList.add('show');
  G('confirm-ok').onclick=async()=>{closeConfirm();await onOk();};
}

async function zmazat(){
  const pid=G('f-pid').value;
  if(!pid) return;
  _confirmDelete(pid,G('f-nazov').value,async()=>{
    const res=await post('/api/profil/zmazat',{pid});
    if(res.ok){closeModal();const old=G('pane-'+pid);if(old)old.remove();activePid=null;await reload();toast('Profil zmazaný');}
  });
}

function zmazatTab(e,pid,nazov){
  e.stopPropagation();
  _confirmDelete(pid,nazov,async()=>{
    const res=await post('/api/profil/zmazat',{pid});
    if(res.ok){const old=G('pane-'+pid);if(old)old.remove();if(activePid===pid)activePid=null;await reload();toast('Profil zmazaný');}
  });
}

async function toggle(pid){
  const res=await post('/api/profil/toggle',{pid});
  if(res.ok){
    const p=profily.find(x=>x.id===pid);
    if(p){p.aktivny=res.aktivny;renderTabs();}
    const tog=G('tog-'+pid);
    if(tog) tog.textContent=res.aktivny?'⏸ Pozastaviť':'▶ Spustiť';
  }
}

reload();
setInterval(()=>{if(activePid) refreshPane(activePid);},60000);
</script>
</body>
</html>"""


@app.route("/debug")
def debug():
    import sys
    info = {"python": sys.version, "use_pg": USE_PG, "db_url_set": bool(DATABASE_URL)}
    try:
        with get_db() as con:
            cur = con.cursor()
            cur.execute("SELECT COUNT(*) as c FROM profiles")
            row = cur.fetchone()
            info["profiles"] = row["c"] if USE_PG else row[0]
            info["db_ok"] = True
    except Exception as e:
        info["db_ok"] = False; info["db_error"] = str(e)
    return jsonify(info)


@app.route("/")
def index():
    if request.args.get("token","") != DASHBOARD_PASSWORD:
        return """<html><body style='font-family:sans-serif;text-align:center;padding:80px'>
        <h2>🔒 Realitný scanner</h2>
        <form><input name='token' type='password' placeholder='Heslo'
          style='padding:8px;font-size:15px;border:1px solid #ddd;border-radius:6px;margin-right:8px'>
        <button type='submit' style='padding:8px 16px;border:1px solid #ddd;border-radius:6px;cursor:pointer'>
        Prihlásiť</button></form></body></html>"""
    return render_template_string(HTML)


@app.route("/api/profily")
def api_profily():
    check_auth()
    return jsonify(db_vsetky_profily())


@app.route("/api/profil/uloz", methods=["POST"])
def api_uloz():
    check_auth()
    try:
        d = request.json
        if not d: return jsonify({"ok":False,"error":"Žiadne dáta"}), 400
        pid = d.get("pid") or _gid(d.get("nazov","profil"))
        db_uloz_profil(pid, d["nazov"], d["kriteria"], d["zdroje"],
                       d.get("interval_min",10), d.get("tg_min_skore",70))
        _seen_cache.pop(pid, None)
        _next_scan[pid] = 0
        return jsonify({"ok":True,"pid":pid})
    except Exception as e:
        _log(f"api_uloz: {e}","err")
        return jsonify({"ok":False,"error":str(e)}), 500


@app.route("/api/profil/zmazat", methods=["POST"])
def api_zmazat():
    check_auth()
    pid = request.json.get("pid")
    if pid:
        db_zmazat_profil(pid)
        _seen_cache.pop(pid,None)
        _next_scan.pop(pid,None)
    return jsonify({"ok":True})


@app.route("/api/profil/toggle", methods=["POST"])
def api_toggle():
    check_auth()
    pid = request.json.get("pid")
    stav = db_toggle_profil(pid) if pid else 0
    return jsonify({"ok":True,"aktivny":stav})


@app.route("/api/leads/<pid>")
def api_leads(pid):
    check_auth()
    return jsonify(db_leady(pid,
        min_skore=int(request.args.get("min_skore",60)),
        sort=request.args.get("sort","skore")))


@app.route("/api/stats/<pid>")
def api_stats(pid):
    check_auth()
    return jsonify(db_stats(pid))


@app.route("/api/scan-now/<pid>", methods=["POST"])
def api_scan_now(pid):
    """Manuálny okamžitý scan profilu — na testovanie."""
    check_auth()
    profil = db_profil(pid)
    if not profil:
        return jsonify({"ok": False, "error": "Profil nenájdený"}), 404
    try:
        _seen_cache.pop(pid, None)
        scan_profil(profil)
        return jsonify({"ok": True, "stats": db_stats(pid)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/scan-now", methods=["POST"])
def api_scan_all():
    """Manuálny scan všetkých aktívnych profilov."""
    check_auth()
    results = {}
    for p in db_vsetky_profily():
        if not p["aktivny"]: continue
        try:
            _seen_cache.pop(p["id"], None)
            scan_profil(p)
            results[p["nazov"]] = "ok"
        except Exception as e:
            results[p["nazov"]] = str(e)
    return jsonify({"ok": True, "results": results})


# ============================================================
#  ŠTART — spustí scheduler pri každom načítaní modulu
#  (funguje aj s gunicorn, nielen python scanner.py)
# ============================================================

def _start_scheduler():
    """Spustí scheduler thread raz — chráni pred viacnásobným spustením."""
    import os
    # Gunicorn fork guard — spusti len v worker procese, nie v master
    if os.environ.get("_SCANNER_STARTED"):
        return
    os.environ["_SCANNER_STARTED"] = "1"
    t = threading.Thread(target=scheduler_loop, daemon=True, name="scanner-scheduler")
    t.start()
    _log("Scheduler spustený ✅")


try:
    init_db()
    _start_scheduler()
except Exception as e:
    _log(f"Chyba pri štarte: {e}", "err")


if __name__ == "__main__":
    _log(f"Dashboard: http://localhost:5000?token={DASHBOARD_PASSWORD}")
    _log(f"DB: {'PostgreSQL' if USE_PG else 'SQLite (lokálne)'}")
    app.run(host="0.0.0.0", port=int(os.getenv("PORT",5000)), debug=False)
