import os
import asyncio
from collections import defaultdict
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("TOKEN")

if not TOKEN:
    raise Exception("❌ Thiếu TOKEN")

# ===== MAP =====
mapping = {
    0:"Tài",1:"Xỉu",2:"Xỉu",3:"Tài",
    4:"Xỉu",5:"Xỉu",6:"Xỉu",7:"Xỉu",
    8:"Tài",9:"Tài",10:"Tài",11:"Xỉu",
    12:"Tài",13:"Tài",14:"Xỉu",
    15:"Tài",16:"Xỉu",17:"Tài"
}

# ===== DATA =====
users = {}
d_stats = defaultdict(lambda: {"win":0,"total":0})

# ===== FORMAT TIỀN =====
def money(x):
    return f"{x:,}".replace(",", ".")

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔥 TX PRO MAX 🔥\n\n"
        "👉 Nhập: 14-12-6\n"
        "👉 /setmoney 500000\n"
        "👉 /reset\n"
    )

# ===== SET MONEY =====
async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        m = int(context.args[0])
    except:
        await update.message.reply_text("❗ /setmoney 500000")
        return

    uid = update.message.from_user.id
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
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid in users:
        users[uid]["history"] = []
        users[uid]["step"] = 1

    await update.message.reply_text("🔄 Reset lịch sử")

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    if uid not in users:
        await update.message.reply_text("❗ /setmoney trước")
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
            msgs.append(f"📥 {num} → chờ thêm")
            continue

        prev = user["history"][-2]
        d = abs(num - prev)
        pred = mapping[d]

        # ===== CƯỢC =====
        base = int(user["money"] * 0.05)
        bet = base * user["step"]

        result = "⏭"

        # ===== TÍNH KẾT QUẢ =====
        if len(user["history"]) >= 3:
            prev_d = abs(user["history"][-2] - user["history"][-3])
            prev_pred = mapping[prev_d]

            if prev_pred == real:
                user["money"] += bet
                user["profit"] += bet
                user["step"] = 1
                user["win"] += 1
                result = f"✅ +{money(bet)}"
                d_stats[d]["win"] += 1
            else:
                user["money"] -= bet
                user["profit"] -= bet
                user["step"] *= 2
                user["lose"] += 1
                result = f"❌ -{money(bet)} (x{user['step']})"

            d_stats[d]["total"] += 1

        # ===== LỆNH TIẾP =====
        next_bet = int(user["money"] * 0.05) * user["step"]

        msgs.append(
            "━━━━━━━━━━━━━━\n"
            f"📊 {prev} ➜ {num} | d={d}\n"
            f"🎯 KQ: {real}\n"
            f"{result}\n\n"

            f"🔮 LỆNH TIẾP\n"
            f"👉 {pred}\n"
            f"💰 Cược: {money(next_bet)}\n\n"

            f"💵 Vốn: {money(user['money'])}\n"
            f"💸 Lãi: {money(user['profit'])}\n"
            f"🏆 {user['win']} | ❌ {user['lose']}"
        )

    await update.message.reply_text("\n".join(msgs))

# ===== RUN =====
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("setmoney", setmoney))
app.add_handler(CommandHandler("reset", reset))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))

async def main():
    print("🔥 BOT RUNNING")

    await app.bot.delete_webhook(drop_pending_updates=True)

    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
