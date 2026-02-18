import asyncio
import os
from datetime import datetime
import aiosqlite
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    CallbackQuery,
    BotCommand,
    MenuButtonCommands,
)
from aiogram.filters import Command
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.utils.keyboard import InlineKeyboardBuilder


# ================= CONFIG =================
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
DB_NAME = "quiz.db"

MAX_QUESTIONS = 20
DEFAULT_Q = 5
TIME_LIMIT_SECONDS = 20

# 3 ta savol ketma-ket hech kim javob bermasa stop
NO_ANSWER_STOP_STREAK = 3

# natija ko‘rinishi (bar uzunligi)
BAR_LEN = 12

dp = Dispatcher(storage=MemoryStorage())

# ================= GROUP QUIZ SESSIONS (in-memory) =================
# chat_id -> session dict
GROUP_SESSIONS: dict[int, dict] = {}


# ================= DATABASE =================
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            question TEXT,
            a TEXT,
            b TEXT,
            c TEXT,
            d TEXT,
            correct TEXT
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT
        )
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            score INTEGER NOT NULL,
            total INTEGER NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """)
        await db.commit()


async def upsert_user(user_id: int, full_name: str):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO users(user_id, full_name) VALUES(?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET full_name=excluded.full_name",
            (user_id, full_name)
        )
        await db.commit()


async def save_result(user_id: int, score: int, total: int):
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute(
            "INSERT INTO results(user_id, score, total) VALUES(?, ?, ?)",
            (user_id, score, total)
        )
        await db.commit()


async def get_top10():
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("""
            SELECT u.full_name, r.user_id, SUM(r.score) AS total_score, COUNT(*) AS games
            FROM results r
            LEFT JOIN users u ON u.user_id = r.user_id
            GROUP BY r.user_id
            ORDER BY total_score DESC
            LIMIT 10
        """)
        return await cur.fetchall()


async def get_my_stats(user_id: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("""
            SELECT COUNT(*) AS games, COALESCE(SUM(score),0) AS total_score, COALESCE(SUM(total),0) AS total_q
            FROM results
            WHERE user_id=?
        """, (user_id,))
        return await cur.fetchone()


async def get_user_questions(user_id: int, limit: int = 20):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("""
            SELECT id, question, correct
            FROM questions
            WHERE user_id=?
            ORDER BY id DESC
            LIMIT ?
        """, (user_id, limit))
        return await cur.fetchall()


async def get_random_questions(limit: int):
    async with aiosqlite.connect(DB_NAME) as db:
        cur = await db.execute("""
            SELECT id, question, a, b, c, d, correct
            FROM questions
            ORDER BY RANDOM()
            LIMIT ?
        """, (limit,))
        return await cur.fetchall()


# ================= STATES =================
class AddQuestion(StatesGroup):
    question = State()
    a = State()
    b = State()
    c = State()
    d = State()
    correct = State()


class PersonalQuizState(StatesGroup):
    answering = State()


# ================= HELPERS =================
def clamp_n(n: int) -> int:
    if n < 1:
        return 1
    if n > MAX_QUESTIONS:
        return MAX_QUESTIONS
    return n


def parse_n_from_text(text: str, default_n: int = DEFAULT_Q) -> int:
    parts = (text or "").split()
    if len(parts) >= 2 and parts[1].isdigit():
        return clamp_n(int(parts[1]))
    return clamp_n(default_n)


def mention_html(user_id: int, full_name: str) -> str:
    safe = (full_name or "user").replace("<", "").replace(">", "")
    return f'<a href="tg://user?id={user_id}">{safe}</a>'


def make_bar(pct: int, length: int = BAR_LEN) -> str:
    filled = int(round(pct * length / 100))
    if filled < 0:
        filled = 0
    if filled > length:
        filled = length
    return "▓" * filled + "░" * (length - filled)


# ✅ Tugmalar qator ko‘rinishda (ustma-ust)
def abcd_keyboard_personal():
    kb = InlineKeyboardBuilder()
    kb.button(text="A", callback_data="pans:A")
    kb.button(text="B", callback_data="pans:B")
    kb.button(text="C", callback_data="pans:C")
    kb.button(text="D", callback_data="pans:D")
    kb.adjust(1)
    return kb.as_markup()


def abcd_keyboard_group(chat_id: int, q_index: int):
    kb = InlineKeyboardBuilder()
    kb.button(text="A", callback_data=f"gans:{chat_id}:{q_index}:A")
    kb.button(text="B", callback_data=f"gans:{chat_id}:{q_index}:B")
    kb.button(text="C", callback_data=f"gans:{chat_id}:{q_index}:C")
    kb.button(text="D", callback_data=f"gans:{chat_id}:{q_index}:D")
    kb.adjust(1)
    return kb.as_markup()


def group_result_keyboard(chat_id: int, q_index: int, correct: str):
    kb = InlineKeyboardBuilder()
    kb.button(text=f"✅ To‘g‘ri javob: {correct}", callback_data=f"gshow:{chat_id}:{q_index}")
    kb.button(text="👥 Qatnashchilar", callback_data=f"gshow:{chat_id}:{q_index}")
    kb.adjust(1)
    return kb.as_markup()


def build_poll_like_result_text(session: dict) -> str:
    """
    Telegram pollga o‘xshash natija:
    A ▓▓▓░░ 30% (3) — @name @name
    ...
    """
    counts = session["answer_counts"]
    voters = session["voters"]  # {"A":[uid...], ...}
    names = session["names"]    # uid -> name
    total = sum(counts.values())

    correct = session.get("current_correct")
    header = "📊 Natijalar (poll uslubida)\n"
    if total == 0:
        return header + "\nHech kim javob bermadi."

    lines = []
    for opt in ["A", "B", "C", "D"]:
        c = counts.get(opt, 0)
        pct = int(c * 100 / total) if total else 0
        bar = make_bar(pct)

        # oxirida shu variantni bosganlar (max 6 ta ko‘rsatamiz)
        ids = voters.get(opt, [])
        show_ids = ids[:6]
        who = ""
        if show_ids:
            who = " — " + ", ".join(mention_html(uid, names.get(uid, f"user:{uid}")) for uid in show_ids)
            if len(ids) > 6:
                who += f" (+{len(ids)-6})"

        mark = " ✅" if opt == correct else ""
        lines.append(f"{opt}{mark}  {bar}  {pct}%  ({c}){who}")

    return header + "\n" + "\n".join(lines)


# ================= START / HELP =================
@dp.message(Command("start"))
async def start_handler(message: Message):
    await upsert_user(message.from_user.id, message.from_user.full_name)
    await message.answer(
        "✅ Professional Quiz Bot 🇺🇿\n\n"
        "Shaxsiy quiz:\n"
        f"/quiz [1..{MAX_QUESTIONS}]  - test (default {DEFAULT_Q})\n\n"
        "Guruh umumiy quiz:\n"
        f"/gquiz [1..{MAX_QUESTIONS}] - hamma qatnashadigan test (default {DEFAULT_Q})\n"
        "/gstop - guruh quizni to‘xtatish\n\n"
        "Savollar:\n"
        "/add - savol qo‘shish (faqat private)\n"
        "/list - mening savollarim (faqat private, 20 tagacha)\n\n"
        "Reyting:\n"
        "/top - TOP 10\n"
        "/me - mening statistikam\n\n"
        "/cancel - jarayonni bekor qilish"
    )


@dp.message(Command("cancel"))
async def cancel_handler(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("✅ Bekor qilindi.")


# ================= TOP / ME =================
@dp.message(Command("top"))
async def top_handler(message: Message):
    rows = await get_top10()
    if not rows:
        await message.answer("Hali reyting yo‘q. Avval /quiz o‘ynang.")
        return

    text = "🏆 TOP 10 (umumiy ball)\n\n"
    medal = ["🥇", "🥈", "🥉"]
    for i, (name, user_id, total_score, games) in enumerate(rows, start=1):
        name = name or f"user:{user_id}"
        prefix = medal[i-1] if i <= 3 else f"{i})"
        text += f"{prefix} {name} — {total_score} ball ({games} ta test)\n"

    await message.answer(text)


@dp.message(Command("me"))
async def me_handler(message: Message):
    await upsert_user(message.from_user.id, message.from_user.full_name)
    games, total_score, total_q = await get_my_stats(message.from_user.id)
    avg = (total_score / games) if games else 0
    await message.answer(
        "📊 Mening statistikam\n\n"
        f"🎮 Testlar: {games}\n"
        f"✅ Jami ball: {total_score}\n"
        f"🧮 Jami savol: {total_q}\n"
        f"📈 O‘rtacha ball: {avg:.2f}\n"
    )


# ================= LIST (PRIVATE ONLY) =================
@dp.message(Command("list"), F.chat.type != "private")
async def list_block_in_groups(message: Message):
    await message.answer("❌ /list faqat botning shaxsiy chatida ishlaydi.")


@dp.message(Command("list"), F.chat.type == "private")
async def list_handler(message: Message):
    rows = await get_user_questions(message.from_user.id, limit=20)
    if not rows:
        await message.answer("Siz hali savol qo‘shmagansiz. /add bilan qo‘shing.")
        return

    text = "🧾 Mening savollarim (oxirgi 20 ta)\n\n"
    for q_id, q_text, correct in rows:
        short = q_text.replace("\n", " ")
        if len(short) > 40:
            short = short[:40] + "..."
        text += f"ID:{q_id} | ✅{correct} | {short}\n"

    await message.answer(text)


# ================= ADD FLOW (PRIVATE ONLY) =================
@dp.message(Command("add"), F.chat.type != "private")
async def add_block_in_groups(message: Message):
    await message.answer(
        "❌ Savol qo‘shish guruhda ishlamaydi.\n\n"
        "Savol qo‘shish uchun botning shaxsiy chatiga kiring va /add yozing."
    )


@dp.message(Command("add"), F.chat.type == "private")
async def add_start(message: Message, state: FSMContext):
    await upsert_user(message.from_user.id, message.from_user.full_name)
    await state.set_state(AddQuestion.question)
    await message.answer("Savol matnini yozing:")


@dp.message(AddQuestion.question, F.chat.type == "private")
async def add_question_text(message: Message, state: FSMContext):
    await state.update_data(question=message.text)
    await state.set_state(AddQuestion.a)
    await message.answer("A variantni yozing:")


@dp.message(AddQuestion.a, F.chat.type == "private")
async def add_a(message: Message, state: FSMContext):
    await state.update_data(a=message.text)
    await state.set_state(AddQuestion.b)
    await message.answer("B variantni yozing:")


@dp.message(AddQuestion.b, F.chat.type == "private")
async def add_b(message: Message, state: FSMContext):
    await state.update_data(b=message.text)
    await state.set_state(AddQuestion.c)
    await message.answer("C variantni yozing:")


@dp.message(AddQuestion.c, F.chat.type == "private")
async def add_c(message: Message, state: FSMContext):
    await state.update_data(c=message.text)
    await state.set_state(AddQuestion.d)
    await message.answer("D variantni yozing:")


@dp.message(AddQuestion.d, F.chat.type == "private")
async def add_d(message: Message, state: FSMContext):
    await state.update_data(d=message.text)
    await state.set_state(AddQuestion.correct)
    await message.answer("To‘g‘ri javobni kiriting (A/B/C/D):")


@dp.message(AddQuestion.correct, F.chat.type == "private")
async def add_correct(message: Message, state: FSMContext):
    correct = message.text.upper()
    if correct not in ["A", "B", "C", "D"]:
        await message.answer("Faqat A/B/C/D yozing!")
        return

    data = await state.get_data()

    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute("""
            INSERT INTO questions (user_id, question, a, b, c, d, correct)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            message.from_user.id,
            data["question"],
            data["a"],
            data["b"],
            data["c"],
            data["d"],
            correct
        ))
        await db.commit()

    await state.clear()
    await message.answer("✅ Savol saqlandi!")


