import logging
import os
import sqlite3
from datetime import datetime, timedelta
import pandas as pd
import matplotlib
import matplotlib.pyplot as plt
import warnings

from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
    CallbackQueryHandler,
)
from telegram import (
    ParseMode,
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)

# --- Logging setup ---
warnings.filterwarnings("ignore", message="python-telegram-bot is using upstream urllib3")
warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API.")
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

matplotlib.rcParams["font.family"] = ["Calibri", "Arial", "DejaVu Sans", "Liberation Sans", "sans-serif"]

# --- Config ---
DB_FILE = "expenses.db"
EXCEL_FILE = "expenses.xlsx"
TELEGRAM_TOKEN = "7002604173:AAHbyKtSPFRpqPlb-MTVoQ4cGEtYGi43a4Y"   # ← replace once with your real token

# --- Database helpers ---
def ensure_db():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute(
            """CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                user TEXT,
                entry_type TEXT,
                name TEXT,
                amount REAL,
                category TEXT,
                note TEXT,
                payment_method TEXT,
                account_type TEXT
            )"""
        )
        conn.commit()

def log_expense_to_db(category_lower, amount, note, user_hint=""):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO expenses (timestamp,user,entry_type,name,amount,category,note,payment_method,account_type)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (now, user_hint, "Expense", "", amount, category_lower, note, "Cash", ""),
        )
        exp_id = c.lastrowid
        conn.commit()
        return exp_id

def get_totals_all(start_dt=None, end_dt=None):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        if start_dt and end_dt:
            c.execute(
                "SELECT category, SUM(amount) FROM expenses "
                "WHERE timestamp BETWEEN ? AND ? AND entry_type='Expense' "
                "GROUP BY category",
                (start_dt, end_dt),
            )
        else:
            c.execute(
                "SELECT category, SUM(amount) FROM expenses "
                "WHERE entry_type='Expense' GROUP BY category"
            )
        return c.fetchall()

def get_all_expenses():
    with sqlite3.connect(DB_FILE) as conn:
        return pd.read_sql_query("SELECT * FROM expenses", conn)

def export_db_to_excel():
    get_all_expenses().to_excel(EXCEL_FILE, index=False)

def clear_all_data():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM expenses")
        conn.commit()

def get_categories_with_sums_all():
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT category, SUM(amount) as total FROM expenses "
            "WHERE entry_type='Expense' GROUP BY category ORDER BY SUM(amount) DESC"
        )
        return c.fetchall()

def delete_expense_by_id_global(expense_id):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
        conn.commit()
        return c.rowcount > 0

# --- Category helpers ---
def get_category_sum_all(category_lower):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT COALESCE(SUM(amount),0) FROM expenses "
            "WHERE entry_type='Expense' AND LOWER(category)=?",
            (category_lower,),
        )
        row = c.fetchone()
        return float(row[0] or 0.0)

def get_category_details_all(category_lower):
    with sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, timestamp, amount, note FROM expenses "
            "WHERE entry_type='Expense' AND LOWER(category)=? "
            "ORDER BY datetime(timestamp) DESC, id DESC",
            (category_lower,),
        )
        return c.fetchall()

# --- Utility ---
def pretty(cat):
    return (cat or "").strip().title()

def schedule_autodelete(job_queue, chat_id, message_id, seconds=60):
    """Deletes messages automatically (default 60 s)."""
    def _delete(context: CallbackContext):
        try:
            context.bot.delete_message(chat_id, message_id)
        except Exception:
            pass
    job_queue.run_once(_delete, seconds)

# --- /help ---
def help_command(update: Update, context: CallbackContext):
    text = (
        "*💰 Welcome to your Expense AI Tracker!*\n"
        "To log an expense, simply type:\n"
        "`Category Amount [optional note]`\n"
        "Example: `Food 25 Lunch`\n\n"
        "✨ *Available Commands:*\n"
        "📊 `/sum` — View total expenses by category\n"
        "🗓 `/today` — Show today’s expenses\n"
        "📅 `/week` — Show this week’s expenses\n"
        "📈 `/month` — Show this month’s expenses\n"
        "🏆 `/top` — View expense charts\n"
        "🔎 `/detail <category>` — View total and detailed logs for a category\n"
        "❌ `/delete <id>` — Delete a specific entry\n"
        "🗑️ `/clear` — Delete all your data\n\n"
        "💡 No need to use the `$` sign — all entries are logged in dollars.\n\n"
        "✅ Every transaction is automatically saved to your Excel file.\n"
    )
    msg = update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    schedule_autodelete(context.job_queue, msg.chat_id, msg.message_id, 60)

