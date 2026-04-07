#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram bot thống kê Tài/Xỉu theo lịch sử.

Chỉ ADMIN mới sử dụng được.

Tính năng:
- /start, /help
- /add <dữ liệu>      : thêm 1 hoặc nhiều kết quả
- /import <dữ liệu>   : nhập chuỗi lịch sử dài
- /history [n]        : xem n kết quả gần nhất
- /stats [n]          : thống kê tần suất
- /scan [n]           : phân tích lịch sử (chỉ 2 dòng)
- /clear              : xóa toàn bộ lịch sử

Hỗ trợ nhập:
- T, X
- Tài, Xỉu
- số 4-10 => X
- số 11-17 => T
- số 3, 18 bỏ qua
"""

import os
import re
import sqlite3
import logging
from collections import Counter
from typing import List, Optional, Tuple

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DB_PATH = os.getenv("TAI_XIU_DB_PATH", "tai_xiu_stats.db")
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tai_xiu_bot")


# =========================
# ADMIN CHECK
# =========================
def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ADMIN_USER_ID)


async def deny_if_not_admin(update: Update):
    if update.message:
        await update.message.reply_text("Bot này chỉ dành cho ADMIN.")
    return


# =========================
# DB
# =========================
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rounds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                raw TEXT NOT NULL,
                outcome TEXT NOT NULL CHECK(outcome IN ('T', 'X'))
            )
            """
        )
        conn.commit()


def save_outcomes(outcomes: List[str], raw: str):
    if not outcomes:
        return
    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO rounds(raw, outcome) VALUES(?, ?)",
            [(raw, o) for o in outcomes],
        )
        conn.commit()


def delete_all():
    with get_conn() as conn:
        conn.execute("DELETE FROM rounds")
        conn.commit()


def load_history(limit: int = 200) -> List[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT outcome FROM rounds ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [r["outcome"] for r in reversed(rows)]


def load_rows(limit: int = 20):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, created_at, raw, outcome FROM rounds ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return list(reversed(rows))


# =========================
# PARSE DỮ LIỆU
# =========================
def normalize_token(token: str) -> Optional[str]:
    t = token.strip().upper()

    if t in {"T", "TAI", "TÀI"}:
        return "T"
    if t in {"X", "XIU", "XỈU"}:
        return "X"

    if re.fullmatch(r"\d{1,2}", t):
        n = int(t)
        if 11 <= n <= 17:
            return "T"
        if 4 <= n <= 10:
            return "X"
        return None

    return None


def extract_outcomes(text: str) -> List[str]:
    if not text:
        return []

    upper_text = text.upper()
    tokens = re.findall(r"TÀI|TAI|XỈU|XIU|T|X|\d{1,2}", upper_text)

    results = []
    for tok in tokens:
        mapped = normalize_token(tok)
        if mapped in {"T", "X"}:
            results.append(mapped)
    return results


# =========================
# PHÂN TÍCH MẪU
# =========================
def rle(seq: List[str]) -> List[Tuple[str, int]]:
    if not seq:
        return []

    out = []
    cur = seq[0]
    cnt = 1

    for x in seq[1:]:
        if x == cur:
            cnt += 1
        else:
            out.append((cur, cnt))
            cur = x
            cnt = 1

    out.append((cur, cnt))
    return out


def current_streak(seq: List[str]) -> Tuple[str, int]:
    if not seq:
        return ("", 0)

    last = seq[-1]
    count = 1
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] == last:
            count += 1
        else:
            break
    return (last, count)


def detect_alternating_tail(seq: List[str], min_len: int = 6) -> Optional[int]:
    if len(seq) < min_len:
        return None

    tail = seq[-min_len:]
    if all(tail[i] != tail[i + 1] for i in range(len(tail) - 1)):
        length = min_len
        i = len(seq) - min_len - 1
        while i >= 0 and seq[i] != seq[i + 1]:
            length += 1
            i -= 1
        return length

    return None


