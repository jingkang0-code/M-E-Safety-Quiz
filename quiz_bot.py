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
# IMPORTANT: Do NOT hardcode your token in GitHub.
# Set BOT_TOKEN as an environment variable in your host (Railway/Render/etc.).
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

QUESTIONS_PATH = pathlib.Path("questions.json")
SCORES_PATH = pathlib.Path("scores.json")

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
# Private mode (DM): per-user sequential quiz
USER_STATE = defaultdict(dict)  # key: user_id -> state dict
POLL_TO_PRIVATE = {}  # poll_id -> (user_id, qid, correct_option_id)

# Group mode leaderboard:
# poll_id -> {"chat_id": int, "correct_option_id": int}
POLL_TO_GROUP = {}

# ===================== SCORE STORAGE =====================
# Stored as:
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
    # Prefer @username, else full name
    if getattr(user, "username", None):
        return f"@{user.username}"
    name = f"{getattr(user, 'first_name', '')} {getattr(user, 'last_name', '')}".strip()
    return name if name else str(getattr(user, "id", "unknown"))

def add_group_point(chat_id: int, user, delta: int = 1) -> None:
    scores = _load_scores()
    c = scores.setdefault(str(chat_id), {})
    uid = str(user.id)
    entry = c.setdefault(uid, {"name": _display_name_from_user(user), "score": 0})
    entry["name"] = _display_name_from_user(user)  # keep updated
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

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Ready.\n"
        "/quiz - start quiz (DM = full quiz, Group = single question)\n"
        "/retest - retry only wrong ones (DM only)\n"
        "/score - last DM quiz score\n"
        "/leaderboard - group leaderboard\n"
        "/reset_scores - reset group leaderboard (admin-controlled by you if you want)\n"
        "/help - this help"
    )

help_cmd = start_cmd

# ===================== DM MODE COMMANDS =====================
async def score_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user.id
    st = USER_STATE.get(u, {})
    last = st.get("last_score")
    if last:
        await update.message.reply_text(f"📊 Last: {last['correct']}/{last['total']} on {last['time']}")
    else:
        await update.message.reply_text("No attempts yet. Use /quiz to start (in DM).")

async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Behaviour:
    - If private chat: full sequential quiz (your original design).
    - If group chat: send ONE question to group (leaderboard mode).
    """
    if not QUIZ:
        await update.message.reply_text("❌ questions.json invalid. Fix it and redeploy.")
        return

    chat = update.effective_chat
    user = update.effective_user

    # GROUP MODE: one question to entire chat
    if chat.type != "private":
        qid = random.randrange(len(QUIZ))
        q = QUIZ[qid]

        idxs = list(range(len(q["opts"])))
        random.shuffle(idxs)
        opts = [q["opts"][i] for i in idxs]
        correct_option_id = idxs.index(q["answer"])

        msg = await context.bot.send_poll(
            chat_id=chat.id,
            question=q["q"],
            options=opts,
            type=Poll.QUIZ,
            correct_option_id=correct_option_id,
            is_anonymous=False,
        )
        POLL_TO_GROUP[msg.poll.id] = {
            "chat_id": chat.id,
            "correct_option_id": correct_option_id,
        }
        await update.message.reply_text("Question posted. Use /leaderboard anytime.")
        return

    # PRIVATE MODE: full quiz per user (your current flow)
    u = user.id
    USER_STATE[u] = {
        "order": new_order(),
        "idx": 0,
        "wrong_ids": set(),
        "correct_count": 0,
        "total": len(QUIZ),
        "mode": "full",
    }
    await send_next_private(update, context, u)

async def retest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Retest only supported in private flow
    if update.effective_chat.type != "private":
        await update.message.reply_text("Retest is DM-only. Please DM the bot to use /retest.")
        return

    u = update.effective_user.id
    prev_wrong = list(USER_STATE.get(u, {}).get("wrong_ids", []))
    if not prev_wrong:
        await update.message.reply_text("✅ Nothing to retest. Run /quiz first.")
        return

    random.shuffle(prev_wrong)
    USER_STATE[u] = {
        "order": prev_wrong,
        "idx": 0,
        "wrong_ids": set(),
        "correct_count": 0,
        "total": len(prev_wrong),
        "mode": "retest",
    }
    await send_next_private(update, context, u)

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

# ===================== GROUP LEADERBOARD COMMANDS =====================
async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rows = get_group_leaderboard(chat_id, limit=10)
    if not rows:
        await update.message.reply_text("No scores yet. Use /quiz in this group to post a question.")
        return

    lines = ["🏆 Leaderboard (Top 10)"]
    for i, (name, score) in enumerate(rows, start=1):
        lines.append(f"{i}. {name} — {score}")
    await update.message.reply_text("\n".join(lines))

async def reset_scores_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Minimal version: anyone can reset.
    # If you want admin-only, tell me and I’ll add Telegram admin checks.
    chat = update.effective_chat
    if chat.type == "private":
        await update.message.reply_text("This resets group leaderboard only. Use it in a group chat.")
        return

    reset_group_scores(chat.id)
    await update.message.reply_text("✅ Group leaderboard reset.")

# ===================== POLL ANSWERS HANDLER =====================
async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    chosen = ans.option_ids[0] if ans.option_ids else None

    # 1) GROUP MODE scoring
    meta = POLL_TO_GROUP.get(ans.poll_id)
    if meta:
        if chosen == meta["correct_option_id"]:
            add_group_point(meta["chat_id"], ans.user, delta=1)
        return

    # 2) PRIVATE MODE scoring (your original flow)
    entry = POLL_TO_PRIVATE.pop(ans.poll_id, None)
    if entry is None:
        # Bot restarted or poll mapping lost; ignore safely.
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

    print("✅ Bot running... open Telegram and DM /start to your bot.")
    app.run_polling()

if __name__ == "__main__":
    main()
