import sqlite3

DB_PATH = "leaderboard.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS scores (
        chat_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT,
        score INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (chat_id, user_id)
    )
    """)
    conn.commit()
    conn.close()

def add_point(chat_id: int, user_id: int, username: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO scores (chat_id, user_id, username, score)
    VALUES (?, ?, ?, 1)
    ON CONFLICT(chat_id, user_id)
    DO UPDATE SET score = score + 1, username = excluded.username
    """, (chat_id, user_id, username))
    conn.commit()
    conn.close()

def get_top(chat_id: int, limit: int = 10):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    SELECT username, score FROM scores
    WHERE chat_id = ?
    ORDER BY score DESC, username ASC
    LIMIT ?
    """, (chat_id, limit))
    rows = cur.fetchall()
    conn.close()
    return rows

ACTIVE_POLLS = {}  # poll_id -> {"chat_id": int, "correct_option_id": int}




import json, pathlib, random, datetime, logging
from collections import defaultdict
from telegram import Update, Poll
from telegram.ext import Application, CommandHandler, PollAnswerHandler, ContextTypes

# ======== CONFIG (edit only the token) ========
BOT_TOKEN = "8087825133:AAF-6dxJ7g6GYCJmu5anPhYWzDatxm7mR24"
QUESTIONS_PATH = pathlib.Path("questions.json")
# =============================================

logging.basicConfig(format="%(asctime)s %(levelname)s: %(message)s",
                    level=logging.INFO)

# ---- load questions (with friendly errors) ----
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
            if not {"q","opts","answer"} <= set(q):
                print(f"❌ Question #{i+1} missing keys (q, opts, answer).")
                return None
            if not isinstance(q["opts"], list) or len(q["opts"]) < 2:
                print(f"❌ Question #{i+1} needs 2+ options.")
                return None
        print(f"✅ Loaded {len(data)} questions.")
        return data
    except Exception as e:
        print("❌ Failed to parse questions.json:", e)
        return None

QUIZ = load_questions()
USER_STATE = defaultdict(dict)
POLL_TO_QID = {}

def new_order():
    return random.sample(range(len(QUIZ)), k=len(QUIZ))

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 Ready!\n/quiz – full quiz\n/retest – only wrong ones\n/score – last score & history\n/help – this help"
    )

help_cmd = start_cmd

async def score_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user.id
    st = USER_STATE.get(u, {})
    last = st.get("last_score")
    if last:
        await update.message.reply_text(
            f"📊 Last: {last['correct']}/{last['total']} on {last['time']}"
        )
    else:
        await update.message.reply_text("No attempts yet. Use /quiz to start.")

async def quiz_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user.id
    USER_STATE[u] = {
        "order": new_order(),
        "idx": 0,
        "wrong_ids": set(),
        "correct_count": 0,
        "total": len(QUIZ),
        "mode": "full",
    }
    await send_next(update, context, u)

async def retest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    await send_next(update, context, u)

async def send_next(update_or_ctx, context: ContextTypes.DEFAULT_TYPE, uid: int):
    st = USER_STATE[uid]
    if st["idx"] >= len(st["order"]):
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
        result = {"correct": st["correct_count"], "total": st["total"], "time": ts}
        USER_STATE[uid]["last_score"] = result
        USER_STATE[uid].setdefault("history", []).append(result)
        await context.bot.send_message(
            chat_id=uid,
            text=f"🏁 Score: {st['correct_count']}/{st['total']}\nUse /retest to try the {len(st['wrong_ids'])} you missed.",
        )
        return

    qid = st["order"][st["idx"]]
    q = QUIZ[qid]
    idxs = list(range(len(q["opts"])))
    random.shuffle(idxs)
    opts = [q["opts"][i] for i in idxs]
    correct = idxs.index(q["answer"])

    msg = await context.bot.send_poll(
        chat_id=uid,
        question=q["q"],
        options=opts,
        type=Poll.QUIZ,
        correct_option_id=correct,
        is_anonymous=False,
    )
    POLL_TO_QID[msg.poll.id] = (uid, qid, correct)


async def on_poll_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    uid_from_update = ans.user.id
    chosen = ans.option_ids[0] if ans.option_ids else None

    entry = POLL_TO_QID.pop(ans.poll_id, None)

    if entry is None:
        # Fallback: likely a restart or second instance; don't hang
        st = USER_STATE.get(uid_from_update)
        if not st:
            return
        # Treat as wrong and continue so the quiz progresses
        if st["idx"] < len(st["order"]):
            st["wrong_ids"].add(st["order"][st["idx"]])
        st["idx"] += 1
        await send_next(update, context, uid_from_update)
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
    await send_next(update, context, uid)


def main():
    if not QUIZ:
        print("🛑 Fix questions.json and run again.")
        return
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("quiz", quiz_cmd))
    app.add_handler(CommandHandler("retest", retest_cmd))
    app.add_handler(CommandHandler("score", score_cmd))
    app.add_handler(PollAnswerHandler(on_poll_answer))
    print("🚀 Bot running… open Telegram and DM /start to your bot.")
    # IMPORTANT: blocking mode (no asyncio.run) – avoids Windows loop issues
    app.run_polling()

import json, pathlib
p = pathlib.Path("questions.json")
data = json.loads(p.read_text(encoding="utf-8"))
for i, q in enumerate(data, 1):
    bad = []
    if len(q["q"]) > 300: bad.append(f"Q{i}: question {len(q['q'])}>300")
    for j, opt in enumerate(q["opts"], 1):
        if len(opt) > 100: bad.append(f"Q{i} opt{j}: {len(opt)}>100")
    if bad: print(" | ".join(bad))
print("Done.")

if __name__ == "__main__":
    main()





