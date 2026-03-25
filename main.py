import os
import logging
import random
import asyncio
from collections import defaultdict
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ===== CONFIG =====
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TOKEN")

if not TOKEN:
    raise Exception("❌ Thiếu TOKEN")

users = {}

# ===== FORMAT =====
def money(x):
    return f"{int(x):,}".replace(",", ".")

# ===== PHÂN LOẠI =====
def classify_total(total):
    return "Tài" if total >= 11 else "Xỉu"

# ===== RANDOM =====
def random_dice():
    return sorted([random.randint(1, 6) for _ in range(3)])

# ===== FAKE CẦU =====
def detect_pattern(history):
    if len(history) < 5:
        return "random"

    results = [classify_total(sum(x)) for x in history[-6:]]

    # bệt
    if len(set(results[-4:])) == 1:
        return "bet"

    # đảo
    if results[-4:] == ["Tài","Xỉu","Tài","Xỉu"] or results[-4:] == ["Xỉu","Tài","Xỉu","Tài"]:
        return "dao"

    # zigzag
    return "zigzag"

# ===== BUILD AI =====
def build_smart_ai(history, total_rounds=100):
    mapping = defaultdict(lambda: {"Tài": 0, "Xỉu": 0})

    pattern = detect_pattern(history)

    for _ in range(total_rounds):
        prev = tuple(random_dice())
        curr = random_dice()

        result = classify_total(sum(curr))

        # ===== FAKE CẦU LOGIC =====
        if pattern == "bet":
            result = result  # giữ nguyên
        elif pattern == "dao":
            result = "Tài" if result == "Xỉu" else "Xỉu"
        elif pattern == "zigzag":
            if random.random() > 0.5:
                result = "Tài"
            else:
                result = "Xỉu"

        mapping[tuple(prev)][result] += random.randint(1, 4)

    return mapping

# ===== AI PREDICT =====
def ai_predict(dice, mapping, user):
    key = tuple(sorted(dice))

    data = mapping[key]
    tai = data["Tài"]
    xiu = data["Xỉu"]
    total = tai + xiu if (tai + xiu) != 0 else 1

    # ===== LOGIC THUA ĐỔI CHIẾN THUẬT =====
    if user["lose"] >= 2:
        pred = "Tài" if tai < xiu else "Xỉu"
    else:
        pred = "Xỉu" if tai >= xiu else "Tài"

    # ===== % FAKE =====
    base_percent = abs(tai - xiu) / total * 100
    fake_percent = base_percent + random.uniform(-10, 10)

    fake_percent = max(50, min(95, fake_percent))

    return pred, fake_percent

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 TX AI PRO CASINO\n\n"
        "💰 /setmoney 500000\n"
        "🔄 /reset\n"
        "💣 /resetall\n\n"
        "📥 Nhập: 3-5-6"
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
        "base_percent": 0.05,
        "profit": 0,
        "step": 1,
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "last_bet": 0,
        "history": []
    }

    await update.message.reply_text(f"💰 Vốn: {money(m)}")

# ===== RESET =====
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    if uid not in users:
        return

    start_money = users[uid]["start_money"]

    users[uid].update({
        "money": start_money,
        "profit": 0,
        "step": 1,
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "last_bet": 0,
        "history": []
    })

    await update.message.reply_text("🔄 Reset xong")

# ===== RESET ALL =====
async def resetall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    if uid in users:
        del users[uid]

    await update.message.reply_text("💣 Xoá toàn bộ")

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    if uid not in users:
        await update.message.reply_text("❗ /setmoney trước")
        return

    user = users[uid]

    for c in ["-", ",", "|"]:
        text = text.replace(c, " ")

    nums = [int(x) for x in text.split() if x.isdigit() and 1 <= int(x) <= 6]

    if len(nums) != 3:
        await update.message.reply_text("❗ Nhập dạng: 3-5-6")
        return

    dice = nums
    total = sum(dice)
    real = classify_total(total)

    msg_wait = await update.message.reply_text("⏳ AI đang đọc cầu...")
    await asyncio.sleep(2)

    user["history"].append(dice)
    if len(user["history"]) > 50:
        user["history"].pop(0)

    # 🔥 RANDOM AI MỖI LẦN
    AI_MAPPING = build_smart_ai(user["history"], 100)

    # ===== WIN / LOSE =====
    prev_bet = user["last_bet"]
    result_text = "..."

    if user["last_pred"] is not None:
        if user["last_pred"] == real:
            user["money"] += prev_bet
            user["profit"] += prev_bet
            user["win"] += 1
            user["step"] = 1
            result_text = "✅ WIN"
        else:
            user["money"] -= prev_bet
            user["profit"] -= prev_bet
            user["lose"] += 1
            user["step"] *= 2
            result_text = "❌ LOSE"

    # ===== AI =====
    pred, percent = ai_predict(dice, AI_MAPPING, user)

    # ===== BET =====
    base_bet = user["money"] * user["base_percent"]
    bet = int(base_bet * user["step"])

    if bet > user["money"]:
        bet = user["money"]

    if bet <= 0:
        await update.message.reply_text("🛑 HẾT TIỀN")
        return

    user["last_pred"] = pred
    user["last_bet"] = bet

    percent_total = ((user["money"] - user["start_money"]) / user["start_money"] * 100)

    msg = (
        "🔥 TX AI PRO CASINO\n"
        "━━━━━━━━━━━━\n"
        f"🎲 {dice} → {real}\n\n"

        f"{result_text}\n"
        "━━━━━━━━━━━━\n"

        f"🔮 {pred}\n"
        f"📊 {percent:.1f}%\n"
        f"💰 {money(bet)}\n"
        "━━━━━━━━━━━━\n"

        f"💼 {money(user['money'])}\n"
        f"📈 {money(user['profit'])}\n"
        f"📊 {percent_total:.1f}%\n"
        "━━━━━━━━━━━━\n"

        f"🏆 {user['win']} | ❌ {user['lose']}"
    )

    await msg_wait.edit_text(msg)

# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("resetall", resetall))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 BOT PRO RUNNING")
    app.run_polling()

if __name__ == "__main__":
    main()
