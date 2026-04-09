#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram bot Tài/Xỉu theo lịch sử, có 2 lớp:
- History: lịch sử nhập vào, có thể reset riêng
- Brain: bộ nhớ pattern win/lose, giữ lại khi reset history

Bản này:
- Chỉ ưu tiên theo cầu (pattern)
- Không dùng xu hướng nền / xu hướng gần
- Bỏ delay, trả kết quả ngay
- Reset history không đụng brain
- Reset all mới xóa brain
- Có /report để thống kê nhận diện cầu và brain
- Có fallback nhẹ để không bị "im" khi không bắt được cầu rõ
"""

import os
import re
import sqlite3
import logging
from collections import Counter
from typing import List, Optional, Tuple, Dict, Any

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DB_PATH = os.getenv("TAI_XIU_DB_PATH", "tai_xiu_stats.db")
MIN_PREDICT_HISTORY = 15
MAX_INPUT_ITEMS = 100

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


def is_admin(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id == ADMIN_USER_ID)


async def deny_if_not_admin(update: Update):
    if update.message:
        await update.message.reply_text("Bot này chỉ dành cho ADMIN.")


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

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

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pattern_memory (
                pattern_key TEXT PRIMARY KEY,
                wins INTEGER NOT NULL DEFAULT 0,
                losses INTEGER NOT NULL DEFAULT 0,
                recent_losses INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
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


def reset_history_only():
    with get_conn() as conn:
        conn.execute("DELETE FROM rounds")
        conn.execute("DELETE FROM predictions")
        conn.commit()


def reset_all():
    with get_conn() as conn:
        conn.execute("DELETE FROM rounds")
        conn.execute("DELETE FROM predictions")
        conn.execute("DELETE FROM pattern_memory")
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


def reverse_outcome(v: str) -> str:
    return "T" if v == "X" else "X"


def pattern_family(label: str) -> str:
    t = (label or "").strip()
    if t.startswith("Cầu bệt vừa"):
        return "BET_SOFT"
    if t.startswith("Cầu bệt"):
        return "BET"
    if t.startswith("Cầu đảo 1-1"):
        return "DAO_1_1"
    if t.startswith("Cầu chu kỳ đảo"):
        return "CHU_KY_DAO"
    if t.startswith("Cầu chu kỳ"):
        return "CHU_KY"
    if t.startswith("Cầu gần chu kỳ đảo"):
        return "GAN_CHU_KY_DAO"
    if t.startswith("Cầu gần chu kỳ"):
        return "GAN_CHU_KY"
    if t.startswith("Cầu tăng tiến"):
        return "TANG_TIEN"
    if t.startswith("Cầu giảm tiến"):
        return "GIAM_TIEN"
    if t.startswith("Cầu đối xứng"):
        return "DOI_XUNG"
    if t.startswith("Cầu luân phiên"):
        return "LUAN_PHIEN"
    if t.startswith("Cầu nhịp đều"):
        return "NHIP_DEU"
    if t.startswith("Cầu hỗn hợp biến thể"):
        return "HON_HOP_BIEN_THE"
    if t.startswith("Cầu hỗn hợp"):
        return "HON_HOP"
    if t.startswith("Bẻ cầu"):
        return "BE_CAU"
    if t.startswith("Chuyển"):
        return "CHUYEN_CAU"
    return "OTHER"


def get_pattern_memory(pattern_key: str) -> Optional[Dict[str, int]]:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT wins, losses, recent_losses
            FROM pattern_memory
            WHERE pattern_key = ?
            """,
            (pattern_key,),
        ).fetchone()
    if not row:
        return None
    return {
        "wins": int(row["wins"]),
        "losses": int(row["losses"]),
        "recent_losses": int(row["recent_losses"]),
    }


def update_pattern_memory_in_conn(conn: sqlite3.Connection, pattern_key: str, correct: bool):
    row = conn.execute(
        """
        SELECT wins, losses, recent_losses
        FROM pattern_memory
        WHERE pattern_key = ?
        """,
        (pattern_key,),
    ).fetchone()

    if row:
        wins = int(row["wins"])
        losses = int(row["losses"])
        recent_losses = int(row["recent_losses"])
    else:
        wins = 0
        losses = 0
        recent_losses = 0

    if correct:
        wins += 1
        recent_losses = 0
    else:
        losses += 1
        recent_losses += 1

    conn.execute(
        """
        INSERT INTO pattern_memory(pattern_key, wins, losses, recent_losses, updated_at)
        VALUES(?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(pattern_key) DO UPDATE SET
            wins = excluded.wins,
            losses = excluded.losses,
            recent_losses = excluded.recent_losses,
            updated_at = CURRENT_TIMESTAMP
        """,
        (pattern_key, wins, losses, recent_losses),
    )


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
            update_pattern_memory_in_conn(conn, pattern_family(pred_row["pattern"]), bool(correct))

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


