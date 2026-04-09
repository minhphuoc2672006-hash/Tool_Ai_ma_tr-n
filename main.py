
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram bot Tài/Xỉu theo lịch sử, có 2 lớp:
- History: lịch sử nhập vào, có thể reset riêng
- Brain: bộ nhớ pattern win/lose, giữ lại khi reset history

Bản nâng cấp:
- Ma trận tổng hợp nhiều tín hiệu pattern thay vì chỉ lấy 1 pattern đầu tiên
- Bảng cầu zigzag
- Điểm độ gãy cầu
- Chặn nhiễu tự động

Lưu ý: đây là bot thống kê / phân tích pattern, không thể đảm bảo kết quả.
"""

import os
import re
import sqlite3
import logging
import asyncio
from collections import Counter
from typing import List, Optional, Tuple, Dict, Any

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

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


def has_pending_prediction_for_count(based_on_count: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM predictions
            WHERE resolved = 0 AND based_on_count = ?
            LIMIT 1
            """,
            (based_on_count,),
        ).fetchone()
    return row is not None


def reverse_outcome(v: str) -> str:
    return "T" if v == "X" else "X"


def clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def pattern_family(label: str) -> str:
    t = (label or "").strip()
    if t.startswith("Cầu zigzag"):
        return "ZIGZAG"
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
    if t.startswith("Xu hướng gần"):
        return "XU_HUONG_GAN"
    if t.startswith("Xu hướng nền"):
        return "XU_HUONG_NEN"
    if t.startswith("Ma trận"):
        return "MATRIX"
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


def update_pattern_memory(pattern_key: str, correct: bool):
    with get_conn() as conn:
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
        conn.commit()


def resolve_pending_predictions(actual_outcomes: List[str]) -> List[Dict[str, Any]]:
    if not actual_outcomes:
        return []

    resolved_rows: List[Dict[str, Any]] = []
    memory_updates: List[Tuple[str, bool]] = []

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
            memory_updates.append((pattern_family(pred_row["pattern"]), bool(correct)))

        conn.commit()

    for key, correct in memory_updates:
        update_pattern_memory(key, correct)

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


# =========================
# PARSE
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
    tokens = TOKEN_RE.findall(text.upper())
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


def weighted_trend(seq: List[str], window: int = 20) -> Tuple[Optional[str], int]:
    tail = seq[-window:]
    if not tail:
        return None, 0
    score = 0
    for i, v in enumerate(reversed(tail)):
        w = i + 1
        score += w if v == "T" else -w
    trend = "T" if score > 0 else "X"
    return trend, abs(score)


def detect_noise_score(seq: List[str], segments: List[Tuple[str, int]]) -> Tuple[int, str]:
    if len(seq) < 4:
        return 65, "Chuỗi quá ngắn"

    changes = sum(1 for i in range(1, len(seq)) if seq[i] != seq[i - 1])
    change_rate = changes / max(1, len(seq) - 1)
    singletons = sum(1 for _, ln in segments if ln == 1)
    singleton_ratio = singletons / max(1, len(segments))
    short_runs = sum(1 for _, ln in segments if ln <= 2)
    short_ratio = short_runs / max(1, len(segments))
    longest = max((ln for _, ln in segments), default=1)
    balance_gap = abs(seq.count("T") - seq.count("X")) / max(1, len(seq))

    score = int(
        change_rate * 38
        + singleton_ratio * 32
        + short_ratio * 18
        + (1 - min(longest, 10) / 10) * 12
        - balance_gap * 8
    )
    score = clamp(score, 0, 100)

    if score >= 75:
        reason = "Nhiễu cao, chuỗi đảo liên tục"
    elif score >= 60:
        reason = "Nhiễu trung bình"
    else:
        reason = "Nhiễu thấp"
    return score, reason


