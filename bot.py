import logging
import os
import sqlite3
from datetime import datetime, timedelta
import re
import matplotlib
import matplotlib.pyplot as plt
import warnings
import pathlib
import fcntl
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    Filters,
    CallbackContext,
    CallbackQueryHandler,
)
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton

# ---------------- Matplotlib setup ----------------
matplotlib.use("Agg")
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

DATA_DIR = os.getenv("DATA_DIR", "/data")
pathlib.Path(DATA_DIR).mkdir(parents=True, exist_ok=True)
DB_FILE = os.path.abspath(os.path.join(DATA_DIR, "expenses.db"))

LOCK_PATH = os.path.join(DATA_DIR, "bot.lock")
_lock_fh = None

def acquire_singleton_lock():
    """Ensure only one bot process per container"""
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
        conn.execute(
            """CREATE TABLE IF NOT EXISTS expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT,
                entry_type TEXT,       -- 'Expense' or 'Revenue'
                amount REAL,
                category TEXT,
                note TEXT
            )"""
        )
        conn.commit()

def log_entry(entry_type: str, category: str, amount: int, note: str):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect_db() as conn:
        c = conn.cursor()
        c.execute(
            "INSERT INTO expenses (timestamp, entry_type, amount, category, note) VALUES (?,?,?,?,?)",
            (now, entry_type, amount, category, note),
        )
        conn.commit()
        return c.lastrowid

def get_entries(entry_type: str):
    with connect_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, timestamp, category, amount, note FROM expenses WHERE entry_type=? ORDER BY id ASC",
            (entry_type,),
        )
        return c.fetchall()

def get_total(entry_type: str):
    with connect_db() as conn:
        c = conn.cursor()
        c.execute("SELECT COALESCE(SUM(amount), 0) FROM expenses WHERE entry_type=?", (entry_type,))
        val = c.fetchone()[0]
        return int(round(val or 0))

def get_totals_all(start=None, end=None):
    with connect_db() as conn:
        c = conn.cursor()
        if start and end:
            c.execute(
                "SELECT category, SUM(amount) FROM expenses "
                "WHERE entry_type='Expense' AND timestamp BETWEEN ? AND ? "
                "GROUP BY category",
                (start, end),
            )
        else:
            c.execute(
                "SELECT category, SUM(amount) FROM expenses "
                "WHERE entry_type='Expense' GROUP BY category"
            )
        return c.fetchall()

def delete_by_id(entry_id: int):
    with connect_db() as conn:
        c = conn.cursor()
        c.execute("DELETE FROM expenses WHERE id=?", (entry_id,))
        conn.commit()
        return c.rowcount > 0

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

# ---------------- Utils ----------------
def fmt_money_int(x): return f"${int(round(x)):,}"

def parse_amount(token: str) -> int:
    s = re.sub(r"[^\d+-]", "", token)
    if not re.fullmatch(r"[+\-]?\d+", s):
        raise ValueError("no integer")
    return int(s)

def schedule_autodelete(job_queue, chat_id, msg_id, seconds=60):
    def _delete(context: CallbackContext):
        try:
            context.bot.delete_message(chat_id, msg_id)
        except Exception:
            pass
    job_queue.run_once(_delete, seconds)

# ---------------- Commands ----------------
def help_command(update: Update, context: CallbackContext):
    txt = (
        "**🧠 Welcome to your AI Data Tracker!**\n\n"
        "Easily log, visualize, and manage your financial data — both 💸 expenses and 💵 revenues.\n\n"
        "───────────────\n"
        "**🧾 To log an expense:**\n"
        "Category Amount [optional note]\n"
        "Example: Food 2500 Lunch\n\n"
        "✨ **Commands:**\n"
        "📊 /sum — Totals by expense category\n"
        "🗓 /today — Today’s expenses\n"
        "📅 /week — This week’s expenses\n"
        "📈 /month — This month’s expenses\n"
        "🏆 /top — Expense charts\n"
        "🔎 /detail <category> — View category details\n"
        "❌ /delete <id> — Delete a single entry\n"
        "🗑️ /clear — Clear all data and reset IDs\n\n"
        "───────────────\n\n"
        "**💵 To log a revenue:**\n"
        "/revenue <amount> [note] — Log a revenue entry\n"
        "🧮 /totalrevenue — View detailed revenue list and total\n\n"
        "💡 All entries are automatically saved and logged in dollars ($)."
    )
    m = update.message.reply_text(txt, parse_mode="Markdown")
    schedule_autodelete(context.job_queue, m.chat_id, m.message_id)

