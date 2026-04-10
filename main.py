# bot.py
import os
import re
from pathlib import Path

from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, MessageHandler, filters

def load_env():
    p = Path(".env")
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip().strip('"').strip("'")

load_env()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0").strip() or 0)

if not BOT_TOKEN:
    raise RuntimeError("Thiếu BOT_TOKEN trong file .env")
if not ADMIN_ID:
    raise RuntimeError("Thiếu ADMIN_ID trong file .env")

HEX_RE = re.compile(r"^[0-9a-fA-F]{8,64}$")

def predict(md5: str):
    total = sum(int(c, 16) for c in md5.lower())
    score = (total % 16) + 3   # 3..18
    result = "TÀI" if score >= 11 else "XỈU"
    return score, result

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or update.effective_user.id != ADMIN_ID:
        return

    text = (update.message.text or "").strip()

    if not HEX_RE.fullmatch(text):
        return

    score, result = predict(text)

    # Chỉ trả lại kết quả phân tích, không lưu gì cả
    await update.message.reply_text(f"{result} ({score})")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT, handle))
    print("Bot đang chạy...")
    app.run_polling()

if __name__ == "__main__":
    main()
