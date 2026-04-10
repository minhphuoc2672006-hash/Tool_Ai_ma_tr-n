# bot.py
import asyncio
import json
import os
import re
from pathlib import Path

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ========= .env loader nhẹ =========
def load_env_file(path: str = ".env") -> None:
    p = Path(path)
    if not p.exists():
        return

    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_env_file()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROUP_ID = int(os.getenv("GROUP_ID", "0").strip() or 0)
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or 0)

if not BOT_TOKEN:
    raise RuntimeError("Thiếu BOT_TOKEN trong .env")
if not GROUP_ID:
    raise RuntimeError("Thiếu GROUP_ID trong .env")
if not ADMIN_ID:
    raise RuntimeError("Thiếu ADMIN_ID trong .env")

HEX_RE = re.compile(r"^[0-9a-fA-F]{8,64}$")
RESULT_RE = re.compile(r"^\s*(\d{1,2})\s*-\s*(\d{1,2})\s*-\s*(\d{1,2})\s*$")

STATE_FILE = Path("state.json")
STATE_LOCK = asyncio.Lock()


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


STATE = load_state()


def ensure_group_state() -> dict:
    key = str(GROUP_ID)
    if key not in STATE:
        STATE[key] = {
            "wins": 0,
            "losses": 0,
            "phase": "WAIT_MD5",   # WAIT_MD5 -> PREDICTING -> WAIT_RESULT
            "last_prediction": None,
            "last_score": None,
            "last_input": None,
            "last_result": None,
        }
    return STATE[key]


def is_hex_input(text: str) -> bool:
    return bool(HEX_RE.fullmatch(text.strip()))


def predict_from_hex(hex_text: str):
    cleaned = hex_text.strip().lower()
    total = sum(int(ch, 16) for ch in cleaned)
    score = (total % 16) + 3   # 3..18
    label = "TÀI" if score >= 11 else "XỈU"
    return cleaned, total, score, label


def parse_result_text(text: str):
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


async def gsend(bot, text: str):
    await bot.send_message(
        chat_id=GROUP_ID,
        text=text,
        disable_web_page_preview=True,
    )


async def prediction_flow(bot, hex_text: str):
    async with STATE_LOCK:
        ch = ensure_group_state()
        if ch["phase"] != "WAIT_MD5":
            return

        ch["phase"] = "PREDICTING"
        save_state(STATE)

    try:
        cleaned, total, score, label = predict_from_hex(hex_text)

        async with STATE_LOCK:
            ch = ensure_group_state()
            ch["last_input"] = cleaned
            ch["last_prediction"] = label
            ch["last_score"] = score
            ch["last_result"] = None
            save_state(STATE)
            wins = ch["wins"]
            losses = ch["losses"]

        # Mỗi dòng là 1 tin nhắn riêng
        await gsend(bot, "🔍 Lấy Kết Quả Từ Hệ Thống...")
        await gsend(bot, "🌐 Nguồn: https://www.luckywin882.com/")
        await asyncio.sleep(1)

        await gsend(bot, "📡 Đang kết nối hệ thống...")
        await asyncio.sleep(1)

        await gsend(bot, "🔄 Đang xử lý dữ liệu...")
        await asyncio.sleep(1)

        await gsend(bot, f"🏆 Phiên vừa xong: {label} - {score} ✅")
        await gsend(bot, f"🔸 Tổng thắng: {wins}")
        await gsend(bot, f"🔸 Tổng thua: {losses}")

        await gsend(bot, "🧠 Đang phân tích cầu...")
        await gsend(bot, "📊 Đang tính toán xác suất...")

        await gsend(bot, "🤖 Bot Đang Phân Tích Giải Mã Chuỗi MD5 🤖")
        await gsend(bot, f"• Mã MD5: [{cleaned}]")

        # Delay đúng chỗ này
        await asyncio.sleep(5)

        async with STATE_LOCK:
            ch = ensure_group_state()
            rate = win_rate(ch["wins"], ch["losses"])
            ch["phase"] = "WAIT_RESULT"
            save_state(STATE)

        await gsend(bot, f"📣 Mọi người! Hãy chọn : {label}")
        await gsend(bot, f"🔍 Tỉ lệ thắng : {rate}%")
        await gsend(bot, "🕗 Chờ kết quả...")

    finally:
        # Không mở WAIT_MD5 ở đây, vì phải chờ admin nhập 6-5-4
        pass


