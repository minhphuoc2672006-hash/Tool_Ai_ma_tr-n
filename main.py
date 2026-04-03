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
        "model_accuracy": {"trend": 50, "markov": 50, "pattern": 50},
        "last_note": "",
        "last_structure": "CHƯA ĐỦ DỮ LIỆU",
        "last_mode": "NORMAL",
    }


def _safe_tail(seq: List[Any], limit: int) -> List[Any]:
    return list(seq[-limit:]) if len(seq) > limit else list(seq)


def trim_state_memory(d: Dict[str, Any]) -> None:
    d["values"] = _safe_tail(d.get("values", []), RECENT_CACHE)
    d["labels"] = _safe_tail(d.get("labels", []), RECENT_CACHE)


def rebuild_counters(d: Dict[str, Any]) -> None:
    labels = d.get("labels", [])
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
        d["model_accuracy"] = {"trend": 50, "markov": 50, "pattern": 50}

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

    n = min(len(d["values"]), len(d["labels"]))
    d["values"] = d["values"][-n:] if n else []
    d["labels"] = d["labels"][-n:] if n else []

    trim_state_memory(d)
    rebuild_counters(d)
    return d


def trim_cache() -> None:
    if len(users) <= USER_CACHE_LIMIT:
        return
    overflow = len(users) - USER_CACHE_LIMIT
    for chat_id in list(users.keys())[:overflow]:
        users.pop(chat_id, None)


def map_value(n: int) -> str:
    return HIGH_LABEL if n >= THRESHOLD else LOW_LABEL


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


# =========================
# LOAD / SAVE
# =========================

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


def build_report(labels: List[str]) -> Dict[str, Any]:
    c = Counter(labels)
    last, streak = current_streak(labels)
    alt, alt_ratio = alternating_tail(labels, 6)
    r6 = recent_ratio(labels, 6)
    r12 = recent_ratio(labels, 12)
    r24 = recent_ratio(labels, 24)

    if len(labels) < 6:
        structure = "CHƯA ĐỦ DỮ LIỆU"
        detail = "Cần thêm kết quả"
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


def predict_trend(labels: List[str]) -> Dict[str, Any]:
    tail = labels[-10:] if len(labels) > 10 else labels[:]
    if not tail:
        return {"label": LOW_LABEL, "confidence": 50}
    c = Counter(tail)
    if c[HIGH_LABEL] > c[LOW_LABEL]:
        return {"label": HIGH_LABEL, "confidence": 60}
    if c[LOW_LABEL] > c[HIGH_LABEL]:
        return {"label": LOW_LABEL, "confidence": 60}
    return {"label": tail[-1], "confidence": 52}


def predict_markov(labels: List[str]) -> Dict[str, Any]:
    if len(labels) < 2:
        return {"label": labels[-1] if labels else LOW_LABEL, "confidence": 50}

    transitions = defaultdict(Counter)
    for i in range(len(labels) - 1):
        transitions[labels[i]][labels[i + 1]] += 1

    last = labels[-1]
    next_probs = transitions.get(last, Counter())
    if not next_probs:
        return {"label": last, "confidence": 50}

    best = max(next_probs, key=next_probs.get)
    total = sum(next_probs.values())
    conf = 55 + int((next_probs[best] / total) * 30)
    return {"label": best, "confidence": conf}


def predict_pattern(labels: List[str]) -> Dict[str, Any]:
    if len(labels) < 4:
        return {"label": labels[-1] if labels else LOW_LABEL, "confidence": 50}

    if labels[-1] == labels[-2] == labels[-3]:
        return {"label": labels[-1], "confidence": 66}

    if labels[-1] != labels[-2]:
        return {"label": labels[-2], "confidence": 56}

    return {"label": labels[-1], "confidence": 54}


def meta_decision(predictions: Dict[str, Dict[str, Any]], state: Dict[str, Any]) -> Dict[str, Any]:
    scores = {}
    model_acc = state.get("model_accuracy", {})

    for name, pred in predictions.items():
        acc = int(model_acc.get(name, 50))
        conf = int(pred.get("confidence", 50))
        scores[name] = acc * conf

    best_model = max(scores, key=scores.get)
    final = predictions[best_model]
    return {
        "model": best_model,
        "final_label": final["label"],
        "confidence": int(final["confidence"]),
        "scores": scores,
    }


def update_model_accuracy(state: Dict[str, Any], predictions: Dict[str, Dict[str, Any]], actual: str) -> None:
    state.setdefault("model_accuracy", {"trend": 50, "markov": 50, "pattern": 50})
    for name, pred in predictions.items():
        old = int(state["model_accuracy"].get(name, 50))
        if pred.get("label") == actual:
            old += 1
        else:
            old -= 1
        state["model_accuracy"][name] = max(1, min(99, old))


