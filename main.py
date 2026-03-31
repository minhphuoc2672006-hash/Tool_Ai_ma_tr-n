import os
import asyncio
import logging
import random
from collections import Counter, defaultdict

from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

# ===== CONFIG =====
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MAX_HISTORY = 400

# ===== USER =====
def new_user():
    return {
        "history": [],
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "win_streak": 0,
        "mode": "normal",
        "markov3": defaultdict(Counter),
        "markov4": defaultdict(Counter),
    }

users = {}

def get_key(update):
    return update.effective_chat.id

def to_tx(n):
    return "Tài" if n >= 11 else "Xỉu"

def parse_input(text):
    return [int(x) for x in text.replace("-", " ").split() if x.isdigit() and 1 <= int(x) <= 18]

# ===== FAKE =====
def fake_server(n=200):
    seq = []
    last = random.choice(["Tài", "Xỉu"])

    for _ in range(n):
        r = random.random()
        if r < 0.45:
            seq.append(last)
        elif r < 0.75:
            last = "Xỉu" if last == "Tài" else "Tài"
            seq.append(last)
        elif r < 0.9 and len(seq) >= 2:
            seq.append(seq[-2])
        else:
            seq.append(random.choice(["Tài", "Xỉu"]))

        last = seq[-1]

    return [(x, "fake") for x in seq]

# ===== FORMAT =====
def format_history(h, n=20):
    res = []
    for x, t in h[-n:]:
        icon = "⚫" if x == "Tài" else "⚪"
        if t == "fake":
            icon += "̶"
        res.append(icon)
    return " ".join(res)

def count_rf(h):
    r = sum(1 for _, t in h if t == "real")
    f = sum(1 for _, t in h if t == "fake")
    return r, f

# ===== MARKOV =====
def update_markov(d):
    h = d["history"]

    if len(h) >= 4:
        key = tuple(x[0] for x in h[-4:-1])
        d["markov3"][key][h[-1][0]] += 1

    if len(h) >= 5:
        key = tuple(x[0] for x in h[-5:-1])
        d["markov4"][key][h[-1][0]] += 1

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
                res.append((p, c[p] / total * 100))

    if len(h) >= 5:
        key = tuple(h[-4:])
        if key in d["markov4"]:
            c = d["markov4"][key]
            total = sum(c.values())
            if total >= 5:
                p = max(c, key=c.get)
                res.append((p, c[p] / total * 120))

    return res

# ===== PATTERN =====
def detect_patterns(h_raw):
    h = [x[0] for x in h_raw]
    res = []

    if len(h) < 6:
        return res

    if len(set(h[-4:])) == 1:
        res.append(("BỆT", h[-1], 100))

    if all(h[i] != h[i - 1] for i in range(-1, -6, -1)):
        res.append(("ZIGZAG", h[-2], 90))

    if h[-1] != h[-2] and h[-2] != h[-3]:
        res.append(("1-1", h[-2], 80))

    return res

# ===== TREND =====
def trend_score(h_raw):
    h = [x[0] for x in h_raw]
    if len(h) < 10:
        return []
    c = Counter(h[-15:])
    return [(c.most_common(1)[0][0], 85)]

# ===== AUTO CLEAN =====
def detect_noise(h_raw):
    h = [x[0] for x in h_raw]
    if len(h) < 12:
        return False
    flip = sum(1 for i in range(1, 12) if h[-i] != h[-i - 1])
    return flip >= 9

def remove_noise(d):
    d["history"] = d["history"][:-12]

def decay_fake(d):
    h = d["history"]
    if len(h) < 30:
        return 0

    new = []
    removed = 0

    for i, (x, t) in enumerate(h):
        if i >= len(h) - 20:
            new.append((x, t))
            continue

        if t == "fake" and random.random() < 0.3:
            removed += 1
        else:
            new.append((x, t))

    d["history"] = new
    return removed

def reset_logic(d):
    d["markov3"].clear()
    d["markov4"].clear()
    d["lose"] = 0
    d["last_pred"] = None

# ===== MODE =====
def update_mode(d):
    if d["lose"] >= 1:
        d["mode"] = "safe"
    elif d["win_streak"] >= 3:
        d["mode"] = "aggressive"
    else:
        d["mode"] = "normal"

