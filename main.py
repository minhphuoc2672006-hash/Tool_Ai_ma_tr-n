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

# ===== STRATEGIES =====
STRATEGIES = [
    "FOLLOW","OPPOSITE","TREND","RANDOM",
    "STREAK","ANTI_STREAK","WEIGHTED",
    "LAST2","ALT","BREAK","CHAOS"
]

# ===== BASIC =====
def money(x):
    return f"{int(x):,}".replace(",", ".")

def classify(total):
    return "Tài" if total >= 11 else "Xỉu"

def get_tx(history):
    return ["Tài" if sum(x)>=11 else "Xỉu" for x in history]

# ===== STRATEGY =====
def run_strategy(name, history):
    tx = get_tx(history)

    if not tx:
        return random.choice(["Tài","Xỉu"])

    if name == "FOLLOW":
        return tx[-1]

    if name == "OPPOSITE":
        return "Xỉu" if tx[-1]=="Tài" else "Tài"

    if name == "TREND":
        if len(tx)>=4:
            return "Tài" if tx[-4:].count("Tài") > tx[-4:].count("Xỉu") else "Xỉu"

    if name == "STREAK":
        if len(tx)>=2 and tx[-1]==tx[-2]:
            return tx[-1]

    if name == "ANTI_STREAK":
        if len(tx)>=2 and tx[-1]==tx[-2]:
            return "Xỉu" if tx[-1]=="Tài" else "Tài"

    if name == "WEIGHTED":
        c = Counter(tx)
        return "Tài" if c["Tài"] > c["Xỉu"] else "Xỉu"

    if name == "LAST2":
        if len(tx)>=2:
            return tx[-2]

    if name == "ALT":
        if len(tx)>=2:
            return "Xỉu" if tx[-1]==tx[-2] else tx[-1]

    if name == "BREAK":
        if len(tx)>=3 and tx[-1]==tx[-2]==tx[-3]:
            return "Xỉu" if tx[-1]=="Tài" else "Tài"

    if name == "CHAOS":
        return random.choice(tx)

    return random.choice(["Tài","Xỉu"])

def pick_strategy():
    return random.choice(STRATEGIES)

# ===== AI =====
def ai_predict(user):
    pred = run_strategy(user["strategy"], user["history"])
    conf = random.uniform(55, 75)
    return pred, conf

# ===== GẤP THẾP =====
def bet_calc(user):
    base = user["start"] * 0.05

    if user["lose"] == 0:
        return int(base)

    bet = base * (2 ** user["lose"])
    return int(min(bet, user["money"]))

# ===== COMMAND =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 AI PRO\n/setmoney 500000")

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
        "strategy": pick_strategy()
    }

    await update.message.reply_text(f"💰 Vốn: {money(m)}")

# ===== MAIN =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text

    if uid not in users:
        await update.message.reply_text("❗ /setmoney trước")
        return

    user = users[uid]

    nums = [int(x) for x in text.replace("-", " ").split() if x.isdigit()]
    if len(nums) != 3:
        await update.message.reply_text("❗ Nhập: 3-5-6")
        return

    dice = nums
    real = classify(sum(dice))

    # ===== BƯỚC 1 =====
    await update.message.reply_text(f"🎲 {dice} → {real}")
    await asyncio.sleep(0.5)

    # ===== BƯỚC 2 =====
    msg_wait = await update.message.reply_text("⏳ Đang phân tích...")
    await asyncio.sleep(1)

    # lưu lịch sử
    user["history"].append(dice)
    if len(user["history"]) > 30:
        user["history"].pop(0)

    last_bet = user["last_bet"]

    # ===== XỬ LÝ KẾT QUẢ =====
    if user["last_pred"] is not None:
        if user["last_pred"] == real:
            user["money"] += last_bet
            user["profit"] += last_bet
            user["win"] += 1
            user["lose"] = 0
        else:
            user["money"] -= last_bet
            user["profit"] -= last_bet
            user["lose"] += 1

            # 🔥 THUA 1 → ĐỔI NGAY
            user["strategy"] = pick_strategy()

    # ===== AI =====
    pred, conf = ai_predict(user)
    next_bet = bet_calc(user)

    user["last_pred"] = pred
    user["last_bet"] = next_bet

    # ===== BƯỚC 3 =====
    msg = (
        "━━━━━━━━━━━━━━\n"
        "🤖 AI PHÂN TÍCH\n"
        "━━━━━━━━━━━━━━\n"
        f"🔮 {pred}\n"
        f"📊 {conf:.1f}%\n"
        "──────────────\n"
        f"💸 Cược: {money(next_bet)}\n"
        f"💰 Vốn: {money(user['money'])}\n"
        f"📈 Lãi: {money(user['profit'])}\n"
        "──────────────\n"
        f"🏆 {user['win']} | ❌ {user['lose']}\n"
        "━━━━━━━━━━━━━━"
    )

    await msg_wait.edit_text(msg)

# ===== RUN =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 BOT RUNNING...")
    app.run_polling()

if __name__ == "__main__":
    main()
