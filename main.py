import os
import random
import logging
import asyncio
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
        "🎯 AUTO:\n"
        "+30% → nghỉ 30 phút\n"
        "-30% → nghỉ 30 phút\n\n"
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
        "start_money": m,
        "profit": 0,
        "total_profit": 0,
        "total_percent": 0,
        "history": [],
        "step": 1,
        "win": 0,
        "lose": 0,
        "pause": False
    }

    await update.message.reply_text(f"💰 Vốn: {money(m)}")

# ===== RESET CHU KỲ =====
async def reset_cycle(uid, context):
    user = users[uid]

    await asyncio.sleep(1800)  # 30 phút

    user["money"] = user["start_money"]
    user["profit"] = 0
    user["history"] = []
    user["step"] = 1
    user["win"] = 0
    user["lose"] = 0
    user["pause"] = False

    icon = random.choice(["🔥", "💎", "🚀", "🎯"])

    # 👉 Gửi lại FULL UI sau khi reset
    msg = (
        "╔══════════════════╗\n"
        f"   {icon} TX PRO MAX RESET {icon}\n"
        "╚══════════════════╝\n\n"

        "🔄 ĐÃ RESET SAU 30 PHÚT\n\n"

        f"💰 Vốn: {money(user['money'])}\n"
        f"📈 Lãi phiên: {money(user['profit'])}\n\n"

        f"💎 Tổng lời: {money(user['total_profit'])}\n"
        f"📊 Tổng %: {user['total_percent']}%\n\n"

        "🚀 SẴN SÀNG CHƠI LẠI\n"
        "👉 Nhập tiếp: 14-12-6\n"

        "━━━━━━━━━━━━━━"
    )

    try:
        await context.bot.send_message(chat_id=uid, text=msg)
    except:
        pass

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    if uid not in users:
        await update.message.reply_text("❗ Dùng /setmoney trước")
        return

    user = users[uid]

    if user["pause"]:
        await update.message.reply_text("⏳ Đang nghỉ 30 phút...")
        return

    nums = []
    for x in text.replace("-", " ").split():
        if x.isdigit():
            n = int(x)
            if 1 <= n <= 18:
                nums.append(n)

    if not nums:
        return

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

        # ===== CHECK % =====
        start = user["start_money"]
        current = user["money"]
        percent = ((current - start) / start * 100) if start > 0 else 0

        # ===== AUTO STOP =====
        if percent >= 30:
            cycle_profit = current - start

            user["total_profit"] += cycle_profit
            user["total_percent"] += 30
            user["pause"] = True

            await update.message.reply_text(
                f"🛑 Lãi +30%\n"
                f"💵 +{money(cycle_profit)}\n\n"
                f"📊 Tổng: {money(user['total_profit'])} ({user['total_percent']}%)\n\n"
                "⏳ Nghỉ 30 phút..."
            )

            asyncio.create_task(reset_cycle(uid, context))
            return

        if percent <= -30:
            user["pause"] = True

            await update.message.reply_text(
                "💀 Lỗ -30%\n⏳ Nghỉ 30 phút..."
            )

            asyncio.create_task(reset_cycle(uid, context))
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

            f"🏆 {user['win']} | ❌ {user['lose']}\n\n"

            f"💎 Tổng lời: {money(user['total_profit'])}\n"
            f"📊 Tổng %: {user['total_percent']}%\n"

            "━━━━━━━━━━━━━━"
        )

        msgs.append(msg)

    await update.message.reply_text("\n".join(msgs))

# ===== MAIN =====
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

    print("🔥 BOT RUNNING")

    app.run_polling()

if __name__ == "__main__":
    main()
