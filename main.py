import os
import asyncio
import logging
from collections import Counter, defaultdict

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

# ===== CONFIG =====
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
        "ai_bias": {"Tài": 1.0, "Xỉu": 1.0},
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
        if w < 0.6:
            icon = "🔸" + icon
        out.append(icon)
    return " ".join(out)

# ===== CLEAN NOISE =====
def clean_noise(d):
    if len(d["history"]) < 12:
        return False

    h = [x[0] for x in d["history"][-6:]]
    zigzag = all(h[i] != h[i-1] for i in range(1, len(h)))

    # fix: tránh xoá nhầm
    if zigzag and d["lose"] >= 3 and len(set(h)) == 2:
        d["history"] = d["history"][:-1]
        return True

    return False

# ===== RESET =====
def smart_reset(d):
    d["markov3"].clear()
    d["markov4"].clear()
    d["ai_bias"] = {"Tài": 1.0, "Xỉu": 1.0}

# ===== MARKOV =====
def update_markov(d):
    h = d["history"]

    if len(h) >= 4:
        d["markov3"][tuple(x[0] for x in h[-4:-1])][h[-1][0]] += 1

    if len(h) >= 5:
        d["markov4"][tuple(x[0] for x in h[-5:-1])][h[-1][0]] += 1

def rebuild_markov(d):
    d["markov3"].clear()
    d["markov4"].clear()

    h = d["history"]

    for i in range(len(h)):
        if i >= 3:
            d["markov3"][tuple(x[0] for x in h[i-3:i])][h[i][0]] += 1
        if i >= 4:
            d["markov4"][tuple(x[0] for x in h[i-4:i])][h[i][0]] += 1

# ===== MARKOV PREDICT =====
def markov_predict(d):
    h = [x[0] for x in d["history"]]
    res = []

    if len(h) >= 4:
        key = tuple(h[-3:])
        if key in d["markov3"]:
            c = d["markov3"][key]
            total = sum(c.values())
            if total >= 3:
                p = max(c, key=c.get)
                res.append((p, c[p]/total*100))

    if len(h) >= 5:
        key = tuple(h[-4:])
        if key in d["markov4"]:
            c = d["markov4"][key]
            total = sum(c.values())
            if total >= 3:
                p = max(c, key=c.get)
                res.append((p, c[p]/total*120))

    return res

# ===== PATTERN =====
def detect_patterns(h_raw):
    h = [x[0] for x in h_raw]
    res = []

    if len(h) < 6:
        return res

    if len(set(h[-4:])) == 1:
        res.append(("BỆT", h[-1], 95))

    if all(h[i] != h[i-1] for i in range(1, len(h[-6:]))):
        res.append(("ZIGZAG", h[-2], 88))

    if h[-3] == h[-2] and h[-1] != h[-2]:
        res.append(("2-1", h[-1], 87))

    if len(set(h[-6:-2])) > 1 and len(set(h[-2:])) == 1:
        res.append(("GÃY", h[-1], 90))

    return res

# ===== TRAP =====
def detect_trap(h_raw):
    h = [x[0] for x in h_raw]
    res = []

    if len(h) < 6:
        return res

    # FIX: đúng logic
    if len(set(h[-6:])) == 2:
        res.append(("CẦU ẢO", None, 0))

    if all(h[i] != h[i-1] for i in range(len(h)-6, len(h)-2)) and len(set(h[-2:])) == 1:
        res.append(("CẦU DỤ", h[-1], 100))

    return res

# ===== TREND =====
def trend_score(h_raw):
    h = [x[0] for x in h_raw]
    if len(h) < 10:
        return []
    c = Counter(h[-12:])
    return [(c.most_common(1)[0][0], 75)]

