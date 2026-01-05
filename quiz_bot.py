import os
import json
import pathlib
import random
import datetime
import logging
from collections import defaultdict

from telegram import Update, Poll
from telegram.ext import (
    Application,
    CommandHandler,
    PollAnswerHandler,
    ContextTypes,
)

# ===================== CONFIG =====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
QUESTIONS_PATH = pathlib.Path("questions.json")
SCORES_PATH = pathlib.Path("scores.json")

# Group session settings
GROUP_QUIZ_LEN = 5               # number of questions per group session
GROUP_Q_OPEN_PERIOD = 10         # seconds each poll stays open (auto closes)

logging.basicConfig(
    format="%(asctime)s %(levelname)s: %(message)s",
    level=logging.INFO,
)

# ===================== LOAD QUESTIONS =====================
def load_questions():
    if not QUESTIONS_PATH.exists():
        print("❌ questions.json not found.")
        return None
    try:
        data = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list) or not data:
            print("❌ questions.json is empty or not a list.")
            return None
        for i, q in enumerate(data):
            if not {"q", "opts", "answer"} <= set(q):
                print(f"❌ Question #{i+1} missing keys (q, opts, answer).")
                return None
            if not isinstance(q["opts"], list) or len(q["opts"]) < 2:
                print(f"❌ Question #{i+1} needs 2+ options.")
                return None
            if not isinstance(q["answer"], int) or not (0 <= q["answer"] < len(q["opts"])):
                print(f"❌ Question #{i+1} has invalid answer index.")
                return None
        print(f"✅ Loaded {len(data)} questions.")
        return data
    except Exception as e:
        print("❌ Failed to parse questions.json:", e)
        return None


QUIZ = load_questions()

# ===================== STATE =====================
# DM sequential quiz state
USER_STATE = defaultdict(dict)          # user_id -> state dict
POLL_TO_PRIVATE = {}                   # poll_id -> (user_id, qid, correct_option_id)

# Group polling mapping:
# poll_id -> {"chat_id": int, "correct_option_id": int, "session_id": str}
POLL_TO_GROUP = {}

# Active group sessions:
# chat_id -> {
#   "session_id": str,
#   "qids": [int...],
#   "idx": int,
#   "scores": {user_id(str): {"name": str, "score": int}},
# }
GROUP_SESSIONS = {}

# ===================== SCORE STORAGE (optional cumulative leaderboard) =====================
# scores.json structure:
# {
#   "<chat_id>": {
#       "<user_id>": {"name": "display name", "score": 12}
#   }
# }
def _load_scores() -> dict:
    if not SCORES_PATH.exists():
        return {}
    try:
        return json.loads(SCORES_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_scores(scores: dict) -> None:
    SCORES_PATH.write_text(json.dumps(scores, ensure_ascii=False, indent=2), encoding="utf-8")

def _display_name_from_user(user) -> str:
    if getattr(user, "username", None):
        return f"@{user.username}"
    name = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
    return name if name else str(getattr(user, "id", "unknown"))

def add_group_point_cumulative(chat_id: int, user, delta: int = 1) -> None:
    scores = _load_scores()
    c = scores.setdefault(str(chat_id), {})
    uid = str(user.id)
    entry = c.setdefault(uid, {"name": _display_name_from_user(user), "score": 0})
    entry["name"] = _display_name_from_user(user)
    entry["score"] = int(entry.get("score", 0)) + delta
    _save_scores(scores)

def get_group_leaderboard(chat_id: int, limit: int = 10):
    scores = _load_scores()
    c = scores.get(str(chat_id), {})
    rows = [(v.get("name", k), int(v.get("score", 0))) for k, v in c.items()]
    rows.sort(key=lambda x: (-x[1], x[0].lower()))
    return rows[:limit]

def reset_group_scores(chat_id: int) -> None:
    scores = _load_scores()
    if str(chat_id) in scores:
        del scores[str(chat_id)]
        _save_scores(scores)

# ===================== HELPERS =====================
def new_order():
    return random.sample(range(len(QUIZ)), k=len(QUIZ))

def _format_scoreboard(title: str, entries: dict, limit: int = 10) -> str:
    """
    entries: {user_id: {"name": str, "score": int}}
    """
    rows = [(v["name"], int(v["score"])) for v in entries.values()]
    rows.sort(key=lambda x: (-x[1], x[0].lower()))
    if not rows:
        return f"{title}\nNo scores recorded."
    lines = [title]
    for i, (name, score) in enumerate(rows[:limit], start=1):
        lines.append(f"{i}. {name} — {score}")
    return "\n".join(lines)

# ===================== COMMANDS =====================
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ready.\n"
        "/quiz - start quiz (DM: full quiz, Group: 5 questions)\n"
        "/retest - retry only wrong ones (DM only)\n"
        "/score - last DM quiz score\n"
        "/leaderboard - cumulative group leaderboard\n"
        "/reset_scores - reset cumulative group leaderboard\n"
        "/help - this help"
    )

