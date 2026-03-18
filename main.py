import os
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

logging.basicConfig(level=logging.INFO)

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise Exception("❌ Thiếu TOKEN")

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
        "🎯 AUTO STOP:\n"
        "+30% → DỪNG\n"
        "-30% → DỪNG\n\n"
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

    users[uid] = {
        "money": m,
        "start_money": m,  # 👈 quan trọng
        "profit": 0,
        "history": [],
        "step": 1,
        "win": 0,
        "lose": 0
    }

    await update.message.reply_text(f"💰 Vốn: {money(m)}")

# ===== RESET ALL =====
async def resetall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    users[uid] = {
        "money": 0,
        "start_money": 0,
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

        # ===== AUTO STOP THEO TIỀN =====
        start = user["start_money"]
        current = user["money"]

        percent = ((current - start) / start * 100) if start > 0 else 0

        if percent >= 30:
            await update.message.reply_text("🛑 Lãi +30% → DỪNG!")
            return

        if percent <= -30:
            await update.message.reply_text("💀 Lỗ -30% → DỪNG!")
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
            f"📈 Lãi: {money(user['profit'])}\n"
            f"📊 %: {percent:.1f}%\n\n"

            f"🏆 {user['win']} | ❌ {user['lose']}\n"

            "━━━━━━━━━━━━━━"
        )

        msgs.append(msg)

    await update.message.reply_text("\n".join(msgs))

# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(CommandHandler("resetall", resetall))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 BOT RUNNING")

    app.run_polling()

if __name__ == "__main__":
    main()
