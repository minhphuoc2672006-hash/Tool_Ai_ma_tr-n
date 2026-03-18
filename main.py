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

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 TOOL TX AI\n\n"
        "👉 Nhập số (1-18)\n"
        "👉 /setmoney 1000"
    )

# ===== SET MONEY =====
async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        money = int(context.args[0])
    except:
        await update.message.reply_text("❗ /setmoney 1000")
        return

    uid = update.message.from_user.id

    users[uid] = {
        "money": money,
        "profit": 0,
        "history": [],
        "win": 0,
        "lose": 0
    }

    await update.message.reply_text(f"💰 Vốn: {money}")

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    if not text.isdigit():
        return

    num = int(text)
    if num < 1 or num > 18:
        return

    if uid not in users:
        await update.message.reply_text("❗ /setmoney trước")
        return

    user = users[uid]
    user["history"].append(num)

    real = "Tài" if num >= 11 else "Xỉu"

    # ===== CHƯA ĐỦ DATA =====
    if len(user["history"]) < 2:
        await update.message.reply_text(f"📥 {num} ({real})\n👉 Nhập thêm số nữa")
        return

    prev = user["history"][-2]
    d = abs(num - prev)

    # ===== PREDICT =====
    pred = mapping.get(d, "Tài")

    # ===== WINRATE =====
    ds = d_stats[d]
    wr = (ds["win"]/ds["total"]*100) if ds["total"] else 50

    # ===== BET =====
    base = int(user["money"] * 0.03)

    if wr >= 65:
        bet = base * 2
        level = "🔥 MẠNH"
    elif wr >= 55:
        bet = base
        level = "⚠️ TB"
    else:
        bet = 0
        level = "❌ BỎ"

    # ===== TÍNH KẾT QUẢ =====
    result = "⏭"

    if bet > 0:
        prev_pred = mapping.get(abs(prev - user["history"][-3]), "Tài") if len(user["history"]) >= 3 else None

        if prev_pred:
            if prev_pred == real:
                user["money"] += bet
                user["profit"] += bet
                user["win"] += 1
                result = f"✅ +{bet}"
                d_stats[d]["win"] += 1
            else:
                user["money"] -= bet
                user["profit"] -= bet
                user["lose"] += 1
                result = f"❌ -{bet}"

            d_stats[d]["total"] += 1

    # ===== HIỂN THỊ =====
    msg = (
        f"📊 {prev} ➜ {num}  |  d = {d}\n"
        f"🎯 Dự đoán: {pred}\n"
        f"{level} | WR: {round(wr,1)}%\n\n"
        f"💰 Gợi ý cược: {bet}\n"
        f"📈 KQ: {result}\n\n"
        f"💵 Vốn: {user['money']}\n"
        f"💸 Lãi: {user['profit']}\n\n"
        f"🏆 {user['win']} | ❌ {user['lose']}"
    )

    await update.message.reply_text(msg)

# ===== RUN =====
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("setmoney", setmoney))
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
