"""Журнал правок карточек Kaspi + расчёт эффекта по заказам (agg.json из kaspi_pull.py)."""
import json, re, sqlite3, datetime as dt
from pathlib import Path

TZ = dt.timezone(dt.timedelta(hours=5))
KINDS = [  # (ключевые слова, тип)
    (("назван", "заголов"), "название"), (("описан",), "описание"), (("фото", "картин"), "фото"),
    (("видео", "ролик"), "видео"), (("рич", "rich", "инфограф", "слайд"), "рич-контент"),
    (("цен",), "цена"), (("бонус",), "бонус"), (("остат",), "остаток"), (("реклам",), "реклама"),
]
BAD = {"CANCELLED", "CANCELLING", "RETURNED", "KASPI_DELIVERY_RETURN_REQUESTED"}


def init_db(path):
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE IF NOT EXISTS changes(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, user_id INTEGER,
        user_name TEXT, card TEXT, sku TEXT, kind TEXT, before TEXT, after TEXT, note TEXT, reported INTEGER DEFAULT 0)""")
    db.commit(); return db


def resolve_card(token, skus):
    """номер 1–10, SKU или название-часть -> (sku, подпись)"""
    t = token.strip()
    if t in skus: return t, skus[t]
    for sku, name in skus.items():
        if name.split()[0] == t: return sku, name
    return None, t


def parse_line(text, skus, now=None):
    """'7500089 заголовок: было → стало в 12:40' или '3 видео: добавили ролик'"""
    now = now or dt.datetime.now(TZ)
    m = re.match(r"\s*(\S+)\s+([^:]+?)\s*:\s*(.+)", text, re.S)
    if not m: return None, "Формат: <номер или SKU карточки> <что изменили>: было → стало"
    card, kind_raw, rest = m.groups()
    kind = next((k for words, k in KINDS if any(w in kind_raw.lower() for w in words)), "другое")
    ts = now
    tm = re.search(r"\bв?\s*(\d{1,2})[:.](\d{2})\s*$", rest)
    if tm and int(tm.group(1)) < 24:
        ts = now.replace(hour=int(tm.group(1)), minute=int(tm.group(2)), second=0, microsecond=0)
        if ts > now: ts -= dt.timedelta(days=1)
        rest = rest[:tm.start()].strip()
    parts = re.split(r"\s*(?:→|->|=>)\s*", rest, maxsplit=1)
    before, after = (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])
    sku, label = resolve_card(card, skus)
    return dict(ts=ts, card=label, sku=sku, kind=kind, before=before.strip(), after=after.strip()), None


def add_change(db, user_id, user_name, rec):
    db.execute("INSERT INTO changes(ts,user_id,user_name,card,sku,kind,before,after) VALUES(?,?,?,?,?,?,?,?)",
               (rec["ts"].isoformat(), user_id, user_name, rec["card"], rec["sku"], rec["kind"], rec["before"], rec["after"]))
    db.commit()


def load_orders(agg_path):
    d = json.load(open(agg_path, encoding="utf-8"))
    per = {}  # sku -> date -> [orders, revenue]
    for k, (orders, units, rev) in d["agg"].items():
        day, sku, st = k.split("|")
        if st in BAD: continue
        x = per.setdefault(sku, {}).setdefault(day, [0, 0.0]); x[0] += orders; x[1] += rev
    return per, d["skus"], d["start_ms"], d["end_ms"]


def _sum(per, sku, d0, n):
    o = r = 0
    for i in range(n):
        v = per.get(sku, {}).get((d0 + dt.timedelta(days=i)).isoformat())
        if v: o += v[0]; r += v[1]
    return o, r


def analyze(db, change_id, agg_path, window=7):
    if not Path(agg_path).exists():
        return "Заказы Kaspi ещё не выгружены. Нажмите «🔄 Обновить заказы»."
    per, skus, start_ms, end_ms = load_orders(agg_path)
    row = db.execute("SELECT ts,card,sku,kind,before,after,user_name FROM changes WHERE id=?", (change_id,)).fetchone()
    ts, card, sku, kind, before, after, who = row
    t = dt.datetime.fromisoformat(ts); day = t.date()
    head = _head(change_id, card, kind, t, who)
    if sku not in skus: return head + "\n\nЭта карточка не в списке отслеживаемых, посчитать заказы не могу."
    d_before = day - dt.timedelta(days=window); d_after = day + dt.timedelta(days=1)
    first = dt.datetime.fromtimestamp(start_ms / 1000, TZ).date(); last = dt.datetime.fromtimestamp(end_ms / 1000, TZ).date()
    if d_before < first: return head + f"\n\n⏳ Для сравнения нужны заказы за {window} дн. до правки, а выгрузка начинается с {first:%d.%m}. Нажмите «🔄 Обновить заказы» и попробуйте снова."
    if day + dt.timedelta(days=window) >= last: return head + f"\n\n⏳ После правки прошло меньше {window} полных дней, итог будет позже."
    b, br = _sum(per, sku, d_before, window); a, ar = _sum(per, sku, d_after, window)
    # контроль: карточки без правок в окне ±window
    busy = {r[0] for r in db.execute("SELECT DISTINCT sku FROM changes WHERE date(ts) BETWEEN ? AND ?",
            ((day - dt.timedelta(days=window)).isoformat(), (day + dt.timedelta(days=window)).isoformat()))}
    ctrl = [s for s in skus if s not in busy]
    cb = sum(_sum(per, s, d_before, window)[0] for s in ctrl); ca = sum(_sum(per, s, d_after, window)[0] for s in ctrl)
    pct = lambda x, y: (y / x - 1) * 100 if x else None
    g, cg = pct(b, a), pct(cb, ca)
    other = [r for r in db.execute("SELECT kind,ts,user_name FROM changes WHERE sku=? AND id<>? AND date(ts) BETWEEN ? AND ?",
             (sku, change_id, (day - dt.timedelta(days=3)).isoformat(), (day + dt.timedelta(days=3)).isoformat()))]
    head = _head(change_id, card, kind, t, who)
    icon, text = _verdict(b + a >= 20, other, g, cg, kind, window < 7)
    lines = [head, "", f"⏱ Сравниваем {window} дн. до правки и {window} дн. после ({d_before:%d.%m}–{(day - dt.timedelta(days=1)):%d.%m} и {d_after:%d.%m}–{(day + dt.timedelta(days=window)):%d.%m})", "",
             "🧴 Эта карточка", f"• Заказов до правки: {b}", f"• Заказов после правки: {a}", f"• Разница: {_p(g)} {_arrow(g)}"]
    if b: lines.append(f"• Выручка: {br:,.0f} → {ar:,.0f} ₸".replace(",", " "))
    if cb: lines += ["", f"🏪 Остальные карточки без правок ({len(ctrl)})", f"• Заказов: {cb} → {ca} ({_p(cg)}) {_arrow(cg)}"]
    lines += ["", f"{icon}", text, "", "ℹ️ Сравниваем с остальными карточками, чтобы не принять общий рост рынка за эффект правки."]
    return "\n".join(lines)


def _arrow(p):
    return "📈" if p is not None and p > 5 else "📉" if p is not None and p < -5 else "➖"


def _p(p):
    return "нет данных" if p is None else f"{p:+.0f}%".replace("-", "−")


def _head(cid, card, kind, t, who):
    return f"📊 Правка №{cid}\n🧴 {card}\n✏️ {kind} · {t:%d.%m} в {t:%H:%M} · {who}"


def _verdict(base_ok, other, g, cg, kind, prelim):
    """иконка + короткий вывод + пояснение простыми словами"""
    if not base_ok:
        return "⏳ Пока мало данных", "Заказов у карточки мало, поэтому любой вывод сейчас был бы случайным. Посмотрите позже."
    if other:
        what = ", ".join(f"{k} ({datetime_short(x)})" for k, x, *_ in other)
        return "❓ Неясно, что повлияло", f"Рядом менялось ещё: {what}. Эффект одной правки от другой отделить нельзя."
    if g is None:
        return "⏳ Нет заказов до правки", "Не с чем сравнивать."
    eff = g - (cg or 0)
    tail = " Пока предварительно." if prelim else ""
    if eff > 20:
        return "🟢 Заказов стало больше", f"Карточка выросла на {eff:.0f} п.п. сильнее остальных. Похоже, правка ({kind}) помогла.{tail}"
    if eff < -20:
        return "🔴 Заказов стало меньше", f"Карточка просела на {-eff:.0f} п.п. сильнее остальных. Стоит проверить правку ({kind}).{tail}"
    return "⚪ Заметного эффекта нет", f"Карточка меняется так же, как остальные.{tail}"


def datetime_short(iso): return dt.datetime.fromisoformat(iso).strftime("%d.%m %H:%M")


def history(db, token, skus, limit=15):
    sku, label = resolve_card(token, skus)
    rows = db.execute("SELECT ts,user_name,kind,before,after FROM changes WHERE sku=? OR card=? ORDER BY ts DESC LIMIT ?", (sku, label, limit)).fetchall()
    if not rows: return "Правок по этой карточке нет."
    return f"{label}\n" + "\n".join(f"• {datetime_short(ts)} {u}: {k}" + (f" ({b} → {a})" if b else (f" ({a})" if a else "")) for ts, u, k, b, a in rows)


def export_xlsx(db, path):
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = "Журнал правок"
    ws.append(["Время", "Кто", "Карточка", "SKU", "Что изменили", "Было", "Стало"])
    for ts, u, c, s, k, b, a in db.execute("SELECT ts,user_name,card,sku,kind,before,after FROM changes ORDER BY ts"):
        ws.append([datetime_short(ts), u, c, s, k, b, a])
    for col, w in zip("ABCDEFG", (14, 14, 30, 16, 14, 40, 40)): ws.column_dimensions[col].width = w
    wb.save(path)


def load_hourly(agg_path):
    d = json.load(open(agg_path, encoding="utf-8"))
    h = {}
    for k, n in d.get("hourly", {}).items():
        day, hh, sku, st = k.split("|")
        if st in BAD: continue
        h[(day, int(hh), sku)] = h.get((day, int(hh), sku), 0) + n
    return h, d["skus"], d["end_ms"]


def _hsum(h, sku, start, end):
    n = 0; t = start
    while t < end:
        n += h.get((t.date().isoformat(), t.hour, sku), 0); t += dt.timedelta(hours=1)
    return n


def early(db, change_id, agg_path, now=None):
    """Первые часы после правки: заказы с момента правки против среднего за то же время суток за предыдущие 7 дней."""
    if not Path(agg_path).exists():
        return "Заказы Kaspi ещё не выгружены. Нажмите «🔄 Обновить заказы»."
    h, skus, end_ms = load_hourly(agg_path)
    ts, card, sku, kind, before, after, who = db.execute("SELECT ts,card,sku,kind,before,after,user_name FROM changes WHERE id=?", (change_id,)).fetchone()
    t = dt.datetime.fromisoformat(ts)
    head = _head(change_id, card, kind, t, who)
    if sku not in skus: return head + "\n\nЭта карточка не в списке отслеживаемых, заказы посчитать не могу."
    now = now or dt.datetime.fromtimestamp(end_ms / 1000, TZ)
    start = (t + dt.timedelta(minutes=59)).replace(minute=0, second=0, microsecond=0)
    end = now.replace(minute=0, second=0, microsecond=0)
    if (end - start) < dt.timedelta(hours=1):
        return head + "\n\n⏳ После правки ещё не прошёл полный час. Первые данные появятся через час-два, нажмите позже."
    n_h = int((end - start).total_seconds() // 3600)
    a = _hsum(h, sku, start, end)
    exp = [ _hsum(h, sku, start - dt.timedelta(days=k), end - dt.timedelta(days=k)) for k in range(1, 8)]
    base = sum(exp) / 7
    busy = {r[0] for r in db.execute("SELECT DISTINCT sku FROM changes WHERE ts>=?", ((t - dt.timedelta(days=7)).isoformat(),))}
    ctrl = [s for s in skus if s not in busy]
    ca = sum(_hsum(h, s, start, end) for s in ctrl); cb = sum(sum(_hsum(h, s, start - dt.timedelta(days=k), end - dt.timedelta(days=k)) for k in range(1, 8)) / 7 for s in ctrl)
    g = (a / base - 1) * 100 if base else None
    cg = (ca / cb - 1) * 100 if cb else None
    other = db.execute("SELECT kind,ts FROM changes WHERE sku=? AND id<>? AND ts>=?", (sku, change_id, (t - dt.timedelta(hours=24)).isoformat())).fetchall()
    icon, text = _verdict(base >= 3, other, g, cg, kind, True)
    lines = [_head(change_id, card, kind, t, who), "",
             f"⏱ Смотрим первые {n_h} ч после правки ({start:%d.%m %H:%M} – {end:%d.%m %H:%M})", "",
             "🧴 Эта карточка", f"• Заказов после правки: {a}", f"• Обычно за это же время суток: ≈{base:.0f}", f"• Разница: {_p(g)} {_arrow(g)}"]
    if cb: lines += ["", f"🏪 Остальные карточки без правок ({len(ctrl)})", f"• Заказов: {ca}, обычно ≈{cb:.0f} ({_p(cg)}) {_arrow(cg)}"]
    lines += ["", icon, text, "", "ℹ️ Это ранний замер по часам. Сравниваем с обычными днями и с остальными карточками."]
    return "\n".join(lines)


def report(db, change_id, agg_path, now=None):
    """Автовыбор: первые сутки считаем по часам, дальше по полным дням (до 7)."""
    if not Path(agg_path).exists():
        return "Заказы Kaspi ещё не выгружены. Нажмите «🔄 Обновить заказы»."
    ts = db.execute("SELECT ts FROM changes WHERE id=?", (change_id,)).fetchone()[0]
    t = dt.datetime.fromisoformat(ts)
    now = now or dt.datetime.now(TZ)
    full_days = (now.date() - t.date()).days - 1
    if full_days >= 2:
        w = min(7, full_days)
        txt = analyze(db, change_id, agg_path, window=w)
        return txt + ("\n(Предварительно: после правки пока только %d полн. дн., окончательный итог через 8 дней.)" % w if w < 7 else "")
    return early(db, change_id, agg_path, now=now)