# ================= PERSONAL QUIZ (INLINE + DISABLE, 1..20) =================
async def personal_send_question(chat_id: int, bot: Bot, state: FSMContext):
    data = await state.get_data()
    questions = data["questions"]
    index = data["index"]

    if index >= len(questions):
        score = data["score"]
        total = len(questions)
        user_id = data["user_id"]
        await save_result(user_id, score, total)

        percent = int(score * 100 / total) if total else 0
        await bot.send_message(
            chat_id,
            f"🏁 Test tugadi!\n\nNatija: {score}/{total} ({percent}%)\n\nReyting: /top\nStatistika: /me"
        )
        await state.clear()
        return

    _, question, a, b, c, d, _ = questions[index]
    text = (
        f"❓ Savol {index+1}/{len(questions)}\n\n"
        f"{question}\n\n"
        f"A) {a}\nB) {b}\nC) {c}\nD) {d}\n\n"
        "Javobni tanlang:"
    )

    await bot.send_message(chat_id, text, reply_markup=abcd_keyboard_personal())


@dp.message(Command("quiz"))
async def personal_quiz_start(message: Message, state: FSMContext, bot: Bot):
    await upsert_user(message.from_user.id, message.from_user.full_name)
    n = parse_n_from_text(message.text, default_n=DEFAULT_Q)

    rows = await get_random_questions(n)
    if not rows:
        await message.answer("Bazada savol yo‘q. Avval /add bilan qo‘shing.")
        return

    await state.update_data(user_id=message.from_user.id, questions=rows, index=0, score=0)
    await state.set_state(PersonalQuizState.answering)
    await personal_send_question(message.chat.id, bot, state)


