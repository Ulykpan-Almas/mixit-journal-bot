import json, os, sys, time, datetime, threading, urllib.request, urllib.parse, urllib.error
from concurrent.futures import ThreadPoolExecutor

TOKEN = os.environ["KASPI_TOKEN"]
OUT_DIR = sys.argv[1]
TZ = datetime.timezone(datetime.timedelta(hours=5))
DAYS = int(os.environ.get("KASPI_DAYS", "21"))
NOW = datetime.datetime.now(TZ)
FULL_START = (NOW - datetime.timedelta(days=DAYS)).replace(hour=0, minute=0, second=0, microsecond=0)
START = FULL_START
OLD = None
_old_path = os.path.join(OUT_DIR, "agg.json")
if os.path.exists(_old_path):  # докачка: берём старые данные и обновляем только последние 2 дня
    try:
        OLD = json.load(open(_old_path, encoding="utf-8"))
        if OLD["start_ms"] <= int(FULL_START.timestamp() * 1000) + 86400000:
            START = max(FULL_START, (NOW - datetime.timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0))
        else:
            OLD = None
    except Exception:
        OLD = None
START_MS = int(START.timestamp() * 1000)
END_MS = int(time.time() * 1000)
WINDOW_MS = 24 * 3600 * 1000
PAGE_SIZE = 100

SKUS = {
    "SKUA000275700": "1 Скраб Нежный кокос",
    "SKUA000059600": "2 Крем для ног 15%",
    "SKUA000854400": "3 Мист Дикая вишня",
    "SKUA000276200": "4 Крем-баттер Райский кокос",
    "SKUA000555000": "5 Крем с мочевиной 460",
    "SKUA000380800": "6 SOS крем-воск",
    "4620400206756": "7 Гель Formula",
    "4620400206992": "8 Скраб Солёная карамель",
    "SKUA000665800": "9 Крем Нероли",
    "4650358453254": "10 Скраб Прощай целлюлит",
}

lock = threading.Lock()
agg = {}          # "date|sku|status" -> [orders, units, revenue]
store = {}        # "date|status" -> orders (весь магазин)
hourly = {}       # "date|HH|sku|status" -> заказов
done_pages = 0
total_pages = 0
errors = []


def fetch(frm, to, page):
    q = urllib.parse.urlencode({
        "page[number]": page, "page[size]": PAGE_SIZE,
        "filter[orders][creationDate][$ge]": frm,
        "filter[orders][creationDate][$le]": to,
        "include[orders]": "entries",
    })
    req = urllib.request.Request(
        os.environ.get("KASPI_BASE", "https://kaspi.kz") + "/shop/api/v2/orders?" + q,
        headers={"X-Auth-Token": TOKEN, "Content-Type": "application/vnd.api+json", "User-Agent": "Mozilla/5.0"},
    )
    for attempt in range(7):
        try:
            return json.load(urllib.request.urlopen(req, timeout=60))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(3 * (attempt + 1))
                continue
            body = ""
            try: body = e.read().decode("utf-8", "replace")[:200]
            except Exception: pass
            raise RuntimeError("HTTP %s от Kaspi API (%s)" % (e.code, body or e.reason))
        except Exception:
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("fetch failed: page %s" % page)


def process(d):
    inc = {i["id"]: i for i in d.get("included", []) if i["type"] == "orderentries"}
    with lock:
        for o in d["data"]:
            a = o["attributes"]
            dtm = datetime.datetime.fromtimestamp(a["creationDate"] / 1000, TZ)
            day = dtm.strftime("%Y-%m-%d"); hh = dtm.strftime("%H")
            st = a.get("status") or "UNKNOWN"
            k = day + "|" + st
            store[k] = store.get(k, 0) + 1
            seen = {}
            for e in o.get("relationships", {}).get("entries", {}).get("data", []):
                ent = inc.get(e["id"])
                if not ent:
                    continue
                code = (ent["attributes"].get("offer") or {}).get("code")
                if code in SKUS:
                    s = seen.setdefault(code, [0, 0.0])
                    s[0] += ent["attributes"].get("quantity", 1)
                    s[1] += ent["attributes"].get("totalPrice", 0.0)
            for code, (units, rev) in seen.items():
                kk = "%s|%s|%s" % (day, code, st)
                r = agg.setdefault(kk, [0, 0, 0.0])
                r[0] += 1
                r[1] += units
                r[2] += rev
                hk = "%s|%s|%s|%s" % (day, hh, code, st)
                hourly[hk] = hourly.get(hk, 0) + 1


def run_page(frm, to, page):
    global done_pages
    try:
        d = fetch(frm, to, page)
        process(d)
    except Exception as ex:
        with lock:
            errors.append("%s-%s p%s: %s" % (frm, to, page, ex))
    with lock:
        done_pages += 1
        if done_pages % 20 == 0:
            with open(os.path.join(OUT_DIR, "progress.txt"), "w", encoding="utf-8") as f:
                f.write("%d/%d pages, errors=%d\n" % (done_pages, total_pages, len(errors)))


windows = []
cur = START_MS
while cur < END_MS:
    windows.append((cur, min(cur + WINDOW_MS - 1, END_MS)))
    cur += WINDOW_MS

with ThreadPoolExecutor(max_workers=4) as ex:
    for frm, to in windows:
        try:
            first = fetch(frm, to, 0)
        except Exception as e1:
            errors.append("%s-%s p0: %s" % (frm, to, e1))
            if "HTTP 401" in str(e1) or "HTTP 403" in str(e1):
                break
            continue
        process(first)
        pc = first["meta"]["pageCount"]
        if first["meta"]["totalCount"] > 9000:
            errors.append("window too big: %s-%s total=%s" % (frm, to, first["meta"]["totalCount"]))
        with lock:
            total_pages += pc
            done_pages += 1
        list(ex.map(lambda p: run_page(frm, to, p), range(1, pc)))

if OLD:
    cut = START.strftime("%Y-%m-%d"); low = FULL_START.strftime("%Y-%m-%d")
    for name, cur in (("agg", agg), ("hourly", hourly), ("store", store)):
        for k, v in OLD.get(name, {}).items():
            if low <= k.split("|")[0] < cut:
                cur[k] = v
with open(os.path.join(OUT_DIR, "agg.json"), "w", encoding="utf-8") as f:
    json.dump({"skus": SKUS, "agg": agg, "store": store, "hourly": hourly, "errors": errors,
               "start_ms": int(FULL_START.timestamp() * 1000), "end_ms": END_MS, "windows": windows}, f, ensure_ascii=False)
with open(os.path.join(OUT_DIR, "progress.txt"), "w", encoding="utf-8") as f:
    f.write("DONE %d/%d pages, errors=%d\n" % (done_pages, total_pages, len(errors)))

if errors:
    sys.stderr.write("ОШИБКИ (%d). Первая: %s\n" % (len(errors), errors[0]))
if errors and done_pages == 0 or any("HTTP 401" in e or "HTTP 403" in e for e in errors):
    sys.exit(1)
