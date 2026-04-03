import os
import re
import json
import math
import sqlite3
import asyncio
import logging
import time
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from telegram import Update
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DB_FILE = os.getenv("DB_FILE", "ai_state.db")

THRESHOLD = int(os.getenv("THRESHOLD", "11"))
LOW_LABEL = os.getenv("LOW_LABEL", "Xỉu")
HIGH_LABEL = os.getenv("HIGH_LABEL", "Tài")

RECENT_CACHE = int(os.getenv("RECENT_CACHE", "500"))
MAX_KEEP_HISTORY = int(os.getenv("MAX_KEEP_HISTORY", "0"))  # 0 = giữ lâu dài
MAX_INPUT_NUMS = int(os.getenv("MAX_INPUT_NUMS", "120"))
USER_CACHE_LIMIT = int(os.getenv("USER_CACHE_LIMIT", "500"))
MIN_ANALYSIS_LEN = int(os.getenv("MIN_ANALYSIS_LEN", "6"))
HISTORY_ANALYSIS_LIMIT = int(os.getenv("HISTORY_ANALYSIS_LIMIT", "0"))  # 0 = toàn bộ

if not TOKEN:
    raise RuntimeError("Thiếu TELEGRAM_BOT_TOKEN")

DB_LOCK = asyncio.Lock()
STATE_LOCK = asyncio.Lock()
users: Dict[int, Dict[str, Any]] = {}


# =========================
# DB
# =========================
def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    return conn


async def run_db_work(fn):
    return await asyncio.to_thread(fn)


