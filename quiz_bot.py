import os, json, pathlib, random, datetime, logging
from collections import defaultdict

from telegram import Update, Poll
from telegram.ext import (
    Application, CommandHandler, PollAnswerHandler, PollHandler, ContextTypes
)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
QUESTIONS_PATH = pathlib.Path("questions.json")
SCORES_PATH = pathlib.Path("scores.json")

GROUP_QUIZ_LEN = 5
GROUP_Q_OPEN_PERIOD = 10  # 10s, then Telegram closes poll automatically

logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s", level=logging.INFO)

def load_questions():
    if not QUESTIONS_PATH.exists():
        print("❌ questions.json not found.")
        return None
    data = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    return data if isinstance(data, list) and data else None

QUIZ = load_questions()

# -------- DM state --------
USER_STATE = defaultdict(dict)
POLL_TO_PRIVATE = {}

# -------- Group state --------
# poll_id -> {"chat_id": int, "session_id": str, "q_index": int, "correct_option_id": int}
GROUP_POLL_META = {}

# chat_id -> {"session_id": str, "qids": [int], "idx": int, "scores": {uid: {"name":str,"score":int}}}
GROUP_SESSIONS = {}

def _display_name(user) -> str:
    if getattr(user, "username", None):
        return f"@{user.username}"
    name = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
    return name if name else str(user.id)

def _format_scoreboard(entries: dict, title="🏁 Session Results (Top 10)", limit=10) -> str:
    rows = [(v["name"], int(v["score"])) for v in entries.values()]
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
        "/quiz - DM full quiz; Group timed 5 questions\n"
        "/score - DM last score\n"
        "/help - help"
    )
help_cmd = start_cmd

# ---------------- DM mode ----------------
def new_order():
    return random.sample(range(len(QUIZ)), k=len(QUIZ))

async def score_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user.id
    last = USER_STATE.get(u, {}).get("last_score")
    if last:
        await update.message.reply_text(f"Last: {last['correct']}/{last['total']} on {last['time']}")
    else:
        await update.message.reply_text("No attempts yet. Use /quiz in DM.")

async def send_next_private(context: ContextTypes.DEFAULT_TYPE, uid: int):
    st = USER_STATE[uid]
    if st["idx"] >= len(st["order"]):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        result = {"correct": st["correct_count"], "total": st["total"], "time": ts}
        st["last_score"] = result
        await context.bot.send_message(chat_id=uid, text=f"✅ Score: {st['correct_count']}/{st['total']}")
        return

    qid = st["order"][st["idx"]]
    q = QUIZ[qid]
    idxs = list(range(len(q["opts"])))
    random.shuffle(idxs)
    opts = [q["opts"][i] for i in idxs]
    correct_option_id = idxs.index(q["answer"])

    msg = await context.bot.send_poll(
        chat_id=uid,
        question=q["q"],
        options=opts,
        type=Poll.QUIZ,
        correct_option_id=correct_option_id,
        is_anonymous=False,
    )
    POLL_TO_PRIVATE[msg.poll.id] = (uid, qid, correct_option_id)

# ---------------- Group mode ----------------
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

    msg = await context.bot.send_poll(
        chat_id=chat_id,
        question=f"Q{idx+1}/{len(session['qids'])}: {q['q']}",
        options=opts,
        type=Poll.QUIZ,
        correct_option_id=correct_option_id,
        is_anonymous=False,
        open_period=GROUP_Q_OPEN_PERIOD,  # Telegram closes after 10s
    )

    GROUP_POLL_META[msg.poll.id] = {
        "chat_id": chat_id,
        "session_id": session["session_id"],
        "q_index": idx,
        "correct_option_id": correct_option_id,
    }

async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not QUIZ:
        await update.message.reply_text("❌ questions.json invalid.")
        return

    chat = update.effective_chat

    # GROUP
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
        }

        await update.message.reply_text(
            f"Starting group quiz: {qcount} questions.\nEach question auto-advances after {GROUP_Q_OPEN_PERIOD}s."
        )
        await _send_next_group_question(context, chat.id)
        return

    # DM
    uid = update.effective_user.id
    USER_STATE[uid] = {
        "order": new_order(),
        "idx": 0,
        "correct_count": 0,
        "total": len(QUIZ),
    }
    await send_next_private(context, uid)

# Score when users answer (group + DM)
async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    chosen = ans.option_ids[0] if ans.option_ids else None

    # GROUP scoring
    meta = GROUP_POLL_META.get(ans.poll_id)
    if meta:
        chat_id = meta["chat_id"]
        session = GROUP_SESSIONS.get(chat_id)
        if not session or session["session_id"] != meta["session_id"]:
            return

        if chosen == meta["correct_option_id"]:
            uid = str(ans.user.id)
            entry = session["scores"].setdefault(uid, {"name": _display_name(ans.user), "score": 0})
            entry["name"] = _display_name(ans.user)
            entry["score"] += 1
        return

    # DM scoring
    entry = POLL_TO_PRIVATE.pop(ans.poll_id, None)
    if not entry:
        return
    uid, qid, correct = entry
    st = USER_STATE.get(uid)
    if not st:
        return
    if chosen == correct:
        st["correct_count"] += 1
    st["idx"] += 1
    await send_next_private(context, uid)

# AUTO-ADVANCE when poll closes (this is the key fix)
async def on_poll_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    poll = update.poll
    if not poll or not poll.is_closed:
        return

    meta = GROUP_POLL_META.pop(poll.id, None)
    if not meta:
        return

    chat_id = meta["chat_id"]
    session = GROUP_SESSIONS.get(chat_id)
    if not session or session["session_id"] != meta["session_id"]:
        return

    # advance exactly one step from the poll that just closed
    if session["idx"] == meta["q_index"]:
        session["idx"] += 1
        await _send_next_group_question(context, chat_id)

def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN not set.")
        return
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("quiz", quiz_cmd))
    app.add_handler(CommandHandler("score", score_cmd))

    app.add_handler(PollAnswerHandler(on_poll_answer))
    app.add_handler(PollHandler(on_poll_update))  # <-- auto-advance trigger

    print("✅ Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