@dp.callback_query(PersonalQuizState.answering, F.data.startswith("pans:"))
async def personal_on_answer(cb: CallbackQuery, state: FSMContext, bot: Bot):
    chosen = cb.data.split(":")[1]
    data = await state.get_data()
    questions = data["questions"]
    index = data["index"]
    score = data["score"]

    *_, correct = questions[index]

    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    if chosen == correct:
        score += 1
        await cb.message.answer("✅ To‘g‘ri!")
    else:
        await cb.message.answer(f"❌ Noto‘g‘ri. To‘g‘ri javob: {correct}")

    await state.update_data(index=index + 1, score=score)
    await cb.answer()
    await personal_send_question(cb.message.chat.id, bot, state)


# ================= GROUP QUIZ (UMUMIY, POLL-LIKE UI, TIMER, AUTO STOP) =================
async def group_finish(bot: Bot, chat_id: int):
    session = GROUP_SESSIONS.get(chat_id)
    if not session:
        return

    scores: dict[int, int] = session["scores"]
    total = session["total"]

    if not scores:
        await bot.send_message(chat_id, "🏁 Umumiy test tugadi! Hech kim javob bermadi 😅")
        GROUP_SESSIONS.pop(chat_id, None)
        return

    items = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    text = "🏁 Umumiy test tugadi!\n\n🏆 Natijalar:\n"
    medal = ["🥇", "🥈", "🥉"]
    for i, (uid, sc) in enumerate(items[:10], start=1):
        m = medal[i-1] if i <= 3 else f"{i})"
        name = session["names"].get(uid, f"user:{uid}")
        text += f"{m} {name} — {sc}/{total}\n"

    await bot.send_message(chat_id, text)

    for uid, sc in items:
        await save_result(uid, sc, total)

    GROUP_SESSIONS.pop(chat_id, None)


