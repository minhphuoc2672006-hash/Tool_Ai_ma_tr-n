import os
from collections import defaultdict
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CallbackQueryHandler,
    CommandHandler, ContextTypes,
    MessageHandler, filters
)

TOKEN = os.getenv("TOKEN")
GROUP_ID = os.getenv("GROUP_ID")

if not TOKEN:
    raise ValueError("❌ Thiếu TOKEN")

GROUP_ID = int(GROUP_ID) if GROUP_ID else None

mapping = {
    0:"Tài",1:"Xỉu",2:"Xỉu",3:"Tài",
    4:"Xỉu",5:"Xỉu",6:"Xỉu",7:"Xỉu",
    8:"Tài",9:"Tài",10:"Tài",11:"Xỉu",
    12:"Tài",13:"Tài",14:"Xỉu",
    15:"Tài",16:"Xỉu",17:"Tài"
}

users = {}
last_num = {}
stats = defaultdict(lambda: {"win":0,"lose":0})
d_stats = defaultdict(lambda: {"win":0,"total":0})

# ===== KEYBOARD =====
def kb():
    k=[]
    row=[]
    for i in range(1,19):
        row.append(InlineKeyboardButton(str(i),callback_data=str(i)))
        if len(row)==6:
            k.append(row); row=[]
    return InlineKeyboardMarkup(k)

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 BOT TX 30% STOP\n/setmoney 1000",
        reply_markup=kb()
    )

# ===== SET MONEY =====
async def setmoney(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id

    if not context.args:
        await update.message.reply_text("❗ Dùng: /setmoney 1000")
        return

    try:
        money = int(context.args[0])
    except:
        await update.message.reply_text("❗ Số tiền không hợp lệ")
        return

    users[uid] = {
        "start": money,
        "current": money,
        "target": int(money*1.3),
        "stop": int(money*0.7),
        "profit": 0,
        "lose_streak": 0
    }

    await update.message.reply_text(
        f"💰 Vốn: {money}\n🎯 +30%: {int(money*1.3)}\n❌ -30%: {int(money*0.7)}"
    )

# ===== BUTTON (GIỮ NGUYÊN) =====
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    num = int(q.data)

    if uid not in last_num:
        last_num[uid] = num
        await q.edit_message_text(f"✅ Lưu {num}", reply_markup=kb())
        return

    if uid not in users:
        await q.edit_message_text("❗ /setmoney trước", reply_markup=kb())
        return

    user = users[uid]

    if user["current"] >= user["target"]:
        await q.edit_message_text("🎯 ĐẠT +30% → DỪNG", reply_markup=kb())
        return

    if user["current"] <= user["stop"]:
        await q.edit_message_text("❌ THUA -30% → DỪNG", reply_markup=kb())
        return

    prev = last_num[uid]
    d = abs(num - prev)
    pred = mapping.get(d, "Xỉu")

    ds = d_stats[d]
    wr = (ds["win"]/ds["total"]*100) if ds["total"]>0 else 50

    base = int(user["current"] * 0.05)
    bet = base * (2 ** user["lose_streak"])

    real = "Tài" if num >= 11 else "Xỉu"

    if pred == real:
        user["current"] += bet
        user["profit"] += bet
        user["lose_streak"] = 0
        stats[uid]["win"] += 1
        d_stats[d]["win"] += 1
        result = "✅ THẮNG"
    else:
        user["current"] -= bet
        user["profit"] -= bet
        user["lose_streak"] += 1
        stats[uid]["lose"] += 1
        result = "❌ THUA"

    d_stats[d]["total"] += 1
    last_num[uid] = num

    msg = (
        f"📊 {prev} ➜ {num} (d={d})\n"
        f"🎯 Dự đoán: {pred}\n"
        f"📈 WR: {round(wr,1)}%\n\n"
        f"💰 Cược: {bet}\n"
        f"{result}\n\n"
        f"💰 Vốn: {user['current']}\n"
        f"💵 Lãi: {user['profit']}\n"
        f"🔥 Gấp thếp: {user['lose_streak']}\n\n"
        f"🏆 {stats[uid]['win']} | ❌ {stats[uid]['lose']}"
    )

    await q.edit_message_text(msg, reply_markup=kb())

    # FIX gửi group
    if GROUP_ID:
        try:
            await context.bot.send_message(
                chat_id=GROUP_ID,
                text=f"🔥 TX SIGNAL\n\n{msg}"
            )
        except Exception as e:
            print("❌ Lỗi group:", e)

# ===== TEXT INPUT (THÊM MỚI) =====
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        uid = update.message.from_user.id
        text = update.message.text.strip()

        # ===== XỬ LÝ INPUT =====
        if "-" in text:
            parts = text.split("-")
            if len(parts) != 2:
                return
            if not parts[0].isdigit() or not parts[1].isdigit():
                return

            prev = int(parts[0])
            num = int(parts[1])

        else:
            if not text.isdigit():
                return

            num = int(text)

            if uid not in last_num:
                last_num[uid] = num
                await update.message.reply_text(f"✅ Lưu {num}")
                return

            prev = last_num[uid]

        if uid not in users:
            await update.message.reply_text("❗ /setmoney trước")
            return

        user = users[uid]

        if user["current"] >= user["target"]:
            await update.message.reply_text("🎯 ĐẠT +30% → DỪNG")
            return

        if user["current"] <= user["stop"]:
            await update.message.reply_text("❌ THUA -30% → DỪNG")
            return

        d = abs(num - prev)
        pred = mapping.get(d, "Xỉu")

        ds = d_stats[d]
        wr = (ds["win"]/ds["total"]*100) if ds["total"]>0 else 50

        base = int(user["current"] * 0.05)
        bet = base * (2 ** user["lose_streak"])

        real = "Tài" if num >= 11 else "Xỉu"

        if pred == real:
            user["current"] += bet
            user["profit"] += bet
            user["lose_streak"] = 0
            stats[uid]["win"] += 1
            d_stats[d]["win"] += 1
            result = "✅ THẮNG"
        else:
            user["current"] -= bet
            user["profit"] -= bet
            user["lose_streak"] += 1
            stats[uid]["lose"] += 1
            result = "❌ THUA"

        d_stats[d]["total"] += 1
        last_num[uid] = num

        msg = (
            f"📊 {prev} ➜ {num} (d={d})\n"
            f"🎯 Dự đoán: {pred}\n"
            f"📈 WR: {round(wr,1)}%\n\n"
            f"💰 Cược: {bet}\n"
            f"{result}\n\n"
            f"💰 Vốn: {user['current']}\n"
            f"💵 Lãi: {user['profit']}"
        )

        await update.message.reply_text(msg)

        if GROUP_ID:
            try:
                await context.bot.send_message(
                    chat_id=GROUP_ID,
                    text=f"🔥 TX SIGNAL\n\n{msg}"
                )
            except Exception as e:
                print("❌ Lỗi group:", e)

    except Exception as e:
        print("❌ Lỗi text:", e)

# ===== RUN =====
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("setmoney", setmoney))
app.add_handler(CallbackQueryHandler(button))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

print("✅ BOT ĐANG CHẠY...")
app.run_polling()
