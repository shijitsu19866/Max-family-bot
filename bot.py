import asyncio
import html
import re
import sqlite3
from datetime import datetime, timedelta
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo
import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import MAX_BOT_TOKEN, API_URL, DB_NAME
from exporter import export_monthly_report

IRKUTSK_TZ = ZoneInfo("Asia/Irkutsk")
scheduler = AsyncIOScheduler(timezone=IRKUTSK_TZ)
user_states = {}


def get_irkutsk_now() -> datetime:
    return datetime.now(IRKUTSK_TZ)


def btn(text: str, payload: str, intent: str = "default") -> dict:
    return {
        "type": "callback",
        "text": text,
        "payload": payload,
        "intent": intent,
    }


def main_menu_keyboard() -> list:
    return [
        [
            btn("📅 Планировщик", "section:planner", "positive"),
            btn("🛒 Покупки", "section:shopping", "positive"),
        ],
        [
            btn("👨‍👩‍👧‍👦 Семейный режим", "section:family", "default"),
        ],
    ]


def planner_back_keyboard() -> list:
    return [
        [btn("📅 В Планировщик", "section:planner", "positive")],
        [btn("🏠 На главную", "menu:home", "default")],
    ]


def shopping_back_keyboard() -> list:
    return [
        [btn("🛒 В Покупки", "section:shopping", "positive")],
        [btn("🏠 На главную", "menu:home", "default")],
    ]


def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT,
                family_code TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                family_code TEXT,
                author_name TEXT,
                title TEXT,
                task_time TEXT,
                target_ids TEXT DEFAULT 'ALL',
                target_names TEXT DEFAULT 'Все участники',
                is_done INTEGER DEFAULT 0,
                alarm_24h_stopped INTEGER DEFAULT 0,
                alarm_main_stopped INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS shopping (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                family_code TEXT,
                author_name TEXT,
                item TEXT,
                is_bought INTEGER DEFAULT 0,
                created_at TEXT
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                family_code TEXT,
                author_name TEXT,
                store_name TEXT,
                amount REAL DEFAULT 0,
                receipt_date TEXT,
                iso_date TEXT,
                photo_url TEXT,
                created_at TEXT
            )
        """)
        conn.commit()


def get_user_info(user_id: int, default_name: str):
    default_code = f"FAM-{user_id}"
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT name, family_code FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        if row:
            return row[0], row[1]
        cursor.execute(
            "INSERT INTO users (user_id, name, family_code) VALUES (?, ?, ?)",
            (user_id, default_name, default_code),
        )
        conn.commit()
        return default_name, default_code


def get_family_members(family_code: str):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT user_id, name FROM users WHERE family_code = ?", (family_code,))
        return cursor.fetchall()


async def send_max_message(
    user_id: int,
    text: str,
    buttons: list = None,
    use_html: bool = False,
):
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    params = {"user_id": user_id}
    payload = {
        "text": text,
        "notify": True,
    }
    if use_html:
        payload["format"] = "html"

    if buttons:
        payload["attachments"] = [
            {
                "type": "inline_keyboard",
                "payload": {"buttons": buttons},
            }
        ]

    async with aiohttp.ClientSession() as session:
        async with session.post(f"{API_URL}/messages", headers=headers, params=params, json=payload) as resp:
            return await resp.json()


async def send_start_screen(user_id: int, raw_name: str):
    user_states.pop(user_id, None)
    name, _ = get_user_info(user_id, raw_name)
    await send_max_message(
        user_id,
        f"👋 Привет, {name}!\n"
        f"Добро пожаловать в семейный помощник (время Иркутское).\n\n"
        f"Выберите нужный раздел на кнопках ниже:\n"
        f"• 📅 Планировщик — задачи, напоминания и будильники\n"
        f"• 🛒 Покупки — список продуктов и архив чеков по датам\n"
        f"• 👨‍👩‍👧‍👦 Семейный режим — подключение близких к общим спискам",
        main_menu_keyboard(),
    )


async def show_planner_section(user_id: int, family_code: str):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, title, task_time, is_done, author_name, target_names
            FROM tasks
            WHERE family_code = ?
            ORDER BY is_done ASC, id DESC LIMIT 15
            """,
            (family_code,),
        )
        rows = cursor.fetchall()

    buttons = [
        [btn("➕ Новая задача с будильником", "wizard:new_task", "positive")],
    ]

    lines = ["📅 РАЗДЕЛ: ПЛАНИРОВЩИК ЗАДАЧ\n━━━━━━━━━━━━━━━━━━"]
    has_done = False

    if not rows:
        lines.append("Список задач пока пуст.\nНажмите «➕ Новая задача с будильником», чтобы запланировать дело!")
    else:
        for t_id, title, t_time, is_done, author, target_names in rows:
            targets = target_names or "Все"
            if is_done:
                has_done = True
                lines.append(f"✅ [{t_time}] {title}\n   └ 👥 {targets}")
            else:
                lines.append(f"⏰ [{t_time}] {title}\n   └ 🔔 Кому: {targets} (от: {author})")
                buttons.append([btn(f"✅ Выполнить: {title[:22]}", f"done_task:{t_id}", "positive")])

    if has_done:
        buttons.append([btn("🧹 Удалить выполненные задачи", "clear_tasks:0", "negative")])

    buttons.append(
        [
            btn("🔔 Тест будильника (15 сек)", "test_alarm:0", "default"),
            btn("📊 Скачать отчет", "menu:report_planner", "default"),
        ]
    )
    buttons.append([btn("🏠 На главную", "menu:home", "default")])

    await send_max_message(user_id, "\n".join(lines), buttons)


