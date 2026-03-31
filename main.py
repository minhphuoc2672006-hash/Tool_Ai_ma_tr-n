import os
import asyncio
import logging
from collections import Counter, defaultdict, deque

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

if not TOKEN:
    raise Exception("❌ Thiếu TELEGRAM_BOT_TOKEN")

# ===== USER STATE =====
def new_user():
    return {
        "history": [],                # [(Tài/Xỉu, ...)]
        "win": 0, "lose": 0,
        "session_win": 0, "session_lose": 0,
        "last_pred": None,
        "markov": defaultdict(Counter),
        "pattern": defaultdict(int),
        "recent_preds": deque(maxlen=5),   # dùng cho Stability AI
        "ai_mode": "WAIT",
    }

users = {}

def get_key(update):
    return update.effective_chat.id

def to_tx(n):
    return "Tài" if n >= 11 else "Xỉu"

def parse_input(text):
    return [int(x) for x in text.replace("-", " ").split()
            if x.isdigit() and 1 <= int(x) <= 18]

# ===== MODEL UPDATE =====
def update_model(d):
    h = d["history"]

    if len(h) >= 3:
        key = tuple(x[0] for x in h[-3:])
        d["markov"][key][h[-1][0]] += 1

    if len(h) >= 4:
        pat = tuple(x[0] for x in h[-4:])
        d["pattern"][pat] += 1

# ===== META AI =====
def meta_ai(d):
    h = [x[0] for x in d["history"][-25:]]

    if len(h) < 12:
        return "WAIT", "📊 Đang học"

    zigzag = all(h[i] != h[i-1] for i in range(1, len(h)))
    if zigzag:
        return "NOISE", "🚫 Cầu nhiễu"

    if d["lose"] >= 3:
        return "STOP", "🛑 Thua chuỗi"

    if len(set(h[-6:])) == 1:
        return "STRONG", "🔥 Bệt mạnh"

    t, x = h.count("Tài"), h.count("Xỉu")
    if abs(t-x) > 12:
        return "BIAS", "⚖️ Lệch mạnh"

    return "OK", "✅ Ổn định"

# ===== PREDICTORS =====
def predict_markov(d):
    h = [x[0] for x in d["history"]]
    if len(h) < 3:
        return None, 0
    key = tuple(h[-3:])
    c = d["markov"].get(key)
    if not c or sum(c.values()) < 6:
        return None, 0
    p = max(c, key=c.get)
    return p, c[p]/sum(c.values())*100

def predict_pattern(d):
    h = [x[0] for x in d["history"]]
    if len(h) < 4:
        return None, 0
    key = tuple(h[-3:])
    score = defaultdict(int)
    for pat, cnt in d["pattern"].items():
        if pat[:3] == key:
            score[pat[3]] += cnt
    if not score:
        return None, 0
    p = max(score, key=score.get)
    return p, score[p]/sum(score.values())*100

def predict_cycle(d):
    h = [x[0] for x in d["history"][-30:]]
    for size in range(2, 6):
        pat = h[-size:]
        count = sum(1 for i in range(len(h)-size) if h[i:i+size]==pat)
        if count >= 3:
            return pat[0], min(count*10, 80)
    return None, 0

# ===== 🧠 STABILITY AI =====
def stability_check(d, new_pred):
    d["recent_preds"].append(new_pred)

    if len(d["recent_preds"]) < 4:
        return False, "⏳ Chưa đủ ổn định"

    # nếu dự đoán nhảy lung tung → không ổn
    if len(set(d["recent_preds"])) > 2:
        return False, "🚫 Tín hiệu không ổn định"

    return True, "✅ Ổn định"

# ===== FINAL AI =====
def final_ai(d):
    mode, note = meta_ai(d)
    d["ai_mode"] = mode

    if mode in ["WAIT", "NOISE", "STOP"]:
        return None, 0, note

    votes = []
    score = defaultdict(float)

    for func in [predict_markov, predict_pattern, predict_cycle]:
        p, c = func(d)
        if p:
            votes.append(p)
            score[p] += c

    if not votes:
        return None, 0, "❓ Không đủ tín hiệu"

    agree = max(votes.count("Tài"), votes.count("Xỉu"))
    if agree < 2:
        return None, 0, "🚫 Không đồng thuận"

    best = max(score, key=score.get)
    total = sum(score.values())
    conf = int(score[best]/(total+1)*100)

    # ===== Stability AI =====
    stable, stable_note = stability_check(d, best)
    if not stable:
        return None, conf, stable_note

    # session learning
    if d["session_lose"] >= 2:
        conf *= 0.75

    if conf < 75:
        return None, conf, "🚫 Kèo chưa đủ mạnh"

    return best, min(conf,95), f"{note} | {stable_note} | 🤖 AI LOCK"

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
                d["session_win"] += 1
                d["lose"] = 0
                d["session_lose"] = 0
            else:
                d["lose"] += 1
                d["session_lose"] += 1

        d["history"].append((tx,"real",1))
        update_model(d)

    msg = await update.message.reply_text("🧠 AI đang kiểm soát tổng...")
    await asyncio.sleep(0.1)

    pred, conf, status = final_ai(d)

    hist = " ".join(["⚫" if x[0]=="Tài" else "⚪" for x in d["history"][-20:]])

    if pred is None:
        await msg.edit_text(f"""
╔══ 🚫 BỎ KÈO ══╗
{status}

📊 {hist}
📈 W:{d['win']} | L:{d['lose']}
╚═══════════════╝
""")
        return

    d["last_pred"] = pred

    await msg.edit_text(f"""
╔══ 🎯 KÈO ĐẸP ══╗
{status}

📊 {hist}
📈 W:{d['win']} | L:{d['lose']}

🎯 {'⚫ TÀI' if pred=='Tài' else '⚪ XỈU'}
🔥 {conf}%
╚══════════════════╝
""")

# ===== RUN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 ULTIMATE STABLE AI RUNNING...")
    app.run_polling()
