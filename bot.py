import logging
import os
import sqlite3
from datetime import datetime, timedelta
import re
import matplotlib
import matplotlib.pyplot as plt
import warnings
import pathlib
import fcntl  # Linux-only; Railway is Linux
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

# ---------------- Matplotlib setup (no GUI, no font warnings) ----------------
matplotlib.use("Agg")  # headless backend
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
matplotlib.rcParams["font.family"] = ["DejaVu Sans", "sans-serif"]

# ---------------- Logging ----------------
warnings.filterwarnings("ignore", message="python-telegram-bot is using upstream urllib3")
warnings.filterwarnings("ignore", message="pkg_resources is deprecated as an API.")
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# ---------------- Config ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
if not TELEGRAM_TOKEN:
    raise SystemExit("Missing TELEGRAM_TOKEN env var")

DATA_DIR = os.getenv("DATA_DIR", "/data")  # Railway volume
pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
DB_FILE = os.path.abspath(os.path.join(DATA_DIR, "expenses.db"))

LOCK_PATH = os.path.join(DATA_DIR, "bot.lock")
_lock_fh = None

def acquire_singleton_lock():
    global _lock_fh
    _lock_fh = open(LOCK_PATH, "w")
    try:
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
        logging.info("🔒 Singleton lock acquired.")
    except BlockingIOError:
        logging.error("❌ Another bot process already holds the lock. Exiting.")
        raise SystemExit(1)

# ---------------- DB helpers ----------------
def connect_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def ensure_db():
    with connect_db() as conn:
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

def log_expense_to_db(category, amount, note):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect_db() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO expenses (timestamp,user,entry_type,name,amount,category,note,payment_method,account_type)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (now, "", "Expense", "", amount, category, note, "Cash", ""),
        )
        exp_id = c.lastrowid
        conn.commit()
        return exp_id

def get_totals_all(start=None, end=None):
    with connect_db() as conn:
        c = conn.cursor()
        if start and end:
            c.execute(
                "SELECT category, SUM(amount) FROM expenses "
                "WHERE timestamp BETWEEN ? AND ? AND entry_type='Expense' GROUP BY category",
                (start, end),
            )
        else:
            c.execute(
                "SELECT category, SUM(amount) FROM expenses WHERE entry_type='Expense' GROUP BY category"
            )
        return c.fetchall()

def clear_all_data_and_reset_ids():
    with connect_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM expenses")
        try:
            c.execute("DELETE FROM sqlite_sequence WHERE name='expenses'")
        except sqlite3.OperationalError:
            pass
        conn.commit()
        c.execute("VACUUM")
        conn.commit()

def get_categories_with_sums_all():
    with connect_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT category, SUM(amount) FROM expenses "
            "WHERE entry_type='Expense' GROUP BY category ORDER BY SUM(amount) DESC"
        )
        return c.fetchall()

def delete_expense_by_id_global(expense_id):
    with connect_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM expenses WHERE id=?", (expense_id,))
        conn.commit()
        return c.rowcount > 0

def get_category_sum_all(category):
    with connect_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT COALESCE(SUM(amount),0) FROM expenses "
            "WHERE entry_type='Expense' AND LOWER(category)=?",
            (category,),
        )
        return float(c.fetchone()[0] or 0.0)

def get_category_details_all(category):
    with connect_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, timestamp, amount, note FROM expenses "
            "WHERE entry_type='Expense' AND LOWER(category)=? ORDER BY datetime(timestamp) DESC, id DESC",
            (category,),
        )
        return c.fetchall()

# ---------------- Utils ----------------
def pretty(cat): return (cat or "").strip().title()

def schedule_autodelete(job_queue, chat_id, msg_id, seconds=60):
    def _delete(context: CallbackContext):
        try: context.bot.delete_message(chat_id, msg_id)
        except Exception: pass
    job_queue.run_once(_delete, seconds)

