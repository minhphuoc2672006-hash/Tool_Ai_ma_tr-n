import os
import logging
import random
import asyncio
import json
import websockets
from collections import defaultdict
from telegram import Bot, ParseMode

# ===== CONFIG =====
logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)

TOKEN = os.getenv("TOKEN")            # Bot token
USER_CHAT_ID = os.getenv("USER_CHAT_ID")  # Chat ID cá nhân

if not TOKEN or not USER_CHAT_ID:
    raise Exception("❌ Thiếu TOKEN hoặc USER_CHAT_ID")

bot = Bot(token=TOKEN)
users = {}

# ===== FORMAT =====
def money(x):
    return f"{int(x):,}".replace(",", ".")

# ===== PHÂN LOẠI =====
def classify_total(total):
    return "Tài" if total >= 11 else "Xỉu"

# ===== RANDOM DICE (AI Mô phỏng) =====
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

# ===== AI DỰ ĐOÁN =====
def ai_predict(dice):
    key = tuple(sorted(dice))
    if key not in AI_MAPPING:
        return "Tài"
    data = AI_MAPPING[key]
    return "Tài" if data["Tài"] >= data["Xỉu"] else "Xỉu"

# ===== CẬP NHẬT NGƯỜI CHƠI =====
def update_user(dice):
    total = sum(dice)
    real = classify_total(total)
    uid = USER_CHAT_ID
    if uid not in users:
        start_money = 500_000
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
    user = users[uid]

    # ===== WIN/LOSE =====
    prev_bet = user["last_bet"]
    result_text = "..."
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

    # ===== AI + ĐÁNH NGƯỢC =====
    ai_pred = ai_predict(dice)
    bet_choice = "Tài" if ai_pred == "Xỉu" else "Xỉu"

    # ===== BET =====
    bet = user["base_bet"] * user["step"]
    if bet > user["money"]:
        bet = user["money"]
    if bet <= 0:
        return "🛑 HẾT TIỀN"

    user["last_pred"] = ai_pred
    user["last_bet"] = int(bet)
    user["last_bet_choice"] = bet_choice

    percent = ((user["money"] - user["start_money"]) / user["start_money"] * 100)

    # ===== GIAO DIỆN =====
    msg = (
        "<pre>"
        "🔥 TX TOOL AUTO\n"
        "━━━━━━━━━━━━━━\n"
        f"🎲 {dice} = {real}\n\n"
        f"📊 {result_text}\n"
        "━━━━━━━━━━━━━━\n"
        f"🎯 Dự đoán: {bet_choice}\n"
        f"💰 Cược: {money(bet)}\n"
        "━━━━━━━━━━━━━━\n"
        f"💰 Vốn: {money(user['money'])}\n"
        f"📈 Lãi: {money(user['profit'])}\n"
        f"📊 {percent:.1f}%\n"
        "━━━━━━━━━━━━━━\n"
        f"🏆 {user['win']} | ❌ {user['lose']}\n"
        "</pre>"
    )
    return msg

# ===== LẮNG NGHE WEBSOCKET GAME =====
async def realtime_listener():
    uri = "wss://your-game-websocket-endpoint"  # Thay bằng WebSocket thực tế
    async with websockets.connect(uri) as ws:
        while True:
            try:
                data = await ws.recv()
                payload = json.loads(data)
                dice = payload.get("dice", [1,2,3])
                msg = update_user(dice)
                await bot.send_message(chat_id=USER_CHAT_ID, text=msg, parse_mode=ParseMode.HTML)
            except Exception as e:
                print("⚠️ Lỗi WebSocket:", e)
                await asyncio.sleep(1)

# ===== MAIN =====
async def main():
    print("🔥 BOT FULL AUTO ĐANG CHẠY...")
    await realtime_listener()

if __name__ == "__main__":
    asyncio.run(main())
