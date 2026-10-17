import asyncio
import sqlite3
from datetime import datetime, timedelta
import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import MAX_BOT_TOKEN, API_URL, DB_NAME
from exporter import export_monthly_report

scheduler = AsyncIOScheduler()
user_states = {}

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
            btn("➕ Новая задача", "wizard:new_task", "positive"),
            btn("➕ Добавить продукты", "wizard:new_shop", "positive"),
        ],
        [
            btn("📋 Список дел", "menu:tasks"),
            btn("🛒 Список покупок", "menu:shop"),
        ],
        [
            btn("👨‍👩‍👧‍👦 Семья и люди", "menu:family"),
            btn("📊 Скачать отчет", "menu:report"),
        ],
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

async def send_max_message(user_id: int, text: str, buttons: list = None):
    headers = {"Authorization": MAX_BOT_TOKEN, "Content-Type": "application/json"}
    params = {"user_id": user_id}
    payload = {"text": text}

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

async def ring_alarm_loop(task_id: int, alarm_type: str):
    for attempt in range(1, 11):
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
            header = f"⏰🔔 БУДИЛЬНИК ЗА 24 ЧАСА (сигнал #{attempt})"
            subtext = "Ровно через сутки запланирована задача!"
            callback_stop = f"stop_24h:{task_id}"
        else:
            header = f"🚨🔔 ОСНОВНОЙ БУДИЛЬНИК (сигнал #{attempt})"
            subtext = "Настало время выполнять задачу!"
            callback_stop = f"stop_main:{task_id}"

        msg = (
            f"{header}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{subtext}\n\n"
            f"📌 Задача: {title}\n"
            f"👥 Ответственные: {target_names}\n"
            f"👤 Создал(а): {author}\n"
            f"🕒 Время: {task_time}\n\n"
            f"👇 Нажмите кнопку ниже, чтобы остановить сигнал будильника:"
        )

        alarm_buttons = [
            [btn("🔕 Выключить будильник", callback_stop, "negative")],
            [btn("✅ Сразу отметить выполненной", f"done_task:{task_id}", "positive")],
        ]

        members = get_family_members(family_code)
        if not target_ids or target_ids == "ALL":
            recipients = [m_id for m_id, _ in members]
        else:
            allowed_ids = {int(x) for x in target_ids.split(",") if x.strip().isdigit()}
            recipients = [m_id for m_id, _ in members if m_id in allowed_ids]

        for r_id in recipients:
            await send_max_message(r_id, msg, alarm_buttons)

        await asyncio.sleep(45)

def schedule_dual_alarms(task_id: int, task_dt: datetime):
    now = datetime.now()

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

async def show_shopping_list(user_id: int, family_code: str):
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

    buttons = [[btn("➕ Добавить продукты", "wizard:new_shop", "positive")]]

    if not rows:
        buttons.extend(main_menu_keyboard()[1:])
        await send_max_message(
            user_id,
            "🛒 ОБЩИЙ СПИСОК ПОКУПОК\n━━━━━━━━━━━━━━━━━━\nСписок пока пуст! Нажмите «➕ Добавить продукты».",
            buttons,
        )
        return

    lines = ["🛒 ОБЩИЙ СПИСОК ПОКУПОК\n━━━━━━━━━━━━━━━━━━"]
    has_bought = False

    for s_id, item, is_bought, author in rows:
        if is_bought:
            has_bought = True
            lines.append(f"✅ ~{item}~ (добавил: {author})")
        else:
            lines.append(f"⬜ {item} (от: {author})")
            buttons.append([btn(f"🛒 Куплено: {item[:24]}", f"buy_item:{s_id}", "default")])

    if has_bought:
        buttons.append([btn("🧹 Очистить уже купленное", "clear_bought:0", "negative")])

    buttons.extend(main_menu_keyboard()[1:])
    await send_max_message(user_id, "\n".join(lines), buttons)