async def show_shopping_section(user_id: int, family_code: str):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, item, is_bought, author_name
            FROM shopping
            WHERE family_code = ?
            ORDER BY is_bought ASC, id DESC LIMIT 25
            """,
            (family_code,),
        )
        rows = cursor.fetchall()

    buttons = [
        [btn("➕ Добавить продукты", "wizard:new_shop", "positive")],
    ]

    lines = ["🛒 РАЗДЕЛ: ПОКУПКИ И ЧЕКИ\n━━━━━━━━━━━━━━━━━━"]
    has_bought = False

    if not rows:
        lines.append("Список покупок пока пуст.\nНажмите «➕ Добавить продукты» или загрузите чек ниже!")
    else:
        for s_id, item, is_bought, author in rows:
            if is_bought:
                has_bought = True
                lines.append(f"✅ ~{item}~ (добавил: {author})")
            else:
                lines.append(f"⬜ {item} (от: {author})")
                buttons.append([btn(f"🛒 Куплено: {item[:24]}", f"buy_item:{s_id}", "default")])

    if has_bought:
        buttons.append([btn("🧹 Очистить уже купленное", "clear_bought:0", "negative")])

    buttons.append(
        [
            btn("🧾 Загрузить чек", "wizard:new_receipt", "positive"),
            btn("🗂 Чеки по датам", "menu:receipts", "default"),
        ]
    )
    buttons.append(
        [
            btn("📊 Скачать отчет", "menu:report_shop", "default"),
            btn("🏠 На главную", "menu:home", "default"),
        ]
    )

    await send_max_message(user_id, "\n".join(lines), buttons)


async def show_family_section(user_id: int, family_code: str):
    members = get_family_members(family_code)
    member_lines = [f" • 👤 {m_name}" + (" (Вы)" if m_id == user_id else "") for m_id, m_name in members]

    msg = (
        f"👨‍👩‍👧‍👦 РАЗДЕЛ: СЕМЕЙНЫЙ РЕЖИМ\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔑 Код вашей группы: {family_code}\n\n"
        f"👥 Подключенные участники ({len(members)}):\n"
        + "\n".join(member_lines)
        + "\n\n💡 Как объединить списки с женой, мужем или близкими:\n"
        f"1. Скопируйте код {family_code} кнопкой ниже и отправьте человеку.\n"
        f"2. Он открывает у себя раздел «👨‍👩‍👧‍👦 Семейный режим» → «🔗 Ввести код другой группы» и отправляет этот код!"
    )

    buttons = [
        [
            {
                "type": "clipboard",
                "text": f"📋 Скопировать код ({family_code})",
                "payload": family_code,
            }
        ],
        [
            btn("🔗 Ввести код другой группы", "wizard:join_family", "positive"),
            btn("✏️ Изменить моё имя", "wizard:rename_me", "default"),
        ],
        [btn("🔓 Разъединить (вернуться в личный)", "family:leave", "negative")],
        [btn("🏠 На главную", "menu:home", "default")],
    ]
    await send_max_message(user_id, msg, buttons)


def extract_photo_url(attachments: list) -> str:
    if not attachments:
        return ""
    for att in attachments:
        att_type = att.get("type")
        payload = att.get("payload", {})
        if att_type in ("image", "file", "share"):
            url = payload.get("url")
            if url:
                return url
            photos = payload.get("photos", {})
            for p_data in photos.values():
                if isinstance(p_data, dict) and p_data.get("url"):
                    return p_data["url"]
    return ""


def parse_store_and_amount(text: str):
    clean = text.strip()

    if "t=" in clean and "s=" in clean:
        qs = parse_qs(clean.lstrip("?"))
        s_val = qs.get("s", ["0"])[0]
        t_val = qs.get("t", [""])[0]
        try:
            amount = float(s_val)
        except ValueError:
            amount = 0.0
        r_date = None
        if len(t_val) >= 8 and t_val[:8].isdigit():
            dt = datetime.strptime(t_val[:8], "%Y%m%d")
            r_date = dt.strftime("%d.%m.%Y")
        return "Чек по QR-коду", amount, r_date

    matches = list(re.finditer(r"(\d+(?:[.,]\d{1,2})?)", clean))
    if not matches:
        return (clean if clean else "Покупка"), 0.0, None

    last_match = matches[-1]
    amount_str = last_match.group(1).replace(",", ".")
    try:
        amount = float(amount_str)
    except ValueError:
        amount = 0.0

    store = (clean[:last_match.start()] + clean[last_match.end():]).strip(" -—:,.₽рРруб")
    if not store:
        store = "Магазин"
    return store, amount, None


def save_receipt_to_db(
    family_code: str,
    author_name: str,
    store_name: str,
    amount: float,
    receipt_date: str,
    photo_url: str,
) -> int:
    try:
        dt = datetime.strptime(receipt_date, "%d.%m.%Y")
        iso_date = dt.strftime("%Y-%m-%d")
    except ValueError:
        now = get_irkutsk_now()
        receipt_date = now.strftime("%d.%m.%Y")
        iso_date = now.strftime("%Y-%m-%d")

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO receipts (family_code, author_name, store_name, amount, receipt_date, iso_date, photo_url, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                family_code,
                author_name,
                store_name,
                amount,
                receipt_date,
                iso_date,
                photo_url or "",
                get_irkutsk_now().strftime("%Y-%m-%d %H:%M"),
            ),
        )
        rcpt_id = cursor.lastrowid
        conn.commit()
    return rcpt_id


async def show_receipts_menu(user_id: int, family_code: str):
    now = get_irkutsk_now()
    month_prefix = now.strftime("%Y-%m")

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COUNT(*), COALESCE(SUM(amount), 0)
            FROM receipts
            WHERE family_code = ? AND iso_date LIKE ?
            """,
            (family_code, f"{month_prefix}%"),
        )
        month_count, month_total = cursor.fetchone()

        cursor.execute(
            """
            SELECT receipt_date, iso_date, COUNT(*), COALESCE(SUM(amount), 0)
            FROM receipts
            WHERE family_code = ?
            GROUP BY iso_date
            ORDER BY iso_date DESC
            LIMIT 12
            """,
            (family_code,),
        )
        date_groups = cursor.fetchall()

    buttons = [
        [btn("➕ Загрузить новый чек", "wizard:new_receipt", "positive")],
    ]

    if not date_groups:
        buttons.extend(shopping_back_keyboard())
        await send_max_message(
            user_id,
            "🧾 АРХИВ ЧЕКОВ ПО ДАТАМ\n━━━━━━━━━━━━━━━━━━\nУ вас пока нет сохраненных чеков.\n\n💡 Нажмите «➕ Загрузить новый чек» или просто отправить фото чека прямо в этот чат!",
            buttons,
        )
        return

    lines = [
        "🧾 АРХИВ ЧЕКОВ И РАСХОДОВ\n━━━━━━━━━━━━━━━━━━",
        f"📊 За текущий месяц ({now.strftime('%m.%Y')}): {month_count} шт. на сумму {month_total:.2f} ₽\n",
        "📅 Выберите дату ниже, чтобы открыть чеки и фото за нужный день:",
    ]

    for r_date, _, cnt, day_sum in date_groups:
        buttons.append(
            [btn(f"📅 {r_date} — {cnt} шт. ({day_sum:.0f} ₽)", f"rcpt_day:{r_date}", "default")]
        )

    buttons.extend(shopping_back_keyboard())
    await send_max_message(user_id, "\n".join(lines), buttons)


