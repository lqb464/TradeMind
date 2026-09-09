"""Thread-safe tracing, throttling, and durable per-user AI budgets."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator


@dataclass
class TraceEvent:
    name: str
    detail: str
    duration_ms: float


@dataclass
class RequestTrace:
    request_id: str = field(default_factory=lambda: str(uuid.uuid4())[:12])
    started_at: float = field(default_factory=time.time)
    events: list[TraceEvent] = field(default_factory=list)
    estimated_cost_usd: float = 0.0

    def add(self, name: str, detail: str = "", duration_ms: float = 0.0) -> None:
        self.events.append(TraceEvent(name, detail, duration_ms))

    def summary(self) -> dict:
        return {
            "request_id": self.request_id,
            "duration_ms": round((time.time() - self.started_at) * 1000, 2),
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "events": [
                {
                    "name": event.name,
                    "detail": event.detail,
                    "duration_ms": round(event.duration_ms, 2),
                }
                for event in self.events
            ],
        }


@contextmanager
def timed(trace: RequestTrace, name: str, detail: str = "") -> Iterator[None]:
    started = time.perf_counter()
    try:
        yield
    finally:
        trace.add(name, detail, (time.perf_counter() - started) * 1000)


class TraceStore:
    def __init__(self, size: int = 100):
        self._items: deque[dict] = deque(maxlen=size)
        self._lock = threading.Lock()

    def add(self, trace: RequestTrace) -> None:
        with self._lock:
            self._items.appendleft(trace.summary())

    def recent(self, limit: int = 20) -> list[dict]:
        with self._lock:
            return list(self._items)[:limit]


class SlidingWindowLimiter:
    def __init__(self, max_calls: int = 30, seconds: float = 60, max_keys: int = 10_000):
        self.max_calls = max_calls
        self.seconds = seconds
        self.max_keys = max_keys
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            queue = self._hits.get(key)
            if queue is None:
                if len(self._hits) >= self.max_keys:
                    oldest = min(
                        self._hits,
                        key=lambda item: self._hits[item][-1] if self._hits[item] else -1,
                    )
                    self._hits.pop(oldest, None)
                queue = self._hits[key] = deque()
            while queue and now - queue[0] > self.seconds:
                queue.popleft()
            if len(queue) >= self.max_calls:
                return False
            queue.append(now)
            return True


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _ensure_usage_schema(db: sqlite3.Connection) -> None:
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_usage(
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL DEFAULT '',
            endpoint TEXT NOT NULL,
            model TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            total_tokens INTEGER NOT NULL DEFAULT 0,
            estimated_cost REAL NOT NULL DEFAULT 0,
            trace_id TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    columns = {row[1] for row in db.execute("PRAGMA table_info(ai_usage)")}
    if "user_id" not in columns:
        db.execute("ALTER TABLE ai_usage ADD COLUMN user_id TEXT NOT NULL DEFAULT ''")
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_budget_reservations(
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            reserved_tokens INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_ai_usage_user_created ON ai_usage(user_id,created_at)"
    )


def reserve_ai_budget(db_path: Path, user_id: str) -> str | None:
    """Atomically reserve one bounded call, returning ``None`` when quota is exhausted."""

    max_daily_tokens = _bounded_env_int(
        "MAX_AI_TOKENS_PER_USER_PER_DAY", 200_000, 1_000, 10_000_000
    )
    max_daily_calls = _bounded_env_int("MAX_AI_CALLS_PER_USER_PER_DAY", 100, 1, 10_000)
    input_chars = _bounded_env_int("LLM_MAX_INPUT_CHARS", 60_000, 2_000, 200_000)
    output_tokens = _bounded_env_int("LLM_MAX_OUTPUT_TOKENS", 1_200, 64, 4_096)
    reservation_tokens = input_chars // 4 + output_tokens
    reservation_id = str(uuid.uuid4())
    now = int(time.time())
    with sqlite3.connect(db_path, timeout=10, isolation_level=None) as db:
        _ensure_usage_schema(db)
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM ai_budget_reservations WHERE created_at < ?", (now - 900,))
        used = db.execute(
            """
            SELECT COUNT(*),COALESCE(SUM(total_tokens),0) FROM ai_usage
            WHERE user_id=? AND date(created_at)=date('now')
            """,
            (user_id,),
        ).fetchone()
        reserved = db.execute(
            """
            SELECT COUNT(*),COALESCE(SUM(reserved_tokens),0)
            FROM ai_budget_reservations WHERE user_id=?
            """,
            (user_id,),
        ).fetchone()
        if (
            used[0] + reserved[0] >= max_daily_calls
            or used[1] + reserved[1] + reservation_tokens > max_daily_tokens
        ):
            db.rollback()
            return None
        db.execute(
            "INSERT INTO ai_budget_reservations(id,user_id,reserved_tokens,created_at) VALUES (?,?,?,?)",
            (reservation_id, user_id, reservation_tokens, now),
        )
        db.commit()
    return reservation_id


def release_ai_reservation(db_path: Path, reservation_id: str | None) -> None:
    if not reservation_id:
        return
    with sqlite3.connect(db_path) as db:
        _ensure_usage_schema(db)
        db.execute("DELETE FROM ai_budget_reservations WHERE id=?", (reservation_id,))


def finalize_ai_reservation(
    db_path: Path,
    reservation_id: str,
    *,
    endpoint: str,
    model: str = "interrupted-stream",
    trace_id: str | None = None,
) -> bool:
    """Consume an interrupted, already-started AI reservation conservatively.

    Once provider work has begun, a client disconnect must not turn a billable
    attempt into a free retry.  The reserved token amount is charged atomically;
    a completed stream still records its provider-reported usage instead.
    """

    with sqlite3.connect(db_path, timeout=10, isolation_level=None) as db:
        _ensure_usage_schema(db)
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            "SELECT user_id,reserved_tokens FROM ai_budget_reservations WHERE id=?",
            (reservation_id,),
        ).fetchone()
        if row is None:
            db.rollback()
            return False
        db.execute(
            """
            INSERT INTO ai_usage(
                id,user_id,endpoint,model,input_tokens,output_tokens,total_tokens,
                estimated_cost,trace_id
            ) VALUES (?,?,?,?,?,0,?,0,?)
            """,
            (
                str(uuid.uuid4()),
                str(row[0]),
                endpoint,
                model,
                int(row[1]),
                int(row[1]),
                trace_id or f"interrupted-{str(uuid.uuid4())[:12]}",
            ),
        )
        db.execute("DELETE FROM ai_budget_reservations WHERE id=?", (reservation_id,))
        db.commit()
    return True


def log_ai_usage(
    db_path: Path,
    *,
    endpoint: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    estimated_cost: float,
    trace_id: str,
    user_id: str = "",
    reservation_id: str | None = None,
) -> None:
    with sqlite3.connect(db_path) as db:
        _ensure_usage_schema(db)
        db.execute(
            """
            INSERT INTO ai_usage(
                id,user_id,endpoint,model,input_tokens,output_tokens,total_tokens,
                estimated_cost,trace_id
            ) VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                str(uuid.uuid4()),
                user_id,
                endpoint,
                model,
                input_tokens,
                output_tokens,
                input_tokens + output_tokens,
                estimated_cost,
                trace_id,
            ),
        )
        if reservation_id:
            db.execute("DELETE FROM ai_budget_reservations WHERE id=?", (reservation_id,))