async def group_send_question(bot: Bot, chat_id: int):
    session = GROUP_SESSIONS.get(chat_id)
    if not session:
        return

    idx = session["index"]
    questions = session["questions"]
    if idx >= len(questions):
        await group_finish(bot, chat_id)
        return

    _, question, a, b, c, d, correct = questions[idx]
    session["current_correct"] = correct
    session["answered_users"] = set()

    # har savolda reset:
    session["answer_counts"] = {"A": 0, "B": 0, "C": 0, "D": 0}
    session["voters"] = {"A": [], "B": [], "C": [], "D": []}  # option -> [uid, uid...]

    text = (
        f"👥 UMUMIY QUIZ | Savol {idx+1}/{len(questions)}\n\n"
        f"{question}\n\n"
        f"A) {a}\nB) {b}\nC) {c}\nD) {d}\n\n"
        f"Javobni tanlang (⏳ {TIME_LIMIT_SECONDS}s):"
    )

    msg = await bot.send_message(chat_id, text, reply_markup=abcd_keyboard_group(chat_id, idx), parse_mode=None)
    session["last_msg_id"] = msg.message_id

    session["timer_token"] += 1
    token = session["timer_token"]
    session["timer_task"] = asyncio.create_task(group_timer(bot, chat_id, msg.message_id, token))


