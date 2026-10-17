import csv
import os
import sqlite3
from config import DB_NAME


def export_monthly_report(family_code: str, requester_name: str, year: int, month: int):
    month_prefix = f"{year:04d}-{month:02d}"
    os.makedirs("reports", exist_ok=True)

    txt_path = f"reports/report_{family_code}_{month_prefix}.txt"
    csv_path = f"reports/report_{family_code}_{month_prefix}.csv"

    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            SELECT title, task_time, is_done, author_name, target_names
            FROM tasks
            WHERE family_code = ? AND created_at LIKE ?
            ORDER BY id ASC
            """,
            (family_code, f"{month_prefix}%"),
        )
        tasks = cursor.fetchall()

        cursor.execute(
            """
            SELECT item, is_bought, created_at, author_name
            FROM shopping
            WHERE family_code = ? AND created_at LIKE ?
            ORDER BY created_at ASC
            """,
            (family_code, f"{month_prefix}%"),
        )
        shopping = cursor.fetchall()

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write(f"📊 СЕМЕЙНЫЙ ОТЧЕТ ПЛАНИРОВЩИКА MAX (Группа: {family_code})\n")
        f.write(f"Запросил(а): {requester_name} | Период: {month:02d}.{year}\n")
        f.write("=" * 60 + "\n\n")

        f.write("📅 ОБЩИЙ СПИСОК ДЕЛ:\n")
        if tasks:
            for title, t_time, is_done, author, target_names in tasks:
                status = "✅ Выполнено" if is_done else "⏳ В процессе"
                targets = target_names or "Все"
                f.write(f" • [{t_time}] {title} | Кому: {targets} (от: {author}) — {status}\n")
        else:
            f.write(" Задач за этот месяц нет.\n")

        f.write("\n🛒 ОБЩИЙ СПИСОК ПОКУПОК:\n")
        if shopping:
            for item, is_bought, created_at, author in shopping:
                status = "✅ Куплено" if is_bought else "⬜ Не куплено"
                f.write(f" • {item} (добавил: {author}, {created_at[:10]}) — {status}\n")
        else:
            f.write(" Покупок за этот месяц нет.\n")

    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Категория", "Название", "Кто добавил", "Для кого / Будильник", "Дата/Время", "Статус"])

        for title, t_time, is_done, author, target_names in tasks:
            writer.writerow(["Задача", title, author, target_names or "Все", t_time, "Выполнено" if is_done else "Не выполнено"])

        for item, is_bought, created_at, author in shopping:
            writer.writerow(["Покупка", item, author, "Общий список", created_at, "Куплено" if is_bought else "В списке"])

    return txt_path, csv_path
