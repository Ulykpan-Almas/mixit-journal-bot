#!/usr/bin/env python3
"""Telegram-бот «Журнал правок Kaspi» (@mixit_kaspi_journal_bot).
Всё работает кнопками: артикул → что изменили → было/стало → когда → сохранить.
Через 8 дней бот присылает разбор заказов до/после. Настройки — в .env (BOT_TOKEN, ALLOWED_IDS, KASPI_TOKEN)."""
import asyncio, os, re, subprocess, sys, datetime as dt
from pathlib import Path
import journal_core as jc

BASE = Path(__file__).resolve().parent
for _s in (sys.stdout, sys.stderr):
    try: _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass


def load_env():
    p = BASE / ".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
TOKEN = os.environ.get("BOT_TOKEN", "")
ALLOWED = {int(x) for x in re.split(r"[,\s]+", os.environ.get("ALLOWED_IDS", "")) if x.strip().isdigit()}
AGG = BASE / "data" / "agg.json"
(BASE / "data").mkdir(exist_ok=True)
import cloud_sync
cloud_sync.pull(BASE)
DB = jc.init_db(BASE / "journal.db")
DB.execute("CREATE TABLE IF NOT EXISTS access(user_id INTEGER PRIMARY KEY, name TEXT, status TEXT)"); DB.commit()
ADMINS = set(ALLOWED)  # админы — из ALLOWED_IDS (секрет GitHub); остальных одобряют кнопкой в боте


def allowed():
    return ADMINS | {r[0] for r in DB.execute("SELECT user_id FROM access WHERE status='ok'")}

B_ADD, B_HIST, B_REP, B_EXP, B_UPD = "➕ Записать правку", "📜 История карточки", "📊 Как идут заказы", "📁 Журнал в Excel", "🔄 Обновить заказы"
KINDS = ["название", "описание", "фото", "видео", "рич-контент", "инфографика", "цена", "бонус", "остаток", "реклама", "другое"]
ICON = {"название": "✏️", "описание": "📝", "фото": "🖼", "видео": "🎬", "рич-контент": "✨", "инфографика": "📊",
        "цена": "💰", "бонус": "🎁", "остаток": "📦", "реклама": "📣", "другое": "⚙️"}


# 10 отслеживаемых карточек: SKU в кабинете -> название; код товара Kaspi (как «Код товара» на карточке) -> SKU
KNOWN = {
    "SKUA000275700": "1 Скраб Нежный кокос", "SKUA000059600": "2 Крем для ног 15%", "SKUA000854400": "3 Мист Дикая вишня",
    "SKUA000276200": "4 Крем-баттер Райский кокос", "SKUA000555000": "5 Крем с мочевиной 460", "SKUA000380800": "6 SOS крем-воск",
    "4620400206756": "7 Гель Formula", "4620400206992": "8 Скраб Солёная карамель", "SKUA000665800": "9 Крем Нероли",
    "4650358453254": "10 Скраб Прощай целлюлит",
}
CODES = {
    "117411541": "SKUA000275700", "121031717": "SKUA000059600", "146388134": "SKUA000854400", "112388044": "SKUA000276200",
    "143055024": "SKUA000555000", "123186653": "SKUA000380800", "164420710": "4620400206756", "167353548": "4620400206992",
    "143059665": "SKUA000665800", "146116307": "4650358453254",
}


def skus():
    try: return jc.load_orders(AGG)[1]
    except Exception: return dict(KNOWN)


def find_card(text):
    """артикул/SKU Kaspi → (sku, название). Любой введённый артикул принимаем, но помечаем, если он не отслеживается."""
    t = text.strip().replace(" ", "")
    s = skus()
    if t in CODES: t = CODES[t]
    for sku, name in s.items():
        if sku.lower() == t.lower(): return sku, name, True
    return t, t, False


LAST_ERR = {"text": ""}
PULL_LOCK = asyncio.Lock()