async def group_timer(bot: Bot, chat_id: int, message_id: int, token: int):
    await asyncio.sleep(TIME_LIMIT_SECONDS)

    session = GROUP_SESSIONS.get(chat_id)
    if not session:
        return
    if session.get("timer_token") != token:
        return
    if session.get("last_msg_id") != message_id:
        return

    # 3 savol ketma-ket javobsiz bo‘lsa STOP
    if len(session.get("answered_users", set())) == 0:
        session["no_answer_streak"] = session.get("no_answer_streak", 0) + 1
    else:
        session["no_answer_streak"] = 0

    if session["no_answer_streak"] >= NO_ANSWER_STOP_STREAK:
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
        except Exception:
            pass

        await bot.send_message(chat_id, "🛑 Quiz avtomatik to‘xtatildi.\n\n[ ilmga befarq bo'lmang ]")
        GROUP_SESSIONS.pop(chat_id, None)
        return

    # ✅ SAVOL XABARINI EDIT qilib, poll-like natija chiqaramiz
    result_text = build_poll_like_result_text(session)
    correct = session.get("current_correct", "?")
    q_index = session["index"]

    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=result_text,
            parse_mode="HTML",
            reply_markup=group_result_keyboard(chat_id, q_index, correct),
            disable_web_page_preview=True,
        )
    except Exception:
        # edit fail bo‘lsa, alohida xabar qilib yuboramiz
        await bot.send_message(chat_id, result_text, parse_mode="HTML")

    session["index"] += 1
    await group_send_question(bot, chat_id)


@dp.message(Command("gquiz"))
async def group_quiz_start(message: Message, bot: Bot):
    await upsert_user(message.from_user.id, message.from_user.full_name)

    chat_id = message.chat.id
    if chat_id in GROUP_SESSIONS:
        await message.answer("Bu guruhda umumiy quiz allaqachon ketayapti. To‘xtatish: /gstop")
        return

    n = parse_n_from_text(message.text, default_n=DEFAULT_Q)
    rows = await get_random_questions(n)
    if not rows:
        await message.answer("Bazada savol yo‘q. Avval /add bilan qo‘shing.")
        return

    GROUP_SESSIONS[chat_id] = {
        "started_at": datetime.utcnow().isoformat(),
        "questions": rows,
        "total": len(rows),
        "index": 0,
        "scores": {},
        "names": {},

        "answered_users": set(),
        "answer_counts": {"A": 0, "B": 0, "C": 0, "D": 0},
        "voters": {"A": [], "B": [], "C": [], "D": []},

        "current_correct": None,
        "last_msg_id": None,
        "timer_token": 0,
        "timer_task": None,
        "no_answer_streak": 0,
    }

    await message.answer(
        f"👥 Umumiy quiz boshlandi! Savollar: {len(rows)} ta\n"
        f"⏳ Har savolga: {TIME_LIMIT_SECONDS}s\n\n"
        "Hamma tugma bosib qatnashishi mumkin ✅"
    )

    await group_send_question(bot, chat_id)


@dp.message(Command("gstop"))
async def group_quiz_stop(message: Message, bot: Bot):
    chat_id = message.chat.id
    session = GROUP_SESSIONS.get(chat_id)
    if not session:
        await message.answer("Bu guruhda aktiv umumiy quiz yo‘q.")
        return

    task = session.get("timer_task")
    if task and not task.done():
        task.cancel()

    GROUP_SESSIONS.pop(chat_id, None)
    await message.answer("🛑 Umumiy quiz to‘xtatildi.")