async def show_receipts_for_date(user_id: int, family_code: str, receipt_date: str):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT id, store_name, amount, author_name, photo_url, created_at
            FROM receipts
            WHERE family_code = ? AND receipt_date = ?
            ORDER BY id DESC
            """,
            (family_code, receipt_date),
        )
        rows = cursor.fetchall()

    if not rows:
        await show_receipts_menu(user_id, family_code)
        return

    day_total = sum(r[2] for r in rows)
    lines = [
        f"📅 ЧЕКИ ЗА {receipt_date}\n━━━━━━━━━━━━━━━━━━",
        f"💰 Итого за день: {day_total:.2f} ₽ ({len(rows)} шт.)\n",
    ]
    buttons = []

    for idx, (r_id, store, amount, author, photo_url, _) in enumerate(rows, 1):
        photo_status = "📸 Фото прикреплено" if photo_url else "📄 Без фото"
        lines.append(f"{idx}. 🏪 {store} — {amount:.2f} ₽\n   └ 👤 {author} | {photo_status}")
        row_btns = []
        if photo_url:
            row_btns.append({"type": "link", "text": f"🖼 Фото чека #{idx} ({store[:12]})", "url": photo_url})
        row_btns.append(btn(f"🗑 Удалить #{idx}", f"del_rcpt:{r_id}", "negative"))
        buttons.append(row_btns)

    buttons.append([btn("🔙 Ко всем датам чеков", "menu:receipts", "default")])
    buttons.extend(shopping_back_keyboard())
    await send_max_message(user_id, "\n".join(lines), buttons)


async def ask_receipt_date(user_id: int, store: str, amount: float):
    now = get_irkutsk_now()
    d_today = now.strftime("%d.%m.%Y")
    d_yest = (now - timedelta(days=1)).strftime("%d.%m.%Y")
    d_before = (now - timedelta(days=2)).strftime("%d.%m.%Y")

    buttons = [
        [
            btn(f"📅 Сегодня ({now.strftime('%d.%m')})", f"rcpt_date:{d_today}", "positive"),
            btn(f"📅 Вчера ({(now - timedelta(days=1)).strftime('%d.%m')})", f"rcpt_date:{d_yest}", "positive"),
        ],
        [
            btn(f"📅 Позавчера ({(now - timedelta(days=2)).strftime('%d.%m')})", f"rcpt_date:{d_before}"),
        ],
        [btn("❌ Отмена", "wizard:cancel_shop", "negative")],
    ]
    await send_max_message(
        user_id,
        f"🏪 Магазин: {store}\n💰 Сумма: {amount:.2f} ₽\n\n📅 Выберите дату покупки на кнопках ниже или напишите дату в формате ДД.ММ.ГГГГ (например, 15.10.2026):",
        buttons,
    )


async def finalize_receipt(user_id: int, name: str, family_code: str, receipt_date: str):
    state = user_states.pop(user_id, {})
    store = state.get("store", "Магазин")
    amount = state.get("amount", 0.0)
    photo_url = state.get("photo_url", "")

    save_receipt_to_db(family_code, name, store, amount, receipt_date, photo_url)

    photo_note = "📸 Фото чека сохранено в архиве" if photo_url else "📄 Чек сохранен (без фото)"
    msg = (
        f"🧾 ЧЕК СОХРАНЕН!\n━━━━━━━━━━━━━━━━━━\n"
        f"📅 Дата: {receipt_date}\n"
        f"🏪 Магазин: {store}\n"
        f"💰 Сумма: {amount:.2f} ₽\n"
        f"👤 Добавил(а): {name}\n"
        f"{photo_note}"
    )

    buttons = []
    if photo_url:
        buttons.append([{"type": "link", "text": "🖼 Открыть фото чека", "url": photo_url}])
    buttons.append([btn(f"📅 Все чеки за {receipt_date}", f"rcpt_day:{receipt_date}", "positive")])
    buttons.extend(shopping_back_keyboard())

    for m_id, _ in get_family_members(family_code):
        await send_max_message(m_id, msg, buttons)


async def ring_alarm_loop(task_id: int, alarm_type: str):
    for attempt in range(1, 16):
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT family_code, author_name, title, task_time,
                       target_ids, target_names, is_done,
                       alarm_24h_stopped, alarm_main_stopped
                FROM tasks
                WHERE id = ?
                """,
                (task_id,),
            )
            row = cursor.fetchone()

        if not row:
            return

        (
            family_code,
            author,
            title,
            task_time,
            target_ids,
            target_names,
            is_done,
            stop_24h,
            stop_main,
        ) = row

        if is_done:
            return
        if alarm_type == "24h" and stop_24h:
            return
        if alarm_type == "main" and stop_main:
            return

        if alarm_type == "24h":
            header = f"⏰🔔 БУДИЛЬНИК ЗА 24 ЧАСА (вызов #{attempt}/15)"
            subtext = "Ровно через сутки запланирована важная задача!"
            callback_stop = f"stop_24h:{task_id}"
            snooze_payload = f"snooze_24h:{task_id}"
        else:
            header = f"🚨🔔 СРОЧНЫЙ БУДИЛЬНИК! ВРЕМЯ ПРИШЛО! (вызов #{attempt}/15)"
            subtext = "Пора выполнять запланированную задачу!"
            callback_stop = f"stop_main:{task_id}"
            snooze_payload = f"snooze_main:{task_id}"

        alarm_buttons = [
            [btn("🔕 ВЫКЛЮЧИТЬ БУДИЛЬНИК", callback_stop, "negative")],
            [btn("💤 Отложить на 10 минут", snooze_payload, "default")],
            [btn("✅ Сразу отметить выполненной", f"done_task:{task_id}", "positive")],
        ]

        members = get_family_members(family_code)
        if not target_ids or target_ids == "ALL":
            recipients = members
        else:
            allowed_ids = {int(x) for x in target_ids.split(",") if x.strip().isdigit()}
            recipients = [(m_id, m_name) for m_id, m_name in members if m_id in allowed_ids]

        safe_title = html.escape(title)
        safe_targets = html.escape(target_names or "Все")
        safe_author = html.escape(author or "")

        for r_id, r_name in recipients:
            safe_r_name = html.escape(r_name)
            ping_text = (
                f"🚨🔔 <a href=\"max://user/{r_id}\">{safe_r_name}, ЗВОНИТ БУДИЛЬНИК!</a>\n"
                f"🔥 <b>{safe_title.upper()}</b> ({task_time})"
            )
            await send_max_message(r_id, ping_text, use_html=True)

        await asyncio.sleep(2)

        for r_id, r_name in recipients:
            safe_r_name = html.escape(r_name)
            msg = (
                f"<b>{header}</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🔔 <a href=\"max://user/{r_id}\">{safe_r_name}</a>, {subtext}\n\n"
                f"📌 Задача: <b>{safe_title}</b>\n"
                f"👥 Кому: {safe_targets}\n"
                f"👤 Создал(а): {safe_author}\n"
                f"🕒 Время: <b>{task_time}</b>\n\n"
                f"👇 Нажмите кнопку ниже, чтобы выключить звук:"
            )
            await send_max_message(r_id, msg, alarm_buttons, use_html=True)

        await asyncio.sleep(25)