async def refresh_orders():
    """выгрузка заказов; при ошибке причина остаётся в LAST_ERR и в data/pull_error.log"""
    if not os.environ.get("KASPI_TOKEN"):
        LAST_ERR["text"] = "не указан KASPI_TOKEN в файле .env"; return False
    (BASE / "data").mkdir(exist_ok=True)
    async with PULL_LOCK:
        try:
            r = await asyncio.to_thread(subprocess.run, [sys.executable, str(BASE / "kaspi_pull.py"), str(BASE / "data")],
                                        timeout=3 * 3600, capture_output=True, encoding="utf-8", errors="replace",
                                        env={**os.environ, "PYTHONUTF8": "1"})
            out = ((r.stderr or "") + (r.stdout or "")).replace(os.environ.get("KASPI_TOKEN", "~~"), "***").strip()
            ok_ = r.returncode == 0 and AGG.exists()
            LAST_ERR["text"] = "" if ok_ else (out[-700:] or f"скрипт завершился с кодом {r.returncode}")
            (BASE / "data" / "pull_error.log").write_text(out, encoding="utf-8")
            return ok_
        except Exception as e:
            LAST_ERR["text"] = f"{type(e).__name__}: {e}"; return False


async def ensure_fresh(max_age_min=20):
    """обновить заказы, если файла нет или он старше max_age_min минут; True, если данные есть"""
    import time
    if AGG.exists() and (time.time() - AGG.stat().st_mtime) / 60 < max_age_min:
        return True
    ok_ = await refresh_orders()
    return ok_ or AGG.exists()


def stage_due(ts_iso, now):
    """какой по счёту отчёт пора слать: 1 — через 3 ч, 2 — на следующее утро (от 18 ч), 3 — через 3 дня, 4 — итог через 8 дней"""
    t = dt.datetime.fromisoformat(ts_iso); h = (now - t).total_seconds() / 3600
    return 4 if h >= 8 * 24 else 3 if h >= 3 * 24 else 2 if h >= 18 else 1 if h >= 3 else 0


