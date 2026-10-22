"""Облачное хранение: journal.db и agg.json лежат в Supabase Storage (закрытый бакет).
Включается, только если заданы SUPABASE_URL и SUPABASE_KEY. На компьютере без них всё работает как раньше."""
import os, sys, json, time, shutil, sqlite3, hashlib, threading, urllib.request, urllib.error
from pathlib import Path

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_KEY", "")
BUCKET = os.environ.get("SUPABASE_BUCKET", "mixit-journal")
ON = bool(URL and KEY)


def _req(method, path, data=None, ctype=None):
    h = {"Authorization": "Bearer " + KEY, "apikey": KEY}
    if ctype: h["Content-Type"] = ctype
    if method in ("POST", "PUT") and "/object/" in path: h["x-upsert"] = "true"
    r = urllib.request.Request(URL + path, data=data, method=method, headers=h)
    return urllib.request.urlopen(r, timeout=60).read()


def _ensure_bucket():
    try:
        _req("POST", "/storage/v1/bucket", json.dumps({"id": BUCKET, "name": BUCKET, "public": False}).encode(), "application/json")
    except urllib.error.HTTPError:
        pass  # уже есть


def _download(name, dest: Path):
    try:
        data = _req("GET", "/storage/v1/object/%s/%s" % (BUCKET, name))
    except urllib.error.HTTPError as e:
        if e.code in (400, 404): return False
        raise
    dest.parent.mkdir(parents=True, exist_ok=True); dest.write_bytes(data); return True


def _upload(name, src: Path):
    _req("POST", "/storage/v1/object/%s/%s" % (BUCKET, name), src.read_bytes(), "application/octet-stream")


def pull(base: Path):
    if not ON: return
    _ensure_bucket()
    for name, rel in (("journal.db", "journal.db"), ("agg.json", "data/agg.json")):
        try: print("Supabase: скачано" if _download(name, base / rel) else "Supabase: нового файла нет", name)
        except Exception as e: print("Supabase: не удалось скачать", name, e)


_last = {}


def push(base: Path):
    """выгрузить, если файл изменился"""
    if not ON: return
    db = base / "journal.db"
    if db.exists():
        tmp = base / "journal.snap.db"
        s = sqlite3.connect(db); d = sqlite3.connect(tmp); s.backup(d); d.close(); s.close()
        h = hashlib.md5(tmp.read_bytes()).hexdigest()
        if _last.get("db") != h:
            try: _upload("journal.db", tmp); _last["db"] = h
            except Exception as e: print("Supabase: ошибка выгрузки journal.db", e)
        tmp.unlink(missing_ok=True)
    agg = base / "data" / "agg.json"
    if agg.exists():
        h = str(agg.stat().st_mtime)
        if _last.get("agg") != h:
            try: _upload("agg.json", agg); _last["agg"] = h
            except Exception as e: print("Supabase: ошибка выгрузки agg.json", e)


def start_pusher(base: Path, every=45):
    if not ON: return
    def loop():
        while True:
            time.sleep(every)
            try: push(base)
            except Exception as e: print("Supabase:", e)
    threading.Thread(target=loop, daemon=True).start()
