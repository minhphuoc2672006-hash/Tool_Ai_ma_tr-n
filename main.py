#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram bot thống kê Tài/Xỉu theo lịch sử.

Chỉ ADMIN mới sử dụng được.

Tính năng:
- /start, /help
- /add <dữ liệu>      : thêm 1 hoặc nhiều kết quả (live)
- /import <dữ liệu>   : nhập chuỗi lịch sử dài (lưu thôi, không chốt kèo)
- /history [n]        : xem n kết quả gần nhất
- /stats [n]          : thống kê tần suất
- /scan [n]           : phân tích lịch sử
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
import asyncio
from collections import Counter
from typing import List, Optional, Tuple, Dict, Any

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
PREDICT_DELAY_SECONDS = 7
MIN_PREDICT_HISTORY = 15

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

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                based_on_count INTEGER NOT NULL,
                pattern TEXT NOT NULL,
                predicted_outcome TEXT NOT NULL CHECK(predicted_outcome IN ('T', 'X')),
                confidence INTEGER NOT NULL,
                resolved INTEGER NOT NULL DEFAULT 0,
                actual_outcome TEXT,
                correct INTEGER,
                resolved_at TEXT
            )
            """
        )
        conn.commit()


def save_outcomes(outcomes: List[str], raw: str) -> int:
    if not outcomes:
        return 0

    with get_conn() as conn:
        conn.executemany(
            "INSERT INTO rounds(raw, outcome) VALUES(?, ?)",
            [(raw, o) for o in outcomes],
        )
        conn.commit()

    return len(outcomes)


def delete_all():
    with get_conn() as conn:
        conn.execute("DELETE FROM rounds")
        conn.execute("DELETE FROM predictions")
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


def count_saved_rounds() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM rounds").fetchone()
    return int(row["c"]) if row else 0


def save_prediction(pattern: str, predicted_outcome: str, confidence: int, based_on_count: int):
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO predictions(based_on_count, pattern, predicted_outcome, confidence)
            VALUES(?, ?, ?, ?)
            """,
            (based_on_count, pattern, predicted_outcome, confidence),
        )
        conn.commit()


def resolve_pending_predictions(actual_outcomes: List[str]) -> List[Dict[str, Any]]:
    if not actual_outcomes:
        return []

    resolved_rows: List[Dict[str, Any]] = []

    with get_conn() as conn:
        pending = conn.execute(
            """
            SELECT id, predicted_outcome, pattern, confidence
            FROM predictions
            WHERE resolved = 0
            ORDER BY id ASC
            """
        ).fetchall()

        for pred_row, actual in zip(pending, actual_outcomes):
            correct = 1 if pred_row["predicted_outcome"] == actual else 0
            conn.execute(
                """
                UPDATE predictions
                SET resolved = 1,
                    actual_outcome = ?,
                    correct = ?,
                    resolved_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (actual, correct, pred_row["id"]),
            )
            resolved_rows.append(
                {
                    "predicted_outcome": pred_row["predicted_outcome"],
                    "actual_outcome": actual,
                    "correct": bool(correct),
                    "pattern": pred_row["pattern"],
                    "confidence": int(pred_row["confidence"]),
                }
            )

        conn.commit()

    return resolved_rows


def get_prediction_stats() -> Tuple[int, int]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN correct = 1 THEN 1 ELSE 0 END), 0) AS wins
            FROM predictions
            WHERE resolved = 1
            """
        ).fetchone()

    total = int(row["total"]) if row else 0
    wins = int(row["wins"]) if row else 0
    losses = max(0, total - wins)
    return wins, losses