def detect_break_score(seq: List[str], segments: List[Tuple[str, int]]) -> Tuple[int, str]:
    if len(seq) < 5 or len(segments) < 2:
        return 0, "Không đủ dữ liệu"

    prev_val, prev_len = segments[-2]
    cur_val, cur_len = segments[-1]
    recent = segments[-6:]
    singleton_tail = sum(1 for _, ln in recent if ln == 1)
    short_tail = sum(1 for _, ln in recent if ln <= 2)

    score = 0
    if prev_len >= 6 and cur_len == 1:
        score += 45
    elif prev_len >= 5 and cur_len <= 2:
        score += 35
    elif prev_len >= 4 and cur_len == 1:
        score += 25

    if singleton_tail >= 3:
        score += 15
    if short_tail >= 4:
        score += 10
    if len(segments) >= 4 and segments[-3][1] >= 4 and segments[-2][1] == 1 and segments[-1][1] == 1:
        score += 10
    if cur_len == 1 and cur_val != prev_val:
        score += 5

    score = clamp(score, 0, 100)
    if score >= 70:
        desc = "Gãy mạnh"
    elif score >= 45:
        desc = "Gãy vừa"
    elif score > 0:
        desc = "Gãy nhẹ"
    else:
        desc = "Ổn định"
    return score, desc


def build_zigzag_table(seq: List[str], limit_segments: int = 10) -> str:
    if not seq:
        return "Chưa có dữ liệu zigzag."
    segs = rle(seq[-80:])
    if not segs:
        return "Chưa có dữ liệu zigzag."

    tail = segs[-limit_segments:]
    lines = ["Bảng cầu zigzag:"]
    for idx, (val, ln) in enumerate(tail, start=max(1, len(segs) - len(tail) + 1)):
        arrow = "↗" if val == "T" else "↘"
        lines.append(f"{idx:>2}. {fmt_outcome(val):<4} | x{ln:<2} | {arrow}")
    return "\n".join(lines)


def build_matrix_preview(signals: List[Dict[str, Any]], limit: int = 6) -> str:
    if not signals:
        return "Ma trận: Không có tín hiệu đủ mạnh"
    lines = ["Ma trận tín hiệu:"]
    for s in signals[:limit]:
        pred = fmt_outcome(s["predicted_outcome"]) if s["predicted_outcome"] in {"T", "X"} else "-"
        lines.append(
            f"- {s['pattern_label']} -> {pred} | {s['confidence']}% | w={s['weight']}"
        )
    return "\n".join(lines)


def build_extra_report(analysis: Dict[str, Any]) -> str:
    lines = []
    lines.append(f"Độ gãy cầu: {analysis.get('break_score', 0)}/100 ({analysis.get('break_desc', 'N/A')})")
    lines.append(f"Nhiễu tự động: {analysis.get('noise_score', 0)}/100 ({analysis.get('noise_desc', 'N/A')})")
    if analysis.get("blocked"):
        lines.append(f"Chặn nhiễu: {analysis.get('block_reason', 'Đã chặn')}")
    if analysis.get("zigzag_text"):
        lines.append(analysis["zigzag_text"])
    return "\n".join(lines)


def make_signal(label: str, predicted_outcome: Optional[str], confidence: int, source: str, strength: int) -> Optional[Dict[str, Any]]:
    if predicted_outcome not in {"T", "X"}:
        return None

    adjusted_pred, adjusted_conf, flipped, reason = adjust_prediction_by_memory(label, predicted_outcome, confidence)
    weight = max(1, int(round(adjusted_conf + min(strength, 12) * 2)))
    return {
        "pattern_label": label,
        "predicted_outcome": adjusted_pred,
        "confidence": adjusted_conf,
        "source": source,
        "strength": strength,
        "weight": weight,
        "flipped": flipped,
        "flip_reason": reason if flipped else "",
        "family": pattern_family(label),
    }


def matrix_vote(signals: List[Dict[str, Any]]) -> Tuple[Optional[str], int, int, int, int]:
    if not signals:
        return None, 0, 0, 0, 0

    grouped: Dict[str, Dict[str, Any]] = {}
    for s in signals:
        family = s["family"]
        current = grouped.get(family)
        if current is None or (s["weight"] > current["weight"]) or (
            s["weight"] == current["weight"] and s["confidence"] > current["confidence"]
        ):
            grouped[family] = s

    chosen = list(grouped.values())
    score_t = sum(int(s["weight"]) for s in chosen if s["predicted_outcome"] == "T")
    score_x = sum(int(s["weight"]) for s in chosen if s["predicted_outcome"] == "X")
    total = score_t + score_x
    if total <= 0:
        return None, 0, score_t, score_x, total

    pred = "T" if score_t >= score_x else "X"
    share = max(score_t, score_x) / total
    margin = abs(score_t - score_x) / total
    confidence = clamp(int(round(50 + share * 25 + margin * 35)), 0, 100)
    return pred, confidence, score_t, score_x, total