def detect_periodic_tail(
    seq: List[str],
    min_period: int = 2,
    max_period: int = 8,
    min_repeats: int = 3,
):
    n = len(seq)
    for period in range(min_period, max_period + 1):
        for repeats in range(min_repeats, 7):
            need = period * repeats
            if n < need:
                continue
            tail = seq[-need:]
            motif = tail[:period]
            if tail == motif * repeats:
                return motif, repeats
    return None, 0


def detect_equal_segments(segments: List[Tuple[str, int]]) -> Optional[str]:
    if len(segments) < 3:
        return None

    last = segments[-4:] if len(segments) >= 4 else segments[-3:]
    lengths = [s[1] for s in last]
    if len(lengths) >= 3 and len(set(lengths)) == 1:
        size = lengths[0]
        vals = "-".join([f"{size}" for _ in lengths])
        return f"Cầu nhịp đều {vals}"

    return None


def detect_mixed_pattern(segments: List[Tuple[str, int]]) -> Optional[str]:
    if len(segments) < 5:
        return None

    last = segments[-6:]
    lengths = [s[1] for s in last]
    uniq = len(set(lengths))

    if uniq >= 3:
        return "Cầu hỗn hợp"

    if uniq == 2 and max(lengths) >= 3 and min(lengths) == 1:
        return "Cầu pha trộn"

    return None


def detect_break_type(segments: List[Tuple[str, int]]) -> Optional[str]:
    if len(segments) < 2:
        return None

    prev_val, prev_len = segments[-2]
    cur_val, cur_len = segments[-1]

    if prev_len >= 5 and cur_len == 1:
        return f"Bẻ cầu yếu sau bệt {prev_val} x{prev_len}"
    if prev_len >= 5 and cur_len >= 2:
        return f"Bẻ cầu mạnh sau bệt {prev_val} x{prev_len}"

    return None


def detect_phase_shift(seq: List[str]) -> Optional[str]:
    if len(seq) < 20:
        return None

    prev = seq[-20:-10]
    curr = seq[-10:]

    c1 = Counter(prev)
    c2 = Counter(curr)

    prev_major, prev_count = c1.most_common(1)[0]
    curr_major, curr_count = c2.most_common(1)[0]

    if prev_major != curr_major:
        if prev_count >= 7 and curr_count >= 7:
            return f"Chuyển pha từ {prev_major} sang {curr_major}"

    return None


def ai_analyze(seq: List[str]) -> str:
    """
    Phân tích theo dữ liệu gần nhất, có trọng số giảm dần để không bị kẹt ở quá khứ.
    Trả về đúng 2 dòng.
    """
    if not seq:
        return "Xu hướng gần nhất: Chưa đủ dữ liệu\nĐộ tin cậy phân tích: 60%"

    recent_limit = min(len(seq), 140)
    window = seq[-recent_limit:]

    decay = 0.965
    weights = [decay ** (len(window) - 1 - i) for i in range(len(window))]

    w_t = 0.0
    w_x = 0.0
    for x, w in zip(window, weights):
        if x == "T":
            w_t += w
        elif x == "X":
            w_x += w

    total_w = w_t + w_x
    if total_w == 0:
        trend = "Cân bằng"
        base_strength = 0.0
    else:
        diff = w_t - w_x
        trend = "Tài" if diff > 0 else "Xỉu" if diff < 0 else "Cân bằng"
        base_strength = abs(diff) / total_w

    segments = rle(window)
    _, streak_len = current_streak(window)
    alt_len = detect_alternating_tail(window)
    motif, rep = detect_periodic_tail(window)
    phase = detect_phase_shift(window)

    bonus = 0
    if streak_len >= 3:
        bonus += 1
    if streak_len >= 5:
        bonus += 2
    if streak_len >= 8:
        bonus += 3
    if alt_len:
        bonus += 2
    if motif:
        bonus += 3
    if detect_equal_segments(segments):
        bonus += 1
    if detect_mixed_pattern(segments):
        bonus -= 1
    if detect_break_type(segments):
        bonus += 1
    if phase:
        bonus += 1

    confidence = 60 + int(base_strength * 18) + bonus
    confidence = max(60, min(89, confidence))

    return f"Xu hướng gần nhất: {trend}\nĐộ tin cậy phân tích: {confidence}%"