help_cmd = start_cmd

# ===================== DM MODE =====================
async def score_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user.id
    st = USER_STATE.get(u, {})
    last = st.get("last_score")
    if last:
        await update.message.reply_text(f"📊 Last: {last['correct']}/{last['total']} on {last['time']}")
    else:
        await update.message.reply_text("No attempts yet. Use /quiz to start (in DM).")

async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not QUIZ:
        await update.message.reply_text("❌ questions.json invalid. Fix it and redeploy.")
        return

    chat = update.effective_chat
    user = update.effective_user

    # ===================== GROUP MODE (5-question session) =====================
    if chat.type != "private":
        # Block if session already running
        if chat.id in GROUP_SESSIONS:
            await update.message.reply_text("A group quiz session is already in progress. Please wait for it to finish.")
            return

        session_id = f"{chat.id}:{int(datetime.datetime.now().timestamp())}"
        qcount = min(GROUP_QUIZ_LEN, len(QUIZ))
        qids = random.sample(range(len(QUIZ)), k=qcount)

        GROUP_SESSIONS[chat.id] = {
            "session_id": session_id,
            "qids": qids,
            "idx": 0,
            "scores": {},  # session-only scoreboard
        }

        await update.message.reply_text(f"Starting group quiz: {qcount} questions. Each question is open for {GROUP_Q_OPEN_PERIOD}s.")
        await _send_next_group_question(context, chat.id)
        return

    # ===================== PRIVATE MODE (full quiz) =====================
    uid = user.id
    USER_STATE[uid] = {
        "order": new_order(),
        "idx": 0,
        "wrong_ids": set(),
        "correct_count": 0,
        "total": len(QUIZ),
        "mode": "full",
    }
    await send_next_private(update, context, uid)

async def retest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Retest is DM-only. Please DM the bot to use /retest.")
        return

    uid = update.effective_user.id
    prev_wrong = list(USER_STATE.get(uid, {}).get("wrong_ids", []))
    if not prev_wrong:
        await update.message.reply_text("✅ Nothing to retest. Run /quiz first.")
        return

    random.shuffle(prev_wrong)
    USER_STATE[uid] = {
        "order": prev_wrong,
        "idx": 0,
        "wrong_ids": set(),
        "correct_count": 0,
        "total": len(prev_wrong),
        "mode": "retest",
    }
    await send_next_private(update, context, uid)

async def send_next_private(update_or_ctx, context: ContextTypes.DEFAULT_TYPE, uid: int):
    st = USER_STATE[uid]

    if st["idx"] >= len(st["order"]):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        result = {"correct": st["correct_count"], "total": st["total"], "time": ts}
        USER_STATE[uid]["last_score"] = result
        USER_STATE[uid].setdefault("history", []).append(result)

        await context.bot.send_message(
            chat_id=uid,
            text=f"✅ Score: {st['correct_count']}/{st['total']}\n"
                 f"Use /retest to try the {len(st['wrong_ids'])} you missed.",
        )
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