@dp.callback_query(F.data.startswith("gans:"))
async def group_on_answer(cb: CallbackQuery, bot: Bot):
    # gans:<chat_id>:<q_index>:<A/B/C/D>
    parts = cb.data.split(":")
    chat_id = int(parts[1])
    q_index = int(parts[2])
    chosen = parts[3]

    session = GROUP_SESSIONS.get(chat_id)
    if not session:
        await cb.answer("Bu quiz tugagan yoki yo‘q ❌", show_alert=True)
        return

    # faqat hozirgi savol
    if q_index != session["index"]:
        await cb.answer("Bu eski savol ❌", show_alert=True)
        return

    uid = cb.from_user.id
    name = cb.from_user.full_name or "User"
    session["names"][uid] = name
    await upsert_user(uid, name)

    # 1 user 1 marta
    if uid in session["answered_users"]:
        await cb.answer("Siz javob bergansiz ✅", show_alert=True)
        return

    session["answered_users"].add(uid)

    # ✅ poll-like statistik uchun saqlaymiz
    if chosen in session["answer_counts"]:
        session["answer_counts"][chosen] += 1
        session["voters"][chosen].append(uid)

    # score
    if chosen == session["current_correct"]:
        session["scores"][uid] = session["scores"].get(uid, 0) + 1
        await cb.answer("✅ To‘g‘ri!", show_alert=False)
    else:
        await cb.answer("❌ Noto‘g‘ri", show_alert=False)


@dp.callback_query(F.data.startswith("gshow:"))
async def group_show_participants(cb: CallbackQuery, bot: Bot):
    # gshow:<chat_id>:<q_index>
    parts = cb.data.split(":")
    chat_id = int(parts[1])
    q_index = int(parts[2])

    session = GROUP_SESSIONS.get(chat_id)
    # session bo‘lmasligi mumkin (quiz davom etib ketgan yoki tugagan)
    # lekin biz hozirgi sessiondan ko‘rsatamiz. Bo‘lmasa xabar beramiz.
    if not session:
        await cb.answer("Ma’lumot topilmadi (quiz tugagan bo‘lishi mumkin).", show_alert=True)
        return

    # faqat o‘sha savolga mos bo‘lsa (hozir index allaqachon keyingiga o‘tgan bo‘lishi mumkin)
    # Shuning uchun qatnashchilarni “oxirgi natija” sifatida ko‘rsatamiz:
    names = session.get("names", {})
    voters = session.get("voters", {"A": [], "B": [], "C": [], "D": []})

    all_ids = []
    for opt in ["A", "B", "C", "D"]:
        all_ids.extend(voters.get(opt, []))

    # unique tartib bilan
    seen = set()
    uniq = []
    for uid in all_ids:
        if uid not in seen:
            seen.add(uid)
            uniq.append(uid)

    if not uniq:
        await cb.answer("Hech kim qatnashmagan.", show_alert=True)
        return

    # popup juda uzun bo‘lib ketmasin
    show = uniq[:30]
    text = "👥 Qatnashchilar:\n" + "\n".join(f"• {names.get(uid, f'user:{uid}')}" for uid in show)
    if len(uniq) > 30:
        text += f"\n… va yana {len(uniq)-30} ta"

    # alert uzun bo‘lsa kesiladi, shuning uchun alohida xabar ham yuboramiz:
    try:
        await cb.answer("Qatnashchilar ro‘yxati yuborildi ✅", show_alert=False)
    except Exception:
        pass

    await bot.send_message(chat_id, text)


# ================= MAIN =================
async def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN topilmadi (.env ni tekshiring)")

    await init_db()

    bot = Bot(token=TOKEN)

    # ✅ Telegram / menyu komandalar ro‘yxati
    commands = [
        BotCommand(command="start", description="Botni ishga tushirish"),
        BotCommand(command="quiz", description="Shaxsiy test (1..20)"),
        BotCommand(command="gquiz", description="Guruh umumiy test (1..20)"),
        BotCommand(command="gstop", description="Guruh testini to‘xtatish"),
        BotCommand(command="add", description="Savol qo‘shish (private)"),
        BotCommand(command="list", description="Mening savollarim (private)"),
        BotCommand(command="top", description="TOP 10 reyting"),
        BotCommand(command="me", description="Mening statistikam"),
        BotCommand(command="cancel", description="Jarayonni bekor qilish"),
    ]
    await bot.set_my_commands(commands)
    await bot.set_chat_menu_button(menu_button=MenuButtonCommands())

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