# =========================
# TELEGRAM HANDLERS
# =========================
WELCOME = (
    "Bot thống kê Tài/Xỉu đã sẵn sàng.\n\n"
    "Lệnh dùng:\n"
    "/add <dữ liệu>      - thêm 1 hoặc nhiều kết quả\n"
    "/import <dữ liệu>   - dán lịch sử dài\n"
    "/history [n]        - xem n kết quả gần nhất\n"
    "/stats [n]          - thống kê tần suất\n"
    "/scan [n]           - phân tích lịch sử (2 dòng)\n"
    "/clear              - xóa toàn bộ lịch sử\n\n"
    "Chỉ ADMIN mới dùng được."
)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    await update.message.reply_text(WELCOME)


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)

    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Dùng: /add T X T 11 8 14")
        return

    items = extract_outcomes(text)
    if not items:
        await update.message.reply_text("Không tìm thấy dữ liệu hợp lệ.")
        return

    save_outcomes(items, text)
    seq = load_history(200)
    report = ai_analyze(seq)
    await update.message.reply_text(f"Đã thêm {len(items)} kết quả.\n\n{report}")


async def import_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)

    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Dùng: /import T X X T 11 8 14 6 ...")
        return

    items = extract_outcomes(text)
    if not items:
        await update.message.reply_text("Không tìm thấy dữ liệu hợp lệ.")
        return

    save_outcomes(items, text)
    seq = load_history(200)
    report = ai_analyze(seq)
    await update.message.reply_text(f"Đã nhập {len(items)} kết quả từ lịch sử.\n\n{report}")


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)

    n = 20
    if context.args and context.args[0].isdigit():
        n = max(1, min(200, int(context.args[0])))

    rows = load_rows(n)
    if not rows:
        await update.message.reply_text("Chưa có lịch sử.")
        return

    lines = ["Lịch sử gần nhất:"]
    for r in rows:
        lines.append(f"#{r['id']} | {r['outcome']} | {r['created_at']}")
    await update.message.reply_text("\n".join(lines))


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)

    n = 200
    if context.args and context.args[0].isdigit():
        n = max(1, min(2000, int(context.args[0])))

    seq = load_history(n)
    if not seq:
        await update.message.reply_text("Chưa có dữ liệu.")
        return

    count = Counter(seq)
    total = len(seq)
    msg = (
        f"Thống kê {total} mẫu gần nhất:\n"
        f"- Tài: {count['T']} ({count['T'] / total * 100:.1f}%)\n"
        f"- Xỉu: {count['X']} ({count['X'] / total * 100:.1f}%)"
    )
    await update.message.reply_text(msg)


async def scan_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)

    n = 200
    if context.args and context.args[0].isdigit():
        n = max(1, min(2000, int(context.args[0])))

    seq = load_history(n)
    if not seq:
        await update.message.reply_text("Chưa có dữ liệu để quét.")
        return

    report = ai_analyze(seq)
    await update.message.reply_text(report)


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)

    delete_all()
    await update.message.reply_text("Đã xóa toàn bộ lịch sử.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return

    text = update.message.text or ""
    items = extract_outcomes(text)
    if not items:
        return

    save_outcomes(items, text)
    seq = load_history(200)
    report = ai_analyze(seq)
    await update.message.reply_text(f"Đã tự động lưu {len(items)} kết quả.\n\n{report}")


def main():
    if not TOKEN:
        raise RuntimeError("Thiếu TELEGRAM_BOT_TOKEN trong biến môi trường.")
    if not ADMIN_USER_ID:
        raise RuntimeError("Thiếu ADMIN_USER_ID trong biến môi trường.")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("import", import_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot đang chạy...")
    app.run_polling()


if __name__ == "__main__":
    main()