async def check_admin_result(bot, text: str):
    parsed = parse_result_text(text)
    if not parsed:
        return False

    nums, total, actual_label = parsed

    async with STATE_LOCK:
        ch = ensure_group_state()

        # Chỉ cho check khi đang chờ kết quả
        if ch["phase"] != "WAIT_RESULT":
            return True

        predicted = ch["last_prediction"]
        if not predicted:
            return True

        ch["last_result"] = actual_label

        if actual_label == predicted:
            ch["wins"] += 1
            status = "✅ DỰ ĐOÁN ĐÚNG"
        else:
            ch["losses"] += 1
            status = "❌ DỰ ĐOÁN SAI"

        wins = ch["wins"]
        losses = ch["losses"]
        rate = win_rate(wins, losses)

        # Xong phiên, mở lại để nhận MD5 mới
        ch["phase"] = "WAIT_MD5"
        save_state(STATE)

    # Mỗi dòng là 1 tin riêng
    await gsend(bot, "🏆 KẾT QUẢ PHIÊN")
    await gsend(bot, f"🎲 Kết quả: {nums[0]}-{nums[1]}-{nums[2]} → {total} ({actual_label})")
    await gsend(bot, f"📊 Dự đoán trước: {predicted}")
    await gsend(bot, f"🎯 {status}")
    await gsend(bot, f"🔸 Tổng thắng: {wins}")
    await gsend(bot, f"🔸 Tổng thua: {losses}")
    await gsend(bot, f"🔍 Tỉ lệ thắng: {rate}%")
    return True


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    # Không trả lời trong inbox nếu bạn không muốn


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return
    # Không trả lời trong inbox nếu bạn không muốn


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return

    async with STATE_LOCK:
        STATE[str(GROUP_ID)] = {
            "wins": 0,
            "losses": 0,
            "phase": "WAIT_MD5",
            "last_prediction": None,
            "last_score": None,
            "last_input": None,
            "last_result": None,
        }
        save_state(STATE)

    await gsend(context.bot, "🔄 Đã reset thống kê.")


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return

    async with STATE_LOCK:
        ch = ensure_group_state()
        wins = ch["wins"]
        losses = ch["losses"]
        rate = win_rate(wins, losses)

    await gsend(context.bot, f"🔸 Tổng thắng: {wins}")
    await gsend(context.bot, f"🔸 Tổng thua: {losses}")
    await gsend(context.bot, f"🔍 Tỉ lệ thắng: {rate}%")


async def private_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Chỉ nhận tin nhắn riêng của admin
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return

    text = (update.message.text or "").strip()

    # /reset
    if text == "/reset":
        await reset_cmd(update, context)
        return

    # /stats
    if text == "/stats":
        await stats_cmd(update, context)
        return

    # MD5 -> phân tích -> gửi sang group
    async with STATE_LOCK:
        phase = ensure_group_state()["phase"]

    if phase == "WAIT_MD5" and is_hex_input(text):
        context.application.create_task(prediction_flow(context.bot, text))
        return

    # 6-5-4 -> check -> gửi sang group
    if phase == "WAIT_RESULT" and parse_result_text(text):
        await check_admin_result(context.bot, text)
        return

    # Không trả lời inbox nếu nhập sai
    return


def main():
    ensure_group_state()
    save_state(STATE)

    app: Application = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))

    # Chỉ đọc chat riêng với bot
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            private_text_handler,
        )
    )

    print("Bot đang chạy...")
    app.run_polling()


if __name__ == "__main__":
    main()
