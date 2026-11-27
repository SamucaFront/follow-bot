"""Decide o que está na hora de fazer em cada execução e controla limites diários."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from config import LimitsConfig, SuggestionConfig
from storage import from_iso, to_iso

# Folga para o cron do GitHub, que às vezes dispara alguns minutos antes ou depois.
INTERVAL_TOLERANCE = timedelta(minutes=10)


def today(now: datetime) -> str:
    """Data em UTC, que é o fuso do cron do GitHub Actions."""
    return now.date().isoformat()


def is_run_due(state: dict[str, Any], limits: LimitsConfig, now: datetime, manual: bool) -> bool:
    if manual or not state.get("last_run_at"):
        return True
    elapsed = now - from_iso(state["last_run_at"])
    return elapsed >= timedelta(hours=limits.run_interval_hours) - INTERVAL_TOLERANCE


def mark_run(state: dict[str, Any], now: datetime) -> None:
    state["last_run_at"] = to_iso(now)


def is_suggestion_due(state: dict[str, Any], cfg: SuggestionConfig, now: datetime) -> bool:
    return state.get("last_suggestion_date") != today(now) and now.hour >= cfg.hour_utc


def is_review_due(state: dict[str, Any], now: datetime) -> bool:
    return state.get("pending_review") is None and state.get("last_review_date") != today(now)


def operations_left(state: dict[str, Any], limits: LimitsConfig, now: datetime) -> int:
    ops = state.setdefault("daily_ops", {"date": None, "count": 0})
    if ops.get("date") != today(now):
        ops["date"] = today(now)
        ops["count"] = 0
    return max(0, limits.max_operations_per_day - int(ops["count"]))


def record_operation(state: dict[str, Any], now: datetime, count: int = 1) -> None:
    ops = state.setdefault("daily_ops", {"date": None, "count": 0})
    if ops.get("date") != today(now):
        ops["date"] = today(now)
        ops["count"] = 0
    ops["count"] = int(ops["count"]) + count