def process_new_result(state: Dict[str, Any], actual_label: Optional[str] = None) -> Dict[str, Any]:
    labels = state.get("labels", [])
    rebuild_counters(state)

    report = build_report(labels)
    adv = advanced_metrics(labels)

    predictions = {
        "trend": predict_trend(labels),
        "markov": predict_markov(labels),
        "pattern": predict_pattern(labels),
    }

    meta = meta_decision(predictions, state)

    if actual_label in (LOW_LABEL, HIGH_LABEL) and len(labels) >= 1:
        update_model_accuracy(state, predictions, actual_label)

    state["last_prediction_label"] = meta["final_label"]
    state["last_prediction_conf"] = meta["confidence"]
    state["last_note"] = f"Model: {meta['model']}"
    state["last_structure"] = report["structure"]
    state["last_mode"] = "READY" if len(labels) >= MIN_ANALYSIS_LEN else "NORMAL"

    return {
        "report": report,
        "adv": adv,
        "predictions": predictions,
        "meta": meta,
    }


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


# =========================
# RENDER
# =========================

def fmt_pct(r: Dict[str, float]) -> str:
    return f"{LOW_LABEL}: {r[LOW_LABEL]*100:.1f}% | {HIGH_LABEL}: {r[HIGH_LABEL]*100:.1f}%"


def build_stats_message(report: Dict[str, Any], state: Dict[str, Any], adv: Dict[str, Any]) -> str:
    total = report["total"]
    low_p = safe_div(report["low"] * 100.0, total)
    high_p = safe_div(report["high"] * 100.0, total)
    acc = safe_div(state.get("prediction_hits", 0) * 100.0, state.get("prediction_total", 0))

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
        f"║ 10 gần   : {adv.get('r10_high', 0.5)*100:.1f}% {HIGH_LABEL}\n"
        f"║ 20 gần   : {adv.get('r20_high', 0.5)*100:.1f}% {HIGH_LABEL}\n"
        f"║ Momentum : {adv.get('momentum', 0.0):.2f}\n"
        f"║ Nhiễu    : {adv.get('noise', 0.0):.2f}\n"
        f"║ Đảo chiều: {adv.get('reversal', 0.0):.1f}%\n"
        f"║ Chính xác: {acc:.1f}%\n"
        f"║ Dự đoán  : {state.get('last_prediction_label') or '-'}\n"
        f"║ Tỷ lệ    : {state.get('last_prediction_conf', 0)}%\n"
        f"║ Kết quả  : {state.get('last_prediction_result', 'CHƯA RÕ')}\n"
        f"║ Hit/Miss : {state.get('prediction_hits', 0)}/{state.get('prediction_misses', 0)}\n"
        "╚════════════════════════════╝"
    )


def build_analysis_message(report: Dict[str, Any], adv: Dict[str, Any], meta: Dict[str, Any]) -> str:
    warning = ""
    if adv.get("noise", 0.0) > 0.70:
        warning = "⚠️ Cầu nhiễu cao - nên thận trọng"
    elif adv.get("reversal", 0.0) > 25:
        warning = "⚠️ Có khả năng đảo chiều mạnh"

    return (
        "╔════════════════════════════╗\n"
        "║       🔍 PHÂN TÍCH         ║\n"
        "╠════════════════════════════╣\n"
        f"║ Model   : {meta.get('model', '-')}\n"
        f"║ Chốt gốc: {meta.get('final_label', '-')}\n"
        f"║ Tỷ lệ   : {meta.get('confidence', 0)}%\n"
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

        actual_label: Optional[str] = None
        if nums:
            for n in nums:
                actual_label = map_value(n)
                update_prediction_feedback(state, actual_label)
                state["values"].append(n)
                state["labels"].append(actual_label)
                entries.append((n, actual_label))

        state = repair_state(state)
        await append_history(chat_id, entries)

        result = process_new_result(state, actual_label if actual_label in (LOW_LABEL, HIGH_LABEL) else None)
        report = result["report"]
        adv = result["adv"]
        meta = result["meta"]

        await save_state(chat_id, state)
        users[chat_id] = state
        trim_cache()

    await update.message.reply_text(build_stage_message(1))
    await update.message.reply_text(build_stats_message(report, state, adv))
    await update.message.reply_text(build_stage_message(2))
    await update.message.reply_text(build_analysis_message(report, adv, meta))
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
        "Luồng hoạt động: cập nhật thống kê → phân tích thống kê → chốt cuối."
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = get_key(update)
    state = repair_state(await load_state(chat_id, force_reload=True))
    report = build_report(state.get("labels", []))
    adv = advanced_metrics(state.get("labels", []))
    await update.message.reply_text(build_stats_message(report, state, adv))


async def ai_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    chat_id = get_key(update)
    async with STATE_LOCK:
        state = repair_state(await load_state(chat_id, force_reload=True))
        labels = state.get("labels", [])
        report = build_report(labels)
        adv = advanced_metrics(labels)
        predictions = {
            "trend": predict_trend(labels),
            "markov": predict_markov(labels),
            "pattern": predict_pattern(labels),
        }
        meta = meta_decision(predictions, state)
        state["last_prediction_label"] = meta["final_label"]
        state["last_prediction_conf"] = meta["confidence"]
        state["last_note"] = f"Model: {meta['model']}"
        state["last_structure"] = report["structure"]
        state["last_mode"] = "READY" if len(labels) >= MIN_ANALYSIS_LEN else "NORMAL"
        await save_state(chat_id, state)

    await update.message.reply_text(build_stage_message(1))
    await update.message.reply_text(build_stats_message(report, state, adv))
    await update.message.reply_text(build_stage_message(2))
    await update.message.reply_text(build_analysis_message(report, adv, meta))
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