def usage_summary(db_path: Path) -> dict:
    with sqlite3.connect(db_path) as db:
        _ensure_usage_schema(db)
        row = db.execute(
            """
            SELECT COUNT(*),COALESCE(SUM(input_tokens),0),
                   COALESCE(SUM(output_tokens),0),COALESCE(SUM(estimated_cost),0)
            FROM ai_usage
            """
        ).fetchone()
        by_model = db.execute(
            """
            SELECT model,COUNT(*),SUM(total_tokens),SUM(estimated_cost)
            FROM ai_usage GROUP BY model ORDER BY SUM(estimated_cost) DESC
            """
        ).fetchall()
    return {
        "calls": row[0],
        "input_tokens": row[1],
        "output_tokens": row[2],
        "estimated_cost_usd": round(row[3], 6),
        "by_model": [
            {
                "model": item[0],
                "calls": item[1],
                "tokens": item[2],
                "cost_usd": round(item[3], 6),
            }
            for item in by_model
        ],
    }


trace_store = TraceStore()
ai_limiter = SlidingWindowLimiter()
auth_limiter = SlidingWindowLimiter(max_calls=10, seconds=300)
public_limiter = SlidingWindowLimiter(max_calls=120, seconds=60)
upload_limiter = SlidingWindowLimiter(max_calls=10, seconds=3600)