def parse_amount(token: str) -> int:
    s = re.sub(r'[^0-9+\-]', '', token)
    if not re.fullmatch(r'[+\-]?\d+', s):
        raise ValueError("no integer")
    return int(s)

def fmt_money_int(x): 
    return f"${int(round(x)):,}"  # adds commas

# ---------------- Commands ----------------
def help_command(update: Update, context: CallbackContext):
    txt = (
        "*💰 Expense AI Tracker*\n"
        "`Category Amount [optional note]`\nExample: `Food 2500 Lunch`\n\n"
        "📊 `/sum` — Totals by category\n"
        "🗓 `/today` — Today\n"
        "📅 `/week` — This week\n"
        "📈 `/month` — This month\n"
        "🏆 `/top` — Charts\n"
        "🔎 `/detail <category>` — Details\n"
        "❌ `/delete <id>` — Delete entry\n"
        "🗑️ `/clear` — Clear all + reset IDs\n"
        "💡 Whole numbers only. Values show commas."
    )
    m = update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)
    schedule_autodelete(context.job_queue, m.chat_id, m.message_id)

def _period_summary(update, context, title, start, end):
    totals = get_totals_all(start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"))
    if not totals:
        m = update.message.reply_text(f"No {title.lower()} expenses yet.")
        return schedule_autodelete(context.job_queue, m.chat_id, m.message_id)
    totals.sort(key=lambda t: (t[1] or 0), reverse=True)
    txt = f"📅 *{title} Expenses:*\n\n" + "".join(f"{pretty(c)}: {fmt_money_int(a)}\n" for c, a in totals)
    m = update.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)
    schedule_autodelete(context.job_queue, m.chat_id, m.message_id)

def today(u, c):
    n = datetime.now(); start = n.replace(hour=0, minute=0, second=0, microsecond=0)
    _period_summary(u, c, "Today", start, n)

def week(u, c):
    n = datetime.now(); start = (n - timedelta(days=n.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    _period_summary(u, c, "Week", start, n)

def month(u, c):
    n = datetime.now(); start = n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    _period_summary(u, c, "Month", start, n)

def sum_all(u, c):
    totals = get_totals_all()
    if not totals:
        m = u.message.reply_text("No expenses logged yet.")
        return schedule_autodelete(c.job_queue, m.chat_id, m.message_id)
    totals.sort(key=lambda t: (t[1] or 0), reverse=True)
    txt = "💰 *Total Expenses:*\n\n" + "".join(f"{pretty(ca)}: {fmt_money_int(a)}\n" for ca, a in totals)
    m = u.message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)
    schedule_autodelete(c.job_queue, m.chat_id, m.message_id)

def top_command(update: Update, context: CallbackContext):
    data = get_categories_with_sums_all()
    if not data:
        m = update.message.reply_text("No expenses yet.")
        return schedule_autodelete(context.job_queue, m.chat_id, m.message_id)
    labels = [pretty(c) for c, _ in data]
    sizes = [int(round(a)) for _, a in data]
    fig1, ax1 = plt.subplots()
    ax1.pie(sizes, labels=labels, autopct=lambda p: f"{int(p)}%", startangle=90)
    plt.title("Expense Distribution (%)")
    pie = "pie.png"
    plt.savefig(pie, bbox_inches="tight", facecolor="white"); plt.close(fig1)
    with open(pie, "rb") as f: pie_msg = update.message.reply_photo(f)
    os.remove(pie)
    msg = update.message.reply_text(
        "🏆 *Expense Chart Summary:*\n" + "".join(f"{l}: {fmt_money_int(s)}\n" for l, s in zip(labels, sizes)),
        parse_mode=ParseMode.MARKDOWN)
    schedule_autodelete(context.job_queue, msg.chat_id, msg.message_id)

def detail_command(update: Update, context: CallbackContext):
    if not context.args:
        m = update.message.reply_text("Usage: /detail <category>")
        return schedule_autodelete(context.job_queue, m.chat_id, m.message_id)
    cat = " ".join(context.args).lower()
    total = get_category_sum_all(cat)
    rows = get_category_details_all(cat)
    if not rows:
        m = update.message.reply_text(f"No entries for {pretty(cat)}.")
        return schedule_autodelete(context.job_queue, m.chat_id, m.message_id)
    header = f"💰 *{pretty(cat)}* Total: {fmt_money_int(total)}\n"
    lines = [f"#{i} · {t} · {fmt_money_int(a)} {n or ''}" for i, t, a, n in rows]
    msg = update.message.reply_text(header + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)
    schedule_autodelete(context.job_queue, msg.chat_id, msg.message_id)

def delete_command(u, c):
    if not c.args:
        m = u.message.reply_text("Usage: /delete <id>")
        return schedule_autodelete(c.job_queue, m.chat_id, m.message_id)
    try:
        i = int(c.args[0])
        msg = "✅ Deleted." if delete_expense_by_id_global(i) else "No such entry."
    except: msg = "Invalid ID."
    m = u.message.reply_text(msg); schedule_autodelete(c.job_queue, m.chat_id, m.message_id)

def clear_command(u, c):
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data="clear_confirm"),
                                InlineKeyboardButton("❌ Cancel", callback_data="clear_cancel")]])
    u.message.reply_text("🗑️ Delete ALL data and reset IDs?", reply_markup=kb)