# ---- Revenue ----
def revenue_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("Usage: /revenue <amount> [note]")
        return
    try:
        amount = parse_amount(context.args[0])
        note = " ".join(context.args[1:]) if len(context.args) > 1 else ""
        rid = log_entry("Revenue", "revenue", amount, note)
        update.message.reply_text(f"✅ Revenue logged (ID {rid}) — {fmt_money_int(amount)}")
    except Exception:
        update.message.reply_text("❌ Invalid input. Use: /revenue <amount> [note]")

def total_revenue(update: Update, context: CallbackContext):
    entries = get_entries("Revenue")
    if not entries:
        update.message.reply_text("No revenues logged yet.")
        return
    total = get_total("Revenue")
    lines = ["💵 *Revenue Log:*\n"]
    for i, t, _, a, n in entries:
        lines.append(f"#{i} · {t} · {fmt_money_int(a)}{(' · ' + n) if n else ''}")
    lines.append(f"\n✅ *Total Revenue:* {fmt_money_int(total)}")
    update.message.reply_text("\n".join(lines), parse_mode="Markdown")

# ---- Expense summaries ----
def _period_summary(update: Update, context: CallbackContext, title: str, start_dt: datetime, end_dt: datetime):
    totals = get_totals_all(start_dt.strftime("%Y-%m-%d %H:%M:%S"), end_dt.strftime("%Y-%m-%d %H:%M:%S"))
    if not totals:
        m = update.message.reply_text(f"No {title.lower()} expenses yet.")
        return schedule_autodelete(context.job_queue, m.chat_id, m.message_id)
    totals.sort(key=lambda t: (t[1] or 0), reverse=True)
    lines = [f"{c.title()}: {fmt_money_int(a)}" for c, a in totals]
    txt = f"{title} Expenses:\n\n" + "\n".join(lines)
    m = update.message.reply_text(txt)
    schedule_autodelete(context.job_queue, m.chat_id, m.message_id)

def today(update: Update, context: CallbackContext):
    now = datetime.now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    _period_summary(update, context, "Today", start, now)

def week(update: Update, context: CallbackContext):
    now = datetime.now()
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    _period_summary(update, context, "Week", start, now)

def month(update: Update, context: CallbackContext):
    now = datetime.now()
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    _period_summary(update, context, "Month", start, now)

def sum_all(update: Update, context: CallbackContext):
    totals = get_totals_all()
    if not totals:
        m = update.message.reply_text("No expenses logged yet.")
        return schedule_autodelete(context.job_queue, m.chat_id, m.message_id)
    totals.sort(key=lambda t: (t[1] or 0), reverse=True)
    grand = sum(a for _, a in totals)
    lines = [f"{c.title()}: {fmt_money_int(a)}" for c, a in totals]
    txt = "💰 Total Expenses:\n\n" + "\n".join(lines) + f"\n\n✅ Total Spent to Date: {fmt_money_int(grand)}"
    m = update.message.reply_text(txt)
    schedule_autodelete(context.job_queue, m.chat_id, m.message_id)

# ---- Detail & CRUD ----
def detail_command(update: Update, context: CallbackContext):
    if not context.args:
        m = update.message.reply_text("Usage: /detail <category>")
        return schedule_autodelete(context.job_queue, m.chat_id, m.message_id)
    cat = " ".join(context.args).strip().lower()
    with connect_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT id, timestamp, amount, note FROM expenses "
            "WHERE entry_type='Expense' AND LOWER(category)=? "
            "ORDER BY datetime(timestamp) DESC, id DESC",
            (cat,),
        )
        rows = c.fetchall()
        c.execute(
            "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE entry_type='Expense' AND LOWER(category)=?",
            (cat,),
        )
        total = int(round(c.fetchone()[0] or 0))
    if not rows:
        m = update.message.reply_text(f"No entries for {cat.title()}.")
        return schedule_autodelete(context.job_queue, m.chat_id, m.message_id)
    header = f"💰 {cat.title()} Total: {fmt_money_int(total)}\n"
    lines = [f"#{i} · {t} · {fmt_money_int(a)}{(' · ' + (n or '')) if n else ''}".rstrip() for i, t, a, n in rows]
    msg = update.message.reply_text(header + "\n".join(lines))
    schedule_autodelete(context.job_queue, msg.chat_id, msg.message_id)