async def show_tasks_list(user_id: int, family_code: str):
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

    buttons = [[btn("➕ Новая задача с будильником", "wizard:new_task", "positive")]]

    if not rows:
        buttons.extend(main_menu_keyboard()[1:])
        await send_max_message(
            user_id,
            "📋 ОБЩИЙ СПИСОК ДЕЛ\n━━━━━━━━━━━━━━━━━━\nЗадач пока нет! Нажмите «➕ Новая задача с будильником».",
            buttons,
        )
        return

    lines = ["📋 ОБЩИЙ СПИСОК ДЕЛ\n━━━━━━━━━━━━━━━━━━"]
    has_done = False

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

    buttons.extend(main_menu_keyboard()[1:])
    await send_max_message(user_id, "\n".join(lines), buttons)

async def show_family_menu(user_id: int, family_code: str):
    members = get_family_members(family_code)
    member_lines = [f" • 👤 {m_name}" + (" (Вы)" if m_id == user_id else "") for m_id, m_name in members]

    msg = (
        f"👨‍👩‍👧‍👦 СЕМЬЯ И УЧАСТНИКИ ГРУППЫ\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔑 Код вашей группы: {family_code}\n\n"
        f"👥 Подключенные люди ({len(members)}):\n"
        + "\n".join(member_lines)
        + "\n\n💡 Как добавить жену, мужа, детей или друзей:\n"
        f"1. Скопируйте код {family_code} кнопкой ниже и отправьте человеку.\n"
        f"2. Он нажимает у себя в боте «🔗 Ввести код другой группы» и присылает этот код!"
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
        [btn("🏠 В главное меню", "menu:home", "default")],
    ]
    await send_max_message(user_id, msg, buttons)

