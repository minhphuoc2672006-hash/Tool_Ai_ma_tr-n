import os
import asyncio
import logging
from collections import Counter, defaultdict

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MAX_HISTORY = 300

if not TOKEN:
    raise Exception("❌ Thiếu TELEGRAM_BOT_TOKEN")

# ===== USER =====
def new_user():
    return {
        "history": [],
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "markov3": defaultdict(Counter),
        "markov4": defaultdict(Counter),
        "pattern_memory": defaultdict(int),
        "ai_mode": "NORMAL",   # 🧠 trạng thái AI tổng
    }

users = {}

def get_key(update):
    return update.effective_chat.id

def to_tx(n):
    return "Tài" if n >= 11 else "Xỉu"

def parse_input(text):
    return [int(x) for x in text.replace("-", " ").split()
            if x.isdigit() and 1 <= int(x) <= 18]

# ===== UI =====
def format_history(h):
    out = []
    for x,t,w in h[-20:]:
        icon = "⚫" if x=="Tài" else "⚪"
        out.append(icon)
    return " ".join(out)

# ===== MARKOV =====
def update_markov(d):
    h = d["history"]
    if len(h) >= 4:
        d["markov3"][tuple(x[0] for x in h[-4:-1])][h[-1][0]] += 1
    if len(h) >= 5:
        d["markov4"][tuple(x[0] for x in h[-5:-1])][h[-1][0]] += 1

# ===== PATTERN MEMORY =====
def update_pattern_memory(d):
    h = d["history"]
    if len(h) >= 4:
        pat = tuple(x[0] for x in h[-4:])
        d["pattern_memory"][pat] += 1

# ===== CYCLE =====
def detect_cycle(d):
    h = [x[0] for x in d["history"][-30:]]
    if len(h) < 10:
        return None, 0

    best = None
    best_score = 0

    for size in range(2, 8):
        pat = tuple(h[-size:])
        count = sum(1 for i in range(len(h)-size) if tuple(h[i:i+size]) == pat)

        score = count / size
        if score > best_score and count >= 2:
            best_score = score
            best = pat

    if not best:
        return None, 0

    return best[0], min(int(best_score*100), 85)

# ===== 🧠 SUPER META AI =====
def super_meta_ai(d):

    h = [x[0] for x in d["history"][-20:]]

    if len(h) < 10:
        return "NORMAL", "📊 Đang học"

    t = h.count("Tài")
    x = h.count("Xỉu")

    # ===== PHÁT HIỆN =====
    zigzag = all(h[i] != h[i-1] for i in range(1, len(h)))
    lệch = abs(t - x)

    # ===== LOGIC =====
    if zigzag and lệch < 4:
        return "DANGER", "🚫 Cầu ảo (zigzag)"

    if d["lose"] >= 4:
        return "RECOVER", "♻️ Thua sâu"

    if lệch > 12:
        return "BALANCE", "⚖️ Lệch mạnh"

    if d["win"] >= 5 and d["lose"] == 0:
        return "OVERCONF", "⚠️ Win ảo"

    return "NORMAL", "✅ Ổn định"

# ===== MARKOV =====
def markov_predict(d):
    h = [x[0] for x in d["history"]]
    res = []

    if len(h) >= 4:
        key = tuple(h[-3:])
        if key in d["markov3"]:
            c = d["markov3"][key]
            total = sum(c.values())
            if total >= 5:
                p = max(c, key=c.get)
                res.append((p, c[p]/total*110))

    if len(h) >= 5:
        key = tuple(h[-4:])
        if key in d["markov4"]:
            c = d["markov4"][key]
            total = sum(c.values())
            if total >= 5:
                p = max(c, key=c.get)
                res.append((p, c[p]/total*130))

    return res

# ===== AI CORE =====
def final_ai(d):

    if len(d["history"]) < 15:
        return None, 0, "📊 Thiếu dữ liệu"

    # ===== META AI =====
    mode, note = super_meta_ai(d)
    d["ai_mode"] = mode

    if mode == "DANGER":
        return None, 0, note

    score = defaultdict(float)

    # ===== MARKOV =====
    for p,c in markov_predict(d):
        score[p] += c

    # ===== PATTERN MEMORY =====
    h = [x[0] for x in d["history"]]
    if len(h) >= 3:
        key = tuple(h[-3:])
        for pat, cnt in d["pattern_memory"].items():
            if pat[:3] == key:
                score[pat[3]] += min(cnt*2, 80)

    # ===== CYCLE =====
    cycle_pred, cycle_conf = detect_cycle(d)
    if cycle_pred:
        score[cycle_pred] += cycle_conf

    if not score:
        return None,50,"❓ Không rõ"

    # ===== META CAN THIỆP =====
    if mode == "BALANCE":
        score["Tài"] *= 0.8
        score["Xỉu"] *= 0.8

    if mode == "RECOVER":
        for k in score:
            score[k] *= 0.7

    best = max(score, key=score.get)
    total = sum(score.values())

    conf = int(score[best]/(total+1)*100)
    conf = min(conf, 95)

    if mode == "OVERCONF":
        conf = int(conf * 0.85)

    if conf < 60:
        return None, conf, f"🛑 STOP | {note}"

    return best, conf, f"{note} | 🤖 AI CONTROL"

# ===== RESET =====
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users[get_key(update)] = new_user()
    await update.message.reply_text("🔄 RESET TOÀN BỘ AI")

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = get_key(update)
    users.setdefault(key, new_user())
    d = users[key]

    nums = parse_input(update.message.text)[:5]
    if not nums:
        return

    for n in nums:
        tx = to_tx(n)

        if d["last_pred"]:
            if d["last_pred"] == tx:
                d["win"] += 1
                d["lose"] = 0
            else:
                d["lose"] += 1

        d["history"].append((tx, "real", 1.0))
        update_markov(d)
        update_pattern_memory(d)

        if len(d["history"]) > MAX_HISTORY:
            d["history"] = d["history"][-MAX_HISTORY:]

    msg = await update.message.reply_text("🧠 AI đang phân tích...")
    await asyncio.sleep(0.1)

    pred, conf, status = final_ai(d)

    hist = format_history(d["history"])

    if pred is None:
        await msg.edit_text(f"""
╔══ 🚫 AI STOP ══╗
{status}

📊 {hist}
╚═══════════════╝
""")
        return

    d["last_pred"] = pred

    await msg.edit_text(f"""
╔══ 🤖 AI MASTER ══╗
{status}

📊 {hist}

📈 Win: {d['win']} | ❌ Lose: {d['lose']}

🎯 {'⚫ TÀI' if pred=='Tài' else '⚪ XỈU'}
🔥 {conf}%
╚══════════════════╝
""")

# ===== RUN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 AI MASTER CONTROL RUNNING...")
    app.run_polling()
