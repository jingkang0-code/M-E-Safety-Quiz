import os, json, pathlib, random, datetime, logging
from telegram import Update, Poll
from telegram.ext import (
    Application, CommandHandler, PollAnswerHandler, ContextTypes
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
QUESTIONS_PATH = pathlib.Path("questions.json")

GROUP_QUIZ_LEN = 5
GROUP_Q_OPEN_PERIOD = 12  # seconds

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

def load_questions():
    if not QUESTIONS_PATH.exists():
        logging.error("questions.json not found")
        return None
    try:
        data = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data:
            logging.error("questions.json invalid or empty")
            return None
        return data
    except Exception as e:
        logging.exception("Failed to parse questions.json: %s", e)
        return None

QUIZ = load_questions()

# chat_id -> session
# {
#   "session_id": str,
#   "qids": [int],
#   "idx": int,
#   "scores": {uid: {"name": str, "score": int}},
#   "active_poll_id": str | None
# }
GROUP_SESSIONS = {}

# poll_id -> (chat_id, correct_option_id)
POLL_META = {}

def display_name(user) -> str:
    if getattr(user, "username", None):
        return f"@{user.username}"
    full = f"{getattr(user,'first_name','')} {getattr(user,'last_name','')}".strip()
    return full if full else str(user.id)

def format_scoreboard(scores: dict) -> str:
    rows = [(v["name"], int(v["score"])) for v in scores.values()]

    if not rows:
        return "🏁 Results\nNo scores recorded."

    # 1) Sort by score ASC (lowest first)
    rows.sort(key=lambda x: (x[1], x[0].lower()))

    # 2) Take bottom 10
    bottom_10 = rows[:10]

    # 3) Reverse display so lowest score is LAST
    bottom_10.reverse()

    out = ["🏁 Results (Bottom 10 — lowest score at bottom)"]
    for i, (name, score) in enumerate(bottom_10, 1):
        out.append(f"{i}. {name} — {score}")

    return "\n".join(out)



async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/quiz - start group quiz (5 Q, auto-advance)\n"
        "/leaderboard - show current session leaderboard\n"
        "/next - force advance to next question (admin fallback)\n"
        "/stopquiz - stop current session"
    )

async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    s = GROUP_SESSIONS.get(chat_id)
    if not s:
        await update.message.reply_text("No active quiz session.")
        return
    await update.message.reply_text(format_scoreboard(s["scores"]))

async def stopquiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in GROUP_SESSIONS:
        GROUP_SESSIONS.pop(chat_id, None)
        await update.message.reply_text("Quiz session stopped.")
    else:
        await update.message.reply_text("No active quiz session.")

async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    s = GROUP_SESSIONS.get(chat_id)
    if not s:
        await update.message.reply_text("No active quiz session.")
        return
    s["idx"] += 1
    await update.message.reply_text("Forcing advance to next question…")
    await send_next_question(context, chat_id, reason="manual")

async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not QUIZ:
        await update.message.reply_text("questions.json invalid.")
        return

    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("Use /quiz in a GROUP chat.")
        return

    chat_id = chat.id
    if chat_id in GROUP_SESSIONS:
        await update.message.reply_text("A group quiz session is already in progress.")
        return

    qcount = min(GROUP_QUIZ_LEN, len(QUIZ))
    qids = random.sample(range(len(QUIZ)), k=qcount)
    session_id = f"{chat_id}:{int(datetime.datetime.now().timestamp())}"

    GROUP_SESSIONS[chat_id] = {
        "session_id": session_id,
        "qids": qids,
        "idx": 0,
        "scores": {},
        "active_poll_id": None,
    }

    await update.message.reply_text(
        f"Starting group quiz: {qcount} questions.\n"
        f"Auto-advance every {GROUP_Q_OPEN_PERIOD}s (does NOT wait for answers)."
    )
    await send_next_question(context, chat_id, reason="start")

async def send_next_question(context: ContextTypes.DEFAULT_TYPE, chat_id: int, reason: str):
    s = GROUP_SESSIONS.get(chat_id)
    if not s:
        return

    if s["idx"] >= len(s["qids"]):
        await context.bot.send_message(chat_id=chat_id, text=format_scoreboard(s["scores"]))
        GROUP_SESSIONS.pop(chat_id, None)
        return

    idx = s["idx"]
    qid = s["qids"][idx]
    q = QUIZ[qid]

    order = list(range(len(q["opts"])))
    random.shuffle(order)
    options = [q["opts"][i] for i in order]
    correct_option_id = order.index(q["answer"])

    logging.info("SEND Q%d/%d chat=%s reason=%s", idx+1, len(s["qids"]), chat_id, reason)

    msg = await context.bot.send_poll(
        chat_id=chat_id,
        question=f"Q{idx+1}/{len(s['qids'])}: {q['q']}",
        options=options,
        type=Poll.QUIZ,
        correct_option_id=correct_option_id,
        is_anonymous=False,
        open_period=GROUP_Q_OPEN_PERIOD,
    )

    s["active_poll_id"] = msg.poll.id
    POLL_META[msg.poll.id] = (chat_id, correct_option_id)

    # schedule forced advance (THIS is the key)
    try:
        context.job_queue.run_once(
            force_advance_job,
            when=GROUP_Q_OPEN_PERIOD + 1,
            data={"chat_id": chat_id, "poll_id": msg.poll.id, "idx": idx},
            name=f"adv_{chat_id}_{idx}"
        )
        logging.info("SCHEDULE ADVANCE chat=%s idx=%s poll=%s", chat_id, idx, msg.poll.id)
    except Exception:
        logging.exception("FAILED to schedule advance job (job-queue not installed?)")
        await context.bot.send_message(chat_id=chat_id, text="⚠️ Internal error: timer not scheduled.")

async def force_advance_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    poll_id = data.get("poll_id")
    idx = data.get("idx")

    s = GROUP_SESSIONS.get(chat_id)
    if not s:
        logging.info("JOB fired but no session chat=%s", chat_id)
        return

    # Only advance if we are still on the same poll/question
    if s.get("active_poll_id") != poll_id or s.get("idx") != idx:
        logging.info("JOB ignored (session moved) chat=%s idx=%s active=%s job_poll=%s",
                     chat_id, s.get("idx"), s.get("active_poll_id"), poll_id)
        return

    logging.info("JOB ADVANCE chat=%s idx=%s poll=%s", chat_id, idx, poll_id)
    s["idx"] += 1
    await send_next_question(context, chat_id, reason="timer")

async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    chosen = ans.option_ids[0] if ans.option_ids else None

    meta = POLL_META.get(ans.poll_id)
    if not meta:
        return
    chat_id, correct = meta

    s = GROUP_SESSIONS.get(chat_id)
    if not s:
        return

    if chosen == correct:
        uid = str(ans.user.id)
        entry = s["scores"].setdefault(uid, {"name": display_name(ans.user), "score": 0})
        entry["name"] = display_name(ans.user)
        entry["score"] += 1

def main():
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN not set")
        return
    if not QUIZ:
        logging.error("QUIZ not loaded")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("quiz", quiz_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(CommandHandler("stopquiz", stopquiz_cmd))

    app.add_handler(PollAnswerHandler(on_poll_answer))

    logging.info("Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()


