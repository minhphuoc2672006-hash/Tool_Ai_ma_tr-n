import os
import logging
import asyncio
import random
from collections import Counter
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise Exception("❌ Thiếu TOKEN")

users = {}

# ===== CHIẾN THUẬT MỞ RỘNG =====
BASE_STRATEGIES = [
    "FOLLOW","OPPOSITE","TREND","RANDOM",
    "STREAK","ANTI_STREAK","WEIGHTED",
    "LAST2","ALT","BREAK","CHAOS"
]

PATTERN_EXTENSIONS = [f"P{i}" for i in range(1,21)]
MARKOV_EXTENSIONS = [f"M{i}" for i in range(1,11)]
HYBRID_EXTENSIONS = [f"H{i}" for i in range(1,21)]
BETTING_EXTENSIONS = ["MARTINGALE","REVERSE","FIBONACCI"]

STRATEGIES = []
for base in BASE_STRATEGIES:
    for pat in PATTERN_EXTENSIONS:
        for mark in MARKOV_EXTENSIONS:
            for hyb in HYBRID_EXTENSIONS:
                STRATEGIES.append(f"{base}_{pat}_{mark}_{hyb}")
STRATEGIES += BETTING_EXTENSIONS
STRATEGIES = STRATEGIES[:1200]

# ===== HÀM HỖ TRỢ =====
def money(x):
    return f"{int(x):,}".replace(",", ".")

def classify(total):
    return "Tài" if total >= 11 else "Xỉu"

def get_tx(history):
    return ["Tài" if sum(x)>=11 else "Xỉu" for x in history]

# ===== CHẠY CHIẾN THUẬT =====
def run_strategy(name, history):
    tx = get_tx(history)
    if not tx:
        return random.choice(["Tài","Xỉu"])

    base = name.split("_")[0]
    if base == "FOLLOW":
        return tx[-1]
    if base == "OPPOSITE":
        return "Xỉu" if tx[-1]=="Tài" else "Tài"
    if base == "TREND":
        if len(tx)>=4:
            return "Tài" if tx[-4:].count("Tài") > tx[-4:].count("Xỉu") else "Xỉu"
    if base == "STREAK":
        if len(tx)>=2 and tx[-1]==tx[-2]:
            return tx[-1]
    if base == "ANTI_STREAK":
        if len(tx)>=2 and tx[-1]==tx[-2]:
            return "Xỉu" if tx[-1]=="Tài" else "Tài"
    if base == "WEIGHTED":
        c = Counter(tx)
        return "Tài" if c["Tài"] > c["Xỉu"] else "Xỉu"
    if base == "LAST2":
        if len(tx)>=2:
            return tx[-2]
    if base == "ALT":
        if len(tx)>=2:
            return "Xỉu" if tx[-1]==tx[-2] else tx[-1]
    if base == "BREAK":
        if len(tx)>=3 and tx[-1]==tx[-2]==tx[-3]:
            return "Xỉu" if tx[-1]=="Tài" else "Tài"
    if base == "CHAOS":
        return random.choice(tx)

    if "P" in name:
        if len(tx)>=3:
            return "Tài" if tx[-3:].count("Tài") > tx[-3:].count("Xỉu") else "Xỉu"
    if "M" in name:
        if len(tx)>=2:
            last = tx[-2:]
            count_follow = sum(1 for i in range(len(tx)-2) if tx[i:i+2]==last and tx[i+2]=="Tài")
            count_follow_xiu = sum(1 for i in range(len(tx)-2) if tx[i:i+2]==last and tx[i+2]=="Xỉu")
            if count_follow + count_follow_xiu == 0:
                return random.choice(["Tài","Xỉu"])
            return "Tài" if count_follow >= count_follow_xiu else "Xỉu"
    if "H" in name:
        c = Counter(tx[-5:])
        return "Tài" if c["Tài"]>=c["Xỉu"] else "Xỉu"

    if name in BETTING_EXTENSIONS:
        return random.choice(["Tài","Xỉu"])

    return random.choice(["Tài","Xỉu"])