async def run_test_alarm(user_id: int, name: str):
    safe_name = html.escape(name)
    ping_text = (
        f"🚨🔔 <a href=\"max://user/{user_id}\">{safe_name}, ТЕСТОВЫЙ БУДИЛЬНИК СРАБОТАЛ!</a>\n"
        f"🔥 <b>ПРОВЕРКА ЗВУКА И ВСПЛЫВАЮЩЕГО ОКНА</b>"
    )
    await send_max_message(user_id, ping_text, use_html=True)
    await asyncio.sleep(2)
    await send_max_message(
        user_id,
        "✅ <b>Тестовый сигнал доставлен!</b>\n\nЕсли телефон был заблокирован (или приложение MAX свернуто), у вас должно было появиться всплывающее окно со звуком.",
        planner_back_keyboard(),
        use_html=True,
    )


def schedule_dual_alarms(task_id: int, task_dt: datetime):
    if task_dt.tzinfo is None:
        task_dt = task_dt.replace(tzinfo=IRKUTSK_TZ)

    now = get_irkutsk_now()
    alarm_24h_dt = task_dt - timedelta(days=1)
    if alarm_24h_dt > now:
        scheduler.add_job(
            ring_alarm_loop,
            trigger="date",
            run_date=alarm_24h_dt,
            args=[task_id, "24h"],
            id=f"alarm_24h_{task_id}",
            replace_existing=True,
        )

    if task_dt > now:
        scheduler.add_job(
            ring_alarm_loop,
            trigger="date",
            run_date=task_dt,
            args=[task_id, "main"],
            id=f"alarm_main_{task_id}",
            replace_existing=True,
        )


def restore_alarms_on_startup():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, task_time FROM tasks WHERE is_done = 0")
        for task_id, task_time_str in cursor.fetchall():
            try:
                task_dt = datetime.strptime(task_time_str, "%d.%m.%Y %H:%M")
                schedule_dual_alarms(task_id, task_dt)
            except ValueError:
                pass


async def ask_task_date(user_id: int, title: str):
    now = get_irkutsk_now()
    d_today = now.strftime("%d.%m.%Y")
    d_tom = (now + timedelta(days=1)).strftime("%d.%m.%Y")
    d_after = (now + timedelta(days=2)).strftime("%d.%m.%Y")
    d_week = (now + timedelta(days=7)).strftime("%d.%m.%Y")

    buttons = [
        [
            btn(f"📅 Сегодня ({now.strftime('%d.%m')})", f"pick_date:{d_today}", "positive"),
            btn(f"📅 Завтра ({(now + timedelta(days=1)).strftime('%d.%m')})", f"pick_date:{d_tom}", "positive"),
        ],
        [
            btn(f"📅 Послезавтра ({(now + timedelta(days=2)).strftime('%d.%m')})", f"pick_date:{d_after}"),
            btn(f"📅 +7 дней ({(now + timedelta(days=7)).strftime('%d.%m')})", f"pick_date:{d_week}"),
        ],
        [btn("❌ Отмена", "wizard:cancel_planner", "negative")],
    ]
    await send_max_message(
        user_id,
        f"📌 Задача: «{title}»\n\n📅 Шаг 2 из 4: Выберите дату на кнопках ниже или напишите дату сообщением в формате ДД.ММ.ГГГГ (например, 25.10.2026):",
        buttons,
    )


async def ask_task_time(user_id: int, date_str: str):
    buttons = [
        [
            btn("🌅 09:00", "pick_time:09:00"),
            btn("☀️ 12:00", "pick_time:12:00"),
            btn("🌤 15:00", "pick_time:15:00"),
        ],
        [
            btn("🌆 18:00", "pick_time:18:00"),
            btn("🌙 20:00", "pick_time:20:00"),
            btn("🕙 21:30", "pick_time:21:30"),
        ],
        [btn("❌ Отмена", "wizard:cancel_planner", "negative")],
    ]
    await send_max_message(
        user_id,
        f"📅 Выбрана дата: {date_str}\n\n🕒 Шаг 3 из 4: Выберите время будильника на кнопках или напишите своё время в чат (например, 14:30):",
        buttons,
    )


