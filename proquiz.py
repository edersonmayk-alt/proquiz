import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

TOKEN = "8236988483:AAHgtpeXZjPdkf36MWkiCK8DtGjZaSOrGPk"
DB = "proquiz.db"


# ---------- DB ----------
def db():
    con = sqlite3.connect(DB)
    con.execute("""
    CREATE TABLE IF NOT EXISTS questions(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner INTEGER NOT NULL,
        q TEXT NOT NULL,
        a TEXT NOT NULL,
        b TEXT NOT NULL,
        c TEXT NOT NULL,
        d TEXT NOT NULL,
        correct INTEGER NOT NULL
    )
    """)
    con.commit()
    return con

def add_q(owner: int, q: str, opts: list[str], correct: int) -> int:
    con = db()
    cur = con.cursor()
    cur.execute(
        "INSERT INTO questions(owner,q,a,b,c,d,correct) VALUES(?,?,?,?,?,?,?)",
        (owner, q, opts[0], opts[1], opts[2], opts[3], correct)
    )
    con.commit()
    qid = cur.lastrowid
    con.close()
    return qid

def my_q(owner: int):
    con = db()
    cur = con.cursor()
    cur.execute("SELECT id,q FROM questions WHERE owner=? ORDER BY id DESC", (owner,))
    rows = cur.fetchall()
    con.close()
    return rows

def del_q(owner: int, qid: int) -> bool:
    con = db()
    cur = con.cursor()
    cur.execute("DELETE FROM questions WHERE id=? AND owner=?", (qid, owner))
    con.commit()
    ok = cur.rowcount > 0
    con.close()
    return ok

def random_q(exclude_ids: list[int]):
    con = db()
    cur = con.cursor()
    if exclude_ids:
        ph = ",".join("?" for _ in exclude_ids)
        cur.execute(
            f"SELECT id,q,a,b,c,d,correct FROM questions WHERE id NOT IN ({ph}) ORDER BY RANDOM() LIMIT 1",
            exclude_ids
        )
    else:
        cur.execute("SELECT id,q,a,b,c,d,correct FROM questions ORDER BY RANDOM() LIMIT 1")
    row = cur.fetchone()
    con.close()
    return row


# ---------- UI ----------
def kb(qid: int, a: str, b: str, c: str, d: str):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"A) {a}", callback_data=f"ans|{qid}|0")],
        [InlineKeyboardButton(f"B) {b}", callback_data=f"ans|{qid}|1")],
        [InlineKeyboardButton(f"C) {c}", callback_data=f"ans|{qid}|2")],
        [InlineKeyboardButton(f"D) {d}", callback_data=f"ans|{qid}|3")],
        [InlineKeyboardButton("⏭ Keyingi savol", callback_data="next")]
    ])


# ---------- Commands ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✅ Professional Quiz Bot\n\n"
        "Buyruqlar:\n"
        "/add - savol qo‘shish\n"
        "/quiz - test boshlash\n"
        "/score - ball\n"
        "/my - men qo‘shgan savollar\n"
        "/del ID - savolni o‘chirish\n"
        "/cancel - jarayonni bekor qilish"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("add_flow", None)
    await update.message.reply_text("Bekor qilindi ✅")

async def score(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sc = context.user_data.get("score", 0)
    await update.message.reply_text(f"Ball: {sc}")

async def my(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = my_q(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Siz hali savol qo‘shmagansiz. /add")
        return
    text = "🗂 Sizning savollaringiz:\n" + "\n".join([f"{i}) {q}" for i, q in rows[:30]])
    await update.message.reply_text(text)

async def delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    if len(parts) != 2 or not parts[1].isdigit():
        await update.message.reply_text("Format: /del ID (masalan: /del 12)")
        return
    qid = int(parts[1])
    ok = del_q(update.effective_user.id, qid)
    await update.message.reply_text("O‘chirildi ✅" if ok else "Topilmadi yoki bu sizniki emas ❌")


# ---------- Add flow ----------
async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["add_flow"] = {"step": "q", "q": "", "opts": []}
    await update.message.reply_text("Savol matnini yuboring:")

async def add_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    flow = context.user_data.get("add_flow")
    if not flow:
        return

    t = (update.message.text or "").strip()
    if not t:
        await update.message.reply_text("Bo‘sh bo‘lmasin. Qayta yuboring.")
        return

    step = flow["step"]

    if step == "q":
        flow["q"] = t
        flow["step"] = "a"
        await update.message.reply_text("Variant A ni yuboring:")
        return

    if step in ("a", "b", "c", "d"):
        flow["opts"].append(t)
        flow["step"] = {"a": "b", "b": "c", "c": "d", "d": "correct"}[step]
        await update.message.reply_text(
            "Variant B ni yuboring:" if step == "a" else
            "Variant C ni yuboring:" if step == "b" else
            "Variant D ni yuboring:" if step == "c" else
            "To‘g‘ri javobni yuboring: A/B/C/D"
        )
        return

    if step == "correct":
        m = {"A": 0, "B": 1, "C": 2, "D": 3}
        k = t.upper()
        if k not in m:
            await update.message.reply_text("A/B/C/D dan birini yuboring (masalan: B)")
            return
        qid = add_q(update.effective_user.id, flow["q"], flow["opts"], m[k])
        context.user_data.pop("add_flow", None)
        await update.message.reply_text(f"✅ Saqlandi! ID: {qid}\nYana: /add\nTest: /quiz")


# ---------- Quiz flow ----------
async def quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["seen"] = []
    context.user_data["score"] = 0
    row = random_q([])
    if not row:
        await update.message.reply_text("Hali savol yo‘q. Avval /add")
        return
    qid, q, a, b, c, d, correct = row
    context.user_data["current"] = {"qid": qid, "correct": correct}
    context.user_data["seen"].append(qid)
    await update.message.reply_text(f"🧩 {q}", reply_markup=kb(qid, a, b, c, d))

async def cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    # next
    if data == "next":
        seen = context.user_data.get("seen", [])
        row = random_q(seen)
        if not row:
            sc = context.user_data.get("score", 0)
            await q.message.reply_text(f"🎉 Tugadi! Ball: {sc}\nQayta: /quiz")
            return
        qid, text, a, b, c, d, correct = row
        context.user_data["current"] = {"qid": qid, "correct": correct}
        seen.append(qid)
        context.user_data["seen"] = seen
        await q.message.reply_text(f"🧩 {text}", reply_markup=kb(qid, a, b, c, d))
        return

    # answer
    if data.startswith("ans|"):
        _, qid_s, opt_s = data.split("|")
        qid = int(qid_s)
        opt = int(opt_s)

        cur = context.user_data.get("current")
        if not cur or cur["qid"] != qid:
            return

        if opt == cur["correct"]:
            context.user_data["score"] = context.user_data.get("score", 0) + 1
            res = "✅ To‘g‘ri!"
        else:
            res = "❌ Noto‘g‘ri!"

        await q.edit_message_text(q.message.text + "\n\n" + res)
        return


def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("quiz", quiz))
    app.add_handler(CommandHandler("score", score))
    app.add_handler(CommandHandler("my", my))
    app.add_handler(CommandHandler("del", delete))
    app.add_handler(CommandHandler("cancel", cancel))

    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, add_text))

    app.run_polling()

if __name__ == "__main__":
    main()