# ===== AI =====
def final_ai(d):
    if len(d["history"]) < 15:
        return None, 0, [], "📊 Thiếu dữ liệu"

    score = defaultdict(float)

    for p, c in markov_predict(d):
        score[p] += c

    patterns = detect_patterns(d["history"])
    for name, p, w in patterns:
        score[p] += w

    for p, c in trend_score(d["history"]):
        score[p] += c

    if not score:
        return None, 50, patterns, "❓ Không rõ"

    best = max(score, key=score.get)

    if d["mode"] == "aggressive":
        score[best] *= 1.2
    elif d["mode"] == "safe":
        score[best] *= 0.8

    conf = int(score[best] / sum(score.values()) * 100)

    if conf < 60:
        return None, conf, patterns, "🛑 STOP"

    return best, conf, patterns, "✅ OK"

# ===== AI GUARD (NEW) =====
def ai_guard(d, pred, conf, patterns):
    if pred is None:
        return None, conf, "🛑 STOP (Guard)"

    h = [x[0] for x in d["history"]]

    flip = sum(1 for i in range(1, min(len(h), 10)) if h[-i] != h[-i-1])
    if flip >= 8:
        return None, conf, "🧠 Nhiễu cao → STOP"

    if len(patterns) >= 2:
        if patterns[0][1] != patterns[1][1]:
            return None, conf-10, "⚠️ Xung đột cầu"

    if d["lose"] >= 1:
        conf -= 15

    if len(h) >= 6 and h[-1] != h[-2] != h[-3]:
        return None, conf, "🔄 Đảo liên tục → STOP"

    if conf < 65:
        return None, conf, "🛑 Guard chặn"

    return pred, conf, "✅ Guard OK"

# ===== HANDLER =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = get_key(update)
    users.setdefault(key, new_user())
    d = users[key]

    text = update.message.text.strip()

    if text == "/fake":
        d["history"] = fake_server(200)
        await update.message.reply_text("🎲 Fake loaded")
        return

    if text == "/reset":
        d["history"] = d["history"][-50:]
        await update.message.reply_text("🔄 Reset nhẹ")
        return

    if text == "/resetall":
        users[key] = new_user()
        await update.message.reply_text("💥 Reset ALL")
        return

    nums = parse_input(text)
    if not nums:
        return

    for n in nums:
        tx = to_tx(n)

        if d["last_pred"]:
            if d["last_pred"] == tx:
                d["win"] += 1
                d["lose"] = 0
                d["win_streak"] += 1
            else:
                d["lose"] += 1
                d["win_streak"] = 0

        d["history"].append((tx, "real"))
        update_markov(d)

        await update.message.reply_text(f"🎲 {n} → {'⚫' if tx=='Tài' else '⚪'}")

        removed = decay_fake(d)
        if removed > 0:
            await update.message.reply_text(f"🧹 Xoá {removed} fake")

    wait = await update.message.reply_text("🤖 Đang phân tích...")
    await asyncio.sleep(1)

    if detect_noise(d["history"]):
        remove_noise(d)
        await wait.edit_text("🧹 Nhiễu → đã xoá")
        return

    if d["lose"] >= 1:
        reset_logic(d)
        await wait.edit_text("🧠 Thua → reset não")
        return

    update_mode(d)

    pred, conf, patterns, status = final_ai(d)

    # ===== GUARD APPLY =====
    pred, conf, guard_status = ai_guard(d, pred, conf, patterns)
    status = guard_status if pred is None else status + " | " + guard_status

    hist = format_history(d["history"])
    real, fake = count_rf(d["history"])

    header = "╔════ AI BOT ════╗"
    stats = f"Win:{d['win']} Lose:{d['lose']} | Real:{real} Fake:{fake}"
    mode = f"Mode: {d['mode']}"

    if pred is None:
        await wait.edit_text(f"{header}\n{stats}\n{mode}\n\n{status}\n\n{hist}")
        return

    d["last_pred"] = pred

    await wait.edit_text(
        f"{header}\n{stats}\n{mode}\n\n{status}\n\n{hist}\n\n🎯 {'⚫' if pred=='Tài' else '⚪'} {pred}\n📈 {conf}%"
    )

# ===== RUN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("fake", handle))
    app.add_handler(CommandHandler("reset", handle))
    app.add_handler(CommandHandler("resetall", handle))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 BOT FINAL + AI GUARD RUNNING...")
    app.run_polling()