async def ask_task_targets(user_id: int, family_code: str):
    members = get_family_members(family_code)
    state = user_states.get(user_id, {})
    selected_ids = state.get("selected_ids", set())

    buttons = [
        [btn("👨‍👩‍👧‍👦 У всех в группе (обоим / всей семье)", "target_all:0", "positive")],
        [btn("👤 Только у меня", f"target_one:{user_id}", "default")],
    ]

    for m_id, m_name in members:
        if m_id != user_id:
            buttons.append([btn(f"🎯 Только: {m_name}", f"target_one:{m_id}", "default")])

    if len(members) > 1:
        row_checks = []
        for m_id, m_name in members:
            mark = "✅" if m_id in selected_ids else "⬜"
            row_checks.append(btn(f"{mark} {m_name}", f"toggle_member:{m_id}"))
            if len(row_checks) == 2:
                buttons.append(row_checks)
                row_checks = []
        if row_checks:
            buttons.append(row_checks)

        if selected_ids:
            buttons.append([btn("🚀 Подтвердить выбранных галочками", "target_confirm_multi:0", "positive")])

    buttons.append([btn("✍️ + Вписать имя другого человека", "target_custom_person:0", "default")])
    buttons.append([btn("❌ Отмена", "wizard:cancel_planner", "negative")])

    await send_max_message(
        user_id,
        "🔔 Шаг 4 из 4: У кого должен сработать будильник (за 24ч и в назначенное время)?\n\n• Нажмите «👨‍👩‍👧‍👦 У всех в группе», чтобы будильник прозвенел у обоих.\n• Нажмите на конкретного человека, если задача только для него.\n• Или отметьте нужных людей галочками (⬜/✅):",
        buttons,
    )


async def finalize_task_creation(
    creator_id: int,
    creator_name: str,
    family_code: str,
    title: str,
    dt_str: str,
    target_ids_str: str,
    target_names_str: str,
):
    task_dt = datetime.strptime(dt_str, "%d.%m.%Y %H:%M").replace(tzinfo=IRKUTSK_TZ)

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO tasks (family_code, author_name, title, task_time, target_ids, target_names, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                family_code,
                creator_name,
                title,
                dt_str,
                target_ids_str,
                target_names_str,
                get_irkutsk_now().strftime("%Y-%m-%d %H:%M"),
            ),
        )
        task_id = cursor.lastrowid
        conn.commit()

    schedule_dual_alarms(task_id, task_dt)
    user_states.pop(creator_id, None)

    alarm_24h_dt = task_dt - timedelta(days=1)
    alarm_24h_str = alarm_24h_dt.strftime("%d.%m.%Y %H:%M")
    first_alarm_info = (
        f"🔔 1-й будильник (за 24ч): {alarm_24h_str}"
        if alarm_24h_dt > get_irkutsk_now()
        else "⚠️ До задачи меньше 24ч — сработает только основной будильник"
    )

    summary = (
        f"✅ ЗАДАЧА СОЗДАНА! (По Иркутску)\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📌 Задача: {title}\n"
        f"👤 Создал(а): {creator_name}\n"
        f"🔔 Будильник сработает у: {target_names_str}\n"
        f"{first_alarm_info}\n"
        f"🚨 2-й будильник (вовремя): {dt_str}"
    )

    members = get_family_members(family_code)
    if target_ids_str == "ALL":
        notify_ids = {m_id for m_id, _ in members}
    else:
        notify_ids = {int(x) for x in target_ids_str.split(",") if x.strip().isdigit()}
        notify_ids.add(creator_id)

    for m_id in notify_ids:
        await send_max_message(m_id, summary, planner_back_keyboard())