# ===================== GROUP MODE HELPERS =====================
async def _send_next_group_question(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    session = GROUP_SESSIONS.get(chat_id)
    if not session:
        return

    idx = session["idx"]
    qids = session["qids"]

    # Finish session
    if idx >= len(qids):
        # post session scoreboard
        scoreboard = _format_scoreboard("🏁 Session Results (Top 10)", session["scores"], limit=10)
        await context.bot.send_message(chat_id=chat_id, text=scoreboard)
        await context.bot.send_message(chat_id=chat_id, text="Use /leaderboard to view cumulative scores.")
        # clear session
        GROUP_SESSIONS.pop(chat_id, None)
        return

    qid = qids[idx]
    q = QUIZ[qid]

    idxs = list(range(len(q["opts"])))
    random.shuffle(idxs)
    opts = [q["opts"][i] for i in idxs]
    correct_option_id = idxs.index(q["answer"])

    msg = await context.bot.send_poll(
        chat_id=chat_id,
        question=f"Q{idx+1}/{len(qids)}: {q['q']}",
        options=opts,
        type=Poll.QUIZ,
        correct_option_id=correct_option_id,
        is_anonymous=False,
        open_period=GROUP_Q_OPEN_PERIOD,   # auto close after N seconds
    )

    POLL_TO_GROUP[msg.poll.id] = {
        "chat_id": chat_id,
        "correct_option_id": correct_option_id,
        "session_id": session["session_id"],
    }

    # schedule next question after poll closes (+1s buffer)
    context.job_queue.run_once(
        _group_next_question_job,
        when=GROUP_Q_OPEN_PERIOD + 1,
        data={"chat_id": chat_id, "session_id": session["session_id"]},
        name=f"group_next_{chat_id}_{session['session_id']}_{idx}",
    )

async def _group_next_question_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    chat_id = data.get("chat_id")
    session_id = data.get("session_id")

    session = GROUP_SESSIONS.get(chat_id)
    # session might have ended or restarted
    if not session or session.get("session_id") != session_id:
        return

    session["idx"] += 1
    await _send_next_group_question(context, chat_id)

# ===================== GROUP LEADERBOARD COMMANDS =====================
async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = get_group_leaderboard(chat_id, limit=10)
    if not rows:
        await update.message.reply_text("No cumulative scores yet. Run /quiz in this group and answer questions.")
        return

    lines = ["🏆 Cumulative Leaderboard (Top 10)"]
    for i, (name, score) in enumerate(rows, start=1):
        lines.append(f"{i}. {name} — {score}")
    await update.message.reply_text("\n".join(lines))

async def reset_scores_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("This resets group cumulative leaderboard only. Use it in a group chat.")
        return

    reset_group_scores(chat.id)
    await update.message.reply_text("✅ Cumulative group leaderboard reset.")

# ===================== POLL ANSWERS =====================
async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    chosen = ans.option_ids[0] if ans.option_ids else None

    # GROUP MODE scoring
    meta = POLL_TO_GROUP.get(ans.poll_id)
    if meta:
        chat_id = meta["chat_id"]
        correct = meta["correct_option_id"]
        session_id = meta["session_id"]

        session = GROUP_SESSIONS.get(chat_id)
        # only count if still same session
        if session and session.get("session_id") == session_id and chosen == correct:
            uid = str(ans.user.id)
            entry = session["scores"].setdefault(uid, {"name": _display_name_from_user(ans.user), "score": 0})
            entry["name"] = _display_name_from_user(ans.user)
            entry["score"] += 1

            # also update cumulative leaderboard (optional)
            add_group_point_cumulative(chat_id, ans.user, delta=1)

        return

    # PRIVATE MODE scoring
    entry = POLL_TO_PRIVATE.pop(ans.poll_id, None)
    if entry is None:
        return

    uid, qid, correct = entry
    st = USER_STATE.get(uid)
    if not st:
        return

    if chosen == correct:
        st["correct_count"] += 1
    else:
        st["wrong_ids"].add(qid)

    st["idx"] += 1
    await send_next_private(update, context, uid)

# ===================== MAIN =====================
def main():
    if not BOT_TOKEN:
        print("❌ BOT_TOKEN not set. Set environment variable BOT_TOKEN and redeploy.")
        return
    if not QUIZ:
        print("❌ Fix questions.json and run again.")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(CommandHandler("quiz", quiz_cmd))
    app.add_handler(CommandHandler("retest", retest_cmd))
    app.add_handler(CommandHandler("score", score_cmd))

    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("reset_scores", reset_scores_cmd))

    app.add_handler(PollAnswerHandler(on_poll_answer))

    print("✅ Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