async def ask_task_date(user_id: int, title: str):
    now = datetime.now()
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
        [btn("❌ Отмена", "wizard:cancel", "negative")],
    ]
    await send_max_message(
        user_id,
        f"📌 Задача: «{title}»\n\n"
        f"📅 Шаг 2 из 4: Выберите дату на кнопках ниже или напишите дату сообщением в формате ДД.ММ.ГГГГ (например, 25.10.2026):",
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
        [btn("❌ Отмена", "wizard:cancel", "negative")],
    ]
    await send_max_message(
        user_id,
        f"📅 Выбрана дата: {date_str}\n\n"
        f"🕒 Шаг 3 из 4: Выберите время будильника на кнопках или напишите своё время в чат (например, 14:30):",
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
    buttons.append([btn("❌ Отмена", "wizard:cancel", "negative")])

    await send_max_message(
        user_id,
        "🔔 Шаг 4 из 4: У кого должен сработать будильник (за 24ч и в назначенное время)?\n\n"
        "• Нажмите «👨‍👩‍👧‍👦 У всех в группе», чтобы будильник прозвенел у обоих.\n"
        "• Нажмите на конкретного человека, если задача только для него.\n"
        "• Или отметьте нужных людей галочками (⬜/✅):",
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
    task_dt = datetime.strptime(dt_str, "%d.%m.%Y %H:%M")

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
                datetime.now().strftime("%Y-%m-%d %H:%M"),
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
        if alarm_24h_dt > datetime.now()
        else "⚠️ До задачи меньше 24ч — сработает только основной будильник"
    )

    summary = (
        f"✅ ЗАДАЧА СОЗДАНА!\n"
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
        await send_max_message(m_id, summary, main_menu_keyboard())

async def handle_text(user_id: int, raw_name: str, text: str):
    name, family_code = get_user_info(user_id, raw_name)
    clean_text = text.strip()
    lower_text = clean_text.lower()

    state = user_states.get(user_id)
    if state:
        step = state.get("step")

        if step == "wait_shop_items":
            items = [i.strip() for i in clean_text.replace("\n", ",").split(",") if i.strip()]
            user_states.pop(user_id, None)
            if not items:
                await send_max_message(user_id, "⚠️ Список пуст.", main_menu_keyboard())
                return

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
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
                    await show_shopping_list(user_id, family_code)
                else:
                    await send_max_message(
                        m_id,
                        f"🛒 {name} добавил(а) в список покупок:\n• {added_str}",
                        main_menu_keyboard(),
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
            await send_max_message(
                user_id,
                f"✅ Ваше имя в группе изменено на: {clean_text}",
                main_menu_keyboard(),
            )
            return

    if lower_text.startswith("войти "):
        target_code = clean_text.split(maxsplit=1)[1].strip().upper()
        await join_family_by_code(user_id, name, target_code)
        return

    if lower_text.startswith("купить "):
        items = [i.strip() for i in clean_text[7:].split(",") if i.strip()]
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        with sqlite3.connect(DB_NAME) as conn:
            for item in items:
                conn.execute(
                    "INSERT INTO shopping (family_code, author_name, item, created_at) VALUES (?, ?, ?, ?)",
                    (family_code, name, item, now_str),
                )
            conn.commit()
        await show_shopping_list(user_id, family_code)
        return

    await send_max_message(
        user_id,
        f"👋 Привет, {name}!\n"
        f"Я ваш семейный планировщик с двойным будильником.\n\n"
        f"• Нажмите «➕ Новая задача», чтобы создать напоминание.\n"
        f"• Нажмите «➕ Добавить продукты», чтобы пополнить общий список покупок.",
        main_menu_keyboard(),
    )

async def join_family_by_code(user_id: int, name: str, target_code: str):
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users WHERE family_code = ?", (target_code,))
        exists = cursor.fetchone()[0]

        if exists == 0:
            await send_max_message(
                user_id,
                f"⚠️ Группа с кодом «{target_code}» не найдена. Проверьте код у супруга(и) или друга.",
                main_menu_keyboard(),
            )
            return

        cursor.execute("UPDATE users SET family_code = ? WHERE user_id = ?", (target_code, user_id))
        conn.commit()

    members = get_family_members(target_code)
    member_names = ", ".join([m[1] for m in members])

    for m_id, _ in members:
        await send_max_message(
            m_id,
            f"🎉 {name} присоединился(-ась) к вашей группе!\n"
            f"👥 Участники ({len(members)}): {member_names}\n"
            f"Теперь у вас общие списки продуктов и задач.",
            main_menu_keyboard(),
        )

async def handle_callback(user_id: int, raw_name: str, payload: str):
    name, family_code = get_user_info(user_id, raw_name)
    action, value = payload.split(":", maxsplit=1)

    if action == "menu":
        user_states.pop(user_id, None)
        if value == "shop":
            await show_shopping_list(user_id, family_code)
        elif value == "tasks":
            await show_tasks_list(user_id, family_code)
        elif value == "family":
            await show_family_menu(user_id, family_code)
        elif value == "report":
            now = datetime.now()
            txt_file, csv_file = export_monthly_report(family_code, name, now.year, now.month)
            await send_max_message(
                user_id,
                f"📊 Отчеты группы за {now.month:02d}.{now.year} сформированы!\n\n"
                f"📄 Файл TXT: {txt_file}\n"
                f"📊 Таблица CSV: {csv_file}",
                main_menu_keyboard(),
            )
        elif value == "home":
            await send_max_message(
                user_id,
                f"🏠 Главное меню ({name}):\nВыберите нужное действие на кнопках ниже:",
                main_menu_keyboard(),
            )
        return

    if action == "wizard":
        if value == "cancel":
            user_states.pop(user_id, None)
            await send_max_message(user_id, "❎ Действие отменено.", main_menu_keyboard())
        elif value == "new_shop":
            user_states[user_id] = {"step": "wait_shop_items"}
            await send_max_message(
                user_id,
                "🛒 Напишите продукты, которые нужно купить (через запятую или каждый с новой строки):\n\n"
                "Например: Молоко, Хлеб, Сыр, Яблоки",
                [[btn("❌ Отмена", "wizard:cancel", "negative")]],
            )
        elif value == "new_task":
            user_states[user_id] = {"step": "wait_task_title"}
            await send_max_message(
                user_id,
                "📝 Шаг 1 из 4: Напишите название задачи:\n\n"
                "Например: Забрать заказ из пункта выдачи",
                [[btn("❌ Отмена", "wizard:cancel", "negative")]],
            )
        elif value == "join_family":
            user_states[user_id] = {"step": "wait_join_code"}
            await send_max_message(
                user_id,
                "🔑 Отправьте в чат код группы (например: FAM-123456), который вам прислал супруг(а) или друг:",
                [[btn("❌ Отмена", "wizard:cancel", "negative")]],
            )
        elif value == "rename_me":
            user_states[user_id] = {"step": "wait_rename"}
            await send_max_message(
                user_id,
                "✏️ Напишите, как отображать ваше имя на кнопках у семьи (например: «Муж Алексей» или «Жена Маша»):",
                [[btn("❌ Отмена", "wizard:cancel", "negative")]],
            )
        return

    if action == "pick_date":
        state = user_states.get(user_id)
        if not state or "title" not in state:
            await send_max_message(user_id, "⚠️ Сессия истекла, нажмите «➕ Новая задача».", main_menu_keyboard())
            return
        state["date"] = value
        state["step"] = "wait_task_time"
        await ask_task_time(user_id, value)
        return

    if action == "pick_time":
        state = user_states.get(user_id)
        if not state or "date" not in state:
            await send_max_message(user_id, "⚠️ Сессия истекла, нажмите «➕ Новая задача».", main_menu_keyboard())
            return
        state["time"] = value
        state["step"] = "wait_task_targets"
        state["selected_ids"] = set()
        await ask_task_targets(user_id, family_code)
        return

    if action in ("target_all", "target_one", "toggle_member", "target_confirm_multi", "target_custom_person"):
        state = user_states.get(user_id)
        if not state or "date" not in state or "time" not in state:
            await send_max_message(user_id, "⚠️ Сессия создания задачи завершена.", main_menu_keyboard())
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
                [[btn("❌ Отмена", "wizard:cancel", "negative")]],
            )
        return

    if action == "family" and value == "leave":
        personal_code = f"FAM-{user_id}"
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute("UPDATE users SET family_code = ? WHERE user_id = ?", (personal_code, user_id))
            conn.commit()
        await send_max_message(
            user_id,
            f"🔓 Вы переключились на свой личный список. Ваш индивидуальный код: {personal_code}",
            main_menu_keyboard(),
        )
        return

    item_id = int(value)
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
                main_menu_keyboard(),
            )

        elif action == "stop_main":
            cursor.execute(
                "UPDATE tasks SET alarm_main_stopped = 1 WHERE id = ? AND family_code = ?",
                (item_id, family_code),
            )
            conn.commit()
            await send_max_message(user_id, "🔕 Основной будильник выключен!", main_menu_keyboard())

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

            await show_tasks_list(user_id, family_code)
            for m_id, _ in get_family_members(family_code):
                if m_id != user_id:
                    await send_max_message(
                        m_id, f"✅ {name} выполнил(а) задачу: «{task_title}»", main_menu_keyboard()
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

            await show_shopping_list(user_id, family_code)
            for m_id, _ in get_family_members(family_code):
                if m_id != user_id:
                    await send_max_message(
                        m_id, f"🛒 {name} купил(а): {item_name}", main_menu_keyboard()
                    )

        elif action == "clear_bought":
            cursor.execute("DELETE FROM shopping WHERE family_code = ? AND is_bought = 1", (family_code,))
            conn.commit()
            await show_shopping_list(user_id, family_code)

        elif action == "clear_tasks":
            cursor.execute("DELETE FROM tasks WHERE family_code = ? AND is_done = 1", (family_code,))
            conn.commit()
            await show_tasks_list(user_id, family_code)

async def main():
    init_db()
    scheduler.start()
    restore_alarms_on_startup()
    print("🚀 Семейный планировщик MAX успешно запущен на сервере!")

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

                            if u_type == "message_created":
                                msg = update.get("message", {})
                                sender = msg.get("sender", {})
                                u_id = sender.get("user_id")
                                u_name = sender.get("name", "Участник")
                                text = msg.get("body", {}).get("text", "")
                                if u_id and text:
                                    await handle_text(u_id, u_name, text)

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