async def handle_message(user_id: int, raw_name: str, text: str, attachments: list):
    name, family_code = get_user_info(user_id, raw_name)
    clean_text = (text or "").strip()
    lower_text = clean_text.lower()
    photo_url = extract_photo_url(attachments)

    if lower_text in ("/start", "старт", "начать", "запустить", "меню", "/menu"):
        await send_start_screen(user_id, name)
        return

    state = user_states.get(user_id)

    if photo_url and (not state or state.get("step") != "wait_receipt_photo"):
        if clean_text:
            store, amount, qr_date = parse_store_and_amount(clean_text)
            user_states[user_id] = {
                "step": "wait_receipt_date",
                "photo_url": photo_url,
                "store": store,
                "amount": amount,
            }
            if qr_date:
                await finalize_receipt(user_id, name, family_code, qr_date)
            else:
                await ask_receipt_date(user_id, store, amount)
        else:
            user_states[user_id] = {
                "step": "wait_receipt_info",
                "photo_url": photo_url,
            }
            await send_max_message(
                user_id,
                "📸 Фото чека получено!\n\n✍️ Теперь напишите название магазина и сумму чека (например: Пятерочка 1450 или Лента 2300.50):",
                [[btn("❌ Отмена", "wizard:cancel_shop", "negative")]],
            )
        return

    if state:
        step = state.get("step")

        if step == "wait_receipt_photo":
            if photo_url:
                state["photo_url"] = photo_url
                if clean_text:
                    store, amount, qr_date = parse_store_and_amount(clean_text)
                    state["store"] = store
                    state["amount"] = amount
                    state["step"] = "wait_receipt_date"
                    if qr_date:
                        await finalize_receipt(user_id, name, family_code, qr_date)
                    else:
                        await ask_receipt_date(user_id, store, amount)
                else:
                    state["step"] = "wait_receipt_info"
                    await send_max_message(
                        user_id,
                        "📸 Фото чека прикреплено!\n\n✍️ Шаг 2 из 3: Напишите название магазина и сумму чека (например: Слата 890 или Пятерочка 1450.50):",
                        [[btn("❌ Отмена", "wizard:cancel_shop", "negative")]],
                    )
                return
            elif clean_text:
                store, amount, qr_date = parse_store_and_amount(clean_text)
                state["photo_url"] = ""
                state["store"] = store
                state["amount"] = amount
                state["step"] = "wait_receipt_date"
                if qr_date:
                    await finalize_receipt(user_id, name, family_code, qr_date)
                else:
                    await ask_receipt_date(user_id, store, amount)
                return

        if step == "wait_receipt_info":
            store, amount, qr_date = parse_store_and_amount(clean_text)
            state["store"] = store
            state["amount"] = amount
            state["step"] = "wait_receipt_date"
            if qr_date:
                await finalize_receipt(user_id, name, family_code, qr_date)
            else:
                await ask_receipt_date(user_id, store, amount)
            return

        if step == "wait_receipt_date":
            try:
                datetime.strptime(clean_text, "%d.%m.%Y")
                await finalize_receipt(user_id, name, family_code, clean_text)
            except ValueError:
                await send_max_message(
                    user_id,
                    "⚠️ Неверный формат даты! Введите дату как ДД.ММ.ГГГГ (например: 18.10.2026) или выберите кнопку выше.",
                )
            return

        if step == "wait_shop_items":
            items = [i.strip() for i in clean_text.replace("\n", ",").split(",") if i.strip()]
            user_states.pop(user_id, None)
            if not items:
                await show_shopping_section(user_id, family_code)
                return

            now_str = get_irkutsk_now().strftime("%Y-%m-%d %H:%M")
            with sqlite3.connect(DB_NAME) as conn:
                for item in items:
                    conn.execute(
                        "INSERT INTO shopping (family_code, author_name, item, created_at) VALUES (?, ?, ?, ?)",
                        (family_code, name, item, now_str),
                    )
                conn.commit()

            added_str = ", ".join(items)
            for m_id, _ in get_family_members(family_code):
                if m_id == user_id:
                    await show_shopping_section(user_id, family_code)
                else:
                    await send_max_message(
                        m_id,
                        f"🛒 {name} добавил(а) в список покупок:\n• {added_str}",
                        shopping_back_keyboard(),
                    )
            return

        if step == "wait_task_title":
            state["title"] = clean_text
            state["step"] = "wait_task_date"
            await ask_task_date(user_id, clean_text)
            return

        if step == "wait_task_date":
            try:
                datetime.strptime(clean_text, "%d.%m.%Y")
                state["date"] = clean_text
                state["step"] = "wait_task_time"
                await ask_task_time(user_id, clean_text)
            except ValueError:
                await send_max_message(
                    user_id,
                    "⚠️ Неверный формат даты! Введите дату как ДД.ММ.ГГГГ (например: 20.10.2026) или нажмите кнопку выше.",
                )
            return

        if step == "wait_task_time":
            try:
                datetime.strptime(clean_text, "%H:%M")
                state["time"] = clean_text
                state["step"] = "wait_task_targets"
                state["selected_ids"] = set()
                await ask_task_targets(user_id, family_code)
            except ValueError:
                await send_max_message(
                    user_id,
                    "⚠️ Неверный формат времени! Введите время как ЧЧ:ММ (например: 14:30) или нажмите кнопку выше.",
                )
            return

        if step == "wait_custom_person":
            custom_person = clean_text
            dt_str = f"{state['date']} {state['time']}"
            await finalize_task_creation(
                creator_id=user_id,
                creator_name=name,
                family_code=family_code,
                title=state["title"],
                dt_str=dt_str,
                target_ids_str="ALL",
                target_names_str=f"{custom_person} (и группа)",
            )
            return

        if step == "wait_join_code":
            user_states.pop(user_id, None)
            target_code = clean_text.replace("войти", "").strip().upper()
            await join_family_by_code(user_id, name, target_code)
            return

        if step == "wait_rename":
            user_states.pop(user_id, None)
            with sqlite3.connect(DB_NAME) as conn:
                conn.execute("UPDATE users SET name = ? WHERE user_id = ?", (clean_text, user_id))
                conn.commit()
            await show_family_section(user_id, family_code)
            return

    if lower_text.startswith("войти "):
        target_code = clean_text.split(maxsplit=1)[1].strip().upper()
        await join_family_by_code(user_id, name, target_code)
        return

    if lower_text.startswith("купить "):
        items = [i.strip() for i in clean_text[7:].split(",") if i.strip()]
        now_str = get_irkutsk_now().strftime("%Y-%m-%d %H:%M")
        with sqlite3.connect(DB_NAME) as conn:
            for item in items:
                conn.execute(
                    "INSERT INTO shopping (family_code, author_name, item, created_at) VALUES (?, ?, ?, ?)",
                    (family_code, name, item, now_str),
                )
            conn.commit()
        await show_shopping_section(user_id, family_code)
        return

    if lower_text.startswith("чек "):
        store, amount, qr_date = parse_store_and_amount(clean_text[4:])
        r_date = qr_date or get_irkutsk_now().strftime("%d.%m.%Y")
        user_states[user_id] = {"store": store, "amount": amount, "photo_url": ""}
        await finalize_receipt(user_id, name, family_code, r_date)
        return

    await send_start_screen(user_id, name)


async def join_family_by_code(user_id: int, name: str, target_code: str):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users WHERE family_code = ?", (target_code,))
        exists = cursor.fetchone()[0]

        if exists == 0:
            await send_max_message(
                user_id,
                f"⚠️ Группа с кодом «{target_code}» не найдена. Проверьте код у супруга(и) или друга.",
                [[btn("👨‍👩‍👧‍👦 Назад в Семейный режим", "section:family", "default")]],
            )
            return

        cursor.execute("UPDATE users SET family_code = ? WHERE user_id = ?", (target_code, user_id))
        conn.commit()

    members = get_family_members(target_code)
    member_names = ", ".join([m[1] for m in members])

    for m_id, _ in members:
        await send_max_message(
            m_id,
            f"🎉 {name} присоединился(-ась) к вашей группе!\n👥 Участники ({len(members)}): {member_names}\nТеперь у вас общие списки продуктов, задач и чеков.",
            main_menu_keyboard(),
        )