# ===== ĐÁNH GIÁ CHIẾN THUẬT TOÀN BỘ LỊCH SỬ VỚI TRỌNG SỐ =====
def evaluate_strategies(history):
    if len(history) < 5:
        return None, 0
    scores = {}
    tx = get_tx(history)
    n = len(history)
    weights = [1 + i*0.2 for i in range(n)]  # trọng số tăng dần cho kết quả gần đây
    total_weight = sum(weights)
    for strat in STRATEGIES:
        score = 0.0
        for i in range(1, n):
            pred = run_strategy(strat, history[:i])
            real = tx[i]
            if pred == real:
                score += weights[i]
        scores[strat] = score / total_weight * 100  # % có trọng số
    best = max(scores, key=lambda k: scores[k])
    return best, scores[best]

# ===== GẤP THÉP =====
def bet_calc(user):
    base = int(user["start"] * 0.05)
    fib_seq = [1,1,2,3,5,8,13,21,34,55]
    if user["lose"] == 0:
        return base
    if user["lose"] < len(fib_seq):
        bet = base * fib_seq[user["lose"]]
    else:
        bet = base * (2 ** user["lose"])
    return int(min(bet, user["money"]))

# ===== COMMANDS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 AI PRO TÀI/XỈU 1200+ CHIẾN THUẬT\n/setmoney 500000\n/reset để reset dữ liệu"
    )

async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    m = int(context.args[0])
    users[uid] = {
        "money": m,
        "start": m,
        "profit": 0,
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "last_bet": 0,
        "history": [],
        "strategy": random.choice(STRATEGIES)
    }
    await update.message.reply_text(f"💰 Vốn: {money(m)}")

async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid in users:
        users[uid] = {
            "money": 0,
            "start": 0,
            "profit": 0,
            "win": 0,
            "lose": 0,
            "last_pred": None,
            "last_bet": 0,
            "history": [],
            "strategy": random.choice(STRATEGIES)
        }
        await update.message.reply_text("♻️ Đã reset toàn bộ dữ liệu!")
    else:
        await update.message.reply_text("❗ Bạn chưa set tiền /setmoney trước")

# ===== HANDLE INPUT =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text
    if uid not in users:
        await update.message.reply_text("❗ /setmoney trước")
        return

    user = users[uid]
    nums = [int(x) for x in text.replace("-", " ").split() if x.isdigit()]
    if len(nums) != 3:
        await update.message.reply_text("❗ Nhập 3 số ví dụ: 3-5-6")
        return

    dice = nums
    real = classify(sum(dice))
    await update.message.reply_text(f"🎲 {dice} → {real}")
    await asyncio.sleep(0.5)

    # ===== CẬP NHẬT LỊCH SỬ =====
    user["history"].append(dice)
    if len(user["history"]) > 50:
        user["history"].pop(0)

    # ===== PHÂN TÍCH KHI LỊCH SỬ >=5 =====
    if len(user["history"]) >=5:
        best_strategy, conf = evaluate_strategies(user["history"])
        user["strategy"] = best_strategy
        pred = run_strategy(best_strategy, user["history"])
        # ===== CẬP NHẬT TIỀN THẮNG/THUA =====
        if user["last_pred"] is not None:
            if user["last_pred"] == real:
                user["money"] += user["last_bet"]
                user["profit"] += user["last_bet"]
                user["win"] += 1
                user["lose"] = 0
            else:
                user["money"] -= user["last_bet"]
                user["profit"] -= user["last_bet"]
                user["lose"] += 1
        next_bet = bet_calc(user)
        user["last_pred"] = pred
        user["last_bet"] = next_bet

        msg = (
            "━━━━━━━━━━━━━━\n"
            "🤖 AI PHÂN TÍCH TOÀN BỘ LỊCH SỬ\n"
            "━━━━━━━━━━━━━━\n"
            f"🔮 Dự đoán lần tới: {pred}\n"
            f"📊 Xác suất: {conf:.1f}%\n"
            "──────────────\n"
            f"💸 Cược: {money(next_bet)}\n"
            f"💰 Vốn: {money(user['money'])}\n"
            f"📈 Lãi: {money(user['profit'])}\n"
            "──────────────\n"
            f"🏆 {user['win']} | ❌ {user['lose']}\n"
            "━━━━━━━━━━━━━━"
        )
        await update.message.reply_text(msg)
    else:
        await update.message.reply_text(
            f"📌 Chưa đủ 5 kết quả để phân tích ({len(user['history'])}/5)"
        )

# ===== RUN BOT =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    print("🔥 BOT RUNNING...")
    app.run_polling()

if __name__ == "__main__":
    main()