def build_signal_bundle(seq: List[str]) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "pattern_label": "Không nhận diện được cầu",
        "prediction": None,
        "confidence": 0,
        "recognized": False,
        "flipped": False,
        "flip_reason": "",
        "follow_pct": 0,
        "reverse_pct": 0,
        "source": "",
        "signals": [],
        "matrix_text": "",
        "zigzag_text": "",
        "break_score": 0,
        "break_desc": "",
        "noise_score": 0,
        "noise_desc": "",
        "blocked": False,
        "block_reason": "",
        "matrix_score_t": 0,
        "matrix_score_x": 0,
        "matrix_total": 0,
    }

    if len(seq) < 5:
        result["noise_score"], result["noise_desc"] = detect_noise_score(seq, rle(seq))
        result["zigzag_text"] = build_zigzag_table(seq)
        return result

    window = seq[-60:]
    segments = rle(window)
    last_val, streak_len = current_streak(window)
    signals: List[Dict[str, Any]] = []

    soft_streak = detect_soft_streak(window)
    if soft_streak and soft_streak >= 3:
        sig = make_signal(f"Cầu bệt vừa {last_val} x{soft_streak}", last_val, 80, "SOFT_STREAK", soft_streak)
        if sig:
            signals.append(sig)

    if streak_len >= 5:
        sig = make_signal(f"Cầu bệt {last_val} x{streak_len}", last_val, 90, "STREAK", streak_len)
        if sig:
            signals.append(sig)

    alt_len = detect_alternating_pro(window)
    if alt_len and alt_len >= 6:
        pred = "T" if last_val == "X" else "X"
        sig = make_signal(f"Cầu zigzag {last_val} x{alt_len}", pred, 86, "ZIGZAG", alt_len)
        if sig:
            signals.append(sig)

    motif, rep = detect_exact_periodic_pro(window, min_period=2, max_period=6, min_repeats=3)
    if motif:
        motif_text = "-".join(motif)
        if len(motif) == 2 and motif[0] != motif[1]:
            pred = "T" if last_val == "X" else "X"
            sig = make_signal(f"Cầu chu kỳ đảo {motif_text} x{rep}", pred, 84, "PERIODIC_FLIP", rep)
            if sig:
                signals.append(sig)
        else:
            pred = motif[0]
            sig = make_signal(f"Cầu chu kỳ {motif_text} x{rep}", pred, 82, "PERIODIC", rep)
            if sig:
                signals.append(sig)

    approx_motif, approx_rep = detect_approx_periodic_pro(window, min_period=2, max_period=6, min_repeats=3, max_mismatches=2)
    if approx_motif:
        motif_text = "-".join(approx_motif)
        if len(approx_motif) == 2 and approx_motif[0] != approx_motif[1]:
            pred = "T" if last_val == "X" else "X"
            sig = make_signal(f"Cầu gần chu kỳ đảo {motif_text} x{approx_rep}", pred, 78, "NEAR_PERIODIC_FLIP", approx_rep)
            if sig:
                signals.append(sig)
        else:
            pred = approx_motif[0]
            sig = make_signal(f"Cầu gần chu kỳ {motif_text} x{approx_rep}", pred, 76, "NEAR_PERIODIC", approx_rep)
            if sig:
                signals.append(sig)

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
        sig = make_signal(length_sig, pred, conf, "LENGTH_SIGNATURE", sum(s[1] for s in segments[-6:]))
        if sig:
            signals.append(sig)

    mixed = detect_mixed_pattern_pro(window, segments)
    if mixed:
        pred = last_val if last_val in {"T", "X"} else None
        sig = make_signal(mixed, pred, 63, "MIXED", len(segments))
        if sig:
            signals.append(sig)

    break_type = detect_break_pro(window, segments)
    if break_type:
        pred = reverse_outcome(last_val) if last_val in {"T", "X"} else None
        sig = make_signal(break_type, pred, 73, "BREAK", segments[-1][1])
        if sig:
            signals.append(sig)

    transition = detect_transition_pro(segments)
    if transition:
        pred = last_val if last_val in {"T", "X"} else None
        sig = make_signal(transition, pred, 70, "TRANSITION", sum(s[1] for s in segments[-3:]))
        if sig:
            signals.append(sig)

    trend, strength = weighted_trend(window, window=20)
    if trend and strength >= 15:
        sig = make_signal(f"Xu hướng gần {trend}", trend, 60, "WEIGHTED_TREND", strength)
        if sig:
            signals.append(sig)

    # Không dùng “bên nào nhiều dự đoán bên đó” làm tín hiệu dự đoán chính nữa.
    # Chỉ giữ như thông tin nền để hiển thị nếu cần.

    noise_score, noise_desc = detect_noise_score(window, segments)
    break_score, break_desc = detect_break_score(window, segments)
    zigzag_text = build_zigzag_table(window)

    pred, conf, score_t, score_x, total = matrix_vote(signals)
    matrix_text = build_matrix_preview(signals)

    recognized = bool(signals)
    blocked = False
    block_reason = ""

    if pred not in {"T", "X"}:
        recognized = False

    if recognized and total > 0:
        # Chặn nhiễu tự động: chuỗi quá nhiễu thì chỉ cho phép tín hiệu thật sự mạnh đi qua.
        best_conf = max((int(s["confidence"]) for s in signals), default=0)
        if noise_score >= 75 and best_conf < 88:
            blocked = True
            block_reason = "Nhiễu cao, chặn dự đoán"
            pred = None
            conf = 0
            recognized = False
        elif noise_score >= 65 and conf < 68:
            blocked = True
            block_reason = "Nhiễu trung bình, độ mạnh chưa đủ"
            pred = None
            conf = 0
            recognized = False

    if recognized and pred in {"T", "X"}:
        result.update(
            {
                "pattern_label": signals[0]["pattern_label"],
                "prediction": pred,
                "confidence": conf,
                "recognized": True,
                "flipped": False,
                "flip_reason": "",
                "follow_pct": conf,
                "reverse_pct": max(0, min(100, 100 - conf)),
                "source": "MATRIX",
                "signals": signals,
                "matrix_text": matrix_text,
                "zigzag_text": zigzag_text,
                "break_score": break_score,
                "break_desc": break_desc,
                "noise_score": noise_score,
                "noise_desc": noise_desc,
                "blocked": blocked,
                "block_reason": block_reason,
                "matrix_score_t": score_t,
                "matrix_score_x": score_x,
                "matrix_total": total,
            }
        )
    else:
        result.update(
            {
                "signals": signals,
                "matrix_text": matrix_text,
                "zigzag_text": zigzag_text,
                "break_score": break_score,
                "break_desc": break_desc,
                "noise_score": noise_score,
                "noise_desc": noise_desc,
                "blocked": blocked,
                "block_reason": block_reason,
                "matrix_score_t": score_t,
                "matrix_score_x": score_x,
                "matrix_total": total,
            }
        )
        if signals:
            result["pattern_label"] = signals[0]["pattern_label"]

    return result