async def handle_callback(user_id: int, raw_name: str, payload: str):
    name, family_code = get_user_info(user_id, raw_name)
    action, value = payload.split(":", maxsplit=1)

    if action == "section":
        user_states.pop(user_id, None)
        if value == "planner":
            await show_planner_section(user_id, family_code)
        elif value == "shopping":
            await show_shopping_section(user_id, family_code)
        elif value == "family":
            await show_family_section(user_id, family_code)
        return

    if action == "test_alarm":
        run_at = get_irkutsk_now() + timedelta(seconds=15)
        scheduler.add_job(
            run_test_alarm,
            trigger="date",
            run_date=run_at,
            args=[user_id, name],
            id=f"test_alarm_{user_id}",
            replace_existing=True,
        )
        await send_max_message(
            user_id,
            "⏳ Тестовый будильник запущен!\n\n📲 ПРЯМО СЕЙЧАС сверните приложение MAX или заблокируйте экран телефона!\nРовно через 15 секунд придет двойной сигнал с персональным вызовом.",
            planner_back_keyboard(),
        )
        return

    if action == "menu":
        user_states.pop(user_id, None)
        if value == "receipts":
            await show_receipts_menu(user_id, family_code)
        elif value in ("report_planner", "report_shop"):
            now = get_irkutsk_now()
            txt_file, csv_file = export_monthly_report(family_code, name, now.year, now.month)
            back_kb = planner_back_keyboard() if value == "report_planner" else shopping_back_keyboard()
            await send_max_message(
                user_id,
                f"📊 Отчеты группы (дела + покупки + чеки) за {now.month:02d}.{now.year} сформированы!\n\n📄 Файл TXT: {txt_file}\n📊 Таблица CSV: {csv_file}",
                back_kb,
            )
        elif value == "home":
            await send_start_screen(user_id, name)
        return

    if action == "wizard":
        if value == "cancel_planner":
            user_states.pop(user_id, None)
            await show_planner_section(user_id, family_code)
        elif value == "cancel_shop":
            user_states.pop(user_id, None)
            await show_shopping_section(user_id, family_code)
        elif value == "cancel_family":
            user_states.pop(user_id, None)
            await show_family_section(user_id, family_code)
        elif value == "new_shop":
            user_states[user_id] = {"step": "wait_shop_items"}
            await send_max_message(
                user_id,
                "🛒 Напишите продукты, которые нужно купить (через запятую или каждый с новой строки):\n\nНапример: Молоко, Хлеб, Сыр, Яблоки",
                [[btn("❌ Отмена", "wizard:cancel_shop", "negative")]],
            )
        elif value == "new_task":
            user_states[user_id] = {"step": "wait_task_title"}
            await send_max_message(
                user_id,
                "📝 Шаг 1 из 4: Напишите название задачи:\n\nНапример: Забрать заказ из пункта выдачи",
                [[btn("❌ Отмена", "wizard:cancel_planner", "negative")]],
            )
        elif value == "new_receipt":
            user_states[user_id] = {"step": "wait_receipt_photo", "photo_url": ""}
            await send_max_message(
                user_id,
                "🧾 ЗАГРУЗКА ЧЕКА (Шаг 1 из 3)\n━━━━━━━━━━━━━━━━━━\n📸 Пришлите фотографию чека в этот чат (можно сразу с подписью, например: «Пятерочка 1450»).\n\nИли нажмите «⏩ Продолжить без фото», чтобы просто записать сумму и магазин:",
                [
                    [btn("⏩ Продолжить без фото", "rcpt_skip_photo:0", "positive")],
                    [btn("❌ Отмена", "wizard:cancel_shop", "negative")],
                ],
            )
        elif value == "join_family":
            user_states[user_id] = {"step": "wait_join_code"}
            await send_max_message(
                user_id,
                "🔑 Отправьте в чат код группы (например: FAM-123456), который вам прислал супруг(а) или друг:",
                [[btn("❌ Отмена", "wizard:cancel_family", "negative")]],
            )
        elif value == "rename_me":
            user_states[user_id] = {"step": "wait_rename"}
            await send_max_message(
                user_id,
                "✏️ Напишите, как отображать ваше имя на кнопках у семьи (например: «Муж Алексей» или «Жена Маша»):",
                [[btn("❌ Отмена", "wizard:cancel_family", "negative")]],
            )
        return

    if action == "rcpt_skip_photo":
        user_states[user_id] = {"step": "wait_receipt_info", "photo_url": ""}
        await send_max_message(
            user_id,
            "✍️ Шаг 2 из 3: Напишите название магазина и сумму чека:\n\nНапример: Пятерочка 1450.50 или Слата 890",
            [[btn("❌ Отмена", "wizard:cancel_shop", "negative")]],
        )
        return

    if action == "rcpt_date":
        if user_id not in user_states:
            await show_receipts_menu(user_id, family_code)
            return
        await finalize_receipt(user_id, name, family_code, value)
        return

    if action == "rcpt_day":
        await show_receipts_for_date(user_id, family_code, value)
        return

    if action == "del_rcpt":
        rcpt_id = int(value)
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT receipt_date FROM receipts WHERE id = ? AND family_code = ?", (rcpt_id, family_code))
            row = cursor.fetchone()
            r_date = row[0] if row else None
            cursor.execute("DELETE FROM receipts WHERE id = ? AND family_code = ?", (rcpt_id, family_code))
            conn.commit()
        if r_date:
            await show_receipts_for_date(user_id, family_code, r_date)
        else:
            await show_receipts_menu(user_id, family_code)
        return

    if action == "pick_date":
        state = user_states.get(user_id)
        if not state or "title" not in state:
            await show_planner_section(user_id, family_code)
            return
        state["date"] = value
        state["step"] = "wait_task_time"
        await ask_task_time(user_id, value)
        return

    if action == "pick_time":
        state = user_states.get(user_id)
        if not state or "date" not in state:
            await show_planner_section(user_id, family_code)
            return
        state["time"] = value
        state["step"] = "wait_task_targets"
        state["selected_ids"] = set()
        await ask_task_targets(user_id, family_code)
        return

    if action in ("target_all", "target_one", "toggle_member", "target_confirm_multi", "target_custom_person"):
        state = user_states.get(user_id)
        if not state or "date" not in state or "time" not in state:
            await show_planner_section(user_id, family_code)
            return

        dt_str = f"{state['date']} {state['time']}"
        members_dict = dict(get_family_members(family_code))

        if action == "target_all":
            all_names = ", ".join(members_dict.values())
            await finalize_task_creation(
                user_id, name, family_code, state["title"], dt_str, "ALL", f"Все ({all_names})"
            )
        elif action == "target_one":
            target_uid = int(value)
            target_uname = members_dict.get(target_uid, name)
            await finalize_task_creation(
                user_id, name, family_code, state["title"], dt_str, str(target_uid), target_uname
            )
        elif action == "toggle_member":
            target_uid = int(value)
            selected = state.setdefault("selected_ids", set())
            if target_uid in selected:
                selected.remove(target_uid)
            else:
                selected.add(target_uid)
            await ask_task_targets(user_id, family_code)
        elif action == "target_confirm_multi":
            selected = state.get("selected_ids", set())
            if not selected:
                await ask_task_targets(user_id, family_code)
                return
            ids_str = ",".join(str(uid) for uid in selected)
            names_str = ", ".join(members_dict.get(uid, str(uid)) for uid in selected)
            await finalize_task_creation(
                user_id, name, family_code, state["title"], dt_str, ids_str, names_str
            )
        elif action == "target_custom_person":
            state["step"] = "wait_custom_person"
            await send_max_message(
                user_id,
                "✍️ Напишите имя человека (или нескольких людей), для кого эта задача:",
                [[btn("❌ Отмена", "wizard:cancel_planner", "negative")]],
            )
        return

    if action == "family" and value == "leave":
        personal_code = f"FAM-{user_id}"
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("UPDATE users SET family_code = ? WHERE user_id = ?", (personal_code, user_id))
            conn.commit()
        await show_family_section(user_id, personal_code)
        return

    try:
        item_id = int(value)
    except ValueError:
        return

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()

        if action == "stop_24h":
            cursor.execute(
                "UPDATE tasks SET alarm_24h_stopped = 1 WHERE id = ? AND family_code = ?",
                (item_id, family_code),
            )
            conn.commit()
            await send_max_message(
                user_id,
                "🔕 Суточный будильник выключен! Основной будильник прозвенит ровно в назначенное время задачи.",
                planner_back_keyboard(),
            )

        elif action == "stop_main":
            cursor.execute(
                "UPDATE tasks SET alarm_main_stopped = 1 WHERE id = ? AND family_code = ?",
                (item_id, family_code),
            )
            conn.commit()
            await send_max_message(user_id, "🔕 Основной будильник выключен!", planner_back_keyboard())

        elif action in ("snooze_24h", "snooze_main"):
            alarm_kind = "24h" if action == "snooze_24h" else "main"
            col = "alarm_24h_stopped" if alarm_kind == "24h" else "alarm_main_stopped"
            cursor.execute(f"UPDATE tasks SET {col} = 1 WHERE id = ? AND family_code = ?", (item_id, family_code))
            conn.commit()

            snooze_dt = get_irkutsk_now() + timedelta(minutes=10)

            async def restart_snoozed_alarm(t_id: int, a_kind: str, col_name: str):
                with sqlite3.connect(DB_NAME) as c2:
                    c2.execute(f"UPDATE tasks SET {col_name} = 0 WHERE id = ?", (t_id,))
                    c2.commit()
                await ring_alarm_loop(t_id, a_kind)

            scheduler.add_job(
                restart_snoozed_alarm,
                trigger="date",
                run_date=snooze_dt,
                args=[item_id, alarm_kind, col],
                id=f"snooze_{alarm_kind}_{item_id}",
                replace_existing=True,
            )
            await send_max_message(
                user_id,
                f"💤 Будильник отложен на 10 минут (прозвенит снова в {snooze_dt.strftime('%H:%M')} по Иркутску).",
                planner_back_keyboard(),
            )

        elif action == "done_task":
            cursor.execute("SELECT title FROM tasks WHERE id = ? AND family_code = ?", (item_id, family_code))
            row = cursor.fetchone()
            task_title = row[0] if row else "Задача"

            cursor.execute(
                """
                UPDATE tasks
                SET is_done = 1, alarm_24h_stopped = 1, alarm_main_stopped = 1
                WHERE id = ? AND family_code = ?
                """,
                (item_id, family_code),
            )
            conn.commit()

            await show_planner_section(user_id, family_code)
            for m_id, _ in get_family_members(family_code):
                if m_id != user_id:
                    await send_max_message(
                        m_id, f"✅ {name} выполнил(а) задачу: «{task_title}»", planner_back_keyboard()
                    )

        elif action == "buy_item":
            cursor.execute("SELECT item FROM shopping WHERE id = ? AND family_code = ?", (item_id, family_code))
            row = cursor.fetchone()
            item_name = row[0] if row else "Товар"

            cursor.execute(
                "UPDATE shopping SET is_bought = 1 WHERE id = ? AND family_code = ?",
                (item_id, family_code),
            )
            conn.commit()

            await show_shopping_section(user_id, family_code)
            for m_id, _ in get_family_members(family_code):
                if m_id != user_id:
                    await send_max_message(
                        m_id, f"🛒 {name} купил(а): {item_name}", shopping_back_keyboard()
                    )

        elif action == "clear_bought":
            cursor.execute("DELETE FROM shopping WHERE family_code = ? AND is_bought = 1", (family_code,))
            conn.commit()
            await show_shopping_section(user_id, family_code)

        elif action == "clear_tasks":
            cursor.execute("DELETE FROM tasks WHERE family_code = ? AND is_done = 1", (family_code,))
            conn.commit()
            await show_planner_section(user_id, family_code)