# --- Period summaries ---
def _period_summary_core(update, context, title, start, end):
    totals = get_totals_all(start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"))
    if not totals:
        m = update.message.reply_text(f"No {title.lower()} expenses logged yet.")
        return schedule_autodelete(context.job_queue, m.chat_id, m.message_id, 60)
    totals.sort(key=lambda t: (t[1] or 0), reverse=True)
    text = f"📅 *{title} Expenses:*\n\n" + "".join(f"{pretty(c)}: ${a:.2f}\n" for c, a in totals)
    m = update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    schedule_autodelete(context.job_queue, m.chat_id, m.message_id, 60)

def today(u, c):
    n = datetime.now()
    _period_summary_core(u, c, "Today", n.replace(hour=0, minute=0, second=0, microsecond=0), n)

def week(u, c):
    n = datetime.now()
    s = n - timedelta(days=n.weekday())
    s = s.replace(hour=0, minute=0, second=0, microsecond=0)
    _period_summary_core(u, c, "Week", s, n)

def month(u, c):
    n = datetime.now()
    s = n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    _period_summary_core(u, c, "Month", s, n)

def sum_all(u, c):
    totals = get_totals_all()
    if not totals:
        m = u.message.reply_text("No expenses logged yet.")
        return schedule_autodelete(c.job_queue, m.chat_id, m.message_id, 60)
    totals.sort(key=lambda t: (t[1] or 0), reverse=True)
    text = "💰 *Total Expenses by Category:*\n\n" + "".join(f"{pretty(ca)}: ${a:.2f}\n" for ca, a in totals)
    m = u.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
    schedule_autodelete(c.job_queue, m.chat_id, m.message_id, 60)

# --- /top (white background charts) ---
def top_command(update: Update, context: CallbackContext):
    data = get_categories_with_sums_all()
    if not data:
        m = update.message.reply_text("No expenses logged yet.")
        return schedule_autodelete(context.job_queue, m.chat_id, m.message_id, 60)

    labels = [pretty(c) for c, _ in data]
    sizes = [float(a) for _, a in data]

    # PIE (white background)
    fig1, ax1 = plt.subplots()
    wedges, texts, autotexts = ax1.pie(
        sizes, labels=labels, autopct="%1.1f%%", startangle=90
    )
    for t in texts + autotexts:
        t.set_color("black")
        t.set_fontweight("bold")
    ax1.axis("equal")
    plt.title("Expense Distribution (%)", pad=16, color="black")
    pie_path = "categories_pie.png"
    plt.savefig(pie_path, bbox_inches="tight", facecolor="white")
    plt.close(fig1)

    # BAR (white background)
    fig2, ax2 = plt.subplots()
    cmap = matplotlib.cm.get_cmap("tab20")
    colors = [cmap(i % cmap.N) for i in range(len(labels))]
    bars = ax2.bar(labels, sizes, color=colors)
    ax2.set_title("Total by Category ($)", pad=12)
    ax2.set_ylabel("Total ($)")
    ax2.set_xlabel("Category")
    plt.xticks(rotation=20, ha="right")
    for bar, val in zip(bars, sizes):
        x = bar.get_x() + bar.get_width() / 2
        y = bar.get_height() / 2
        ax2.text(x, y, f"${val:.2f}", ha="center", va="center", color="black", fontsize=10, fontweight="bold")
    plt.tight_layout()
    bar_path = "categories_bar.png"
    plt.savefig(bar_path, bbox_inches="tight", facecolor="white")
    plt.close(fig2)

    # Send images
    with open(pie_path, "rb") as img:
        pie_msg = update.message.reply_photo(img)
    with open(bar_path, "rb") as img:
        bar_msg = update.message.reply_photo(img)

    try:
        os.remove(pie_path)
        os.remove(bar_path)
    except Exception:
        pass

    summary = "🏆 *Expense Charts Summary:*\n" + "".join(f"{l}: ${s:.2f}\n" for l, s in zip(labels, sizes))
    text_msg = update.message.reply_text(summary, parse_mode=ParseMode.MARKDOWN)

    for msg in (pie_msg, bar_msg, text_msg):
        schedule_autodelete(context.job_queue, msg.chat_id, msg.message_id, 60)

# --- /detail ---
def detail_command(update: Update, context: CallbackContext):
    if not context.args:
        m = update.message.reply_text("Usage: /detail <category>", parse_mode=ParseMode.MARKDOWN)
        return schedule_autodelete(context.job_queue, m.chat_id, m.message_id, 60)
    category_raw = " ".join(context.args).strip()
    category_lower = category_raw.lower()
    total = get_category_sum_all(category_lower)
    details = get_category_details_all(category_lower)
    if not details:
        m = update.message.reply_text(f"No entries for *{pretty(category_raw)}*.", parse_mode=ParseMode.MARKDOWN)
        return schedule_autodelete(context.job_queue, m.chat_id, m.message_id, 60)
    header = f"💰 *{pretty(category_raw)}* — All-Time Total: ${total:.2f}\n"
    lines = []
    for _id, ts, amt, note in details:
        note_part = f" · {note}" if note else ""
        lines.append(f"#{_id} · {ts} · ${amt:.2f}{note_part}")
    msg = update.message.reply_text(header + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    schedule_autodelete(context.job_queue, msg.chat_id, msg.message_id, 60)

# --- /delete ---
def delete_command(u, c):
    args = c.args
    if not args:
        m = u.message.reply_text("Usage: /delete <id>")
        return schedule_autodelete(c.job_queue, m.chat_id, m.message_id, 60)
    try:
        exp_id = int(args[0])
        success = delete_expense_by_id_global(exp_id)
        msg = f"❌ Entry {exp_id} deleted." if success else f"No entry found with ID {exp_id}."
    except Exception:
        msg = "Invalid ID."
    export_db_to_excel()
    m = u.message.reply_text(msg)
    schedule_autodelete(c.job_queue, m.chat_id, m.message_id, 60)

# --- /clear ---
def clear_command(u, c):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data="clear_confirm"),
         InlineKeyboardButton("❌ Cancel", callback_data="clear_cancel")]
    ])
    prompt = "🗑️ This will permanently delete all data. Continue?"
    u.message.reply_text(prompt, reply_markup=kb)