def build_app():
    from telegram import InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup, ReplyKeyboardMarkup, Update
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

    MENU = ReplyKeyboardMarkup([[B_ADD], [B_REP, B_HIST], [B_UPD, B_EXP]], resize_keyboard=True)
    CANCEL = Markup([[Btn("✖ Отмена", callback_data="cancel")]])

    def ok(u): return bool(u.effective_user and u.effective_user.id in allowed())

    async def deny(u):
        usr = u.effective_user; uid = usr.id
        name = (usr.full_name or "") + (" (@%s)" % usr.username if usr.username else "")
        row = DB.execute("SELECT status FROM access WHERE user_id=?", (uid,)).fetchone()
        if row and row[0] == "no":
            return await u.effective_message.reply_text("Доступ не выдан. Если это ошибка, напишите Алмасу.")
        if row and row[0] == "wait":
            return await u.effective_message.reply_text("Заявка уже отправлена, подождите, пока Алмас её одобрит 🙏")
        DB.execute("INSERT OR REPLACE INTO access(user_id,name,status) VALUES(?,?,'wait')", (uid, name.strip())); DB.commit()
        await u.effective_message.reply_text("Заявка на доступ отправлена Алмасу. Когда её одобрят, бот напишет вам сам 🙌")
        kb = Markup([[Btn("✅ Разрешить", callback_data="acc:ok:%d" % uid), Btn("🚫 Отклонить", callback_data="acc:no:%d" % uid)]])
        for a in ADMINS:
            try: await u.get_bot().send_message(a, "🆕 Просит доступ к журналу:\n%s\nID: %d" % (name.strip(), uid), reply_markup=kb)
            except Exception as e: print("не удалось написать админу", a, e)

    def kinds_kb(sel):
        rows, row = [], []
        for k in KINDS:
            row.append(Btn(("✅ " if k in sel else ICON[k] + " ") + k.capitalize(), callback_data="k:" + k))
            if len(row) == 2: rows.append(row); row = []
        if row: rows.append(row)
        rows.append([Btn("Далее ➡️" if sel else "Выберите хотя бы одно", callback_data="kdone"), Btn("✖ Отмена", callback_data="cancel")])
        return Markup(rows)

    def summary(d):
        t = d["ts"]
        lines = ["📋 Проверьте запись", f"Код товара: {d['code']}" + (f" — {d['card']}" if d["card"] != d["sku"] else ""),
                 "Что изменили: " + ", ".join(d["kinds"]), f"Когда: {t:%d.%m.%Y %H:%M}"]
        if d.get("before"): lines.append(f"Было: {d['before']}")
        if d.get("after"): lines.append(f"Стало: {d['after']}")
        if not d["tracked"]: lines.append("⚠️ Этой карточки нет в списке отслеживаемых (пока 10 карточек «Уход за телом»): правку запишу, но разбор заказов по нему считать не смогу.")
        return "\n".join(lines)

    WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    MONTHS = ["", "январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]

    def grid(btns, per_row):
        return [btns[i:i + per_row] for i in range(0, len(btns), per_row)]

    def day_kb():
        today = dt.datetime.now(jc.TZ).date(); btns = []
        for i in range(0, 10):
            day = today - dt.timedelta(days=i)
            label = ("Сегодня" if i == 0 else "Вчера" if i == 1 else f"{day:%d.%m} {WD[day.weekday()]}")
            btns.append(Btn(label, callback_data="d:" + day.isoformat()))
        rows = grid(btns, 2) + [[Btn("📅 Другая дата", callback_data="othermonth")], [Btn("✖ Отмена", callback_data="cancel")]]
        return Markup(rows)

    def month_kb():
        today = dt.datetime.now(jc.TZ).date(); btns = []
        y, m = today.year, today.month
        for _ in range(4):
            btns.append(Btn(f"{MONTHS[m].capitalize()} {y}", callback_data=f"mon:{y}-{m:02d}"))
            m -= 1
            if m == 0: m, y = 12, y - 1
        return Markup(grid(btns, 2) + [[Btn("⬅️ Назад", callback_data="settime"), Btn("✖ Отмена", callback_data="cancel")]])

    def days_of_month_kb(ym):
        y, m = map(int, ym.split("-")); today = dt.datetime.now(jc.TZ).date(); btns = []
        d = dt.date(y, m, 1)
        while d.month == m and d <= today:
            btns.append(Btn(str(d.day), callback_data="d:" + d.isoformat())); d += dt.timedelta(days=1)
        return Markup(grid(btns, 7) + [[Btn("⬅️ Назад", callback_data="othermonth"), Btn("✖ Отмена", callback_data="cancel")]])

    def hour_kb(day):
        now = dt.datetime.now(jc.TZ); maxh = now.hour if day == now.date() else 23
        btns = [Btn(f"{h:02d}", callback_data=f"h:{h:02d}") for h in range(0, maxh + 1)]
        return Markup(grid(btns, 6) + [[Btn("⬅️ Назад", callback_data="settime"), Btn("✖ Отмена", callback_data="cancel")]])

    def min_kb(day, hour):
        now = dt.datetime.now(jc.TZ); btns = []
        for m in range(0, 60, 5):
            if day == now.date() and hour == now.hour and m > now.minute: break
            btns.append(Btn(f"{hour:02d}:{m:02d}", callback_data=f"m:{m:02d}"))
        return Markup(grid(btns, 4) + [[Btn("⬅️ Назад", callback_data="pickhour"), Btn("✖ Отмена", callback_data="cancel")]])

    async def show_menu(u, text="Выберите действие кнопкой ниже 👇"):
        await u.effective_message.reply_text(text, reply_markup=MENU)

    async def start(u, c):
        if not ok(u): return await deny(u)
        c.user_data.clear()
        await show_menu(u, "👋 Журнал правок карточек Kaspi.\nФиксирую, кто, когда и что поменял в карточке, и показываю, как меняются заказы: первые часы, завтра, через 3 дня и итог через 8 дней.\n\nНажмите «➕ Записать правку».")

    async def begin_add(u, c):
        c.user_data.clear(); c.user_data["state"] = "art"
        await u.effective_message.reply_text("1️⃣ Введите *код товара Kaspi* — число с карточки, строка «Код товара: 123954236»:", parse_mode="Markdown", reply_markup=CANCEL)

    async def on_text(u, c):
        if not ok(u): return await deny(u)
        t = u.effective_message.text.strip(); d = c.user_data; st = d.get("state")
        if t == B_ADD: return await begin_add(u, c)
        if t == B_HIST:
            d.clear(); d["state"] = "hist"
            return await u.effective_message.reply_text("Введите код товара Kaspi, по которому показать историю:", reply_markup=CANCEL)
        if t == B_REP:
            d.clear()
            rows = DB.execute("SELECT id,ts,card,kind FROM changes ORDER BY ts DESC LIMIT 8").fetchall()
            if not rows: return await u.effective_message.reply_text("Правок пока нет.")
            kb = Markup([[Btn(f"№{i} · {jc.datetime_short(ts)} · {card[:18]} · {k}", callback_data=f"rep:{i}")] for i, ts, card, k in rows])
            return await u.effective_message.reply_text("Выберите правку (по часам в первые сутки, потом по дням):", reply_markup=kb)
        if t == B_UPD:
            if not os.environ.get("KASPI_TOKEN"): return await u.effective_message.reply_text("Не указан KASPI_TOKEN в файле .env. Без него заказы выгрузить нельзя.")
            m = await u.effective_message.reply_text("⏳ Выгружаю заказы Kaspi, это займёт несколько минут…")
            ok_ = await refresh_orders()
            return await u.effective_message.reply_text("✅ Заказы обновлены." if ok_ else "❌ Не получилось выгрузить заказы.\nПричина: " + LAST_ERR["text"])
        if t == B_EXP:
            p = BASE / "journal.xlsx"; jc.export_xlsx(DB, p)
            with open(p, "rb") as f: return await u.effective_message.reply_document(f, filename="Журнал_правок_Kaspi.xlsx")
        if st == "art":
            if not re.fullmatch(r"[A-Za-z0-9]{5,20}", t.replace(" ", "")):
                return await u.effective_message.reply_text("Не похоже на код. Введите число с карточки, например 123954236")
            sku, name, tracked = find_card(t)
            d.update(sku=sku, code=t.replace(" ", ""), card=name, tracked=tracked, kinds=[], state="kinds")
            return await u.effective_message.reply_text(f"Код товара: {d['code']}" + (f" — {name}" if tracked else "") + "\n\n2️⃣ *Что изменили?* Можно выбрать несколько:", parse_mode="Markdown", reply_markup=kinds_kb([]))
        if st == "hist":
            sku, name, _ = find_card(t); d.clear()
            return await show_menu(u, jc.history(DB, sku, skus()))
        if st == "note":
            parts = re.split(r"\s*(?:→|->|=>)\s*", t, maxsplit=1)
            d["before"], d["after"] = (parts[0], parts[1]) if len(parts) == 2 else ("", parts[0])
            return await ask_time(u, c)
        await show_menu(u)

    async def ask_time(u, c):
        c.user_data["state"] = "when"
        kb = Markup([[Btn("🕐 Сейчас", callback_data="now"), Btn("📅 Выбрать дату и время", callback_data="settime")], [Btn("✖ Отмена", callback_data="cancel")]])
        await u.effective_message.reply_text("4️⃣ *Когда внесли правку?*", parse_mode="Markdown", reply_markup=kb)

    async def confirm(u, c):
        d = c.user_data; d["state"] = "confirm"
        kb = Markup([[Btn("✅ Сохранить", callback_data="save"), Btn("✖ Отмена", callback_data="cancel")]])
        await u.effective_message.reply_text(summary(d), reply_markup=kb)

    async def on_button(u, c):
        q = u.callback_query; await q.answer()
        if q.data.startswith("acc:"):
            if u.effective_user.id not in ADMINS: return
            _, act, tid = q.data.split(":"); tid = int(tid)
            DB.execute("UPDATE access SET status=? WHERE user_id=?", ("ok" if act == "ok" else "no", tid)); DB.commit()
            nm = (DB.execute("SELECT name FROM access WHERE user_id=?", (tid,)).fetchone() or [tid])[0]
            await q.edit_message_text(("✅ Доступ выдан: %s" if act == "ok" else "🚫 Отклонено: %s") % nm)
            try:
                if act == "ok":
                    await c.bot.send_message(tid, "✅ Доступ открыт! Нажмите /start", reply_markup=MENU)
                else: await c.bot.send_message(tid, "Доступ не выдан.")
            except Exception as e: print("не удалось написать", tid, e)
            return
        if not ok(u): return await deny(u)
        d = c.user_data; data = q.data
        if data == "cancel":
            d.clear(); await q.edit_message_text("Отменено."); return
        if data.startswith("rep:"):
            await q.edit_message_text("⏳ Обновляю заказы Kaspi и считаю. В первый раз это занимает несколько минут…")
            have = await ensure_fresh()
            if not have:
                return await q.edit_message_text("❌ Не получилось выгрузить заказы.\nПричина: " + LAST_ERR["text"])
            try: txt = jc.report(DB, int(data[4:]), AGG)
            except Exception as e: txt = f"Не получилось посчитать: {type(e).__name__}: {e}"
            return await q.edit_message_text(txt)
        if d.get("state") == "kinds" and data.startswith("k:"):
            k = data[2:]; sel = d["kinds"]
            sel.remove(k) if k in sel else sel.append(k)
            return await q.edit_message_reply_markup(kinds_kb(sel))
        if d.get("state") == "kinds" and data == "kdone":
            if not d["kinds"]: return
            d["state"] = "note"
            kb = Markup([[Btn("Пропустить ⏭", callback_data="skipnote")], [Btn("✖ Отмена", callback_data="cancel")]])
            await q.edit_message_text("Выбрано: " + ", ".join(d["kinds"]))
            return await q.message.reply_text("3️⃣ Что было и что стало? Напишите так: `было → стало`\n(можно только «стало») или нажмите «Пропустить».", parse_mode="Markdown", reply_markup=kb)
        if d.get("state") == "note" and data == "skipnote":
            d["before"] = d["after"] = ""; await q.edit_message_text("Было/стало: пропущено"); return await ask_time(u, c)
        if d.get("state") == "when" and data == "now":
            d["ts"] = dt.datetime.now(jc.TZ); await q.edit_message_text("Время: сейчас"); return await confirm(u, c)
        if data == "settime" and d.get("state") in ("when", "pickday", "pickmonth", "pickdaym", "pickhour", "pickmin"):
            d["state"] = "pickday"; return await q.edit_message_text("📅 Выберите день правки:", reply_markup=day_kb())
        if d.get("state") in ("pickday", "pickmonth") and data == "othermonth" or data == "othermonth" and d.get("state") == "pickdaym":
            d["state"] = "pickmonth"; return await q.edit_message_text("Выберите месяц:", reply_markup=month_kb())
        if d.get("state") == "pickmonth" and data.startswith("mon:"):
            d["state"] = "pickdaym"; return await q.edit_message_text("Выберите число:", reply_markup=days_of_month_kb(data[4:]))
        if d.get("state") in ("pickday", "pickdaym") and data.startswith("d:"):
            d["pdate"] = dt.date.fromisoformat(data[2:]); d["state"] = "pickhour"
            return await q.edit_message_text(f"День: {d['pdate']:%d.%m.%Y}\n🕐 Выберите час:", reply_markup=hour_kb(d["pdate"]))
        if d.get("state") in ("pickhour", "pickmin") and data == "pickhour":
            d["state"] = "pickhour"; return await q.edit_message_text(f"День: {d['pdate']:%d.%m.%Y}\n🕐 Выберите час:", reply_markup=hour_kb(d["pdate"]))
        if d.get("state") == "pickhour" and data.startswith("h:"):
            d["phour"] = int(data[2:]); d["state"] = "pickmin"
            return await q.edit_message_text(f"{d['pdate']:%d.%m.%Y}, {d['phour']:02d}:__\nВыберите минуты:", reply_markup=min_kb(d["pdate"], d["phour"]))
        if d.get("state") == "pickmin" and data.startswith("m:"):
            d["ts"] = dt.datetime.combine(d["pdate"], dt.time(d["phour"], int(data[2:])), tzinfo=jc.TZ)
            await q.edit_message_text(f"Время правки: {d['ts']:%d.%m.%Y %H:%M}"); return await confirm(u, c)
        if d.get("state") == "confirm" and data == "save":
            user = u.effective_user.full_name or str(u.effective_user.id); ids = []
            for k in d["kinds"]:
                jc.add_change(DB, u.effective_user.id, user, dict(ts=d["ts"], card=d["card"], sku=d["sku"], kind=k, before=d.get("before", ""), after=d.get("after", "")))
                ids.append(DB.execute("SELECT max(id) FROM changes").fetchone()[0])
            d.clear()
            await q.edit_message_text(f"✅ Записано: {', '.join('№%d' % i for i in ids)}\n{user} · {jc.datetime_short(DB.execute('SELECT ts FROM changes WHERE id=?', (ids[-1],)).fetchone()[0])}\nПокажу заказы через 3 часа, завтра утром, через 3 дня и итог через 8 дней. Посмотреть сейчас: «📊 Как идут заказы».")
            return await show_menu(u, "Что дальше?")
        await q.answer("Начните заново: /start", show_alert=False)

    async def tick(context):
        """каждый час: если есть правки, по которым пора слать отчёт, обновить заказы и разослать"""
        now = dt.datetime.now(jc.TZ); known = set(skus()); due = []
        for cid, ts, sku, rep in DB.execute("SELECT id,ts,sku,reported FROM changes WHERE reported<4").fetchall():
            st = stage_due(ts, now)
            if st > rep and sku in known: due.append((cid, st))
        if not due: return
        await refresh_orders()
        titles = {1: "первые часы", 2: "сутки", 3: "3 дня", 4: "итог, 8 дней"}
        for cid, st in due:
            try: txt = jc.report(DB, cid, AGG)
            except Exception as e: txt = f"Правка №{cid}: не удалось посчитать ({e})"
            for uid in allowed(): await context.bot.send_message(uid, "🔔 Отчёт: %s\n\n%s" % (titles[st], txt))
            DB.execute("UPDATE changes SET reported=? WHERE id=?", (st, cid)); DB.commit()

    app = Application.builder().token(TOKEN).build()
    for name in ("start", "menu", "help", "cancel"): app.add_handler(CommandHandler(name, start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(tick, interval=3600, first=120)
    lim = int(os.environ.get("RUN_LIMIT_MIN", "0") or 0)
    if lim:  # в облаке бот работает ограниченное время, потом его перезапускает расписание
        async def _stop(context):
            print("Плановая остановка, выгружаю данные."); cloud_sync.push(BASE); context.application.stop_running()
        app.job_queue.run_once(_stop, lim * 60)

    async def on_error(update, context):
        e = context.error
        if type(e).__name__ == "Conflict":
            print("ВНИМАНИЕ: запущено два бота с одним токеном. Закройте все чёрные окна бота, кроме одного.")
        else:
            print("Ошибка:", type(e).__name__, e)
    app.add_error_handler(on_error)
    return app


def single_instance():
    """второй экземпляр бота не запускаем: иначе Telegram выдаёт Conflict и отвечает то старая, то новая версия"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 47831)); s.listen(1)
    except OSError:
        sys.exit("Бот уже запущен в другом окне. Закройте это окно и все другие окна бота, оставьте одно.")
    return s


if __name__ == "__main__":
    if not TOKEN: sys.exit("Нет BOT_TOKEN в файле .env")
    cloud = os.environ.get("RUN_LIMIT_MIN")
    if not cloud: _lock = single_instance()
    cloud_sync.start_pusher(BASE)
    print("Бот запущен. Остановить — Ctrl+C."); build_app().run_polling(drop_pending_updates=not cloud)
    cloud_sync.push(BASE)