def classify_pattern_pro(seq: List[str]) -> Dict[str, Any]:
    return build_signal_bundle(seq)


# =========================
# OUTPUT BUILDERS
# =========================
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
        if analysis.get("blocked"):
            section5 += f"\nChặn nhiễu: {analysis.get('block_reason', '')}"
    else:
        section5 = "Dự đoán mới: Không dự đoán"
        if analysis.get("blocked"):
            section5 += f"\nChặn nhiễu: {analysis.get('block_reason', '')}"

    extra = build_extra_report(analysis)
    matrix = analysis.get("matrix_text", "")

    return (
        f"Đã lưu kết quả: +{inserted_count} | Tổng đã lưu: {total_saved}\n"
        f"{section2}\n"
        f"{section3}\n"
        f"{section4}\n"
        f"{section5}\n"
        f"{extra}\n"
        f"{matrix}"
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
        if analysis.get("blocked"):
            section4 += f"\nChặn nhiễu: {analysis.get('block_reason', '')}"
    else:
        section4 = "Dự đoán mới: Không dự đoán"
        if analysis.get("blocked"):
            section4 += f"\nChặn nhiễu: {analysis.get('block_reason', '')}"

    extra = build_extra_report(analysis)
    matrix = analysis.get("matrix_text", "")
    return f"{section2}\n{section3}\n{section4}\n{extra}\n{matrix}"


def build_matrix_reply(analysis: Dict[str, Any]) -> str:
    if analysis["recognized"] and analysis["prediction"] in {"T", "X"}:
        out = [
            f"Ma trận tổng hợp: {fmt_outcome(analysis['prediction'])}",
            f"Độ tin cậy: {analysis['confidence']}%",
            f"Tổng điểm: T={analysis.get('matrix_score_t', 0)} | X={analysis.get('matrix_score_x', 0)} | Tổng={analysis.get('matrix_total', 0)}",
            f"Độ gãy cầu: {analysis.get('break_score', 0)}/100 ({analysis.get('break_desc', 'N/A')})",
            f"Nhiễu tự động: {analysis.get('noise_score', 0)}/100 ({analysis.get('noise_desc', 'N/A')})",
            analysis.get("matrix_text", "Ma trận: Không có tín hiệu đủ mạnh"),
            analysis.get("zigzag_text", "Bảng cầu zigzag: Chưa có dữ liệu"),
        ]
        if analysis.get("blocked"):
            out.append(f"Chặn nhiễu: {analysis.get('block_reason', '')}")
        return "\n".join(out)

    out = [
        "Ma trận tổng hợp: Không dự đoán",
        f"Độ gãy cầu: {analysis.get('break_score', 0)}/100 ({analysis.get('break_desc', 'N/A')})",
        f"Nhiễu tự động: {analysis.get('noise_score', 0)}/100 ({analysis.get('noise_desc', 'N/A')})",
        analysis.get("matrix_text", "Ma trận: Không có tín hiệu đủ mạnh"),
        analysis.get("zigzag_text", "Bảng cầu zigzag: Chưa có dữ liệu"),
    ]
    if analysis.get("blocked"):
        out.append(f"Chặn nhiễu: {analysis.get('block_reason', '')}")
    return "\n".join(out)


def save_current_prediction_if_any(seq: List[str]) -> None:
    current_count = len(seq)
    if current_count < MIN_PREDICT_HISTORY:
        return
    if has_pending_prediction_for_count(current_count):
        return

    analysis = classify_pattern_pro(seq)
    if analysis["recognized"] and analysis["prediction"] in {"T", "X"} and not analysis.get("blocked"):
        save_prediction(
            pattern=analysis["pattern_label"],
            predicted_outcome=analysis["prediction"],
            confidence=analysis["confidence"],
            based_on_count=current_count,
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
    "/add <dữ liệu>          - thêm 1 hoặc nhiều kết quả (live)\n"
    "/import <dữ liệu>       - dán lịch sử dài (chỉ lưu)\n"
    "/history [n]            - xem n kết quả gần nhất\n"
    "/stats [n]              - thống kê tần suất\n"
    "/scan [n]               - phân tích lịch sử\n"
    "/patterns [n]           - xem nhận diện cầu\n"
    "/matrix [n]             - xem ma trận tổng hợp + zigzag\n"
    "/brain [n]              - xem bộ nhớ não\n"
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
    if not update.message:
        return
    items = extract_outcomes(text)
    if not items:
        await update.message.reply_text("Không tìm thấy dữ liệu hợp lệ.")
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
            f"Tỷ lệ: Theo {analysis['follow_pct']}% | Bẻ {analysis['reverse_pct']}%\n"
            f"Độ gãy cầu: {analysis.get('break_score', 0)}/100\n"
            f"Nhiễu tự động: {analysis.get('noise_score', 0)}/100"
        )
        if analysis["flipped"]:
            msg += f"\nĐã bẻ: {analysis['flip_reason']}"
        if analysis.get("blocked"):
            msg += f"\nChặn nhiễu: {analysis.get('block_reason', '')}"
    elif analysis["recognized"]:
        msg = (
            f"Cầu: {analysis['pattern_label']}\n"
            f"Vào: Không dự đoán\n"
            f"Độ gãy cầu: {analysis.get('break_score', 0)}/100\n"
            f"Nhiễu tự động: {analysis.get('noise_score', 0)}/100"
        )
        if analysis.get("blocked"):
            msg += f"\nChặn nhiễu: {analysis.get('block_reason', '')}"
    else:
        msg = (
            "Cầu: Không nhận diện được cầu\n"
            f"Độ gãy cầu: {analysis.get('break_score', 0)}/100\n"
            f"Nhiễu tự động: {analysis.get('noise_score', 0)}/100"
        )
        if analysis.get("blocked"):
            msg += f"\nChặn nhiễu: {analysis.get('block_reason', '')}"

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
    segs = rle(seq[-n:])
    seg_text = " ".join(f"{v}{k}" for v, k in segs[-12:])

    if analysis["recognized"] and analysis["prediction"] in {"T", "X"} and len(seq) >= MIN_PREDICT_HISTORY:
        reply = (
            f"Nhận diện: {analysis['pattern_label']}\n"
            f"Chuỗi segment: {seg_text}\n"
            f"Vào: {fmt_outcome(analysis['prediction'])} | Độ tin cậy: {analysis['confidence']}%\n"
            f"Tỷ lệ: Theo {analysis['follow_pct']}% | Bẻ {analysis['reverse_pct']}%\n"
            f"Độ gãy cầu: {analysis.get('break_score', 0)}/100 ({analysis.get('break_desc', 'N/A')})\n"
            f"Nhiễu tự động: {analysis.get('noise_score', 0)}/100 ({analysis.get('noise_desc', 'N/A')})\n"
            f"Tổng điểm: T={analysis.get('matrix_score_t', 0)} | X={analysis.get('matrix_score_x', 0)}"
        )
        if analysis["flipped"]:
            reply += f"\nĐã bẻ: {analysis['flip_reason']}"
        if analysis.get("blocked"):
            reply += f"\nChặn nhiễu: {analysis.get('block_reason', '')}"
    elif analysis["recognized"]:
        reply = (
            f"Nhận diện: {analysis['pattern_label']}\n"
            f"Chuỗi segment: {seg_text}\n"
            f"Vào: Không dự đoán\n"
            f"Độ gãy cầu: {analysis.get('break_score', 0)}/100 ({analysis.get('break_desc', 'N/A')})\n"
            f"Nhiễu tự động: {analysis.get('noise_score', 0)}/100 ({analysis.get('noise_desc', 'N/A')})\n"
            f"Tổng điểm: T={analysis.get('matrix_score_t', 0)} | X={analysis.get('matrix_score_x', 0)}"
        )
        if analysis.get("blocked"):
            reply += f"\nChặn nhiễu: {analysis.get('block_reason', '')}"
    else:
        reply = (
            "Nhận diện: Không nhận diện được cầu\n"
            f"Chuỗi segment: {seg_text}\n"
            f"Độ gãy cầu: {analysis.get('break_score', 0)}/100 ({analysis.get('break_desc', 'N/A')})\n"
            f"Nhiễu tự động: {analysis.get('noise_score', 0)}/100 ({analysis.get('noise_desc', 'N/A')})\n"
            f"Tổng điểm: T={analysis.get('matrix_score_t', 0)} | X={analysis.get('matrix_score_x', 0)}"
        )
        if analysis.get("blocked"):
            reply += f"\nChặn nhiễu: {analysis.get('block_reason', '')}"

    await update.message.reply_text(reply)


async def matrix_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    reply = build_matrix_reply(analysis)
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
    app.add_handler(CommandHandler("matrix", matrix_cmd))
    app.add_handler(CommandHandler("brain", brain_cmd))
    app.add_handler(CommandHandler("clear", clear_cmd))
    app.add_handler(CommandHandler("reset_history", reset_history_cmd))
    app.add_handler(CommandHandler("reset_all", reset_all_cmd))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), handle_text))

    logger.info("Bot đang chạy...")
    app.run_polling()


if __name__ == "__main__":
    main()