async def main():
    init_db()
    scheduler.start()
    restore_alarms_on_startup()
    print("🚀 Семейный планировщик MAX (3 раздела + автостарт) запущен!")

    marker = None
    headers = {"Authorization": MAX_BOT_TOKEN}

    async with aiohttp.ClientSession() as session:
        while True:
            params = {"timeout": 30}
            if marker:
                params["marker"] = marker

            try:
                async with session.get(f"{API_URL}/updates", headers=headers, params=params) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        marker = data.get("marker", marker)

                        for update in data.get("updates", []):
                            u_type = update.get("update_type")

                            if u_type == "bot_started":
                                user_obj = update.get("user") or update.get("sender") or {}
                                u_id = user_obj.get("user_id") or update.get("user_id")
                                u_name = user_obj.get("name", "Участник")
                                if u_id:
                                    await send_start_screen(u_id, u_name)

                            elif u_type == "message_created":
                                msg = update.get("message", {})
                                sender = msg.get("sender", {})
                                u_id = sender.get("user_id")
                                u_name = sender.get("name", "Участник")
                                body = msg.get("body", {})
                                text = body.get("text") or ""
                                attachments = body.get("attachments") or []
                                if u_id and (text or attachments):
                                    await handle_message(u_id, u_name, text, attachments)

                            elif u_type == "message_callback":
                                cb = update.get("callback", {})
                                user_obj = cb.get("user", {})
                                u_id = user_obj.get("user_id")
                                u_name = user_obj.get("name", "Участник")
                                payload = cb.get("payload", "")
                                if u_id and payload:
                                    await handle_callback(u_id, u_name, payload)
                    elif resp.status == 401:
                        print("❌ Ошибка 401: Неверный токен MAX_BOT_TOKEN!")
                        await asyncio.sleep(10)
            except Exception as e:
                print(f"Ошибка соединения: {e}")
                await asyncio.sleep(5)


if __name__ == "__main__":
    asyncio.run(main())