def clear_callback(u, c):
    q = u.callback_query
    if q.data == "clear_confirm":
        clear_all_data_and_reset_ids()
        q.edit_message_text("✅ All data cleared and ID reset.")
    else:
        q.edit_message_text("❌ Cancelled.")
    q.answer()

def debugdb(update, context):
    with connect_db() as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*), MAX(id) FROM expenses")
        count, maxid = c.fetchone()
    m = update.message.reply_text(f"Rows: {count}, Max ID: {maxid}")
    schedule_autodelete(context.job_queue, m.chat_id, m.message_id)

def health(update, context):
    try:
        with connect_db() as conn: conn.execute("SELECT 1")
        update.message.reply_text("✅ OK")
    except Exception as e:
        update.message.reply_text(f"❌ DB error: {e}")

# ---------------- Text Router ----------------
def text_router(u, c):
    parts = u.message.text.strip().split(None, 2)
    if len(parts) < 2:
        m = u.message.reply_text("❌ Example: `Food 2500 Lunch`", parse_mode=ParseMode.MARKDOWN)
        return schedule_autodelete(c.job_queue, m.chat_id, m.message_id)
    cat = parts[0].lower()
    try: amt = parse_amount(parts[1])
    except: 
        m = u.message.reply_text("❌ Enter a valid whole number.")
        return schedule_autodelete(c.job_queue, m.chat_id, m.message_id)
    note = parts[2] if len(parts) > 2 else ""
    eid = log_expense_to_db(cat, amt, note)
    total = get_category_sum_all(cat)
    u.message.reply_text(f"✅ Logged (ID {eid})\n💰 {pretty(cat)} Total: {fmt_money_int(total)}")

# ---------------- Error Handler ----------------
def on_error(update, context):
    logging.exception("Error:", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            update.effective_message.reply_text("⚠️ Something went wrong.")
    except: pass

# ---------------- Main ----------------
def main():
    acquire_singleton_lock()
    ensure_db()
    up = Updater(TELEGRAM_TOKEN, use_context=True)
    up.bot.delete_webhook(drop_pending_updates=True)

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
    dp.add_handler(CommandHandler("debugdb", debugdb))
    dp.add_handler(CommandHandler("health", health))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, text_router))
    dp.add_error_handler(on_error)

    up.start_polling(drop_pending_updates=True)
    logging.info("✅ Bot started successfully.")
    up.idle()

if __name__ == "__main__":
    main()
