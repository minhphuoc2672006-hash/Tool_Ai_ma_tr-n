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
- /scan [n]           : phân tích lịch sử (2 dòng)
- /patterns [n]       : xem nhận diện cầu
- /clear              : xóa toàn bộ lịch sử

Hỗ trợ nhập:
- T, X
- Tài, Xỉu
- số 3-10  => X
- số 11-18 => T
- số 1,2,19+ bỏ qua
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

_admin_raw = os.getenv("ADMIN_USER_ID", "").strip()
try:
    ADMIN_USER_ID = int(_admin_raw) if _admin_raw else 0
except ValueError:
    ADMIN_USER_ID = 0

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
TOKEN_RE = re.compile(r"\b(?:TÀI|TAI|XỈU|XIU|T|X|\d+)\b", re.UNICODE)


def normalize_token(token: str) -> Optional[str]:
    t = token.strip().upper()

    if t in {"T", "TAI", "TÀI"}:
        return "T"
    if t in {"X", "XIU", "XỈU"}:
        return "X"

    if t.isdigit() and 1 <= len(t) <= 2:
        n = int(t)
        if 3 <= n <= 10:
            return "X"
        if 11 <= n <= 18:
            return "T"
        return None

    return None


def extract_outcomes(text: str) -> List[str]:
    if not text:
        return []

    upper_text = text.upper()
    tokens = TOKEN_RE.findall(upper_text)

    results = []
    for tok in tokens:
        mapped = normalize_token(tok)
        if mapped in {"T", "X"}:
            results.append(mapped)
    return results


# =========================
# PATTERN HELPERS
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


