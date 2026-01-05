import os, json, pathlib, random, datetime, logging
from collections import defaultdict

from telegram import Update, Poll
from telegram.ext import (
    Application,
    CommandHandler,
    PollAnswerHandler,
    ContextTypes,
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
QUESTIONS_PATH = pathlib.Path("questions.json")

GROUP_QUIZ_LEN = 5
GROUP_Q_OPEN_PERIOD = 10  # seconds per question

logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO)

def load_questions():
    if not QUESTIONS_PATH.exists():
        print("❌ questions.json not found.")
        return None
    try:
        data = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data:
            print("❌ questions.json invalid.")
            return None
        return data
    except Exception as e:
        print("❌ Failed to parse questions.json:", e)
        return None

QUIZ = load_questions()

# -------- DM state (optional; kept minimal) --------
USER_STATE = defaultdict(dict)
POLL_TO_PRIVATE = {}

# -------- Group session state --------
# poll_id -> {"chat_id": int, "session_id": str, "correct_option_id": int}
POLL_TO_GROUP = {}

# chat_id -> session
# {
#   "session_id": str,
#   "qids": [int],
#   "idx": int,
#   "scores": {uid: {"name": str, "score": int}},
#   "advance_token": str
# }
GROUP_SESSIONS = {}

def _display_name(user) -> str:
    if getattr(user, "username", None):
        return f"@{user.username}"
    name = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
    return name if name else str(user.id)

def _format_scoreboard(scores: dict, title="🏁 Session Results (Top 10)", limit=10) -> str:
    rows = [(v["name"], int(v["score"])) for v in scores.values()]
    rows.sort(key=lambda x: (-x[1], x[0].lower()))
    if not rows:
        return f"{title}\nNo scores recorded."
    lines = [title]
    for i, (name, score) in enumerate(rows[:limit], 1):
        lines.append(f"{i}. {name} — {score}")
    return "\n".join(lines)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ready.\n"
        "/quiz - start quiz (Group: 5 questions, auto-advance)\n"
        "/help - help"
    )

help_cmd = start_cmd

# ---------------- GROUP QUIZ ----------------
async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not QUIZ:
        await update.message.reply_text("❌ questions.json invalid.")
        return

    chat = update.effective_chat

    # GROUP mode
    if chat.type != "private":
        if chat.id in GROUP_SESSIONS:
            await update.message.reply_text("A group quiz session is already in progress.")
            return

        session_id = f"{chat.id}:{int(datetime.datetime.now().timestamp())}"
        qcount = min(GROUP_QUIZ_LEN, len(QUIZ))
        qids = random.sample(range(len(QUIZ)), k=qcount)

        GROUP_SESSIONS[chat.id] = {
            "session_id": session_id,
            "qids": qids,
            "idx": 0,
            "scores": {},
            "advance_token": "",
        }

        await update.message.reply_text(
            f"Starting group quiz: {qcount} questions.\nEach question auto-advances after {GROUP_Q_OPEN_PERIOD}s."
        )

        await _send_next_group_question(context, chat.id)
        return

    # DM mode (optional)
    await update.message.reply_text("DM mode not enabled in this minimal build. Use in a group chat.")

async def _send_next_group_question(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    session = GROUP_SESSIONS.get(chat_id)
    if not session:
        return

    idx = session["idx"]
    if idx >= len(session["qids"]):
        await context.bot.send_message(chat_id=chat_id, text=_format_scoreboard(session["scores"]))
        GROUP_SESSIONS.pop(chat_id, None)
        return

    qid = session["qids"][idx]
    q = QUIZ[qid]

    idxs = list(range(len(q["opts"])))
    random.shuffle(idxs)
    opts = [q["opts"][i] for i in idxs]
    correct_option_id = idxs.index(q["answer"])

    # token prevents old jobs from advancing the wrong question
    advance_token = f"{session['session_id']}:{idx}"
    session["advance_token"] = advance_token

    msg = await context.bot.send_poll(
        chat_id=chat_id,
        question=f"Q{idx+1}/{len(session['qids'])}: {q['q']}",
        options=opts,
        type=Poll.QUIZ,
        correct_option_id=correct_option_id,
        is_anonymous=False,
        open_period=GROUP_Q_OPEN_PERIOD,  # closes for users after 10s
    )

    POLL_TO_GROUP[msg.poll.id] = {
        "chat_id": chat_id,
        "session_id": session["session_id"],
        "correct_option_id": correct_option_id,
    }

    # THIS is the real auto-advance (server-side timer)
    context.job_queue.run_once(
        _force_advance_job,
        when=GROUP_Q_OPEN_PERIOD + 1,
        data={"chat_id": chat_id, "advance_token": advance_token},
        name=f"advance_{chat_id}_{advance_token}",
    )

async def _force_advance_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    token = data.get("advance_token")

    session = GROUP_SESSIONS.get(chat_id)
    if not session:
        return

    # only advance if still on the same question
    if session.get("advance_token") != token:
        return

    session["idx"] += 1
    await _send_next_group_question(context, chat_id)

# ---------------- SCORING (does NOT control progression) ----------------
async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    chosen = ans.option_ids[0] if ans.option_ids else None

    meta = POLL_TO_GROUP.get(ans.poll_id)
    if not meta:
        return

    chat_id = meta["chat_id"]
    session = GROUP_SESSIONS.get(chat_id)
    if not session or session.get("session_id") != meta["session_id"]:
        return

    if chosen == meta["correct_option_id"]:
        uid = str(ans.user.id)
        entry = session["scores"].setdefault(uid, {"name": _display_name(ans.user), "score": 0})
        entry["name"] = _display_name(ans.user)
        entry["score"] += 1

def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN not set.")
        return
    if not QUIZ:
        print("❌ Fix questions.json and redeploy.")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("quiz", quiz_cmd))

    app.add_handler(PollAnswerHandler(on_poll_answer))

    print("✅ Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