def delete_command(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("Usage: /delete <id>")
        return
    try:
        entry_id = int(context.args[0])
        msg = "✅ Deleted." if delete_by_id(entry_id) else "No such entry."
    except Exception:
        msg = "Invalid ID."
    update.message.reply_text(msg)

def clear_command(update: Update, context: CallbackContext):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Confirm", callback_data="clear_confirm"),
         InlineKeyboardButton("❌ Cancel", callback_data="clear_cancel")]
    ])
    update.message.reply_text("🗑️ Delete ALL data and reset IDs?", reply_markup=kb)

def clear_callback(update: Update, context: CallbackContext):
    q = update.callback_query
    if q.data == "clear_confirm":
        clear_all_data_and_reset_ids()
        q.edit_message_text("✅ All data cleared and IDs reset.")
    else:
        q.edit_message_text("❌ Cancelled.")
    q.answer()

# ---- Free text expense logger ----
def text_router(update: Update, context: CallbackContext):
    parts = (update.message.text or "").strip().split(None, 2)
    if len(parts) < 2:
        m = update.message.reply_text("❌ Example: Food 2500 Lunch")
        return schedule_autodelete(context.job_queue, m.chat_id, m.message_id)
    cat = parts[0].lower()
    try:
        amt = parse_amount(parts[1])
    except Exception:
        m = update.message.reply_text("❌ Enter a valid whole number (e.g., 2500).")
        return schedule_autodelete(context.job_queue, m.chat_id, m.message_id)
    note = parts[2] if len(parts) > 2 else ""
    eid = log_entry("Expense", cat, amt, note)
    # category total:
    with connect_db() as conn:
        c = conn.cursor()
        c.execute(
            "SELECT COALESCE(SUM(amount),0) FROM expenses WHERE entry_type='Expense' AND LOWER(category)=?",
            (cat,),
        )
        cat_total = int(round(c.fetchone()[0] or 0))
    update.message.reply_text(f"✅ Logged (ID {eid})\n💰 {cat.title()} Total: {fmt_money_int(cat_total)}")

# ---- Error handler ----
def on_error(update, context):
    logging.exception("Error:", exc_info=context.error)
    try:
        if isinstance(update, Update) and update.effective_message:
            update.effective_message.reply_text("⚠️ Something went wrong.")
    except Exception:
        pass

# ---------------- Main ----------------
def main():
    acquire_singleton_lock()
    ensure_db()
    up = Updater(TELEGRAM_TOKEN, use_context=True)
    up.bot.delete_webhook(drop_pending_updates=True)

    dp = up.dispatcher
    dp.add_handler(CommandHandler("help", help_command))
    dp.add_handler(CommandHandler("revenue", revenue_command))
    dp.add_handler(CommandHandler("totalrevenue", total_revenue))
    dp.add_handler(CommandHandler("sum", sum_all))
    dp.add_handler(CommandHandler("today", today))
    dp.add_handler(CommandHandler("week", week))
    dp.add_handler(CommandHandler("month", month))
    dp.add_handler(CommandHandler("top", lambda u, c: u.message.reply_text("Charts currently disabled in this build.")))
    dp.add_handler(CommandHandler("detail", detail_command))
    dp.add_handler(CommandHandler("delete", delete_command))
    dp.add_handler(CommandHandler("clear", clear_command))
    dp.add_handler(CallbackQueryHandler(clear_callback, pattern="^clear_(confirm|cancel)$"))
    dp.add_handler(MessageHandler(Filters.text & ~Filters.command, text_router))
    dp.add_error_handler(on_error)

    up.start_polling(drop_pending_updates=True)
    logging.info("✅ Bot started successfully.")
    up.idle()

if __name__ == "__main__":
    main()