def detect_exact_periodic_tail(
    seq: List[str],
    min_period: int = 2,
    max_period: int = 10,
    min_repeats: int = 3,
):
    n = len(seq)
    for period in range(min_period, max_period + 1):
        for repeats in range(min_repeats, min(10, n // period) + 1):
            need = period * repeats
            if n < need:
                continue
            tail = seq[-need:]
            motif = tail[:period]
            if tail == motif * repeats:
                return motif, repeats
    return None, 0


def detect_approx_periodic_tail(
    seq: List[str],
    min_period: int = 2,
    max_period: int = 10,
    min_repeats: int = 3,
    max_mismatches: int = 1,
):
    n = len(seq)
    best_motif = None
    best_repeats = 0
    best_score = -10**9

    for period in range(min_period, max_period + 1):
        for repeats in range(min_repeats, min(10, n // period) + 1):
            need = period * repeats
            if n < need:
                continue

            tail = seq[-need:]
            motif = tail[:period]

            mismatches = 0
            for i, x in enumerate(tail):
                if x != motif[i % period]:
                    mismatches += 1

            if mismatches <= max_mismatches:
                score = repeats * 10 - period * 2 - mismatches * 5
                if score > best_score:
                    best_score = score
                    best_motif = motif
                    best_repeats = repeats

    return best_motif, best_repeats


def detect_length_signature(segments: List[Tuple[str, int]]) -> Optional[str]:
    if len(segments) < 3:
        return None

    tail = [s[1] for s in segments[-5:]]

    if len(tail) >= 4 and all(tail[i] < tail[i + 1] for i in range(len(tail) - 1)):
        return f"Cầu tăng tiến {'-'.join(map(str, tail))}"

    if len(tail) >= 4 and all(tail[i] > tail[i + 1] for i in range(len(tail) - 1)):
        return f"Cầu giảm tiến {'-'.join(map(str, tail))}"

    if len(tail) >= 4:
        diffs = [tail[i + 1] - tail[i] for i in range(len(tail) - 1)]
        if len(set(diffs)) == 1 and abs(diffs[0]) == 1:
            if diffs[0] > 0:
                return f"Cầu nhịp tăng đều {'-'.join(map(str, tail))}"
            return f"Cầu nhịp giảm đều {'-'.join(map(str, tail))}"

    if len(tail) >= 5 and tail == tail[::-1]:
        return f"Cầu đối xứng {'-'.join(map(str, tail))}"

    if len(tail) >= 4 and tail[0] == tail[2] and tail[1] == tail[3]:
        return f"Cầu luân phiên {'-'.join(map(str, tail))}"

    if len(set(tail)) == 1:
        return f"Cầu nhịp đều {'-'.join(map(str, tail))}"

    if len(set(tail)) >= 3:
        return f"Cầu hỗn hợp {'-'.join(map(str, tail))}"

    return None


def detect_break_type(seq: List[str]) -> Optional[str]:
    segments = rle(seq)
    if len(segments) < 2:
        return None

    prev_val, prev_len = segments[-2]
    cur_val, cur_len = segments[-1]

    if prev_len >= 5 and cur_len == 1:
        return f"Bẻ cầu yếu sau bệt {prev_val} x{prev_len}"
    if prev_len >= 5 and cur_len == 2:
        return f"Bẻ cầu mạnh sau bệt {prev_val} x{prev_len}"

    return None


def classify_pattern(seq: List[str]) -> Tuple[str, str, int]:
    """
    Trả về:
    - label: tên cầu
    - next_hint: T/X
    - confidence: độ tin cậy
    """
    if not seq:
        return ("Chưa đủ dữ liệu", "X", 60)

    window = seq[-160:]
    segments = rle(window)
    last_val, streak_len = current_streak(window)

    alt_len = detect_alternating_tail(window, min_len=6)
    motif, rep = detect_exact_periodic_tail(window, min_period=2, max_period=10, min_repeats=3)
    approx_motif, approx_rep = detect_approx_periodic_tail(window, min_period=2, max_period=10, min_repeats=3)
    length_sig = detect_length_signature(segments)
    break_type = detect_break_type(window)

    if streak_len >= 5:
        return (f"Cầu bệt {last_val} x{streak_len}", last_val, 88)

    if alt_len and alt_len >= 6:
        next_hint = "T" if last_val == "X" else "X"
        return (f"Cầu đảo 1-1 x{alt_len}", next_hint, 86)

    if motif:
        motif_text = "-".join(motif)
        if len(motif) == 2 and motif[0] != motif[1]:
            next_hint = "T" if last_val == "X" else "X"
            return (f"Cầu chu kỳ đảo {motif_text} x{rep}", next_hint, 84)

        next_hint = motif[0]
        return (f"Cầu chu kỳ {motif_text} x{rep}", next_hint, 82)

    if approx_motif:
        motif_text = "-".join(approx_motif)
        if len(approx_motif) == 2 and approx_motif[0] != approx_motif[1]:
            next_hint = "T" if last_val == "X" else "X"
            return (f"Cầu gần chu kỳ đảo {motif_text} x{approx_rep}", next_hint, 78)

        next_hint = approx_motif[0]
        return (f"Cầu gần chu kỳ {motif_text} x{approx_rep}", next_hint, 76)

    if length_sig:
        if "đối xứng" in length_sig:
            next_hint = "T" if last_val == "X" else "X"
            return (length_sig, next_hint, 80)

        if "luân phiên" in length_sig:
            next_hint = "T" if last_val == "X" else "X"
            return (length_sig, next_hint, 77)

        if "tăng tiến" in length_sig:
            next_hint = "T" if Counter(window)["T"] >= Counter(window)["X"] else "X"
            return (length_sig, next_hint, 72)

        if "giảm tiến" in length_sig:
            next_hint = "T" if Counter(window)["T"] >= Counter(window)["X"] else "X"
            return (length_sig, next_hint, 72)

        if "nhịp đều" in length_sig:
            next_hint = "T" if last_val == "X" else "X"
            return (length_sig, next_hint, 74)

        if "hỗn hợp" in length_sig:
            next_hint = "T" if Counter(window)["T"] >= Counter(window)["X"] else "X"
            return (length_sig, next_hint, 66)

    if break_type:
        prev_val = "T" if "T" in break_type else "X"
        next_hint = "T" if prev_val == "X" else "X"
        return (break_type, next_hint, 73)

    count = Counter(window)
    if count["T"] >= count["X"]:
        return ("Không có cầu rõ, nghiêng Tài", "T", 64)
    return ("Không có cầu rõ, nghiêng Xỉu", "X", 64)


def ai_analyze(seq: List[str]) -> str:
    if not seq:
        return "Cầu: Chưa đủ dữ liệu\nVào: Xỉu | Độ tin cậy: 60%"

    pattern, next_hint, confidence = classify_pattern(seq)
    next_text = "Tài" if next_hint == "T" else "Xỉu"
    return f"Cầu: {pattern}\nVào: {next_text} | Độ tin cậy: {confidence}%"


def pattern_report(seq: List[str], limit: int = 160) -> str:
    if not seq:
        return "Chưa có dữ liệu."

    window = seq[-limit:]
    segments = rle(window)
    label, next_hint, confidence = classify_pattern(window)
    count = Counter(window)

    seg_text = " ".join(f"{v}{n}" for v, n in segments[-12:])
    trend = "Tài" if count["T"] >= count["X"] else "Xỉu"

    return (
        f"Nhận diện: {label}\n"
        f"Chuỗi segment: {seg_text}\n"
        f"Xu hướng nền: {trend} | Gợi ý vào: {'Tài' if next_hint == 'T' else 'Xỉu'} | Độ tin cậy: {confidence}%"
    )


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
    "/patterns [n]       - xem nhận diện cầu\n"
    "/clear              - xóa toàn bộ lịch sử\n\n"
    "Chỉ ADMIN mới dùng được."
)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if update.message:
        await update.message.reply_text(WELCOME)


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return

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
    if not update.message:
        return

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
    if not update.message:
        return

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
    if not update.message:
        return

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
    if not update.message:
        return

    n = 200
    if context.args and context.args[0].isdigit():
        n = max(1, min(2000, int(context.args[0])))

    seq = load_history(n)
    if not seq:
        await update.message.reply_text("Chưa có dữ liệu để quét.")
        return

    report = ai_analyze(seq)
    await update.message.reply_text(report)


async def patterns_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return

    n = 160
    if context.args and context.args[0].isdigit():
        n = max(20, min(2000, int(context.args[0])))

    seq = load_history(n)
    if not seq:
        await update.message.reply_text("Chưa có dữ liệu.")
        return

    await update.message.reply_text(pattern_report(seq, limit=n))


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return

    delete_all()
    await update.message.reply_text("Đã xóa toàn bộ lịch sử.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not update.message:
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
        raise RuntimeError("Thiếu ADMIN_USER_ID trong biến môi trường hoặc giá trị không hợp lệ.")

    init_db()

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", start_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("import", import_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("scan", scan_cmd))
    app.add_handler(CommandHandler("patterns", patterns_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    logger.info("Bot đang chạy...")
    app.run_polling()


if __name__ == "__main__":
    main()
