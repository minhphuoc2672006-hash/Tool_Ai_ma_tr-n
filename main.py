import os
import cv2
import pytesseract
import asyncio
import logging
import random
from collections import Counter, defaultdict
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, CommandHandler, ContextTypes, filters

# ===== CONFIG =====
logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise Exception("❌ Thiếu TOKEN")

# OCR path (Linux server / Railway)
pytesseract.pytesseract.tesseract_cmd = "/usr/bin/tesseract"

users = {}
markov = defaultdict(Counter)

# ===== UTIL =====
def get_key(update):
    return update.effective_chat.id

def to_tx(n):
    return "Tài" if n >= 11 else "Xỉu"

def parse_input(text):
    nums = [int(x) for x in text.split() if x.isdigit()]
    if len(nums) == 3:
        return sum(nums)
    if len(nums) == 1:
        return nums[0]
    return None

# ===== OCR ZIGZAG =====
def read_grid_zigzag(path):
    img = cv2.imread(path)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (5,5), 0)
    thresh = cv2.adaptiveThreshold(
        blur,255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY_INV,
        11,2
    )
    contours,_ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for cnt in contours:
        x,y,w,h = cv2.boundingRect(cnt)
        if 25 < w < 120 and 25 < h < 120:
            roi = gray[y:y+h, x:x+w]
            txt = pytesseract.image_to_string(
                roi,
                config="--psm 6 digits"
            ).strip()
            if txt.isdigit():
                n = int(txt)
                if 3 <= n <= 18:
                    boxes.append((x,y,n))
    boxes.sort(key=lambda b: b[1])
    rows = []
    current = []
    last_y = None
    for b in boxes:
        if last_y is None or abs(b[1] - last_y) < 40:
            current.append(b)
            last_y = b[1]
        else:
            rows.append(current)
            current = [b]
            last_y = b[1]
    if current:
        rows.append(current)
    result = []
    for i,row in enumerate(rows):
        row.sort(key=lambda b: b[0])
        if i % 2 == 0:
            row = row[::-1]
        for b in row:
            result.append(b[2])
    return result

# ===== AI =====
def update_markov(history):
    for i in range(len(history)-1):
        markov[history[i]][history[i+1]] += 1

def markov_predict(history):
    if not history:
        return "Tài", 50
    last = history[-1]
    if last in markov:
        nxt = markov[last]
        total = sum(nxt.values())
        pred = nxt.most_common(1)[0][0]
        conf = int(nxt[pred]/total*100)
        return pred, conf
    return random.choice(["Tài","Xỉu"]), 50

def freq_predict(history):
    if len(history) < 5:
        return "Tài", 50
    c = Counter(history[-30:])
    pred = c.most_common(1)[0][0]
    conf = int(c[pred]/len(history[-30:])*100)
    return pred, conf

def anti_streak(history):
    if len(history) >= 3 and history[-1] == history[-2] == history[-3]:
        return ("Xỉu" if history[-1]=="Tài" else "Tài"), 65
    return history[-1] if history else "Tài", 55

def detect_pattern(history):
    if len(history) < 6:
        return None
    if history[-1] == history[-3] and history[-2] == history[-4]:
        return "repeat"
    return None

def final_ai(history):
    update_markov(history)
    preds, confs = [], []
    for f in [markov_predict, freq_predict, anti_streak]:
        p,c = f(history)
        preds.append(p)
        confs.append(c)
    pattern = detect_pattern(history)
    if pattern == "repeat":
        preds.append(history[-1])
        confs.append(75)
    final = Counter(preds).most_common(1)[0][0]
    conf = int(sum(confs)/len(confs))
    return final, conf

# ===== HANDLER =====
async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = get_key(update)
    users.setdefault(key,{
        "history":[],
        "win":0,
        "lose":0,
        "total":0,
        "last_pred":None
    })
    data = users[key]

    text = update.message.text.strip() if update.message.text else ""
    nums = []

    # Xử lý reset
    if text == "/reset":
        users[key] = {"history":[], "win":0,"lose":0,"total":0,"last_pred":None}
        await update.message.reply_text("Đã reset")
        return

    # Xử lý text
    if text:
        n = parse_input(text)
        if n: nums.append(n)

    # Xử lý ảnh
    if update.message.photo:
        file = await update.message.photo[-1].get_file()
        path = f"{key}.jpg"
        await file.download_to_drive(path)
        await update.message.reply_text("Đang đọc ảnh...")
        results = read_grid_zigzag(path)
        nums.extend(results)
        await update.message.reply_text(f"Đã lấy {len(results)} kết quả từ ảnh")

    if not nums:
        return

    # Xử lý kết quả
    for n in nums:
        if not (3 <= n <= 18): continue
        tx = to_tx(n)
        msg = f"Kết quả: {n} ({tx})"
        if data["last_pred"]:
            if data["last_pred"] == tx:
                data["win"] += 1
                msg += f"\nDự đoán trước: {data['last_pred']} → Thắng"
            else:
                data["lose"] += 1
                msg += f"\nDự đoán trước: {data['last_pred']} → Thua"
        await update.message.reply_text(msg)
        data["total"] += 1
        data["history"].append(tx)
        rate = int((data["win"]/data["total"])*100) if data["total"] else 0
        await update.message.reply_text(f"Tổng: {data['total']}\nThắng: {data['win']}\nThua: {data['lose']}\nTỷ lệ: {rate}%")

    # Dự đoán AI
    wait = await update.message.reply_text("Đang phân tích...")
    await asyncio.sleep(5)
    pred, conf = final_ai(data["history"])
    data["last_pred"] = pred
    await wait.edit_text(f"Dự đoán: {pred}\nTỷ lệ: {conf}%")

# ===== RUN =====
if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("reset", handle))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.add_handler(MessageHandler(filters.PHOTO, handle))
    app.run_polling()