def get_latest_resolution() -> Optional[Dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT predicted_outcome, actual_outcome, correct, pattern, confidence
            FROM predictions
            WHERE resolved = 1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    if not row:
        return None

    return {
        "predicted_outcome": row["predicted_outcome"],
        "actual_outcome": row["actual_outcome"],
        "correct": bool(row["correct"]),
        "pattern": row["pattern"],
        "confidence": int(row["confidence"]),
    }


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


def fmt_outcome(v: str) -> str:
    return "Tài" if v == "T" else "Xỉu"


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


# =========================
# NHẬN DIỆN + DỰ ĐOÁN
# =========================
def classify_pattern(seq: List[str]) -> Tuple[str, Optional[str], int, bool]:
    if not seq:
        return ("Không nhận diện được cầu", None, 0, False)

    window = seq[-160:]
    segments = rle(window)
    last_val, streak_len = current_streak(window)

    alt_len = detect_alternating_tail(window, min_len=6)
    motif, rep = detect_exact_periodic_tail(window, min_period=2, max_period=10, min_repeats=3)
    approx_motif, approx_rep = detect_approx_periodic_tail(window, min_period=2, max_period=10, min_repeats=3)
    length_sig = detect_length_signature(segments)
    break_type = detect_break_type(window)

    def infer_hint_from_pattern(pattern_text: str) -> Optional[str]:
        if last_val not in {"T", "X"}:
            return None

        if "luân phiên" in pattern_text or "đảo" in pattern_text:
            return "T" if last_val == "X" else "X"

        return last_val

    if streak_len >= 5:
        return (f"Cầu bệt {last_val} x{streak_len}", last_val, 88, True)

    if alt_len and alt_len >= 6:
        next_hint = "T" if last_val == "X" else "X"
        return (f"Cầu đảo 1-1 x{alt_len}", next_hint, 86, True)

    if motif:
        motif_text = "-".join(motif)
        if len(motif) == 2 and motif[0] != motif[1]:
            next_hint = "T" if last_val == "X" else "X"
            return (f"Cầu chu kỳ đảo {motif_text} x{rep}", next_hint, 84, True)

        next_hint = motif[0]
        return (f"Cầu chu kỳ {motif_text} x{rep}", next_hint, 82, True)

    if approx_motif:
        motif_text = "-".join(approx_motif)
        if len(approx_motif) == 2 and approx_motif[0] != approx_motif[1]:
            next_hint = "T" if last_val == "X" else "X"
            return (f"Cầu gần chu kỳ đảo {motif_text} x{approx_rep}", next_hint, 78, True)

        next_hint = approx_motif[0]
        return (f"Cầu gần chu kỳ {motif_text} x{approx_rep}", next_hint, 76, True)

    if length_sig:
        next_hint = infer_hint_from_pattern(length_sig)

        if "đối xứng" in length_sig:
            return (length_sig, next_hint, 80, True)
        if "luân phiên" in length_sig:
            return (length_sig, next_hint, 77, True)
        if "tăng tiến" in length_sig:
            return (length_sig, next_hint, 72, True)
        if "giảm tiến" in length_sig:
            return (length_sig, next_hint, 72, True)
        if "nhịp đều" in length_sig:
            return (length_sig, next_hint, 74, True)
        if "hỗn hợp" in length_sig:
            return (length_sig, next_hint, 66, True)

    if break_type:
        next_hint = last_val if last_val in {"T", "X"} else None
        return (break_type, next_hint, 73, True)

    return ("Không nhận diện được cầu", None, 0, False)


def build_live_reply(
    inserted_count: int,
    total_saved: int,
    latest_resolution: Optional[Dict[str, Any]],
    wins: int,
    losses: int,
    pattern_label: str,
    next_hint: Optional[str],
    confidence: int,
    recognized: bool,
) -> str:
    if latest_resolution:
        pred = fmt_outcome(latest_resolution["predicted_outcome"])
        actual = fmt_outcome(latest_resolution["actual_outcome"])
        result_text = "ĐÚNG" if latest_resolution["correct"] else "SAI"
        section2 = f"Kèo trước: {pred} → {actual} | {result_text}"
    else:
        section2 = "Kèo trước: Chưa có kèo trước để chốt"

    section3 = f"Thắng/Thua: Thắng {wins} | Thua {losses}"

    if recognized:
        section4 = f"Cầu: {pattern_label}"
    else:
        section4 = "Cầu: Không nhận diện được cầu"

    if recognized and next_hint in {"T", "X"}:
        section5 = f"Dự đoán mới: {fmt_outcome(next_hint)} | Độ tin cậy: {confidence}%"
    else:
        section5 = "Dự đoán mới: Không dự đoán"

    return (
        f"Đã lưu kết quả: +{inserted_count} | Tổng đã lưu: {total_saved}\n"
        f"{section2}\n"
        f"{section3}\n"
        f"{section4}\n"
        f"{section5}"
    )


def build_import_reply(
    inserted_count: int,
    total_saved: int,
    pattern_label: str,
    next_hint: Optional[str],
    confidence: int,
    recognized: bool,
) -> str:
    section2 = f"Đã lưu kết quả: +{inserted_count} | Tổng đã lưu: {total_saved}"

    if recognized:
        section3 = f"Cầu: {pattern_label}"
    else:
        section3 = "Cầu: Không nhận diện được cầu"

    if recognized and next_hint in {"T", "X"}:
        section4 = f"Dự đoán mới: {fmt_outcome(next_hint)} | Độ tin cậy: {confidence}%"
    else:
        section4 = "Dự đoán mới: Không dự đoán"

    return f"{section2}\n{section3}\n{section4}"


def save_current_prediction_if_any(seq: List[str]) -> None:
    if len(seq) < MIN_PREDICT_HISTORY:
        return

    pattern_label, next_hint, confidence, recognized = classify_pattern(seq)
    if recognized and next_hint in {"T", "X"}:
        save_prediction(
            pattern=pattern_label,
            predicted_outcome=next_hint,
            confidence=confidence,
            based_on_count=len(seq),
        )


def vip_loading_frames() -> List[str]:
    return [
        "🔍 Đang quét dữ liệu...\nTiến độ: 20%",
        "📊 Phân tích cầu...\nTiến độ: 40%",
        "🧠 AI đang tính toán...\nTiến độ: 60%",
        "📈 Đánh giá độ tin cậy...\nTiến độ: 80%",
        "✅ Hoàn tất phân tích...\nTiến độ: 100%",
    ]


async def vip_show_loading_then_reply(update: Update, reply: str):
    if not update.message:
        return

    status_msg = await update.message.reply_text(vip_loading_frames()[0])

    frames = vip_loading_frames()
    delay = max(1, PREDICT_DELAY_SECONDS // len(frames))
    for frame in frames[1:]:
        await asyncio.sleep(delay)
        try:
            await status_msg.edit_text(frame)
        except Exception:
            pass

    remaining = PREDICT_DELAY_SECONDS - delay * (len(frames) - 1)
    if remaining > 0:
        await asyncio.sleep(remaining)

    try:
        await status_msg.edit_text(reply)
    except Exception:
        await update.message.reply_text(reply)


# =========================
# TELEGRAM HANDLERS
# =========================
WELCOME = (
    "Bot thống kê Tài/Xỉu đã sẵn sàng.\n\n"
    "Lệnh dùng:\n"
    "/add <dữ liệu>      - thêm 1 hoặc nhiều kết quả (live)\n"
    "/import <dữ liệu>   - dán lịch sử dài (chỉ lưu)\n"
    "/history [n]        - xem n kết quả gần nhất\n"
    "/stats [n]          - thống kê tần suất\n"
    "/scan [n]           - phân tích lịch sử\n"
    "/patterns [n]       - xem nhận diện cầu\n"
    "/clear              - xóa toàn bộ lịch sử\n\n"
    "Chỉ ADMIN mới dùng được."
)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if update.message:
        await update.message.reply_text(WELCOME)


async def process_live_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    if not update.message:
        return

    items = extract_outcomes(text)
    if not items:
        await update.message.reply_text("Không tìm thấy dữ liệu hợp lệ.")
        return

    inserted = save_outcomes(items, text)

    # Chốt kèo cũ theo dữ liệu mới vừa đến
    resolved = resolve_pending_predictions(items)
    latest_resolution = resolved[-1] if resolved else get_latest_resolution()

    seq = load_history(200)
    pattern_label, next_hint, confidence, recognized = classify_pattern(seq)

    # Chỉ tạo kèo mới khi đủ 15 kết quả và có cầu rõ
    save_current_prediction_if_any(seq)

    total_saved = count_saved_rounds()
    wins, losses = get_prediction_stats()

    reply = build_live_reply(
        inserted_count=inserted,
        total_saved=total_saved,
        latest_resolution=latest_resolution if resolved else None,
        wins=wins,
        losses=losses,
        pattern_label=pattern_label,
        next_hint=next_hint,
        confidence=confidence,
        recognized=recognized,
    )

    await vip_show_loading_then_reply(update, reply)


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)

    if not update.message:
        return

    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Dùng: /add T X T 11 8 14")
        return

    await process_live_input(update, context, text)


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

    inserted = save_outcomes(items, text)

    seq = load_history(200)
    pattern_label, next_hint, confidence, recognized = classify_pattern(seq)
    total_saved = count_saved_rounds()

    reply = build_import_reply(
        inserted_count=inserted,
        total_saved=total_saved,
        pattern_label=pattern_label,
        next_hint=next_hint,
        confidence=confidence,
        recognized=recognized,
    )
    await update.message.reply_text(reply)


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

    pattern_label, next_hint, confidence, recognized = classify_pattern(seq)

    if recognized and next_hint in {"T", "X"} and len(seq) >= MIN_PREDICT_HISTORY:
        msg = f"Cầu: {pattern_label}\nVào: {fmt_outcome(next_hint)} | Độ tin cậy: {confidence}%"
    elif recognized:
        msg = f"Cầu: {pattern_label}\nVào: Không dự đoán"
    else:
        msg = "Cầu: Không nhận diện được cầu\nVào: Không dự đoán"

    await update.message.reply_text(msg)


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

    pattern_label, next_hint, confidence, recognized = classify_pattern(seq)
    count = Counter(seq)
    segs = rle(seq[-n:])
    seg_text = " ".join(f"{v}{k}" for v, k in segs[-12:])
    base = "Tài" if count["T"] >= count["X"] else "Xỉu"

    if recognized and next_hint in {"T", "X"} and len(seq) >= MIN_PREDICT_HISTORY:
        reply = (
            f"Nhận diện: {pattern_label}\n"
            f"Chuỗi segment: {seg_text}\n"
            f"Xu hướng nền: {base}\n"
            f"Vào: {fmt_outcome(next_hint)} | Độ tin cậy: {confidence}%"
        )
    elif recognized:
        reply = (
            f"Nhận diện: {pattern_label}\n"
            f"Chuỗi segment: {seg_text}\n"
            f"Xu hướng nền: {base}\n"
            f"Vào: Không dự đoán"
        )
    else:
        reply = (
            "Nhận diện: Không nhận diện được cầu\n"
            f"Chuỗi segment: {seg_text}\n"
            f"Xu hướng nền: {base}\n"
            f"Vào: Không dự đoán"
        )

    await update.message.reply_text(reply)


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)

    if not update.message:
        return

    delete_all()
    await update.message.reply_text("Đã xóa toàn bộ lịch sử và kèo chờ.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    if not update.message:
        return

    text = update.message.text or ""
    await process_live_input(update, context, text)


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