def get_brain_rows(limit: int = 10):
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT pattern_key, wins, losses, recent_losses, updated_at
            FROM pattern_memory
            ORDER BY (wins + losses) DESC, updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return rows


def count_brain_patterns() -> int:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM pattern_memory").fetchone()
    return int(row["c"]) if row else 0


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
    tokens = TOKEN_RE.findall(text.upper())
    results = []
    for tok in tokens:
        mapped = normalize_token(tok)
        if mapped in {"T", "X"}:
            results.append(mapped)
    return results


def fmt_outcome(v: str) -> str:
    return "Tài" if v == "T" else "Xỉu"


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


def detect_soft_streak(seq: List[str]) -> Optional[int]:
    if len(seq) < 3:
        return None
    count = 1
    last = seq[-1]
    for i in range(len(seq) - 2, -1, -1):
        if seq[i] == last:
            count += 1
        else:
            break
    return count if 3 <= count < 5 else None


def detect_alternating_pro(seq: List[str]) -> Optional[int]:
    if len(seq) < 6:
        return None
    tail = seq[-8:]
    if len(tail) < 6:
        return None
    mismatches = sum(1 for i in range(len(tail) - 1) if tail[i] == tail[i + 1])
    if mismatches <= 1:
        length = len(tail)
        i = len(seq) - len(tail) - 1
        while i >= 0 and seq[i] != seq[i + 1]:
            length += 1
            i -= 1
        return length
    return None