def clear_callback(u, c):
    q = u.callback_query
    if q.data == "clear_confirm":
        clear_all_data()
        export_db_to_excel()
        q.edit_message_text("✅ All data cleared.")
    else:
        q.edit_message_text("❌ Cancelled.")
    q.answer()

# --- Text router (💰 Category All-Time Total) ---
def text_router(u, c):
    parts = (u.message.text or "").strip().split(None, 2)
    if len(parts) < 2:
        m = u.message.reply_text(
            "❌ Please enter Category and Amount.\nExample: `Food 25 Lunch`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return schedule_autodelete(c.job_queue, m.chat_id, m.message_id, 30)

    category_lower = parts[0].lower().strip()
    try:
        amount = float(parts[1])
    except Exception:
        m = u.message.reply_text(
            "❌ Amount must be a number.\nExample: `Food 25 Lunch`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return schedule_autodelete(c.job_queue, m.chat_id, m.message_id, 30)

    note = parts[2] if len(parts) > 2 else ""
    exp_id = log_expense_to_db(category_lower, amount, note)
    export_db_to_excel()
    cat_sum = get_category_sum_all(category_lower)
    cat_name = pretty(category_lower)

    u.message.reply_text(
        f"✅ Your transaction has been logged (ID: {exp_id}).\n"
        f"💰 {cat_name} All-Time Total: ${cat_sum:.2f}"
    )

# --- Main ---
def main():
    ensure_db()
    up = Updater(TELEGRAM_TOKEN, use_context=True)
    dp = up.dispatcher
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(CommandHandler("sum", sum_all))
    dp.add_handler(CommandHandler("today", today))
    dp.add_handler(CommandHandler("week", week))
    dp.add_handler(CommandHandler("month", month))
    dp.add_handler(CommandHandler("top", top_command))
    dp.add_handler(CommandHandler("detail", detail_command))
    dp.add_handler(CommandHandler("delete", delete_command))
    dp.add_handler(CommandHandler("clear", clear_command))
    dp.add_handler(CallbackQueryHandler(clear_callback, pattern="^clear_(confirm|cancel)$"))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, text_router))
    up.start_polling()
    logging.info("✅ Bot started successfully.")
    up.idle()

if __name__ == "__main__":
    main()
