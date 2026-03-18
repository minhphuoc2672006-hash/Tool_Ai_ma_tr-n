import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes

TOKEN = os.getenv("TOKEN")

# ===== DATA =====
users = {}

# ===== START =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users[update.effective_user.id] = {
        "money": 0,
        "profit": 0,
        "win": 0,
        "lose": 0
    }

    msg = """
🔥 BOT PHÂN TÍCH AI 🔥

💰 /set <tiền> — nhập vốn
📊 /stats — xem thống kê
🔄 /reset — reset vốn

📥 Nhập số để bot phân tích
"""

    await update.message.reply_text(msg)

# ===== SET MONEY =====
async def set_money(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        money = int(context.args[0])
        users[update.effective_user.id]["money"] = money

        await update.message.reply_text(f"💰 Đã set vốn: {money}")
    except:
        await update.message.reply_text("❌ Nhập sai! Ví dụ: /set 1000")

# ===== RESET =====
async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    users[update.effective_user.id]["money"] = 0
    users[update.effective_user.id]["win"] = 0
    users[update.effective_user.id]["lose"] = 0

    await update.message.reply_text("🔄 Đã reset vốn (lãi giữ nguyên)")

# ===== STATS =====
async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = users.get(update.effective_user.id)

    if not u:
        return await update.message.reply_text("❌ Chưa có dữ liệu")

    msg = f"""
📊 THỐNG KÊ

💰 Vốn: {u['money']}
📈 Lãi: {u['profit']}

✅ Thắng: {u['win']}
❌ Thua: {u['lose']}
"""
    await update.message.reply_text(msg)

# ===== PHÂN TÍCH =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()

    if not text.isdigit():
        return

    num = int(text)

    # logic đơn giản (bạn có thể thay sau)
    result = "TÀI 🔴" if num % 2 == 0 else "XỈU 🔵"

    user = users.get(update.effective_user.id)
    if not user:
        return await update.message.reply_text("👉 Gõ /start trước")

    bet = int(user["money"] * 0.1) if user["money"] > 0 else 0

    msg = f"""
🎯 PHÂN TÍCH

🔢 Số: {num}
📊 Kết quả: {result}

💰 Gợi ý vào: {bet}

⚡ Mục tiêu: +30% sẽ dừng
"""

    keyboard = [
        [
            InlineKeyboardButton("✅ Thắng", callback_data="win"),
            InlineKeyboardButton("❌ Thua", callback_data="lose"),
        ]
    ]

    await update.message.reply_text(
        msg,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ===== BUTTON =====
async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = users.get(query.from_user.id)

    if query.data == "win":
        user["win"] += 1
        user["profit"] += 100
        await query.edit_message_text("✅ Đã cộng thắng +100")

    elif query.data == "lose":
        user["lose"] += 1
        user["profit"] -= 100
        await query.edit_message_text("❌ Đã trừ -100")

# ===== MAIN =====
app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("set", set_money))
app.add_handler(CommandHandler("reset", reset))
app.add_handler(CommandHandler("stats", stats))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
app.add_handler(telegram.ext.CallbackQueryHandler(button))

print("🔥 BOT ĐANG CHẠY...")
app.run_polling()
