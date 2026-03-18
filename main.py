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
        "🔥 TX PRO MAX 🔥\n\n"
        "👉 Nhập số: 12 hoặc 14-12-6\n"
        "👉 /setmoney 1000\n"
        "👉 /reset (reset lịch sử)\n\n"
        "📊 Có dự đoán + gấp thếp + TP/SL"
    )

# ===== SET MONEY =====
async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        money = int(context.args[0])
    except:
        await update.message.reply_text("❗ /setmoney 1000")
        return

    uid = update.message.from_user.id
    old = users.get(uid, {})

    users[uid] = {
        "money": money,
        "profit": old.get("profit", 0),
        "history": [],
        "win": old.get("win", 0),
        "lose": old.get("lose", 0),
        "step": 1
    }

    await update.message.reply_text(f"💰 Vốn: {money}")

# ===== RESET HISTORY =====
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid in users:
        users[uid]["history"] = []
        users[uid]["step"] = 1

    await update.message.reply_text("🔄 Reset lịch sử (giữ tiền & lãi)")

# ===== HANDLE =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    text = update.message.text.strip()

    if uid not in users:
        await update.message.reply_text("❗ /setmoney trước")
        return

    parts = text.replace("-", " ").split()

    nums = []
    for p in parts:
        if p.isdigit():
            n = int(p)
            if 1 <= n <= 18:
                nums.append(n)

    if not nums:
        return

    user = users[uid]
    msgs = []

    for num in nums:
        user["history"].append(num)
        real = "Tài" if num >= 11 else "Xỉu"

        # ===== CHỜ DATA =====
        if len(user["history"]) < 2:
            msgs.append(f"📥 {num} ({real}) → chờ thêm dữ liệu...")
            continue

        prev = user["history"][-2]
        d = abs(num - prev)
        pred = mapping.get(d, "Tài")

        # ===== WINRATE =====
        ds = d_stats[d]
        wr = (ds["win"]/ds["total"]*100) if ds["total"] else 50

        # ===== GẤP THẾP =====
        base = int(user["money"] * 0.05)
        bet = base * user["step"]

        tp = int(bet * 0.3)
        sl = int(bet * 0.3)

        result = "⏭ CHƯA TÍNH"

        # ===== KẾT QUẢ LỆNH TRƯỚC =====
        if len(user["history"]) >= 3:
            prev_d = abs(user["history"][-2] - user["history"][-3])
            prev_pred = mapping.get(prev_d, "Tài")

            if prev_pred == real:
                user["money"] += tp
                user["profit"] += tp
                user["win"] += 1
                user["step"] = 1
                result = f"✅ THẮNG +{tp} (reset)"
                d_stats[d]["win"] += 1
            else:
                user["money"] -= sl
                user["profit"] -= sl
                user["lose"] += 1
                user["step"] *= 2
                result = f"❌ THUA -{sl} (gấp x{user['step']})"

            d_stats[d]["total"] += 1

        # ===== LỆNH TIẾP =====
        next_bet = int(user["money"] * 0.05) * user["step"]
        next_tp = int(next_bet * 0.3)
        next_sl = int(next_bet * 0.3)
        next_pred = pred

        msgs.append(
            "━━━━━━━━━━━━━━\n"
            f"📊 {prev} ➜ {num}  |  d = {d}\n"
            f"🎯 KQ: {real}\n"
            f"{result}\n\n"

            f"🔮 LỆNH TIẾP THEO\n"
            f"👉 Đánh: {next_pred}\n"
            f"💰 Cược: {next_bet} (x{user['step']})\n"
            f"📈 TP: +{next_tp}\n"
            f"📉 SL: -{next_sl}\n\n"

            f"💵 Vốn: {user['money']}\n"
            f"💸 Lãi: {user['profit']}\n"
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
