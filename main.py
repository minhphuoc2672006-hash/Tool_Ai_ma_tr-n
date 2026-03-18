import os
import asyncio
import random
import logging
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ===== LOG =====
logging.basicConfig(level=logging.INFO)

# ===== TOKEN =====
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise Exception("❌ Thiếu TOKEN")

# ===== LOGIC =====
mapping = {
    0:"Tài",1:"Xỉu",2:"Xỉu",3:"Tài",
    4:"Xỉu",5:"Xỉu",6:"Xỉu",7:"Xỉu",
    8:"Tài",9:"Tài",10:"Tài",11:"Xỉu",
    12:"Tài",13:"Tài",14:"Xỉu",
    15:"Tài",16:"Xỉu",17:"Tài"
}

users = {}

def money(x):
    return f"{x:,}".replace(",", ".")

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 TX PRO MAX 🔥\n\n"
        "👉 /setmoney 500000\n\n"
        "RESET:\n"
        "/resethistory\n"
        "/resetmoney\n"
        "/resetprofit\n"
        "/resetwin\n"
        "/resetall\n\n"
        "👉 Nhập: 14-12-6"
    )

# ===== SET MONEY =====
async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    if not context.args:
        await update.message.reply_text("❗ Ví dụ: /setmoney 500000")
        return

    try:
        m = int(context.args[0])
    except:
        await update.message.reply_text("❗ Số tiền không hợp lệ")
        return

    old = users.get(uid, {})

    users[uid] = {
        "money": m,
        "profit": old.get("profit", 0),
        "history": [],
        "step": 1,
        "win": old.get("win", 0),
        "lose": old.get("lose", 0)
    }

    await update.message.reply_text(f"💰 Vốn: {money(m)}")

# ===== RESET =====
async def resethistory(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid in users:
        users[uid]["history"] = []
        users[uid]["step"] = 1
    await update.message.reply_text("🔄 Reset lịch sử")

async def resetmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid in users:
        users[uid]["money"] = 0
    await update.message.reply_text("💰 Reset vốn")

async def resetprofit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid in users:
        users[uid]["profit"] = 0
    await update.message.reply_text("💸 Reset lãi")

async def resetwin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid in users:
        users[uid]["win"] = 0
        users[uid]["lose"] = 0
    await update.message.reply_text("🏆 Reset thắng/thua")

async def resetall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    users[uid] = {
        "money": 0,
        "profit": 0,
        "history": [],
        "step": 1,
        "win": 0,
        "lose": 0
    }
    await update.message.reply_text("💣 Reset tất cả")

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    if uid not in users:
        await update.message.reply_text("❗ Dùng /setmoney trước")
        return

    nums = []
    for x in text.replace("-", " ").split():
        if x.isdigit():
            n = int(x)
            if 1 <= n <= 18:
                nums.append(n)

    if not nums:
        return

    user = users[uid]
    msgs = []

    for num in nums:
        user["history"].append(num)
        real = "Tài" if num >= 11 else "Xỉu"

        if len(user["history"]) < 2:
            msgs.append(f"📥 Nhận: {num} → Đang phân tích...")
            continue

        prev = user["history"][-2]
        d = abs(num - prev)
        pred = mapping[d]

        base = int(user["money"] * 0.05)
        bet = base * user["step"]

        result = "⏳"

        if len(user["history"]) >= 3:
            prev_d = abs(user["history"][-2] - user["history"][-3])
            prev_pred = mapping[prev_d]

            if prev_pred == real:
                user["money"] += bet
                user["profit"] += bet
                user["step"] = 1
                user["win"] += 1
                result = f"✅ WIN +{money(bet)}"
            else:
                user["money"] -= bet
                user["profit"] -= bet
                user["step"] *= 2
                user["lose"] += 1
                result = f"❌ LOSE -{money(bet)}"

        total = user["win"] + user["lose"]
        rate = (user["win"] / total * 100) if total > 0 else 0

        # ===== AUTO STOP =====
        if total >= 5:
            if rate >= 70:
                await update.message.reply_text("🛑 Đạt ngưỡng thắng → DỪNG!")
                return
            if rate <= 30:
                await update.message.reply_text("⚠️ Tỉ lệ thấp → DỪNG!")
                return

        next_bet = int(user["money"] * 0.05) * user["step"]

        icon = random.choice(["🔥", "💎", "🚀", "🎯"])

        msg = (
            "╔══════════════════╗\n"
            f"   {icon} TX PRO MAX {icon}\n"
            "╚══════════════════╝\n\n"

            f"🎯 DỰ ĐOÁN: {pred}\n"
            f"{result}\n\n"

            f"💸 Cược tiếp: {money(next_bet)}\n\n"

            f"💰 Vốn: {money(user['money'])}\n"
            f"📈 Lãi: {money(user['profit'])}\n\n"

            f"🏆 Thắng: {user['win']}\n"
            f"❌ Thua: {user['lose']}\n"
            f"📊 Tỉ lệ: {rate:.1f}%\n"

            "━━━━━━━━━━━━━━"
        )

        msgs.append(msg)

    await update.message.reply_text("\n".join(msgs))

# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(CommandHandler("resethistory", resethistory))
    app.add_handler(CommandHandler("resetmoney", resetmoney))
    app.add_handler(CommandHandler("resetprofit", resetprofit))
    app.add_handler(CommandHandler("resetwin", resetwin))
    app.add_handler(CommandHandler("resetall", resetall))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 BOT RUNNING")

    app.run_polling()

if __name__ == "__main__":
    main()