def init_db() -> None:
    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS chat_state (
                chat_id INTEGER PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at INTEGER NOT NULL DEFAULT (unixepoch())
            );

            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                raw_value INTEGER NOT NULL,
                label TEXT NOT NULL,
                created_at INTEGER NOT NULL DEFAULT (unixepoch())
            );

            CREATE INDEX IF NOT EXISTS idx_history_chat_id_id ON history(chat_id, id);
            """
        )
        conn.commit()


def prune_history(conn: sqlite3.Connection, chat_id: int, keep_limit: int) -> None:
    if keep_limit <= 0:
        return
    row = conn.execute(
        "SELECT id FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT 1 OFFSET ?",
        (chat_id, max(0, keep_limit - 1)),
    ).fetchone()
    if row:
        conn.execute("DELETE FROM history WHERE chat_id = ? AND id < ?", (chat_id, int(row["id"])))


async def append_history(chat_id: int, items: List[Tuple[int, str]]) -> None:
    if not items:
        return

    def _work():
        with db_connect() as conn:
            for raw_value, label in items:
                conn.execute(
                    "INSERT INTO history (chat_id, raw_value, label) VALUES (?, ?, ?)",
                    (chat_id, int(raw_value), label),
                )
            prune_history(conn, chat_id, MAX_KEEP_HISTORY)
            conn.commit()

    async with DB_LOCK:
        await run_db_work(_work)


async def load_history_rows(chat_id: int, limit: int = HISTORY_ANALYSIS_LIMIT) -> List[Tuple[int, str]]:
    def _work():
        with db_connect() as conn:
            if limit and limit > 0:
                rows = conn.execute(
                    "SELECT raw_value, label FROM history WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
                    (chat_id, limit),
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = conn.execute(
                    "SELECT raw_value, label FROM history WHERE chat_id = ? ORDER BY id ASC",
                    (chat_id,),
                ).fetchall()
            return [(int(r["raw_value"]), str(r["label"])) for r in rows]

    async with DB_LOCK:
        return await run_db_work(_work)


# =========================
# STATE
# =========================
def new_state() -> Dict[str, Any]:
    return {
        "values": [],
        "labels": [],
        "total": 0,
        "low_count": 0,
        "high_count": 0,
        "last_prediction_label": None,
        "last_prediction_conf": 0,
        "last_prediction_result": "CHƯA RÕ",
        "prediction_total": 0,
        "prediction_hits": 0,
        "prediction_misses": 0,
        "model_accuracy": {
            "markov": 50,
            "pattern": 50,
            "structure": 50,
        },
        "last_note": "",
        "last_structure": "CHƯA ĐỦ DỮ LIỆU",
        "last_mode": "NORMAL",
        "last_model_predictions": {},
    }


def _safe_tail(seq: List[Any], limit: int) -> List[Any]:
    return list(seq[-limit:]) if len(seq) > limit else list(seq)


def trim_state_memory(d: Dict[str, Any]) -> None:
    d["values"] = _safe_tail(d.get("values", []), RECENT_CACHE)
    d["labels"] = _safe_tail(d.get("labels", []), RECENT_CACHE)


def rebuild_counters_from_labels(d: Dict[str, Any], labels: List[str]) -> None:
    d["low_count"] = labels.count(LOW_LABEL)
    d["high_count"] = labels.count(HIGH_LABEL)
    d["total"] = len(labels)


def repair_state(d: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(d, dict):
        d = new_state()

    for k in ("values", "labels"):
        if not isinstance(d.get(k), list):
            d[k] = []

    if not isinstance(d.get("model_accuracy"), dict):
        d["model_accuracy"] = {
            "markov": 50,
            "pattern": 50,
            "structure": 50,
        }

    d.setdefault("total", 0)
    d.setdefault("low_count", 0)
    d.setdefault("high_count", 0)
    d.setdefault("last_prediction_label", None)
    d.setdefault("last_prediction_conf", 0)
    d.setdefault("last_prediction_result", "CHƯA RÕ")
    d.setdefault("prediction_total", 0)
    d.setdefault("prediction_hits", 0)
    d.setdefault("prediction_misses", 0)
    d.setdefault("last_note", "")
    d.setdefault("last_structure", "CHƯA ĐỦ DỮ LIỆU")
    d.setdefault("last_mode", "NORMAL")
    d.setdefault("last_model_predictions", {})

    n = min(len(d["values"]), len(d["labels"]))
    d["values"] = d["values"][-n:] if n else []
    d["labels"] = d["labels"][-n:] if n else []

    trim_state_memory(d)
    rebuild_counters_from_labels(d, d.get("labels", []))
    return d


def trim_cache() -> None:
    if len(users) <= USER_CACHE_LIMIT:
        return
    overflow = len(users) - USER_CACHE_LIMIT
    for chat_id in list(users.keys())[:overflow]:
        users.pop(chat_id, None)


def map_value(n: int) -> str:
    return HIGH_LABEL if n >= THRESHOLD else LOW_LABEL


def opposite_label(label: str) -> str:
    if label == LOW_LABEL:
        return HIGH_LABEL
    if label == HIGH_LABEL:
        return LOW_LABEL
    return LOW_LABEL


def get_key(update: Update) -> int:
    return update.effective_chat.id


def parse_input(text: str) -> List[int]:
    nums: List[int] = []
    for x in re.findall(r"\d+", text or ""):
        try:
            n = int(x)
            if n >= 0:
                nums.append(n)
        except Exception:
            pass
    return nums[:MAX_INPUT_NUMS]


async def load_state(chat_id: int, force_reload: bool = False) -> Dict[str, Any]:
    if not force_reload and chat_id in users:
        return repair_state(users[chat_id])

    def _work():
        with db_connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM chat_state WHERE chat_id = ?",
                (chat_id,),
            ).fetchone()
            return row

    async with DB_LOCK:
        row = await run_db_work(_work)

    state = new_state()
    if row:
        try:
            state.update(json.loads(row["state_json"]))
        except Exception:
            pass

    state = repair_state(state)
    users[chat_id] = state
    trim_cache()
    return state


async def save_state(chat_id: int, state: Dict[str, Any]) -> None:
    state = repair_state(state)

    def _work():
        with db_connect() as conn:
            conn.execute(
                """
                INSERT INTO chat_state (chat_id, state_json, updated_at)
                VALUES (?, ?, unixepoch())
                ON CONFLICT(chat_id) DO UPDATE SET
                    state_json=excluded.state_json,
                    updated_at=excluded.updated_at
                """,
                (chat_id, json.dumps(state, ensure_ascii=False)),
            )
            prune_history(conn, chat_id, MAX_KEEP_HISTORY)
            conn.commit()

    async with DB_LOCK:
        await run_db_work(_work)


# =========================
# ANALYSIS HELPERS
# =========================
def safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def current_streak(labels: List[str]) -> Tuple[Optional[str], int]:
    if not labels:
        return None, 0
    last = labels[-1]
    streak = 1
    for i in range(len(labels) - 2, -1, -1):
        if labels[i] == last:
            streak += 1
        else:
            break
    return last, streak


def run_length_encode(labels: List[str]) -> List[Tuple[str, int]]:
    if not labels:
        return []
    out: List[Tuple[str, int]] = []
    cur = labels[0]
    count = 1
    for x in labels[1:]:
        if x == cur:
            count += 1
        else:
            out.append((cur, count))
            cur = x
            count = 1
    out.append((cur, count))
    return out


def recent_ratio(labels: List[str], window: int) -> Dict[str, float]:
    tail = labels[-window:] if len(labels) > window else labels[:]
    if not tail:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}
    c = Counter(tail)
    total = c[LOW_LABEL] + c[HIGH_LABEL]
    if total <= 0:
        return {LOW_LABEL: 0.5, HIGH_LABEL: 0.5}
    return {LOW_LABEL: c[LOW_LABEL] / total, HIGH_LABEL: c[HIGH_LABEL] / total}


def alternating_tail(labels: List[str], window: int = 6) -> Tuple[bool, float]:
    tail = labels[-window:] if len(labels) >= window else labels[:]
    if len(tail) < 4:
        return False, 0.0
    changes = sum(1 for i in range(1, len(tail)) if tail[i] != tail[i - 1])
    ratio = changes / (len(tail) - 1)
    return all(tail[i] != tail[i - 1] for i in range(1, len(tail))), ratio


def entropy_score(labels: List[str], window: int = 20) -> float:
    tail = labels[-window:] if len(labels) > window else labels[:]
    if len(tail) < 4:
        return 0.0
    c = Counter(tail)
    total = len(tail)
    ent = 0.0
    for v in c.values():
        p = v / total
        ent -= p * math.log2(p)
    return ent


def volatility_score(labels: List[str], window: int = 12) -> float:
    tail = labels[-window:] if len(labels) > window else labels[:]
    if len(tail) < 4:
        return 0.0
    changes = sum(1 for i in range(1, len(tail)) if tail[i] != tail[i - 1])
    return changes / (len(tail) - 1)


def detect_repeat_block(labels: List[str], max_block: int = 3) -> Optional[Dict[str, Any]]:
    tail = labels[-12:] if len(labels) > 12 else labels[:]
    for block in range(1, max_block + 1):
        if len(tail) >= block * 2:
            a = tail[-(block * 2):-block]
            b = tail[-block:]
            if a == b:
                return {
                    "name": f"LẶP KHỐI {block}",
                    "detail": f"Khối {block} mẫu gần nhất đang lặp",
                    "score": min(78 + block * 4, 92),
                    "hint": a[0] if a else None,
                }
    return None


def detect_motif_repeat(labels: List[str], max_motif: int = 8) -> Optional[Dict[str, Any]]:
    tail = labels[-40:] if len(labels) > 40 else labels[:]
    if len(tail) < 4:
        return None

    best: Optional[Dict[str, Any]] = None

    for m in range(1, min(max_motif, len(tail) // 2) + 1):
        motif = tail[-m:]
        reps = 1
        idx = len(tail) - m

        while idx - m >= 0 and tail[idx - m:idx] == motif:
            reps += 1
            idx -= m

        if reps >= 2:
            if m == 1:
                name = "BỆT"
                hint = motif[0]
                score = min(70 + reps * 6, 95)
            elif m == 2 and motif[0] != motif[1]:
                name = "XEN KẼ"
                hint = opposite_label(tail[-1])
                score = min(76 + reps * 4, 94)
            else:
                name = f"LẶP MẪU {m}"
                hint = motif[0]
                score = min(72 + reps * 5, 94)

            cand = {
                "name": name,
                "detail": f"Mẫu {m} lặp {reps} lần",
                "score": score,
                "hint": hint,
            }

            if best is None or cand["score"] > best["score"]:
                best = cand

    return best


def detect_explicit_pair_patterns(labels: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if len(labels) < 4:
        return out

    a, b, c, d = labels[-4], labels[-3], labels[-2], labels[-1]

    if a != b and a == c and b == d:
        out.append({
            "name": "1-1",
            "detail": "Mẫu luân phiên 1-1",
            "score": 90,
            "hint": a,
        })

    if a == b and c == d and a != c:
        out.append({
            "name": "2-2",
            "detail": "Mẫu chia cặp 2-2",
            "score": 86,
            "hint": a,
        })

    if len(labels) >= 6:
        t = labels[-6:]
        if t[0] == t[2] == t[4] and t[1] == t[3] == t[5] and t[0] != t[1]:
            out.append({
                "name": "XEN KẼ SÂU",
                "detail": "Luân phiên đều trong 6 mẫu gần nhất",
                "score": 94,
                "hint": t[0],
            })

    return out


def detect_run_cycle(labels: List[str]) -> Optional[Dict[str, Any]]:
    runs = run_length_encode(labels)
    if len(runs) < 4:
        return None

    a1, l1 = runs[-4]
    b1, m1 = runs[-3]
    a2, l2 = runs[-2]
    b2, m2 = runs[-1]

    if a1 == a2 and b1 == b2 and l1 == l2 and m1 == m2 and a1 != b1:
        return {
            "name": f"CẦU {l1}-{m1}",
            "detail": f"Chu kỳ 2 khối lặp: {l1}-{m1}",
            "score": min(82 + (l1 + m1) * 2, 95),
            "hint": a2,
        }

    return None


def detect_bias(labels: List[str]) -> Optional[Dict[str, Any]]:
    if len(labels) < 12:
        return None

    r6 = recent_ratio(labels, 6)
    r12 = recent_ratio(labels, 12)
    r24 = recent_ratio(labels, 24)

    gap6 = abs(r6[LOW_LABEL] - r6[HIGH_LABEL])
    gap12 = abs(r12[LOW_LABEL] - r12[HIGH_LABEL])
    gap24 = abs(r24[LOW_LABEL] - r24[HIGH_LABEL])

    if gap6 >= 0.50:
        winner = LOW_LABEL if r6[LOW_LABEL] > r6[HIGH_LABEL] else HIGH_LABEL
        return {
            "name": "NGHIÊNG NHẸ",
            "detail": f"Đuôi 6 nghiêng về {winner}",
            "score": 72,
            "hint": winner,
        }

    if gap12 >= 0.35:
        winner = LOW_LABEL if r12[LOW_LABEL] > r12[HIGH_LABEL] else HIGH_LABEL
        return {
            "name": "NGHIÊNG",
            "detail": f"Đuôi 12 nghiêng về {winner}",
            "score": 78,
            "hint": winner,
        }

    if gap24 >= 0.25:
        winner = LOW_LABEL if r24[LOW_LABEL] > r24[HIGH_LABEL] else HIGH_LABEL
        return {
            "name": "XU HƯỚNG",
            "detail": f"24 mẫu gần đây nghiêng về {winner}",
            "score": 80,
            "hint": winner,
        }

    if gap24 < 0.10:
        return {
            "name": "CÂN BẰNG",
            "detail": "Hai phía gần như ngang nhau",
            "score": 65,
            "hint": None,
        }

    return None


def detect_reversal(labels: List[str]) -> Optional[Dict[str, Any]]:
    if len(labels) < 12:
        return None

    first = labels[-12:-6]
    second = labels[-6:]
    if not first or not second:
        return None

    c1 = Counter(first)
    c2 = Counter(second)
    d1 = c1[HIGH_LABEL] - c1[LOW_LABEL]
    d2 = c2[HIGH_LABEL] - c2[LOW_LABEL]

    if d1 == 0 or d2 == 0:
        return None

    if (d1 > 0 > d2) or (d1 < 0 < d2):
        return {
            "name": "ĐẢO CHIỀU",
            "detail": "Hai cụm gần nhất đang đổi hướng",
            "score": 84,
            "hint": HIGH_LABEL if d2 > 0 else LOW_LABEL,
        }
    return None


def detect_all_patterns(labels: List[str]) -> List[Dict[str, Any]]:
    patterns: List[Dict[str, Any]] = []

    motif = detect_motif_repeat(labels, 8)
    if motif:
        patterns.append(motif)

    pair_patterns = detect_explicit_pair_patterns(labels)
    patterns.extend(pair_patterns)

    run_cycle = detect_run_cycle(labels)
    if run_cycle:
        patterns.append(run_cycle)

    repeat_block = detect_repeat_block(labels, 3)
    if repeat_block:
        patterns.append(repeat_block)

    bias = detect_bias(labels)
    if bias:
        patterns.append(bias)

    reversal = detect_reversal(labels)
    if reversal:
        patterns.append(reversal)

    last, streak = current_streak(labels)
    if last in (LOW_LABEL, HIGH_LABEL) and streak >= 3:
        patterns.append({
            "name": "BỆT",
            "detail": f"{last} x{streak}",
            "score": min(68 + streak * 6, 95),
            "hint": last,
        })

    alt, alt_ratio = alternating_tail(labels, 6)
    if alt and alt_ratio >= 0.80 and len(labels) >= 6:
        patterns.append({
            "name": "XEN KẼ",
            "detail": "Chuỗi đổi liên tục",
            "score": 88,
            "hint": opposite_label(labels[-1]),
        })

    return sorted(patterns, key=lambda x: x.get("score", 0), reverse=True)[:10]


def build_report(labels: List[str]) -> Dict[str, Any]:
    c = Counter(labels)
    last, streak = current_streak(labels)
    alt, alt_ratio = alternating_tail(labels, 6)
    r6 = recent_ratio(labels, 6)
    r12 = recent_ratio(labels, 12)
    r24 = recent_ratio(labels, 24)
    ent = entropy_score(labels, 20)
    vol = volatility_score(labels, 12)
    patterns = detect_all_patterns(labels)

    if len(labels) < 6:
        structure = "CHƯA ĐỦ DỮ LIỆU"
        detail = "Cần thêm kết quả"
    elif patterns:
        structure = patterns[0]["name"]
        detail = patterns[0]["detail"]
    else:
        gap6 = abs(r6[LOW_LABEL] - r6[HIGH_LABEL])
        gap12 = abs(r12[LOW_LABEL] - r12[HIGH_LABEL])
        gap24 = abs(r24[LOW_LABEL] - r24[HIGH_LABEL])

        if alt and alt_ratio >= 0.80:
            structure = "XEN KẼ"
            detail = "Chuỗi đổi liên tục"
        elif streak >= 4 and last in (LOW_LABEL, HIGH_LABEL):
            structure = "BỆT"
            detail = f"{last} x{streak}"
        elif gap6 >= 0.50:
            winner = LOW_LABEL if r6[LOW_LABEL] > r6[HIGH_LABEL] else HIGH_LABEL
            structure = "NGHIÊNG NHẸ"
            detail = f"Đuôi 6 nghiêng về {winner}"
        elif gap12 >= 0.35:
            winner = LOW_LABEL if r12[LOW_LABEL] > r12[HIGH_LABEL] else HIGH_LABEL
            structure = "NGHIÊNG"
            detail = f"Đuôi 12 nghiêng về {winner}"
        elif gap24 < 0.10:
            structure = "CÂN BẰNG"
            detail = "Hai phía gần như ngang nhau"
        elif gap24 >= 0.25:
            winner = LOW_LABEL if r24[LOW_LABEL] > r24[HIGH_LABEL] else HIGH_LABEL
            structure = "XU HƯỚNG"
            detail = f"24 mẫu gần đây nghiêng về {winner}"
        else:
            structure = "TRUNG TÍNH"
            detail = "Chưa có tín hiệu quá rõ"

    dominant_label = HIGH_LABEL if c[HIGH_LABEL] >= c[LOW_LABEL] else LOW_LABEL
    dominance_gap = abs(c[HIGH_LABEL] - c[LOW_LABEL]) / max(1, len(labels))

    return {
        "total": len(labels),
        "low": c.get(LOW_LABEL, 0),
        "high": c.get(HIGH_LABEL, 0),
        "labels": labels,
        "last_label": last,
        "streak": streak,
        "alternating": alt,
        "alt_ratio": alt_ratio,
        "structure": structure,
        "detail": detail,
        "recent_6": r6,
        "recent_12": r12,
        "recent_24": r24,
        "entropy": ent,
        "volatility": vol,
        "patterns": patterns,
        "dominant_label": dominant_label,
        "dominance_gap": dominance_gap,
    }


def advanced_metrics(labels: List[str]) -> Dict[str, Any]:
    if not labels:
        return {
            "max_streak": 0,
            "r10_high": 0.5,
            "r20_high": 0.5,
            "momentum": 0.0,
            "noise": 0.0,
            "reversal": 0.0,
        }

    max_streak = 1
    cur = 1
    for i in range(1, len(labels)):
        if labels[i] == labels[i - 1]:
            cur += 1
            max_streak = max(max_streak, cur)
        else:
            cur = 1

    last10 = labels[-10:]
    last20 = labels[-20:]

    def ratio_high(seq: List[str]) -> float:
        return safe_div(seq.count(HIGH_LABEL), len(seq)) if seq else 0.5

    r10 = ratio_high(last10)
    r20 = ratio_high(last20)
    momentum = r10 - r20
    changes = sum(1 for i in range(1, len(labels)) if labels[i] != labels[i - 1])
    noise = safe_div(changes, len(labels))
    reversal = abs(momentum) * 100

    return {
        "max_streak": max_streak,
        "r10_high": r10,
        "r20_high": r20,
        "momentum": momentum,
        "noise": noise,
        "reversal": reversal,
    }


# =========================
# PREDICTION MODELS
# =========================
def predict_markov(labels: List[str]) -> Dict[str, Any]:
    if len(labels) < 2:
        return {"label": labels[-1] if labels else LOW_LABEL, "confidence": 50, "source": "markov"}

    scores = Counter()

    for order in range(1, 5):
        if len(labels) < order + 1:
            continue

        key = tuple(labels[-order:])
        next_counts = Counter()
        occur = 0

        start = max(0, len(labels) - 120)
        for i in range(start, len(labels) - order):
            if tuple(labels[i:i + order]) == key and i + order < len(labels):
                age = len(labels) - (i + order)
                weight = 1.0 / (1.0 + age / 8.0)
                next_counts[labels[i + order]] += weight
                occur += 1

        if occur >= 2 and next_counts:
            best = max(next_counts, key=next_counts.get)
            total = sum(next_counts.values())
            ratio = next_counts[best] / total
            scores[best] += ratio * (10 + order * 5)

    if not scores:
        last = labels[-1]
        return {"label": last, "confidence": 52, "source": "markov"}

    best = max(scores, key=scores.get)
    conf = min(93, 54 + int(scores[best] * 2.8))
    return {"label": best, "confidence": conf, "source": "markov"}


def predict_suffix_repeat(labels: List[str]) -> Optional[Dict[str, Any]]:
    if len(labels) < 4:
        return None

    for order in range(2, 6):
        if len(labels) < order + 2:
            continue

        suffix = tuple(labels[-order:])
        next_counts = Counter()
        occur = 0

        start = max(0, len(labels) - 160)
        for i in range(start, len(labels) - order):
            if tuple(labels[i:i + order]) == suffix and i + order < len(labels):
                age = len(labels) - (i + order)
                weight = 1.0 / (1.0 + age / 6.0)
                next_counts[labels[i + order]] += weight
                occur += 1

        if occur >= 2 and next_counts:
            best = max(next_counts, key=next_counts.get)
            total = sum(next_counts.values())
            ratio = next_counts[best] / total
            conf = min(90, 58 + int(ratio * 28))
            return {
                "label": best,
                "confidence": conf,
                "source": f"suffix-{order}",
            }

    return None


def predict_pattern(labels: List[str], report: Dict[str, Any]) -> Dict[str, Any]:
    patterns = report.get("patterns", [])
    if patterns:
        primary = patterns[0]
        hint = primary.get("hint")
        name = primary.get("name", "")
        if hint in (LOW_LABEL, HIGH_LABEL) and name in {
            "BỆT", "XEN KẼ", "1-1", "2-2", "XEN KẼ SÂU",
            "LẶP KHỐI 1", "LẶP KHỐI 2", "LẶP KHỐI 3",
            "LẶP MẪU 2", "LẶP MẪU 3", "LẶP MẪU 4", "LẶP MẪU 5", "LẶP MẪU 6", "LẶP MẪU 7", "LẶP MẪU 8",
            "CẦU 1-1", "CẦU 2-1", "CẦU 1-2", "CẦU 2-2", "CẦU 3-1", "CẦU 1-3", "CẦU 3-2", "CẦU 2-3", "CẦU 3-3",
            "NGHIÊNG", "NGHIÊNG NHẸ", "XU HƯỚNG", "ĐẢO CHIỀU"
        }:
            return {
                "label": hint,
                "confidence": min(95, int(primary.get("score", 60)) + 2),
                "source": f"pattern:{name}",
            }

    if len(labels) < 4:
        return {"label": labels[-1] if labels else LOW_LABEL, "confidence": 50, "source": "pattern"}

    suffix_pred = predict_suffix_repeat(labels)
    if suffix_pred:
        return suffix_pred

    if labels[-1] == labels[-2] == labels[-3]:
        return {"label": labels[-1], "confidence": 66, "source": "pattern"}

    if labels[-1] != labels[-2]:
        return {"label": labels[-2], "confidence": 56, "source": "pattern"}

    return {"label": labels[-1], "confidence": 54, "source": "pattern"}


def predict_structure(labels: List[str], report: Dict[str, Any]) -> Dict[str, Any]:
    patterns = report.get("patterns", [])
    if patterns:
        top = patterns[0]
        hint = top.get("hint")
        name = top.get("name", "")

        strong_same = {
            "BỆT", "LẶP MẪU 1", "LẶP KHỐI 1",
            "NGHIÊNG", "NGHIÊNG NHẸ", "XU HƯỚNG"
        }
        strong_opposite = {
            "XEN KẼ", "1-1", "2-2", "XEN KẼ SÂU"
        }

        if hint in (LOW_LABEL, HIGH_LABEL) and name in strong_same:
            return {
                "label": hint,
                "confidence": min(95, int(top.get("score", 60)) + 1),
                "source": f"structure:{name}",
            }

        if hint in (LOW_LABEL, HIGH_LABEL) and name in strong_opposite:
            return {
                "label": hint,
                "confidence": min(92, int(top.get("score", 60)) + 1),
                "source": f"structure:{name}",
            }

        if name.startswith("CẦU ") or name.startswith("LẶP MẪU"):
            return {
                "label": hint if hint in (LOW_LABEL, HIGH_LABEL) else (labels[-1] if labels else LOW_LABEL),
                "confidence": min(92, int(top.get("score", 60)) + 1),
                "source": f"structure:{name}",
            }

        if name == "CÂN BẰNG":
            return {
                "label": labels[-1] if labels else LOW_LABEL,
                "confidence": 52,
                "source": "structure:balance",
            }

    dominant = report.get("dominant_label", HIGH_LABEL)
    gap = float(report.get("dominance_gap", 0.0))
    if gap >= 0.20:
        return {
            "label": dominant,
            "confidence": min(82, 56 + int(gap * 100)),
            "source": "structure:dominant",
        }

    return {
        "label": dominant,
        "confidence": 55,
        "source": "structure",
    }


def meta_decision(predictions: Dict[str, Dict[str, Any]], state: Dict[str, Any], report: Dict[str, Any]) -> Dict[str, Any]:
    model_acc = state.get("model_accuracy", {})
    vote: Dict[str, float] = defaultdict(float)
    model_scores: Dict[str, float] = {}

    strong_patterns = {
        "BỆT", "XEN KẼ", "1-1", "2-2", "XEN KẼ SÂU",
        "LẶP KHỐI 1", "LẶP KHỐI 2", "LẶP KHỐI 3",
        "LẶP MẪU 2", "LẶP MẪU 3", "LẶP MẪU 4", "LẶP MẪU 5", "LẶP MẪU 6", "LẶP MẪU 7", "LẶP MẪU 8",
        "CẦU 1-1", "CẦU 2-1", "CẦU 1-2", "CẦU 2-2", "CẦU 3-1", "CẦU 1-3", "CẦU 3-2", "CẦU 2-3", "CẦU 3-3",
        "NGHIÊNG", "NGHIÊNG NHẸ", "XU HƯỚNG", "ĐẢO CHIỀU"
    }

    noise = float(report.get("volatility", 0.0))

    for name, pred in predictions.items():
        label = pred.get("label")
        conf = float(pred.get("confidence", 50))
        acc = float(model_acc.get(name, 50))
        score = conf * (0.8 + acc / 100.0)

        if report.get("structure") in strong_patterns:
            if name == "structure":
                score *= 1.28
            elif name == "pattern":
                score *= 1.18
            elif name == "markov":
                score *= 0.96

        if noise > 0.70:
            score *= 0.92
        elif noise > 0.85:
            score *= 0.85

        model_scores[name] = score
        vote[label] += score

    if not vote:
        return {
            "model": "none",
            "final_label": LOW_LABEL,
            "confidence": 50,
            "scores": {},
        }

    best_label = max(vote, key=vote.get)
    total = sum(vote.values())
    top = vote[best_label]
    top_ratio = top / total if total else 0.5
    agreement = sum(1 for p in predictions.values() if p.get("label") == best_label)
    strongest_conf = max((int(p.get("confidence", 50)) for p in predictions.values()), default=50)

    confidence = int(44 + top_ratio * 42 + (agreement - 1) * 5 + (strongest_conf - 50) * 0.2)

    if report.get("structure") in {"CÂN BẰNG", "TRUNG TÍNH", "CHƯA ĐỦ DỮ LIỆU"}:
        confidence -= 4

    confidence = max(0, min(confidence, 95))
    best_model = max(model_scores, key=model_scores.get)

    return {
        "model": best_model,
        "final_label": best_label,
        "confidence": confidence,
        "scores": dict(vote),
    }


def update_model_accuracy(state: Dict[str, Any], predictions: Dict[str, Dict[str, Any]], actual: str) -> None:
    state.setdefault("model_accuracy", {
        "markov": 50,
        "pattern": 50,
        "structure": 50,
    })

    for name, pred in predictions.items():
        old = int(state["model_accuracy"].get(name, 50))
        if pred.get("label") == actual:
            old += 1
        else:
            old -= 1
        state["model_accuracy"][name] = max(1, min(99, old))


def update_prediction_feedback(state: Dict[str, Any], actual_label: str) -> None:
    pred = state.get("last_prediction_label")
    if pred not in (LOW_LABEL, HIGH_LABEL):
        return

    state["prediction_total"] = int(state.get("prediction_total", 0)) + 1
    if pred == actual_label:
        state["prediction_hits"] = int(state.get("prediction_hits", 0)) + 1
        state["last_prediction_result"] = "ĐÚNG"
    else:
        state["prediction_misses"] = int(state.get("prediction_misses", 0)) + 1
        state["last_prediction_result"] = "SAI"


def analyze_state_from_labels(state: Dict[str, Any], labels: List[str]) -> Dict[str, Any]:
    report = build_report(labels)
    adv = advanced_metrics(labels)

    predictions = {
        "markov": predict_markov(labels),
        "pattern": predict_pattern(labels, report),
        "structure": predict_structure(labels, report),
    }

    meta = meta_decision(predictions, state, report)

    state["last_prediction_label"] = meta["final_label"]
    state["last_prediction_conf"] = meta["confidence"]
    state["last_note"] = f"Model: {meta['model']}"
    state["last_structure"] = report["structure"]
    state["last_mode"] = "READY" if len(labels) >= MIN_ANALYSIS_LEN else "NORMAL"
    state["last_model_predictions"] = predictions

    return {
        "report": report,
        "adv": adv,
        "predictions": predictions,
        "meta": meta,
    }


# =========================
# RENDER
# =========================
def build_stats_message(report: Dict[str, Any], state: Dict[str, Any], adv: Dict[str, Any]) -> str:
    total = report["total"]
    low_p = safe_div(report["low"] * 100.0, total)
    high_p = safe_div(report["high"] * 100.0, total)
    acc = safe_div(state.get("prediction_hits", 0) * 100.0, state.get("prediction_total", 0))

    patterns = report.get("patterns", [])[:3]
    if patterns:
        pattern_lines = "\n".join(f"║ • {p['name']}: {p['detail']}" for p in patterns)
    else:
        pattern_lines = "║ • Chưa có cầu nổi bật"

    model_acc = state.get("model_accuracy", {})
    model_acc_line = (
        f"M:{int(model_acc.get('markov', 50))}% "
        f"P:{int(model_acc.get('pattern', 50))}% "
        f"S:{int(model_acc.get('structure', 50))}%"
    )

    return (
        "╔════════════════════════════╗\n"
        "║      ✅ BẢNG THỐNG KÊ      ║\n"
        "╠════════════════════════════╣\n"
        f"║ Tổng    : {total}\n"
        f"║ {LOW_LABEL:<6}: {report['low']} ({low_p:.1f}%)\n"
        f"║ {HIGH_LABEL:<6}: {report['high']} ({high_p:.1f}%)\n"
        f"║ Cấu trúc: {report['structure']}\n"
        f"║ Chi tiết : {report['detail']}\n"
        f"║ Bệt max  : {adv.get('max_streak', 0)}\n"
        f"║ 10 gần   : {adv.get('r10_high', 0.5) * 100:.1f}% {HIGH_LABEL}\n"
        f"║ 20 gần   : {adv.get('r20_high', 0.5) * 100:.1f}% {HIGH_LABEL}\n"
        f"║ Momentum : {adv.get('momentum', 0.0):.2f}\n"
        f"║ Nhiễu    : {adv.get('noise', 0.0):.2f}\n"
        f"║ Đảo chiều: {adv.get('reversal', 0.0):.1f}%\n"
        f"║ Entropy  : {report.get('entropy', 0.0):.2f}\n"
        f"║ Volatility: {report.get('volatility', 0.0):.2f}\n"
        f"║ Chính xác: {acc:.1f}%\n"
        f"║ M.Acc    : {model_acc_line}\n"
        f"║ Dự đoán  : {state.get('last_prediction_label') or '-'}\n"
        f"║ Tỷ lệ    : {state.get('last_prediction_conf', 0)}%\n"
        f"║ Kết quả  : {state.get('last_prediction_result', 'CHƯA RÕ')}\n"
        f"║ Hit/Miss : {state.get('prediction_hits', 0)}/{state.get('prediction_misses', 0)}\n"
        f"{pattern_lines}\n"
        "╚════════════════════════════╝"
    )


def build_analysis_message(report: Dict[str, Any], adv: Dict[str, Any], meta: Dict[str, Any], predictions: Dict[str, Dict[str, Any]]) -> str:
    warning = ""
    if adv.get("noise", 0.0) > 0.70:
        warning = "⚠️ Cầu nhiễu cao - nên thận trọng"
    elif adv.get("reversal", 0.0) > 25:
        warning = "⚠️ Có khả năng đảo chiều mạnh"

    top_patterns = report.get("patterns", [])[:3]
    pattern_text = " / ".join([p["name"] for p in top_patterns]) if top_patterns else "TRUNG TÍNH"

    return (
        "╔════════════════════════════╗\n"
        "║       🔍 PHÂN TÍCH         ║\n"
        "╠════════════════════════════╣\n"
        f"║ Cầu     : {pattern_text}\n"
        f"║ Model   : {meta.get('model', '-')}\n"
        f"║ Chốt gốc: {meta.get('final_label', '-')}\n"
        f"║ Tỷ lệ   : {meta.get('confidence', 0)}%\n"
        f"║ Markov  : {predictions.get('markov', {}).get('label', '-')} ({predictions.get('markov', {}).get('confidence', 0)}%)\n"
        f"║ Pattern : {predictions.get('pattern', {}).get('label', '-')} ({predictions.get('pattern', {}).get('confidence', 0)}%)\n"
        f"║ Struct  : {predictions.get('structure', {}).get('label', '-')} ({predictions.get('structure', {}).get('confidence', 0)}%)\n"
        f"║ Cấu trúc: {report['structure']}\n"
        f"║ Chi tiết : {report['detail']}\n"
        f"║ Momentum : {adv.get('momentum', 0.0):.2f}\n"
        f"║ Nhiễu    : {adv.get('noise', 0.0):.2f}\n"
        f"║ Đảo chiều: {adv.get('reversal', 0.0):.1f}%\n"
        f"║ Kết luận : {meta.get('final_label', '-')}\n"
        f"{warning}\n"
        "╚════════════════════════════╝"
    )


def build_final_message(meta: Dict[str, Any]) -> str:
    return (
        f"CHỐT GỐC: {meta.get('final_label', '-')}\n"
        f"MODEL   : {meta.get('model', '-')}\n"
        f"TỶ LỆ   : {meta.get('confidence', 0)}%\n"
    )


def build_stage_message(step: int) -> str:
    if step == 1:
        return "✅ Bước 1: Đã cập nhật bảng thống kê."
    if step == 2:
        return "🔍 Bước 2: Đã phân tích bảng thống kê."
    return "🧠 Bước 3: Đã chốt kết quả."


# =========================
# PIPELINE
# =========================
async def process_chat(update: Update, context: ContextTypes.DEFAULT_TYPE, nums: Optional[List[int]] = None) -> None:
    if not update.message:
        return

    chat_id = get_key(update)

    async with STATE_LOCK:
        state = repair_state(await load_state(chat_id))
        entries: List[Tuple[int, str]] = []

        if nums:
            for n in nums:
                label = map_value(n)
                entries.append((n, label))

        if entries:
            await append_history(chat_id, entries)

        rows = await load_history_rows(chat_id, limit=HISTORY_ANALYSIS_LIMIT)
        full_values = [r[0] for r in rows]
        full_labels = [r[1] for r in rows]

        state["values"] = _safe_tail(full_values, RECENT_CACHE)
        state["labels"] = _safe_tail(full_labels, RECENT_CACHE)
        rebuild_counters_from_labels(state, full_labels)

        if entries:
            latest_actual = entries[-1][1]
            update_prediction_feedback(state, latest_actual)

            prev_predictions = state.get("last_model_predictions", {})
            if isinstance(prev_predictions, dict) and prev_predictions:
                update_model_accuracy(state, prev_predictions, latest_actual)

        result = analyze_state_from_labels(state, full_labels)

        await save_state(chat_id, state)
        users[chat_id] = state
        trim_cache()

    report = result["report"]
    adv = result["adv"]
    meta = result["meta"]
    predictions = result["predictions"]

    await update.message.reply_text(build_stage_message(1))
    await update.message.reply_text(build_stats_message(report, state, adv))
    await update.message.reply_text(build_stage_message(2))
    await update.message.reply_text(build_analysis_message(report, adv, meta, predictions))
    await update.message.reply_text(build_final_message(meta))


# =========================
# COMMANDS
# =========================
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    await update.message.reply_text(
        "📘 TRỢ GIÚP\n"
        f"/stats - xem bảng thống kê\n"
        f"/ai - phân tích và chốt\n"
        f"/next - giống /ai\n"
        f"/reset - xóa dữ liệu chat hiện tại\n"
        f"/factory_reset - xóa sạch toàn bộ bot\n\n"
        f"Quy đổi: số >= {THRESHOLD} -> {HIGH_LABEL}, số < {THRESHOLD} -> {LOW_LABEL}.\n"
        "Luồng hoạt động: cập nhật thống kê → phân tích thống kê → chốt cuối.\n"
        "Phần chốt không dùng trend kiểu nghiêng bên nào đoán bên đó."
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = get_key(update)
    state = repair_state(await load_state(chat_id, force_reload=True))
    rows = await load_history_rows(chat_id, limit=HISTORY_ANALYSIS_LIMIT)
    labels = [r[1] for r in rows]
    state["labels"] = _safe_tail(labels, RECENT_CACHE)
    rebuild_counters_from_labels(state, labels)
    report = build_report(labels)
    adv = advanced_metrics(labels)
    await update.message.reply_text(build_stats_message(report, state, adv))


async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = get_key(update)
    async with STATE_LOCK:
        state = repair_state(await load_state(chat_id, force_reload=True))
        rows = await load_history_rows(chat_id, limit=HISTORY_ANALYSIS_LIMIT)
        labels = [r[1] for r in rows]
        state["labels"] = _safe_tail(labels, RECENT_CACHE)
        rebuild_counters_from_labels(state, labels)

        report = build_report(labels)
        adv = advanced_metrics(labels)
        predictions = {
            "markov": predict_markov(labels),
            "pattern": predict_pattern(labels, report),
            "structure": predict_structure(labels, report),
        }
        meta = meta_decision(predictions, state, report)

        state["last_prediction_label"] = meta["final_label"]
        state["last_prediction_conf"] = meta["confidence"]
        state["last_note"] = f"Model: {meta['model']}"
        state["last_structure"] = report["structure"]
        state["last_mode"] = "READY" if len(labels) >= MIN_ANALYSIS_LEN else "NORMAL"
        state["last_model_predictions"] = predictions

        await save_state(chat_id, state)

    await update.message.reply_text(build_stage_message(1))
    await update.message.reply_text(build_stats_message(report, state, adv))
    await update.message.reply_text(build_stage_message(2))
    await update.message.reply_text(build_analysis_message(report, adv, meta, predictions))
    await update.message.reply_text(build_final_message(meta))


async def next_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await ai_cmd(update, context)


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = get_key(update)

    def _work():
        with db_connect() as conn:
            conn.execute("DELETE FROM history WHERE chat_id = ?", (chat_id,))
            conn.execute("DELETE FROM chat_state WHERE chat_id = ?", (chat_id,))
            conn.commit()

    async with DB_LOCK:
        await run_db_work(_work)

    users.pop(chat_id, None)
    await update.message.reply_text("🔄 Đã reset chat hiện tại.")


async def factory_reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    def _work():
        with db_connect() as conn:
            conn.execute("DELETE FROM history")
            conn.execute("DELETE FROM chat_state")
            conn.commit()

    async with DB_LOCK:
        await run_db_work(_work)

    users.clear()
    await update.message.reply_text("🧼 Đã xóa sạch toàn bộ dữ liệu.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not update.message or not update.message.text:
            return
        nums = parse_input(update.message.text)
        if not nums:
            return
        await process_chat(update, context, nums)
    except Exception as e:
        logger.exception("handle_text failed: %s", e)
        if update.message:
            await update.message.reply_text("❌ Lỗi khi xử lý dữ liệu")


async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.exception("Global error: %s", context.error)
    err = context.error
    if isinstance(err, RetryAfter):
        await asyncio.sleep(err.retry_after + 1)
    elif isinstance(err, (TimedOut, NetworkError, TelegramError)):
        await asyncio.sleep(1.0)
    try:
        if getattr(update, "message", None):
            await update.message.reply_text("⚠️ Có lỗi tạm thời, bot đã tự giữ an toàn.")
    except Exception:
        pass


# =========================
# MAIN
# =========================
def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).concurrent_updates(False).build()
    app.add_error_handler(global_error_handler)

    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("ai", ai_cmd))
    app.add_handler(CommandHandler("next", next_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("factory_reset", factory_reset_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("🔥 BOT THỐNG KÊ - PHÂN TÍCH - CHỐT ĐANG CHẠY...")
    app.run_polling(drop_pending_updates=True)


def run_bot_forever():
    while True:
        try:
            main()
            break
        except KeyboardInterrupt:
            raise
        except Exception as e:
            logger.exception("Bot crashed, restarting in 5s: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    run_bot_forever()
