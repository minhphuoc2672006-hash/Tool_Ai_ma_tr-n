import os
import logging
import random
from collections import defaultdict, Counter
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise Exception("❌ Thiếu TOKEN")

users = {}

# ====== FORMAT TIỀN ======
def money(x):
    return f"{int(x):,}".replace(",", ".")

# ====== PHÂN LOẠI ======
def classify_total(num):
    return "Tài" if num >= 11 else "Xỉu"

# ====== RANDOM DICE ======
def random_dice():
    return sorted([random.randint(1,6) for _ in range(3)])

# ====== BUILD AI DATA ======
def build_ai_data(rounds=200000):
    data = []

    for _ in range(rounds):
        dice = random_dice()
        total = sum(dice)
        data.append((tuple(dice), classify_total(total)))

    mapping = defaultdict(list)

    for i in range(len(data)-1):
        key = data[i][0]  # (3,5,6)
        next_result = data[i+1][1]  # Tài/Xỉu
        mapping[key].append(next_result)

    return mapping

print("⏳ Đang build AI data...")
AI_MAPPING = build_ai_data()
print("✅ AI READY")

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>🎯 TX TOOL AI (216 + RANDOM)</b>\n\n"
        "💰 /setmoney 500000\n"
        "🔄 /reset\n"
        "💣 /resetall\n\n"
        "📥 Nhập: 3-5-6\n",
        parse_mode=ParseMode.HTML
    )

# ===== SET MONEY =====
async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    try:
        m = int(context.args[0])
    except:
        await update.message.reply_text("❗ /setmoney 500000")
        return

    users[uid] = {
        "money": m,
        "start_money": m,
        "base_bet": int(m * 0.05),
        "profit": 0,
        "history": [],
        "step": 1,
        "win": 0,
        "lose": 0,
        "lose_streak": 0,
        "reverse_mode": False,
        "last_pred": None,
        "last_bet": 0,
        "stopped": False
    }

    await update.message.reply_text(f"💰 Vốn: {money(m)}")

# ===== RESET =====
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    if uid not in users:
        await update.message.reply_text("❗ Chưa set tiền")
        return

    start_money = users[uid]["start_money"]

    users[uid] = {
        "money": start_money,
        "start_money": start_money,
        "base_bet": int(start_money * 0.05),
        "profit": 0,
        "history": [],
        "step": 1,
        "win": 0,
        "lose": 0,
        "lose_streak": 0,
        "reverse_mode": False,
        "last_pred": None,
        "last_bet": 0,
        "stopped": False
    }

    await update.message.reply_text("🔄 RESET")

# ===== RESET ALL =====
async def resetall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    if uid in users:
        del users[uid]

    await update.message.reply_text("💣 XOÁ ALL")

# ===== AI DỰ ĐOÁN =====
def ai_predict(dice):
    key = tuple(sorted(dice))

    if key not in AI_MAPPING:
        return "Tài", 0

    result = Counter(AI_MAPPING[key])
    total = sum(result.values())

    tai = result.get("Tài", 0)
    xiu = result.get("Xỉu", 0)

    pred = "Tài" if tai >= xiu else "Xỉu"
    diff = abs(tai - xiu) / total if total > 0 else 0

    return pred, diff

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    if uid not in users:
        await update.message.reply_text("❗ /setmoney trước")
        return

    user = users[uid]

    if user["stopped"]:
        await update.message.reply_text("🛑 ĐÃ DỪNG → /reset")
        return

    # ===== PARSE =====
    for c in ["-", ",", "|"]:
        text = text.replace(c, " ")

    nums = [int(x) for x in text.split() if x.isdigit() and 1 <= int(x) <= 6]

    if len(nums) != 3:
        await update.message.reply_text("❗ Nhập dạng: 3-5-6")
        return

    dice = nums
    total = sum(dice)
    real = classify_total(total)

    result = "..."
    round_profit = 0
    prev_bet = user["last_bet"]

    # ===== XỬ LÝ VÁN TRƯỚC =====
    if user["last_pred"] is not None:
        if user["last_pred"] == real:
            user["money"] += prev_bet
            user["profit"] += prev_bet
            user["win"] += 1
            user["step"] = 1
            user["lose_streak"] = 0
            result = "✅ WIN"
            round_profit = prev_bet
        else:
            user["money"] -= prev_bet
            user["profit"] -= prev_bet
            user["lose"] += 1
            user["lose_streak"] += 1
            result = "❌ LOSE"
            round_profit = -prev_bet
            user["step"] *= 2

            if user["reverse_mode"]:
                user["reverse_mode"] = not user["reverse_mode"]
            elif user["lose_streak"] >= 2:
                user["reverse_mode"] = True

    # ===== AI PREDICT =====
    pred, diff = ai_predict(dice)

    if user["reverse_mode"]:
        pred = "Tài" if pred == "Xỉu" else "Xỉu"

    # ===== BET =====
    bet = user["base_bet"] * user["step"]

    if bet > user["money"]:
        bet = user["money"]

    if bet <= 0:
        user["stopped"] = True
        await update.message.reply_text("🛑 HẾT TIỀN")
        return

    user["last_pred"] = pred
    user["last_bet"] = int(bet)

    percent = ((user["money"] - user["start_money"]) / user["start_money"] * 100)

    mode = "🔁 ĐẢO" if user["reverse_mode"] else "➡️ AI"

    msg = (
        "<pre>"
        "🎯 TX TOOL AI\n"
        "━━━━━━━━━━━━━━\n"
        f"🎲 {dice} = {real}\n\n"

        f"💵 Cược trước: {money(prev_bet)}\n"
        f"📊 {result}\n"
        f"💸 {money(round_profit)}\n"
        "━━━━━━━━━━━━━━\n"

        f"🔮 Dự đoán: {pred}\n"
        f"📊 Độ lệch: {diff*100:.2f}%\n"
        f"💰 Cược: {money(bet)}\n"
        f"⚙️ Mode: {mode}\n"
        "━━━━━━━━━━━━━━\n"

        f"💰 Vốn: {money(user['money'])}\n"
        f"📈 Lãi: {money(user['profit'])}\n"
        f"📊 {percent:.1f}%\n"
        "━━━━━━━━━━━━━━\n"

        f"🏆 {user['win']} | ❌ {user['lose']}\n"
        "</pre>"
    )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("resetall", resetall))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 BOT AI RUNNING")
    app.run_polling()

if __name__ == "__main__":
    main()
