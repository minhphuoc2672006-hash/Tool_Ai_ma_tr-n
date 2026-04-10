# bot.py
import os
import re
import json
import time
import threading
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROUP_ID = int(os.getenv("GROUP_ID", "0").strip() or 0)
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or 0)

if not BOT_TOKEN:
    raise RuntimeError("Thiếu BOT_TOKEN trong file .env")
if not GROUP_ID:
    raise RuntimeError("Thiếu GROUP_ID trong file .env")
if not ADMIN_ID:
    raise RuntimeError("Thiếu ADMIN_ID trong file .env")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
STATE_FILE = Path("state.json")

HEX_RE = re.compile(r"^[0-9a-fA-F]{8,64}$")
RESULT_RE = re.compile(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$")

lock = threading.Lock()


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(data):
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


state = load_state()


def ensure_group_state():
    key = str(GROUP_ID)
    if key not in state:
        state[key] = {
            "wins": 0,
            "losses": 0,
            "busy": False,
            "last_prediction": None,
            "last_score": None,
            "last_input": None,
            "last_result": None,
        }
    return state[key]


def send_message(text):
    r = requests.post(
        f"{API}/sendMessage",
        json={
            "chat_id": GROUP_ID,
            "text": text,
            "disable_web_page_preview": True,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def is_hex_input(text):
    return bool(HEX_RE.fullmatch(text.strip()))


def predict_from_hex(hex_text):
    cleaned = hex_text.strip().lower()
    total = sum(int(ch, 16) for ch in cleaned)
    score = (total % 16) + 3  # 3..18
    label = "TÀI" if score >= 11 else "XỈU"
    return cleaned, total, score, label


def parse_result_text(text):
    m = RESULT_RE.fullmatch(text.strip())
    if not m:
        return None
    a, b, c = map(int, m.groups())
    total = a + b + c
    label = "TÀI" if total >= 11 else "XỈU"
    return (a, b, c), total, label


def win_rate(wins: int, losses: int) -> int:
    total = wins + losses
    if total <= 0:
        return 0
    return int((wins / total) * 100)


def prediction_flow(hex_text):
    with lock:
        ch = ensure_group_state()
        if ch["busy"]:
            send_message("🕗 Đang xử lý phiên trước...")
            return
        ch["busy"] = True
        save_state(state)

    try:
        cleaned, total, score, label = predict_from_hex(hex_text)

        with lock:
            ch = ensure_group_state()
            ch["last_input"] = cleaned
            ch["last_prediction"] = label
            ch["last_score"] = score
            ch["last_result"] = None
            save_state(state)

        wins = ensure_group_state()["wins"]
        losses = ensure_group_state()["losses"]

        # Gửi từng dòng riêng biệt
        send_message("🔍 Lấy Kết Quả Từ Hệ Thống...")
        send_message("🌐 Nguồn: https://www.luckywin882.com/")
        time.sleep(1)

        send_message("📡 Đang kết nối hệ thống...")
        time.sleep(1)

        send_message("🔄 Đang xử lý dữ liệu...")
        time.sleep(1)

        send_message(f"🏆 Phiên vừa xong: {label} - {score} ✅")
        send_message(f"🔸 Tổng thắng: {wins}")
        send_message(f"🔸 Tổng thua: {losses}")

        send_message("🧠 Đang phân tích cầu...")
        send_message("📊 Đang tính toán xác suất...")

        send_message("🤖 Bot Đang Phân Tích Giải Mã Chuỗi MD5 🤖")
        send_message(f"• Mã MD5: [{cleaned}]")

        time.sleep(5)

        ch = ensure_group_state()
        rate = win_rate(ch["wins"], ch["losses"])

        send_message(f"📣 Mọi người! Hãy chọn : {label}")
        send_message(f"🔍 Tỉ lệ thắng : {rate}%")
        send_message("🕗 Chờ kết quả...")

    finally:
        with lock:
            ch = ensure_group_state()
            ch["busy"] = False
            save_state(state)


def check_admin_result(text):
    parsed = parse_result_text(text)
    if not parsed:
        return False

    nums, total, actual_label = parsed

    with lock:
        ch = ensure_group_state()
        predicted = ch["last_prediction"]

        if not predicted:
            send_message("⚠️ Chưa có dự đoán trước đó để kiểm tra.")
            return True

        ch["last_result"] = actual_label

        if actual_label == predicted:
            ch["wins"] += 1
            status = "✅ DỰ ĐOÁN ĐÚNG"
        else:
            ch["losses"] += 1
            status = "❌ DỰ ĐOÁN SAI"

        save_state(state)

        wins = ch["wins"]
        losses = ch["losses"]
        rate = win_rate(wins, losses)

    # Gửi từng dòng riêng
    send_message("🏆 KẾT QUẢ PHIÊN")
    send_message(f"🎲 Kết quả: {nums[0]}-{nums[1]}-{nums[2]} → {total} ({actual_label})")
    send_message(f"📊 Dự đoán trước: {predicted}")
    send_message(f"🎯 {status}")
    send_message(f"🔸 Tổng thắng: {wins}")
    send_message(f"🔸 Tổng thua: {losses}")
    send_message(f"🔍 Tỉ lệ thắng: {rate}%")
    return True


def handle_message(update):
    message = update.get("message") or update.get("edited_message")
    if not message:
        return

    chat = message.get("chat", {})
    chat_id = chat.get("id")
    if chat_id != GROUP_ID:
        return

    user = message.get("from", {})
    user_id = user.get("id", 0)
    text = (message.get("text") or "").strip()
    if not text:
        return

    # Chỉ admin mới được điều khiển bot
    if user_id != ADMIN_ID:
        return

    # Lệnh phụ cho admin
    if text == "/reset":
        with lock:
            state[str(GROUP_ID)] = {
                "wins": 0,
                "losses": 0,
                "busy": False,
                "last_prediction": None,
                "last_score": None,
                "last_input": None,
                "last_result": None,
            }
            save_state(state)
        send_message("🔄 Đã reset thống kê.")
        return

    if text == "/stats":
        ch = ensure_group_state()
        rate = win_rate(ch["wins"], ch["losses"])
        send_message(f"🔸 Tổng thắng: {ch['wins']}")
        send_message(f"🔸 Tổng thua: {ch['losses']}")
        send_message(f"🔍 Tỉ lệ thắng: {rate}%")
        return

    # Admin nhập kết quả thật: 6-5-4
    if check_admin_result(text):
        return

    # Admin nhập chuỗi MD5 để bot phân tích
    if is_hex_input(text):
        t = threading.Thread(target=prediction_flow, args=(text,), daemon=True)
        t.start()
        return


def main():
    offset = 0
    ensure_group_state()
    save_state(state)
    print("Bot đang chạy...")

    while True:
        try:
            r = requests.get(
                f"{API}/getUpdates",
                params={"offset": offset, "timeout": 30},
                timeout=40,
            )
            r.raise_for_status()
            data = r.json()

            if not data.get("ok"):
                time.sleep(1)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                handle_message(update)

        except Exception as e:
            print("Lỗi:", e)
            time.sleep(2)


if __name__ == "__main__":
    main()