# ===== AI CORE =====
def final_ai(d):

    if d["lose"] >= 4:
        smart_reset(d)

    if len(d["history"]) < 15:
        return None, 0, [], [], "📊 Thiếu dữ liệu"

    clean_noise(d)

    score = defaultdict(float)

    for p,c in markov_predict(d):
        score[p] += c

    patterns = detect_patterns(d["history"])
    for _,p,w in patterns:
        if p:
            score[p] += w

    traps = detect_trap(d["history"])

    for p,c in trend_score(d["history"]):
        score[p] += c

    if not score:
        return None,50,patterns,traps,"❓ Không rõ"

    for name,_,_ in traps:
        if name == "CẦU ẢO":
            return None, 0, patterns, traps, "🚫 Cầu ảo → STOP"

    for name,p,_ in traps:
        if name == "CẦU DỤ":
            score[p] += 50

    # ===== ANTI LỆCH =====
    h = [x[0] for x in d["history"][-20:]]

    if h.count("Tài") > 14:
        score["Xỉu"] += 120
        score["Tài"] *= 0.7

    if h.count("Xỉu") > 14:
        score["Tài"] += 120
        score["Xỉu"] *= 0.7

    if abs(h.count("Tài") - h.count("Xỉu")) > 10:
        score["Tài"] *= 0.8
        score["Xỉu"] *= 0.8

    # ===== AI BIAS =====
    for k in score:
        score[k] *= max(0.6, min(d["ai_bias"][k], 1.3))

    best = max(score, key=score.get)

    total = sum(score.values())
    if total == 0:
        return None, 0, patterns, traps, "❓ Lỗi dữ liệu"

    conf = int(score[best]/total*100)
    conf = min(conf, 96)

    if d["lose"] >= 1:
        conf = max(conf - 5, 1)

    # FIX: đảo cầu muộn hơn
    if d["lose"] >= 4:
        best = "Xỉu" if best == "Tài" else "Tài"
        conf = int(conf * 0.9)

    if conf < 55:
        return None, conf, patterns, traps, "🛑 STOP"

    return best, conf, patterns, traps, "🔥 CHIẾN THỰC"

# ===== RESET =====
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users[get_key(update)] = new_user()
    await update.message.reply_text("🔄 Reset toàn bộ AI")

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = get_key(update)
    users.setdefault(key, new_user())
    d = users[key]

    nums = parse_input(update.message.text)[:5]

    if not nums:
        return

    results = []

    for n in nums:
        tx = to_tx(n)
        results.append(f"{n}→{'⚫' if tx=='Tài' else '⚪'}")

        if d["last_pred"]:
            if d["last_pred"] == tx:
                d["win"] += 1
                d["lose"] = 0
                d["ai_bias"][tx] = min(d["ai_bias"][tx]*1.02, 1.3)
            else:
                d["lose"] += 1
                d["ai_bias"][d["last_pred"]] = max(d["ai_bias"][d["last_pred"]]*0.9, 0.6)

        d["history"].append((tx, "real", 1.0))

        if len(d["history"]) > MAX_HISTORY:
            d["history"] = d["history"][-MAX_HISTORY:]

        update_markov(d)

    # FIX: giảm lag
    if len(d["history"]) % 50 == 0:
        rebuild_markov(d)

    await update.message.reply_text("🎲 " + " | ".join(results))

    wait = await update.message.reply_text("🤖 Đang phân tích...")
    await asyncio.sleep(0.1)

    pred, conf, patterns, traps, status = final_ai(d)

    hist = format_history(d["history"])

    if pred is None:
        await wait.edit_text(f"""🚫 {status}

📊 {hist}
""")
        return

    d["last_pred"] = pred

    await wait.edit_text(f"""╔══ 🤖 AI CHIẾN THỰC ══╗
{status}

📊 {hist}

📈 Win: {d['win']} | ❌ Lose: {d['lose']}

🎯 {'⚫ TÀI' if pred=='Tài' else '⚪ XỈU'}
🔥 {conf}%
╚═══════════════════════╝""")

# ===== RUN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 AI FINAL STABLE RUNNING...")
    app.run_polling()
