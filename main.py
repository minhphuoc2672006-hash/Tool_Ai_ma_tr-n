import os
import asyncio
import random
import logging
from collections import defaultdict
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters
)
import websockets
import json

# ===== CONFIG =====
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise Exception("❌ Thiếu TOKEN")

WEBSOCKET_URL = "wss://example.com/game"  # Thay bằng WebSocket của game
users = {}

# ===== FORMAT =====
def money(x):
    return f"{int(x):,}".replace(",", ".")

# ===== PHÂN LOẠI =====
def classify_total(total):
    return "Tài" if total >= 11 else "Xỉu"

# ===== RANDOM DICE =====
def random_dice():
    return sorted([random.randint(1,6) for _ in range(3)])

# ===== BUILD AI 10 TRIỆU =====
def build_ai_data(rounds=10_000_000):
    mapping = defaultdict(lambda: {"Tài": 0, "Xỉu": 0})
    prev = random_dice()
    for i in range(rounds):
        current = random_dice()
        result = classify_total(sum(current))
        mapping[tuple(prev)][result] += 1
        prev = current
        if i % 1_000_000 == 0 and i > 0:
            print(f"⏳ {i:,} ván...")
    return mapping

print("🔥 Build AI 10M...")
AI_MAPPING = build_ai_data()
print("✅ AI READY")

# ===== AI PREDICT =====
def ai_predict(dice):
    key = tuple(sorted(dice))
    if key not in AI_MAPPING:
        return "Tài"
    data = AI_MAPPING[key]
    return "Tài" if data["Tài"] >= data["Xỉu"] else "Xỉu"

# ===== TELEGRAM HANDLERS =====
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>🔥 TX TOOL BOT</b>\n\n"
        "💰 /setmoney 500000\n"
        "🔄 /reset\n"
        "💣 /resetall\n\n"
        "📥 Nhập: 3-5-6 hoặc chờ auto",
        parse_mode=ParseMode.HTML
    )

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
        "step": 1,
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "last_bet": 0,
        "last_bet_choice": None
    }
    await update.message.reply_text(f"💰 Vốn: {money(m)}")

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
        "step": 1,
        "win": 0,
        "lose": 0,
        "last_pred": None,
        "last_bet": 0,
        "last_bet_choice": None
    }
    await update.message.reply_text("🔄 Đã reset phiên")

async def resetall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.message.from_user.id
    if uid in users:
        del users[uid]
    await update.message.reply_text("💣 Đã xoá toàn bộ dữ liệu")

# ===== AI + BET LOGIC =====
async def process_game(dice):
    total = sum(dice)
    real = classify_total(total)

    # Cập nhật cho tất cả user
    for uid, user in users.items():
        prev_bet = user["last_bet"]
        result_text = "..."

        # WIN / LOSE
        if user["last_bet_choice"] is not None:
            if user["last_bet_choice"] == real:
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

        # AI + ĐÁNH NGƯỢC
        ai_pred = ai_predict(dice)
        bet_choice = "Tài" if ai_pred == "Xỉu" else "Xỉu"
        bet = user["base_bet"] * user["step"]
        if bet > user["money"]:
            bet = user["money"]
        if bet <= 0:
            continue
        user["last_pred"] = ai_pred
        user["last_bet"] = int(bet)
        user["last_bet_choice"] = bet_choice
        percent = ((user["money"] - user["start_money"]) / user["start_money"] * 100)

        # Gửi về Telegram
        app = context_application  # dùng biến global app
        try:
            await app.bot.send_message(
                chat_id=uid,
                text=(
                    f"<pre>"
                    f"🔥 TX TOOL BOT\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"🎲 {dice} = {real}\n"
                    f"📊 {result_text}\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"🎯 Dự đoán: {bet_choice}\n"
                    f"💰 Cược: {money(bet)}\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"💰 Vốn: {money(user['money'])}\n"
                    f"📈 Lãi: {money(user['profit'])}\n"
                    f"📊 {percent:.1f}%\n"
                    f"━━━━━━━━━━━━━━\n"
                    f"🏆 {user['win']} | ❌ {user['lose']}\n"
                    f"</pre>"
                ),
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logging.warning(f"Không gửi được cho {uid}: {e}")

# ===== WEBSOCKET LISTEN =====
async def listen_websocket():
    async with websockets.connect(WEBSOCKET_URL) as ws:
        print("✅ Kết nối WebSocket...")
        while True:
            msg = await ws.recv()
            try:
                data = json.loads(msg)
                if "dice" in data:
                    dice = data["dice"]  # ví dụ: [3,5,6]
                    await process_game(dice)
            except:
                continue

# ===== MAIN =====
async def main():
    global context_application
    app = ApplicationBuilder().token(TOKEN).build()
    context_application = app  # lưu global để send_message

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setmoney", setmoney))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("resetall", resetall))

    # Chạy WebSocket song song
    asyncio.create_task(listen_websocket())

    print("🔥 BOT RUNNING")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