def detect_exact_periodic_pro(seq: List[str], min_period: int = 2, max_period: int = 6, min_repeats: int = 3):
    n = len(seq)
    for period in range(min_period, max_period + 1):
        for repeats in range(min_repeats, min(8, n // period) + 1):
            need = period * repeats
            if n < need:
                continue
            tail = seq[-need:]
            motif = tail[:period]
            if all(tail[i] == motif[i % period] for i in range(need)):
                return motif, repeats
    return None, 0


def detect_approx_periodic_pro(seq: List[str], min_period: int = 2, max_period: int = 6, min_repeats: int = 3, max_mismatches: int = 2):
    n = len(seq)
    best_motif = None
    best_repeats = 0
    best_score = -10**9
    for period in range(min_period, max_period + 1):
        for repeats in range(min_repeats, min(8, n // period) + 1):
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


def detect_length_signature_pro(segments: List[Tuple[str, int]]) -> Optional[str]:
    if len(segments) < 3:
        return None
    tail = [s[1] for s in segments[-8:]]
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
    if len(set(tail)) >= 5:
        return f"Cầu hỗn hợp {'-'.join(map(str, tail))}"
    return None


def detect_mixed_pattern_pro(seq: List[str], segments: List[Tuple[str, int]]) -> Optional[str]:
    if len(seq) < 8 or len(segments) < 4:
        return None
    tail_lengths = [s[1] for s in segments[-6:]]
    if len(tail_lengths) < 4:
        return None
    unique_count = len(set(tail_lengths))
    variance = max(tail_lengths) - min(tail_lengths)
    if 2 <= unique_count <= 4 and variance >= 2:
        return f"Cầu hỗn hợp biến thể {'-'.join(map(str, tail_lengths))}"
    return None


def detect_break_pro(seq: List[str], segments: List[Tuple[str, int]]) -> Optional[str]:
    if len(seq) < 5 or len(segments) < 2:
        return None
    prev_val, prev_len = segments[-2]
    cur_val, cur_len = segments[-1]
    if prev_len >= 5 and cur_len == 1:
        return f"Bẻ cầu yếu sau bệt {prev_val} x{prev_len}"
    if prev_len >= 5 and cur_len == 2:
        return f"Bẻ cầu mạnh sau bệt {prev_val} x{prev_len}"
    if prev_len >= 4 and cur_len == 1:
        return f"Bẻ cầu sau bệt {prev_val} x{prev_len}"
    return None


def detect_transition_pro(segments: List[Tuple[str, int]]) -> Optional[str]:
    if len(segments) < 3:
        return None
    a, b, c = segments[-3:]
    if a[1] >= 4 and b[1] == 1 and c[1] == 1:
        return f"Chuyển bệt -> đảo ({a[0]} -> {c[0]})"
    if a[1] == 1 and b[1] == 1 and c[1] >= 3:
        return f"Chuyển đảo -> bệt ({a[0]} -> {c[0]})"
    if a[1] >= 4 and b[1] >= 2 and c[1] >= 2:
        return f"Chuyển bệt -> chu kỳ ({a[0]} -> {c[0]})"
    return None


def adjust_prediction_by_memory(pattern_label: str, predicted_outcome: Optional[str], confidence: int) -> Tuple[Optional[str], int, bool, str]:
    if not pattern_label or predicted_outcome not in {"T", "X"}:
        return predicted_outcome, confidence, False, "NO_MEMORY"

    key = pattern_family(pattern_label)
    mem = get_pattern_memory(key)
    if not mem:
        return predicted_outcome, confidence, False, "NO_MEMORY"

    wins = mem["wins"]
    losses = mem["losses"]
    recent_losses = mem["recent_losses"]
    total = wins + losses
    if total <= 0:
        return predicted_outcome, confidence, False, "NO_MEMORY"

    win_rate = wins / total
    flip = False
    reason = "FOLLOW"

    if total >= 5 and win_rate < 0.30:
        flip = True
        reason = "LOW_WINRATE"
    elif recent_losses >= 2:
        flip = True
        reason = "DOUBLE_LOSS"
    elif recent_losses == 1 and win_rate < 0.5:
        flip = True
        reason = "EARLY_WEAK"

    adjusted_conf = confidence
    if total >= 10:
        if win_rate >= 0.60:
            adjusted_conf += 5
        elif win_rate <= 0.40:
            adjusted_conf -= 8
        else:
            adjusted_conf -= 4

    if flip:
        predicted_outcome = reverse_outcome(predicted_outcome)
        adjusted_conf -= 5

    adjusted_conf = max(0, min(100, adjusted_conf))
    return predicted_outcome, adjusted_conf, flip, reason


def classify_pattern_pro(seq: List[str]) -> Dict[str, Any]:
    result = {
        "pattern_label": "Không nhận diện được cầu",
        "prediction": None,
        "confidence": 0,
        "recognized": False,
        "flipped": False,
        "flip_reason": "",
        "follow_pct": 0,
        "reverse_pct": 0,
        "source": "",
    }

    if len(seq) < 5:
        return result

    window = seq[-60:]
    segments = rle(window)
    last_val, streak_len = current_streak(window)

    def finalize(label: str, pred: Optional[str], conf: int, source: str):
        pred2, conf2, flipped, reason = adjust_prediction_by_memory(label, pred, conf)
        follow_pct = conf2
        reverse_pct = max(0, min(100, 100 - conf2))
        return {
            "pattern_label": label,
            "prediction": pred2,
            "confidence": conf2,
            "recognized": True,
            "flipped": flipped,
            "flip_reason": reason if flipped else "",
            "follow_pct": follow_pct,
            "reverse_pct": reverse_pct,
            "source": source,
        }

    soft_streak = detect_soft_streak(window)
    if soft_streak and soft_streak >= 3:
        return finalize(f"Cầu bệt vừa {last_val} x{soft_streak}", last_val, 80, "SOFT_STREAK")

    if streak_len >= 5:
        return finalize(f"Cầu bệt {last_val} x{streak_len}", last_val, 90, "STREAK")

    alt_len = detect_alternating_pro(window)
    if alt_len and alt_len >= 6:
        pred = "T" if last_val == "X" else "X"
        return finalize(f"Cầu đảo 1-1 x{alt_len}", pred, 86, "ALTERNATING")

    motif, rep = detect_exact_periodic_pro(window, min_period=2, max_period=6, min_repeats=3)
    if motif:
        motif_text = "-".join(motif)
        if len(motif) == 2 and motif[0] != motif[1]:
            pred = "T" if last_val == "X" else "X"
            return finalize(f"Cầu chu kỳ đảo {motif_text} x{rep}", pred, 84, "PERIODIC_FLIP")
        pred = motif[0]
        return finalize(f"Cầu chu kỳ {motif_text} x{rep}", pred, 82, "PERIODIC")

    approx_motif, approx_rep = detect_approx_periodic_pro(window, min_period=2, max_period=6, min_repeats=3, max_mismatches=2)
    if approx_motif:
        motif_text = "-".join(approx_motif)
        if len(approx_motif) == 2 and approx_motif[0] != approx_motif[1]:
            pred = "T" if last_val == "X" else "X"
            return finalize(f"Cầu gần chu kỳ đảo {motif_text} x{approx_rep}", pred, 78, "NEAR_PERIODIC_FLIP")
        pred = approx_motif[0]
        return finalize(f"Cầu gần chu kỳ {motif_text} x{approx_rep}", pred, 76, "NEAR_PERIODIC")

    length_sig = detect_length_signature_pro(segments)
    if length_sig:
        pred = last_val if last_val in {"T", "X"} else None
        if "đối xứng" in length_sig:
            conf = 80
        elif "luân phiên" in length_sig:
            conf = 77
        elif "tăng tiến" in length_sig:
            conf = 72
        elif "giảm tiến" in length_sig:
            conf = 72
        elif "nhịp đều" in length_sig:
            conf = 74
        elif "hỗn hợp" in length_sig:
            conf = 66
        else:
            conf = 60
        return finalize(length_sig, pred, conf, "LENGTH_SIGNATURE")

    mixed = detect_mixed_pattern_pro(window, segments)
    if mixed:
        pred = last_val if last_val in {"T", "X"} else None
        return finalize(mixed, pred, 63, "MIXED")

    break_type = detect_break_pro(window, segments)
    if break_type:
        pred = last_val if last_val in {"T", "X"} else None
        return finalize(break_type, pred, 73, "BREAK")

    transition = detect_transition_pro(segments)
    if transition:
        pred = last_val if last_val in {"T", "X"} else None
        return finalize(transition, pred, 70, "TRANSITION")

    if len(seq) >= MIN_PREDICT_HISTORY and last_val in {"T", "X"}:
        return {
            "pattern_label": "Fallback đảo nhẹ",
            "prediction": reverse_outcome(last_val),
            "confidence": 55,
            "recognized": True,
            "flipped": False,
            "flip_reason": "",
            "follow_pct": 55,
            "reverse_pct": 45,
            "source": "FALLBACK",
        }

    return result


def build_live_reply(
    inserted_count: int,
    total_saved: int,
    latest_resolution: Optional[Dict[str, Any]],
    wins: int,
    losses: int,
    analysis: Dict[str, Any],
) -> str:
    if latest_resolution:
        pred = fmt_outcome(latest_resolution["predicted_outcome"])
        actual = fmt_outcome(latest_resolution["actual_outcome"])
        result_text = "ĐÚNG" if latest_resolution["correct"] else "SAI"
        section2 = f"Kèo trước: {pred} → {actual} | {result_text}"
    else:
        section2 = "Kèo trước: Chưa có kèo trước để chốt"

    section3 = f"Thắng/Thua: Thắng {wins} | Thua {losses}"

    if analysis["recognized"]:
        section4 = f"Cầu: {analysis['pattern_label']}"
    else:
        section4 = "Cầu: Không nhận diện được cầu"

    if analysis["recognized"] and analysis["prediction"] in {"T", "X"}:
        final_txt = fmt_outcome(analysis["prediction"])
        section5 = (
            f"Dự đoán mới: {final_txt} | Độ tin cậy: {analysis['confidence']}%\n"
            f"Tỷ lệ: Theo {analysis['follow_pct']}% | Bẻ {analysis['reverse_pct']}%"
        )
        if analysis["flipped"]:
            section5 += f"\nĐã bẻ: {analysis['flip_reason']}"
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
    analysis: Dict[str, Any],
) -> str:
    section2 = f"Đã lưu kết quả: +{inserted_count} | Tổng đã lưu: {total_saved}"
    section3 = f"Cầu: {analysis['pattern_label']}" if analysis["recognized"] else "Cầu: Không nhận diện được cầu"

    if analysis["recognized"] and analysis["prediction"] in {"T", "X"}:
        final_txt = fmt_outcome(analysis["prediction"])
        section4 = (
            f"Dự đoán mới: {final_txt} | Độ tin cậy: {analysis['confidence']}%\n"
            f"Tỷ lệ: Theo {analysis['follow_pct']}% | Bẻ {analysis['reverse_pct']}%"
        )
        if analysis["flipped"]:
            section4 += f"\nĐã bẻ: {analysis['flip_reason']}"
    else:
        section4 = "Dự đoán mới: Không dự đoán"

    return f"{section2}\n{section3}\n{section4}"


def build_report_reply(seq: List[str], analysis: Dict[str, Any]) -> str:
    count = Counter(seq)
    total = len(seq)

    brain_rows = get_brain_rows(8)
    brain_lines = []
    if brain_rows:
        for r in brain_rows:
            total_p = int(r["wins"]) + int(r["losses"])
            win_rate = (int(r["wins"]) / total_p * 100) if total_p else 0.0
            brain_lines.append(
                f"- {r['pattern_key']} | W:{r['wins']} L:{r['losses']} | LR:{r['recent_losses']} | {win_rate:.1f}%"
            )
    else:
        brain_lines.append("- Chưa có dữ liệu não.")

    if analysis["recognized"] and analysis["prediction"] in {"T", "X"}:
        predict_text = f"{fmt_outcome(analysis['prediction'])} ({analysis['confidence']}%)"
    elif analysis["recognized"]:
        predict_text = "Không dự đoán"
    else:
        predict_text = "Không nhận diện được cầu"

    return (
        f"Thống kê tổng: {total} mẫu\n"
        f"- Tài: {count['T']}\n"
        f"- Xỉu: {count['X']}\n"
        f"\nCầu hiện tại:\n"
        f"- {analysis['pattern_label']}\n"
        f"- Dự đoán: {predict_text}\n"
        f"\nNão (top pattern):\n"
        + "\n".join(brain_lines)
    )


def save_current_prediction_if_any(seq: List[str]) -> None:
    if len(seq) < MIN_PREDICT_HISTORY:
        return

    analysis = classify_pattern_pro(seq)
    if analysis["recognized"] and analysis["prediction"] in {"T", "X"}:
        save_prediction(
            pattern=analysis["pattern_label"],
            predicted_outcome=analysis["prediction"],
            confidence=analysis["confidence"],
            based_on_count=len(seq),
        )


WELCOME = (
    "Bot thống kê Tài/Xỉu đã sẵn sàng.\n\n"
    "Lệnh dùng:\n"
    "/add <dữ liệu>          - thêm 1 hoặc nhiều kết quả (live)\n"
    "/import <dữ liệu>       - dán lịch sử dài (chỉ lưu)\n"
    "/history [n]            - xem n kết quả gần nhất\n"
    "/stats [n]              - thống kê tần suất\n"
    "/scan [n]               - phân tích lịch sử\n"
    "/patterns [n]           - xem nhận diện cầu\n"
    "/brain [n]              - xem bộ nhớ não\n"
    "/report [n]             - thống kê nhận diện cầu\n"
    "/clear, /reset_history  - xóa lịch sử + kèo chờ, giữ não\n"
    "/reset_all              - xóa toàn bộ, gồm cả não\n\n"
    "Chỉ ADMIN mới dùng được."
)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if update.message:
        await update.message.reply_text(WELCOME)


async def process_live_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        if not update.message:
            return

        items = extract_outcomes(text)
        if not items:
            await update.message.reply_text("Không tìm thấy dữ liệu hợp lệ.")
            return

        if len(items) > MAX_INPUT_ITEMS:
            await update.message.reply_text(f"Dữ liệu quá nhiều. Tối đa {MAX_INPUT_ITEMS} kết quả mỗi lần.")
            return

        inserted = save_outcomes(items, text)

        resolved = resolve_pending_predictions(items)
        latest_resolution = resolved[-1] if resolved else get_latest_resolution()

        seq = load_history(200)
        analysis = classify_pattern_pro(seq)

        save_current_prediction_if_any(seq)

        total_saved = count_saved_rounds()
        wins, losses = get_prediction_stats()

        reply = build_live_reply(
            inserted_count=inserted,
            total_saved=total_saved,
            latest_resolution=latest_resolution,
            wins=wins,
            losses=losses,
            analysis=analysis,
        )

        await update.message.reply_text(reply)

    except Exception as e:
        logger.exception("Error in process_live_input")
        if update.message:
            await update.message.reply_text(f"Bị lỗi khi xử lý: {e}")


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

    if len(items) > MAX_INPUT_ITEMS:
        await update.message.reply_text(f"Dữ liệu quá nhiều. Tối đa {MAX_INPUT_ITEMS} kết quả mỗi lần.")
        return

    inserted = save_outcomes(items, text)
    seq = load_history(200)
    analysis = classify_pattern_pro(seq)
    total_saved = count_saved_rounds()

    reply = build_import_reply(
        inserted_count=inserted,
        total_saved=total_saved,
        analysis=analysis,
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

    analysis = classify_pattern_pro(seq)
    if analysis["recognized"] and analysis["prediction"] in {"T", "X"} and len(seq) >= MIN_PREDICT_HISTORY:
        msg = (
            f"Cầu: {analysis['pattern_label']}\n"
            f"Vào: {fmt_outcome(analysis['prediction'])} | Độ tin cậy: {analysis['confidence']}%\n"
            f"Tỷ lệ: Theo {analysis['follow_pct']}% | Bẻ {analysis['reverse_pct']}%"
        )
        if analysis["flipped"]:
            msg += f"\nĐã bẻ: {analysis['flip_reason']}"
    elif analysis["recognized"]:
        msg = f"Cầu: {analysis['pattern_label']}\nVào: Không dự đoán"
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

    analysis = classify_pattern_pro(seq)
    count = Counter(seq)
    segs = rle(seq[-n:])
    seg_text = " ".join(f"{v}{k}" for v, k in segs[-12:])

    if analysis["recognized"] and analysis["prediction"] in {"T", "X"} and len(seq) >= MIN_PREDICT_HISTORY:
        reply = (
            f"Nhận diện: {analysis['pattern_label']}\n"
            f"Chuỗi segment: {seg_text}\n"
            f"Tài: {count['T']} | Xỉu: {count['X']}\n"
            f"Vào: {fmt_outcome(analysis['prediction'])} | Độ tin cậy: {analysis['confidence']}%\n"
            f"Tỷ lệ: Theo {analysis['follow_pct']}% | Bẻ {analysis['reverse_pct']}%"
        )
        if analysis["flipped"]:
            reply += f"\nĐã bẻ: {analysis['flip_reason']}"
    elif analysis["recognized"]:
        reply = (
            f"Nhận diện: {analysis['pattern_label']}\n"
            f"Chuỗi segment: {seg_text}\n"
            f"Tài: {count['T']} | Xỉu: {count['X']}\n"
            f"Vào: Không dự đoán"
        )
    else:
        reply = (
            "Nhận diện: Không nhận diện được cầu\n"
            f"Chuỗi segment: {seg_text}\n"
            f"Tài: {count['T']} | Xỉu: {count['X']}\n"
            f"Vào: Không dự đoán"
        )

    await update.message.reply_text(reply)


async def brain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return

    n = 10
    if context.args and context.args[0].isdigit():
        n = max(1, min(50, int(context.args[0])))

    rows = get_brain_rows(n)
    if not rows:
        await update.message.reply_text("Não chưa có dữ liệu.")
        return

    lines = [
        f"Bộ nhớ não: {count_brain_patterns()} pattern",
        "Top pattern:",
    ]
    for r in rows:
        total = int(r["wins"]) + int(r["losses"])
        win_rate = (int(r["wins"]) / total * 100) if total else 0.0
        lines.append(
            f"- {r['pattern_key']} | W:{r['wins']} L:{r['losses']} "
            f"| LoseStreak:{r['recent_losses']} | Winrate:{win_rate:.1f}%"
        )

    await update.message.reply_text("\n".join(lines))


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return

    n = 200
    if context.args and context.args[0].isdigit():
        n = max(20, min(2000, int(context.args[0])))

    seq = load_history(n)
    if not seq:
        await update.message.reply_text("Chưa có dữ liệu.")
        return

    analysis = classify_pattern_pro(seq)
    reply = build_report_reply(seq, analysis)
    await update.message.reply_text(reply)


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return
    reset_history_only()
    await update.message.reply_text("Đã xóa toàn bộ lịch sử và kèo chờ. Não vẫn được giữ lại.")


async def reset_history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return
    reset_history_only()
    await update.message.reply_text("Đã reset lịch sử. Não vẫn được giữ lại.")


async def reset_all_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return await deny_if_not_admin(update)
    if not update.message:
        return
    reset_all()
    await update.message.reply_text("Đã xóa toàn bộ: lịch sử, kèo chờ và não.")


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
    app.add_handler(CommandHandler("brain", brain_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("reset_history", reset_history_cmd))
    app.add_handler(CommandHandler("reset_all", reset_all_cmd))

    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    logger.info("Bot đang chạy...")
    app.run_polling()


if __name__ == "__main__":
    main()
