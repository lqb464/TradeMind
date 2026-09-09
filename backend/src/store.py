"""Transactional SQLite persistence for authentication and paper trading.

This module is intentionally broker-free.  The only order it can create is a
filled PAPER order produced after an explicit human approval transaction.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from backend.core.security import token_fingerprint
from backend.src.market_quality import assess_market_snapshot


LOCAL_USER_ID = "00000000-0000-0000-0000-000000000000"
_TICKER_RE = re.compile(r"^[A-Z0-9.\-^]{1,24}$")
_STATUS_VALUES = {"PENDING", "APPROVED", "REJECTED", "EXPIRED"}
_DEMO_MARKERS = ("demo", "synthetic", "fallback", "mock", "sample", "offline")


class StoreError(RuntimeError):
    """Base class for domain-safe persistence errors."""


class NotFoundError(StoreError):
    pass


class ConflictError(StoreError):
    pass


class ValidationError(StoreError):
    pass


class AccessDeniedError(StoreError):
    pass


class UnsafeMarketDataError(StoreError):
    pass


@dataclass(frozen=True)
class UserRecord:
    id: str
    email: str
    password_hash: str
    role: str
    is_active: bool
    is_local: bool
    created_at: int
    display_name: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "email": self.email,
            "name": self.display_name,
            "role": self.role,
            "is_active": self.is_active,
            "is_local": self.is_local,
            "created_at": _iso(self.created_at),
        }


def _now() -> int:
    return int(time.time())


def _iso(timestamp: int | float | None) -> str | None:
    if timestamp is None:
        return None
    return datetime.fromtimestamp(float(timestamp), tz=timezone.utc).isoformat()


def _utc_day(timestamp: int | None = None) -> str:
    return datetime.fromtimestamp(timestamp or _now(), tz=timezone.utc).date().isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str)


def _loads(value: str | None, fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return fallback


def _finite(value: Any, field: str, *, minimum: float | None = None) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be numeric") from exc
    if not math.isfinite(number):
        raise ValidationError(f"{field} must be finite")
    if minimum is not None and number < minimum:
        raise ValidationError(f"{field} must be at least {minimum}")
    return number


def _positive_int_env(name: str, default: int, *, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError as exc:
        raise ValidationError(f"{name} must be an integer") from exc
    if value <= 0 or value > maximum:
        raise ValidationError(f"{name} must be between 1 and {maximum}")
    return value


def _price_deviation_limit() -> float:
    raw = os.getenv("MAX_PRICE_DEVIATION_PCT", "0.05")
    value = _finite(raw, "MAX_PRICE_DEVIATION_PCT", minimum=0)
    if value > 1:
        value /= 100
    if value > 1:
        raise ValidationError("MAX_PRICE_DEVIATION_PCT cannot exceed 100%")
    return value


def _market_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("Market snapshot meta.as_of is required")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("Market snapshot meta.as_of is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError("Market snapshot meta.as_of must include a timezone")
    return parsed.astimezone(timezone.utc)


def _policy_fraction(policy: Mapping[str, Any], key: str, maximum: float) -> float:
    if key not in policy:
        raise ConflictError("Proposal is missing its immutable risk policy")
    value = _finite(policy[key], key, minimum=0.000001)
    if value > maximum:
        raise ValidationError(f"{key} cannot exceed {maximum}")
    return value


def _validated_execution_snapshot(
    snapshot: Mapping[str, Any], expected_ticker: str, expected_currency: str
) -> tuple[float, str, datetime, str]:
    try:
        quality = assess_market_snapshot(snapshot)
    except (TypeError, ValueError) as exc:
        raise ValidationError("Market snapshot failed canonical validation") from exc
    if not quality["execution_eligible"]:
        codes = ", ".join(item["code"] for item in quality["errors"][:5])
        raise UnsafeMarketDataError(
            f"Market snapshot is not execution-quality ({codes or 'quality gate failed'})"
        )
    canonical = quality["canonical"]
    ticker = _ticker(str(canonical.get("ticker") or ""))
    if ticker != expected_ticker:
        raise ValidationError("Market snapshot ticker does not match proposal")
    source = str(canonical.get("source") or "").strip()
    if not source:
        raise ValidationError("Market snapshot provenance is required")
    as_of = _market_timestamp(canonical.get("as_of"))
    currency = str(canonical.get("currency") or "").strip().upper()
    if currency != expected_currency:
        raise UnsafeMarketDataError(
            f"Quote currency {currency or 'unknown'} does not match account currency {expected_currency}"
        )
    price = _finite(canonical.get("price"), "market price", minimum=0.000001)
    return price, source, as_of, currency


def _ticker(value: str) -> str:
    normalized = (value or "").strip().upper()
    if not _TICKER_RE.fullmatch(normalized):
        raise ValidationError("Invalid ticker")
    return normalized


def default_db_path() -> Path:
    configured = os.getenv("TRADEMIND_DB_PATH")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "data" / "trademind.db"


_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS users(
            id TEXT PRIMARY KEY,
            email TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('USER','ADMIN')),
            is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0,1)),
            is_local INTEGER NOT NULL DEFAULT 0 CHECK(is_local IN (0,1)),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS refresh_tokens(
            token_hash TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            revoked_at INTEGER,
            replaced_by_hash TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS user_watchlists(
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY(user_id, ticker)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS paper_accounts(
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            currency TEXT NOT NULL DEFAULT 'USD',
            mode TEXT NOT NULL DEFAULT 'PAPER' CHECK(mode = 'PAPER'),
            initial_cash REAL NOT NULL CHECK(initial_cash >= 0),
            cash_balance REAL NOT NULL CHECK(cash_balance >= 0),
            realized_pnl REAL NOT NULL DEFAULT 0,
            daily_pnl REAL NOT NULL DEFAULT 0,
            trading_enabled INTEGER NOT NULL DEFAULT 1 CHECK(trading_enabled IN (0,1)),
            kill_switch INTEGER NOT NULL DEFAULT 0 CHECK(kill_switch IN (0,1)),
            status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK(status IN ('ACTIVE','CLOSED')),
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            UNIQUE(user_id, name)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS paper_positions(
            account_id TEXT NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            quantity REAL NOT NULL CHECK(quantity > 0),
            average_cost REAL NOT NULL CHECK(average_cost > 0),
            realized_pnl REAL NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY(account_id, ticker)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS trade_proposals(
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            account_id TEXT NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('BUY','SELL','HOLD')),
            quantity REAL NOT NULL CHECK(quantity >= 0),
            reference_price REAL NOT NULL CHECK(reference_price >= 0),
            stop_loss REAL,
            take_profit REAL,
            estimated_risk REAL NOT NULL DEFAULT 0,
            score REAL,
            confidence REAL,
            executable INTEGER NOT NULL DEFAULT 0 CHECK(executable IN (0,1)),
            vetoes_json TEXT NOT NULL DEFAULT '[]',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            decision_json TEXT NOT NULL DEFAULT '{}',
            rationale TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'PENDING'
                CHECK(status IN ('PENDING','APPROVED','REJECTED','EXPIRED')),
            expires_at INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            decided_at INTEGER,
            decision_note TEXT NOT NULL DEFAULT '',
            approved_order_id TEXT
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS paper_orders(
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
            proposal_id TEXT NOT NULL UNIQUE REFERENCES trade_proposals(id) ON DELETE RESTRICT,
            ticker TEXT NOT NULL,
            side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
            quantity REAL NOT NULL CHECK(quantity > 0),
            order_type TEXT NOT NULL DEFAULT 'MARKET' CHECK(order_type = 'MARKET'),
            fill_price REAL NOT NULL CHECK(fill_price > 0),
            notional REAL NOT NULL CHECK(notional > 0),
            status TEXT NOT NULL DEFAULT 'FILLED' CHECK(status = 'FILLED'),
            idempotency_key TEXT NOT NULL,
            fill_source TEXT NOT NULL,
            snapshot_as_of TEXT,
            created_at INTEGER NOT NULL,
            UNIQUE(account_id, idempotency_key)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS agent_runs(
            id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            account_id TEXT REFERENCES paper_accounts(id) ON DELETE SET NULL,
            proposal_id TEXT REFERENCES trade_proposals(id) ON DELETE SET NULL,
            ticker TEXT,
            agent_name TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('SUCCEEDED','FAILED')),
            input_json TEXT NOT NULL DEFAULT '{}',
            output_json TEXT NOT NULL DEFAULT '{}',
            error TEXT NOT NULL DEFAULT '',
            trace_id TEXT,
            created_at INTEGER NOT NULL,
            completed_at INTEGER NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_refresh_user_active ON refresh_tokens(user_id, revoked_at, expires_at)",
        "CREATE INDEX IF NOT EXISTS idx_accounts_user ON paper_accounts(user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS idx_proposals_user_status ON trade_proposals(user_id, status, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_orders_account_created ON paper_orders(account_id, created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_runs_user_created ON agent_runs(user_id, created_at DESC)",
    ),
    2: (
        """
        CREATE TABLE IF NOT EXISTS account_control_events(
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
            user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            old_trading_enabled INTEGER NOT NULL CHECK(old_trading_enabled IN (0,1)),
            new_trading_enabled INTEGER NOT NULL CHECK(new_trading_enabled IN (0,1)),
            old_kill_switch INTEGER NOT NULL CHECK(old_kill_switch IN (0,1)),
            new_kill_switch INTEGER NOT NULL CHECK(new_kill_switch IN (0,1)),
            reason TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_control_events_account_created ON account_control_events(account_id, created_at DESC)",
    ),
    3: (
        "ALTER TABLE trade_proposals ADD COLUMN decision_id TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE trade_proposals ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_proposal_idempotency ON trade_proposals(account_id, idempotency_key) WHERE idempotency_key <> ''",
    ),
    4: (
        "ALTER TABLE trade_proposals ADD COLUMN quote_currency TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE paper_orders ADD COLUMN quote_currency TEXT NOT NULL DEFAULT ''",
    ),
    5: (
        """
        CREATE TABLE IF NOT EXISTS account_daily_risk(
            account_id TEXT NOT NULL REFERENCES paper_accounts(id) ON DELETE CASCADE,
            trading_day TEXT NOT NULL,
            opening_equity REAL NOT NULL CHECK(opening_equity >= 0),
            latest_equity REAL NOT NULL CHECK(latest_equity >= 0),
            daily_pnl REAL NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY(account_id, trading_day)
        )
        """,
    ),
    6: (
        "ALTER TABLE users ADD COLUMN display_name TEXT NOT NULL DEFAULT ''",
    ),
    7: (
        """
        UPDATE trade_proposals SET
          status='EXPIRED',
          decided_at=COALESCE(decided_at, CAST(strftime('%s','now') AS INTEGER)),
          decision_note=CASE
            WHEN decision_note='' THEN 'Expired by decision-binding safety migration'
            ELSE decision_note
          END
        WHERE status='PENDING' AND (
          decision_id='' OR idempotency_key='' OR quote_currency=''
        )
        """,
    ),
}


class SQLiteStore:
    def __init__(self, path: str | Path | None = None, *, initialize: bool = True):
        self.path = Path(path).expanduser().resolve() if path is not None else default_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if initialize:
            self.initialize()

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        return db

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self) -> None:
        with self._connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=NORMAL")
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS trademind_schema_migrations(
                    version INTEGER PRIMARY KEY,
                    applied_at INTEGER NOT NULL
                )
                """
            )
        for version, statements in sorted(_MIGRATIONS.items()):
            with self.transaction() as db:
                applied = db.execute(
                    "SELECT 1 FROM trademind_schema_migrations WHERE version = ?", (version,)
                ).fetchone()
                if applied:
                    continue
                for statement in statements:
                    db.execute(statement)
                db.execute(
                    "INSERT INTO trademind_schema_migrations(version, applied_at) VALUES (?, ?)",
                    (version, _now()),
                )

    @staticmethod
    def _user(row: sqlite3.Row | None) -> UserRecord | None:
        if row is None:
            return None
        return UserRecord(
            id=row["id"],
            email=row["email"],
            password_hash=row["password_hash"],
            role=row["role"],
            is_active=bool(row["is_active"]),
            is_local=bool(row["is_local"]),
            created_at=int(row["created_at"]),
            display_name=str(row["display_name"] or ""),
        )

    def human_user_count(self) -> int:
        with self._connect() as db:
            row = db.execute("SELECT COUNT(*) AS count FROM users WHERE is_local = 0").fetchone()
        return int(row["count"])

    def create_user(
        self,
        email: str,
        password_hash: str,
        *,
        display_name: str = "",
        required_first_email: str | None = None,
    ) -> UserRecord:
        normalized = email.strip().lower()
        clean_name = (display_name or "").strip()
        if not normalized or not password_hash:
            raise ValidationError("Email and password hash are required")
        if len(clean_name) > 80:
            raise ValidationError("Display name must contain at most 80 characters")
        user_id, account_id, now = str(uuid.uuid4()), str(uuid.uuid4()), _now()
        initial_cash = _finite(os.getenv("PAPER_INITIAL_CASH", "100000"), "PAPER_INITIAL_CASH", minimum=0)
        try:
            with self.transaction() as db:
                first = db.execute(
                    "SELECT COUNT(*) AS count FROM users WHERE is_local = 0"
                ).fetchone()["count"] == 0
                if first and required_first_email and normalized != required_first_email.strip().lower():
                    raise AccessDeniedError("Initial administrator email is not authorized")
                role = "ADMIN" if first else "USER"
                db.execute(
                    """
                    INSERT INTO users(
                        id,email,password_hash,role,is_active,is_local,created_at,updated_at,display_name
                    ) VALUES (?,?,?,?,1,0,?,?,?)
                    """,
                    (user_id, normalized, password_hash, role, now, now, clean_name),
                )
                db.execute(
                    """
                    INSERT INTO paper_accounts(
                        id,user_id,name,currency,mode,initial_cash,cash_balance,created_at,updated_at
                    ) VALUES (?,?,?,'USD','PAPER',?,?,?,?)
                    """,
                    (account_id, user_id, "Primary Paper", initial_cash, initial_cash, now, now),
                )
        except sqlite3.IntegrityError as exc:
            if "email" in str(exc).lower() or "unique" in str(exc).lower():
                raise ConflictError("Email is already registered") from exc
            raise StoreError("Could not create user") from exc
        user = self.get_user(user_id)
        assert user is not None
        return user

    def ensure_local_user(self) -> UserRecord:
        now = _now()
        with self.transaction() as db:
            db.execute(
                """
                INSERT OR IGNORE INTO users(
                    id,email,password_hash,role,is_active,is_local,created_at,updated_at,display_name
                ) VALUES (?,?,?,'ADMIN',1,1,?,?,?)
                """,
                (
                    LOCAL_USER_ID,
                    "local@trademind.invalid",
                    "!local-login-disabled",
                    now,
                    now,
                    "Local Trader",
                ),
            )
            account = db.execute(
                "SELECT id FROM paper_accounts WHERE user_id = ? ORDER BY created_at LIMIT 1",
                (LOCAL_USER_ID,),
            ).fetchone()
            if account is None:
                initial_cash = _finite(
                    os.getenv("PAPER_INITIAL_CASH", "100000"),
                    "PAPER_INITIAL_CASH",
                    minimum=0,
                )
                db.execute(
                    """
                    INSERT INTO paper_accounts(
                        id,user_id,name,currency,mode,initial_cash,cash_balance,created_at,updated_at
                    ) VALUES (?,?,?,'USD','PAPER',?,?,?,?)
                    """,
                    (str(uuid.uuid4()), LOCAL_USER_ID, "Local Paper", initial_cash, initial_cash, now, now),
                )
        user = self.get_user(LOCAL_USER_ID)
        assert user is not None
        return user

    def get_user(self, user_id: str) -> UserRecord | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return self._user(row)

    def get_user_by_email(self, email: str) -> UserRecord | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email.strip().lower(),)
            ).fetchone()
        return self._user(row)

    def register_refresh_token(self, user_id: str, token: str, expires_at: int) -> None:
        now = _now()
        if expires_at <= now:
            raise ValidationError("Refresh token is already expired")
        with self.transaction() as db:
            db.execute(
                "DELETE FROM refresh_tokens WHERE expires_at < ?", (now - 24 * 60 * 60,)
            )
            db.execute(
                """
                INSERT INTO refresh_tokens(token_hash,user_id,expires_at,created_at)
                VALUES (?,?,?,?)
                """,
                (token_fingerprint(token), user_id, int(expires_at), now),
            )

    def rotate_refresh_token(
        self,
        user_id: str,
        old_token: str,
        new_token: str,
        new_expires_at: int,
    ) -> None:
        now = _now()
        old_hash, new_hash = token_fingerprint(old_token), token_fingerprint(new_token)
        replay_detected = False
        with self.transaction() as db:
            row = db.execute(
                "SELECT * FROM refresh_tokens WHERE token_hash = ?", (old_hash,)
            ).fetchone()
            if row is None or row["user_id"] != user_id:
                raise AccessDeniedError("Refresh token is not recognized")
            if row["revoked_at"] is not None:
                db.execute(
                    """
                    UPDATE refresh_tokens SET revoked_at=COALESCE(revoked_at, ?)
                    WHERE user_id=? AND revoked_at IS NULL
                    """,
                    (now, user_id),
                )
                replay_detected = True
            elif int(row["expires_at"]) <= now:
                raise AccessDeniedError("Refresh token has expired")
            else:
                db.execute(
                    "UPDATE refresh_tokens SET revoked_at = ?, replaced_by_hash = ? WHERE token_hash = ?",
                    (now, new_hash, old_hash),
                )
                db.execute(
                    """
                    INSERT INTO refresh_tokens(token_hash,user_id,expires_at,created_at)
                    VALUES (?,?,?,?)
                    """,
                    (new_hash, user_id, int(new_expires_at), now),
                )
        if replay_detected:
            raise AccessDeniedError(
                "Refresh token replay detected; all refresh sessions were revoked"
            )

    def revoke_refresh_token(self, token: str) -> bool:
        with self.transaction() as db:
            cursor = db.execute(
                """
                UPDATE refresh_tokens SET revoked_at = COALESCE(revoked_at, ?)
                WHERE token_hash = ?
                """,
                (_now(), token_fingerprint(token)),
            )
        return cursor.rowcount > 0

    def add_watchlist(self, user_id: str, ticker: str) -> dict[str, Any]:
        symbol, now = _ticker(ticker), _now()
        with self.transaction() as db:
            self._require_active_user(db, user_id)
            db.execute(
                "INSERT OR IGNORE INTO user_watchlists(user_id,ticker,created_at) VALUES (?,?,?)",
                (user_id, symbol, now),
            )
            row = db.execute(
                "SELECT ticker,created_at FROM user_watchlists WHERE user_id=? AND ticker=?",
                (user_id, symbol),
            ).fetchone()
        return {"ticker": row["ticker"], "created_at": _iso(row["created_at"])}

    def list_watchlist(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT ticker,created_at FROM user_watchlists WHERE user_id=? ORDER BY created_at DESC,ticker",
                (user_id,),
            ).fetchall()
        return [{"ticker": row["ticker"], "created_at": _iso(row["created_at"])} for row in rows]

    def remove_watchlist(self, user_id: str, ticker: str) -> bool:
        with self.transaction() as db:
            cursor = db.execute(
                "DELETE FROM user_watchlists WHERE user_id=? AND ticker=?",
                (user_id, _ticker(ticker)),
            )
        return cursor.rowcount > 0

    def create_paper_account(
        self,
        user_id: str,
        name: str,
        initial_cash: float = 100_000,
        currency: str = "USD",
    ) -> dict[str, Any]:
        clean_name = (name or "").strip()
        clean_currency = (currency or "").strip().upper()
        if not 1 <= len(clean_name) <= 80:
            raise ValidationError("Account name must contain between 1 and 80 characters")
        if not re.fullmatch(r"[A-Z]{3}", clean_currency):
            raise ValidationError("Currency must be a three-letter code")
        cash, now, account_id = _finite(initial_cash, "initial_cash", minimum=0), _now(), str(uuid.uuid4())
        try:
            with self.transaction() as db:
                self._require_active_user(db, user_id)
                db.execute(
                    """
                    INSERT INTO paper_accounts(
                        id,user_id,name,currency,mode,initial_cash,cash_balance,created_at,updated_at
                    ) VALUES (?,?,?,?,'PAPER',?,?,?,?)
                    """,
                    (account_id, user_id, clean_name, clean_currency, cash, cash, now, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("An account with this name already exists") from exc
        return self.get_account(user_id, account_id)

    def get_account(self, user_id: str, account_id: str | None = None) -> dict[str, Any]:
        with self._connect() as db:
            if account_id:
                row = db.execute(
                    "SELECT * FROM paper_accounts WHERE id=? AND user_id=?", (account_id, user_id)
                ).fetchone()
            else:
                row = db.execute(
                    """
                    SELECT * FROM paper_accounts WHERE user_id=? AND status='ACTIVE'
                    ORDER BY created_at,id LIMIT 1
                    """,
                    (user_id,),
                ).fetchone()
        if row is None:
            raise NotFoundError("Paper account not found")
        return self._account(row)

    def list_accounts(self, user_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM paper_accounts WHERE user_id=? ORDER BY created_at,id", (user_id,)
            ).fetchall()
        return [self._account(row) for row in rows]

    def update_account_controls(
        self,
        user_id: str,
        account_id: str,
        *,
        trading_enabled: bool | None = None,
        kill_switch: bool | None = None,
        reason: str,
    ) -> dict[str, Any]:
        """Atomically change PAPER controls and append an ownership-scoped audit event."""

        clean_reason = (reason or "").strip()
        if trading_enabled is None and kill_switch is None:
            raise ValidationError("At least one account control must be supplied")
        if not 3 <= len(clean_reason) <= 500:
            raise ValidationError("A 3-500 character reason is required")
        now, event_id = _now(), str(uuid.uuid4())
        with self.transaction() as db:
            account = self._require_account(db, user_id, account_id)
            if account["mode"] != "PAPER" or account["status"] != "ACTIVE":
                raise ConflictError("Controls apply only to an active PAPER account")
            old_trading = bool(account["trading_enabled"])
            old_kill = bool(account["kill_switch"])
            new_kill = old_kill if kill_switch is None else bool(kill_switch)
            new_trading = old_trading if trading_enabled is None else bool(trading_enabled)
            if new_kill and trading_enabled is True:
                raise ConflictError("Trading cannot be enabled while the kill switch is active")
            if new_kill:
                new_trading = False
            db.execute(
                """
                UPDATE paper_accounts SET trading_enabled=?,kill_switch=?,updated_at=?
                WHERE id=?
                """,
                (int(new_trading), int(new_kill), now, account_id),
            )
            db.execute(
                """
                INSERT INTO account_control_events(
                    id,account_id,user_id,old_trading_enabled,new_trading_enabled,
                    old_kill_switch,new_kill_switch,reason,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    event_id,
                    account_id,
                    user_id,
                    int(old_trading),
                    int(new_trading),
                    int(old_kill),
                    int(new_kill),
                    clean_reason,
                    now,
                ),
            )
        account_out = self.get_account(user_id, account_id)
        account_out["control_event"] = {
            "id": event_id,
            "old_trading_enabled": old_trading,
            "new_trading_enabled": new_trading,
            "old_kill_switch": old_kill,
            "new_kill_switch": new_kill,
            "reason": clean_reason,
            "created_at": _iso(now),
        }
        return account_out

    def list_account_control_events(
        self, user_id: str, account_id: str, *, limit: int = 50
    ) -> list[dict[str, Any]]:
        self.get_account(user_id, account_id)
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT * FROM account_control_events WHERE account_id=? AND user_id=?
                ORDER BY created_at DESC,id DESC LIMIT ?
                """,
                (account_id, user_id, max(1, min(limit, 500))),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "account_id": row["account_id"],
                "old_trading_enabled": bool(row["old_trading_enabled"]),
                "new_trading_enabled": bool(row["new_trading_enabled"]),
                "old_kill_switch": bool(row["old_kill_switch"]),
                "new_kill_switch": bool(row["new_kill_switch"]),
                "reason": row["reason"],
                "created_at": _iso(row["created_at"]),
            }
            for row in rows
        ]

    @staticmethod
    def _account(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "name": row["name"],
            "currency": row["currency"],
            "mode": row["mode"],
            "initial_cash": round(float(row["initial_cash"]), 6),
            "cash": round(float(row["cash_balance"]), 6),
            "realized_pnl": round(float(row["realized_pnl"]), 6),
            "daily_pnl": round(float(row["daily_pnl"]), 6),
            "trading_enabled": bool(row["trading_enabled"]),
            "kill_switch": bool(row["kill_switch"]),
            "status": row["status"],
            "created_at": _iso(row["created_at"]),
            "updated_at": _iso(row["updated_at"]),
        }

    def list_positions(self, user_id: str, account_id: str) -> list[dict[str, Any]]:
        self.get_account(user_id, account_id)
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM paper_positions WHERE account_id=? ORDER BY ticker", (account_id,)
            ).fetchall()
        return [
            {
                "ticker": row["ticker"],
                "quantity": float(row["quantity"]),
                "average_cost": float(row["average_cost"]),
                "realized_pnl": float(row["realized_pnl"]),
                "updated_at": _iso(row["updated_at"]),
            }
            for row in rows
        ]

    @staticmethod
    def _mark_daily_equity_in_transaction(
        db: sqlite3.Connection,
        account_id: str,
        equity: float,
        now: int,
    ) -> tuple[float, float]:
        value = _finite(equity, "equity", minimum=0)
        day = _utc_day(now)
        row = db.execute(
            "SELECT opening_equity FROM account_daily_risk WHERE account_id=? AND trading_day=?",
            (account_id, day),
        ).fetchone()
        if row is None:
            prior = db.execute(
                """
                SELECT latest_equity FROM account_daily_risk
                WHERE account_id=? ORDER BY trading_day DESC LIMIT 1
                """,
                (account_id,),
            ).fetchone()
            if prior is not None:
                opening = max(value, float(prior["latest_equity"]))
            else:
                account = db.execute(
                    "SELECT initial_cash FROM paper_accounts WHERE id=?", (account_id,)
                ).fetchone()
                if account is None:
                    raise NotFoundError("Paper account not found")
                opening = max(value, float(account["initial_cash"]))
        else:
            opening = float(row["opening_equity"])
        daily_pnl = value - opening
        db.execute(
            """
            INSERT INTO account_daily_risk(
                account_id,trading_day,opening_equity,latest_equity,daily_pnl,updated_at
            ) VALUES (?,?,?,?,?,?)
            ON CONFLICT(account_id,trading_day) DO UPDATE SET
              latest_equity=excluded.latest_equity,
              daily_pnl=excluded.daily_pnl,
              updated_at=excluded.updated_at
            """,
            (account_id, day, opening, value, daily_pnl, now),
        )
        db.execute(
            "UPDATE paper_accounts SET daily_pnl=?,updated_at=? WHERE id=?",
            (daily_pnl, now, account_id),
        )
        return round(daily_pnl, 6), round(opening, 6)

    def mark_daily_equity(self, user_id: str, account_id: str, equity: float) -> float:
        now = _now()
        with self.transaction() as db:
            self._require_account(db, user_id, account_id)
            daily_pnl, _ = self._mark_daily_equity_in_transaction(
                db, account_id, equity, now
            )
            return daily_pnl

    def account_cost_summary(self, user_id: str, account_id: str | None = None) -> dict[str, Any]:
        account = self.get_account(user_id, account_id)
        positions = self.list_positions(user_id, account["id"])
        gross = sum(item["quantity"] * item["average_cost"] for item in positions)
        return {
            **account,
            "positions": positions,
            "gross_exposure": round(gross, 6),
            "equity": round(account["cash"] + gross, 6),
        }

    def create_trade_proposal(
        self,
        user_id: str,
        account_id: str,
        decision: Mapping[str, Any],
        expires_at: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        now = _now()
        if expires_at <= now:
            raise ValidationError("Proposal expiry must be in the future")
        ticker = _ticker(str(decision.get("ticker") or ""))
        decision_id = str(decision.get("decision_id") or "").strip()
        if not re.fullmatch(r"[a-f0-9]{24,64}", decision_id):
            raise ValidationError("Decision must include a valid decision_id")
        key = (idempotency_key or "").strip()
        if not 8 <= len(key) <= 128 or any(char.isspace() for char in key):
            raise ValidationError("Idempotency-Key must contain 8-128 non-whitespace characters")
        action = str(decision.get("action") or "HOLD").upper()
        side = {"BUY": "BUY", "SELL": "SELL", "REDUCE": "SELL"}.get(action, "HOLD")
        sizing = decision.get("position_size") or decision.get("sizing") or {}
        if not isinstance(sizing, Mapping):
            sizing = {}
        quantity = _finite(sizing.get("quantity", 0), "quantity", minimum=0)
        reference_price = _finite(
            sizing.get("price", sizing.get("entry", decision.get("price", 0))),
            "reference_price",
            minimum=0,
        )
        stop_loss = sizing.get("stop_price", sizing.get("stop_loss"))
        take_profit = sizing.get("take_profit")
        estimated_risk = _finite(
            sizing.get("estimated_risk", sizing.get("risk_amount", 0)),
            "estimated_risk",
            minimum=0,
        )
        vetoes = decision.get("vetoes") or []
        evidence = decision.get("evidence") or []
        risk_policy = decision.get("risk_policy")
        if not isinstance(risk_policy, Mapping):
            raise ValidationError("Decision must include an immutable risk policy")
        policy_ceilings = {
            "risk_per_trade_pct": 0.05,
            "max_position_pct": 0.50,
            "max_gross_exposure_pct": 1.0,
            "max_daily_loss_pct": 0.10,
        }
        for policy_key, maximum in policy_ceilings.items():
            _policy_fraction(risk_policy, policy_key, maximum)
        market_quality = decision.get("market_quality")
        canonical = (
            market_quality.get("canonical") if isinstance(market_quality, Mapping) else None
        )
        quote_currency = str(
            canonical.get("currency") if isinstance(canonical, Mapping) else ""
        ).strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", quote_currency):
            raise ValidationError("Decision must include a trusted quote currency")
        executable = bool(
            decision.get("execution_eligible", decision.get("executable", False))
        )
        if side not in {"BUY", "SELL"} or quantity <= 0 or reference_price <= 0:
            executable = False
        proposal_id = str(uuid.uuid4())
        existing_id: str | None = None
        with self.transaction() as db:
            account = self._require_account(db, user_id, account_id)
            if account["mode"] != "PAPER" or account["status"] != "ACTIVE":
                raise ConflictError("Only an active PAPER account can receive proposals")
            if quote_currency != account["currency"]:
                raise ConflictError("Decision quote currency does not match the account")
            existing = db.execute(
                "SELECT id,decision_id FROM trade_proposals WHERE account_id=? AND idempotency_key=?",
                (account_id, key),
            ).fetchone()
            if existing is not None:
                if existing["decision_id"] != decision_id:
                    raise ConflictError("Idempotency-Key was already used for another decision")
                existing_id = str(existing["id"])
            else:
                db.execute(
                """
                INSERT INTO trade_proposals(
                    id,user_id,account_id,ticker,side,quantity,reference_price,stop_loss,
                    take_profit,estimated_risk,score,confidence,executable,vetoes_json,
                    evidence_json,decision_json,rationale,decision_id,idempotency_key,quote_currency,
                    status,expires_at,created_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'PENDING',?,?)
                """,
                (
                    proposal_id,
                    user_id,
                    account_id,
                    ticker,
                    side,
                    quantity,
                    reference_price,
                    _finite(stop_loss, "stop_loss", minimum=0) if stop_loss is not None else None,
                    _finite(take_profit, "take_profit", minimum=0) if take_profit is not None else None,
                    estimated_risk,
                    _finite(decision.get("score"), "score") if decision.get("score") is not None else None,
                    _finite(decision.get("confidence"), "confidence", minimum=0)
                    if decision.get("confidence") is not None
                    else None,
                    int(executable),
                    _json(vetoes),
                    _json(evidence),
                    _json(dict(decision)),
                    str(decision.get("rationale") or "")[:4000],
                    decision_id,
                    key,
                    quote_currency,
                    int(expires_at),
                    now,
                ),
            )
        return self.get_proposal(user_id, existing_id or proposal_id)

    def find_proposal_by_idempotency(
        self, user_id: str, account_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT p.* FROM trade_proposals p
                JOIN paper_accounts a ON a.id=p.account_id
                WHERE p.account_id=? AND p.idempotency_key=? AND a.user_id=?
                """,
                (account_id, idempotency_key, user_id),
            ).fetchone()
        return self._proposal(row) if row else None

    def expire_proposals(self, user_id: str | None = None) -> int:
        now = _now()
        sql = "UPDATE trade_proposals SET status='EXPIRED',decided_at=? WHERE status='PENDING' AND expires_at<=?"
        params: tuple[Any, ...] = (now, now)
        if user_id is not None:
            sql += " AND user_id=?"
            params += (user_id,)
        with self.transaction() as db:
            cursor = db.execute(sql, params)
        return cursor.rowcount

    def list_proposals(
        self,
        user_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        self.expire_proposals(user_id)
        if status is not None and status not in _STATUS_VALUES:
            raise ValidationError("Invalid proposal status")
        with self._connect() as db:
            if status:
                rows = db.execute(
                    """
                    SELECT * FROM trade_proposals WHERE user_id=? AND status=?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (user_id, status, max(1, min(limit, 500))),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT * FROM trade_proposals WHERE user_id=?
                    ORDER BY created_at DESC LIMIT ?
                    """,
                    (user_id, max(1, min(limit, 500))),
                ).fetchall()
        return [self._proposal(row) for row in rows]

    def get_proposal(self, user_id: str, proposal_id: str) -> dict[str, Any]:
        self.expire_proposals(user_id)
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM trade_proposals WHERE id=? AND user_id=?", (proposal_id, user_id)
            ).fetchone()
        if row is None:
            raise NotFoundError("Trade proposal not found")
        return self._proposal(row)

    @staticmethod
    def _proposal(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "account_id": row["account_id"],
            "ticker": row["ticker"],
            "side": row["side"],
            "quantity": float(row["quantity"]),
            "reference_price": float(row["reference_price"]),
            "stop_loss": float(row["stop_loss"]) if row["stop_loss"] is not None else None,
            "take_profit": float(row["take_profit"]) if row["take_profit"] is not None else None,
            "estimated_risk": float(row["estimated_risk"]),
            "score": float(row["score"]) if row["score"] is not None else None,
            "confidence": float(row["confidence"]) if row["confidence"] is not None else None,
            "executable": bool(row["executable"]),
            "vetoes": _loads(row["vetoes_json"], []),
            "evidence": _loads(row["evidence_json"], []),
            "decision": _loads(row["decision_json"], {}),
            "rationale": row["rationale"],
            "decision_id": row["decision_id"],
            "quote_currency": row["quote_currency"],
            "status": row["status"],
            "expires_at": _iso(row["expires_at"]),
            "created_at": _iso(row["created_at"]),
            "decided_at": _iso(row["decided_at"]),
            "decision_note": row["decision_note"],
            "approved_order_id": row["approved_order_id"],
        }

    def find_order_by_idempotency(
        self, user_id: str, account_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute(
                """
                SELECT o.* FROM paper_orders o
                JOIN paper_accounts a ON a.id=o.account_id
                WHERE o.account_id=? AND o.idempotency_key=? AND a.user_id=?
                """,
                (account_id, idempotency_key, user_id),
            ).fetchone()
        return self._order(row) if row else None

    def approve_trade_proposal(
        self,
        user_id: str,
        proposal_id: str,
        idempotency_key: str,
        market_snapshot: Mapping[str, Any],
        *,
        note: str = "",
        portfolio_marks: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        key = (idempotency_key or "").strip()
        if not 8 <= len(key) <= 128 or any(char.isspace() for char in key):
            raise ValidationError("Idempotency-Key must contain 8-128 non-whitespace characters")
        now = _now()
        expired = False
        with self.transaction() as db:
            proposal = db.execute(
                "SELECT * FROM trade_proposals WHERE id=? AND user_id=?", (proposal_id, user_id)
            ).fetchone()
            if proposal is None:
                raise NotFoundError("Trade proposal not found")
            account = self._require_account(db, user_id, proposal["account_id"])

            existing = db.execute(
                "SELECT * FROM paper_orders WHERE account_id=? AND idempotency_key=?",
                (account["id"], key),
            ).fetchone()
            if existing is not None:
                if existing["proposal_id"] != proposal_id:
                    raise ConflictError("Idempotency-Key was already used for another proposal")
                return self._order(existing)

            if proposal["status"] != "PENDING":
                raise ConflictError(f"Proposal is already {proposal['status'].lower()}")
            if int(proposal["expires_at"]) <= now:
                db.execute(
                    "UPDATE trade_proposals SET status='EXPIRED',decided_at=? WHERE id=?",
                    (now, proposal_id),
                )
                expired = True
            else:
                stored_decision_id = str(proposal["decision_id"] or "")
                stored_proposal_key = str(proposal["idempotency_key"] or "")
                stored_currency = str(proposal["quote_currency"] or "").upper()
                stored_decision = _loads(proposal["decision_json"], {})
                stored_quality = (
                    stored_decision.get("market_quality")
                    if isinstance(stored_decision, Mapping)
                    else None
                )
                stored_canonical = (
                    stored_quality.get("canonical")
                    if isinstance(stored_quality, Mapping)
                    else None
                )
                decision_currency = str(
                    stored_canonical.get("currency")
                    if isinstance(stored_canonical, Mapping)
                    else ""
                ).upper()
                if (
                    not re.fullmatch(r"[a-f0-9]{24,64}", stored_decision_id)
                    or not 8 <= len(stored_proposal_key) <= 128
                    or any(char.isspace() for char in stored_proposal_key)
                    or stored_currency != account["currency"]
                    or decision_currency != stored_currency
                    or not isinstance(stored_decision, Mapping)
                    or stored_decision.get("decision_id") != stored_decision_id
                ):
                    raise ConflictError(
                        "Proposal predates or fails the decision-binding safety contract"
                    )
                if account["mode"] != "PAPER" or account["status"] != "ACTIVE":
                    raise ConflictError("Approval is restricted to an active PAPER account")
                if not bool(account["trading_enabled"]) or bool(account["kill_switch"]):
                    raise ConflictError("Paper trading is disabled for this account")
                if not bool(proposal["executable"]):
                    raise ConflictError("Proposal is not execution-eligible")
                if _loads(proposal["vetoes_json"], []):
                    raise ConflictError("Proposal has unresolved risk vetoes")
                if proposal["side"] not in {"BUY", "SELL"}:
                    raise ConflictError("HOLD proposals cannot create orders")

                fill_price, source, as_of, quote_currency = _validated_execution_snapshot(
                    market_snapshot, proposal["ticker"], account["currency"]
                )
                reference_price = float(proposal["reference_price"])
                deviation = abs(fill_price - reference_price) / reference_price
                max_deviation = _price_deviation_limit()
                if deviation > max_deviation:
                    raise UnsafeMarketDataError(
                        "Market price moved beyond the configured proposal deviation limit"
                    )
                quantity = float(proposal["quantity"])
                notional = round(quantity * fill_price, 8)
                order_id = str(uuid.uuid4())

                position = db.execute(
                    "SELECT * FROM paper_positions WHERE account_id=? AND ticker=?",
                    (account["id"], proposal["ticker"]),
                ).fetchone()
                if proposal["side"] == "BUY":
                    decision = stored_decision
                    policy = decision.get("risk_policy")
                    if not isinstance(policy, Mapping):
                        raise ConflictError("Proposal is missing its immutable risk policy")
                    risk_fraction = _policy_fraction(policy, "risk_per_trade_pct", 0.05)
                    max_position_fraction = _policy_fraction(policy, "max_position_pct", 0.50)
                    max_gross_fraction = _policy_fraction(
                        policy, "max_gross_exposure_pct", 1.0
                    )
                    max_daily_loss_fraction = _policy_fraction(
                        policy, "max_daily_loss_pct", 0.10
                    )

                    current_gross = 0.0
                    target_value = 0.0
                    rows = db.execute(
                        "SELECT ticker,quantity FROM paper_positions WHERE account_id=?",
                        (account["id"],),
                    ).fetchall()
                    marks = portfolio_marks or {}
                    for row in rows:
                        symbol = str(row["ticker"])
                        mark_snapshot = market_snapshot if symbol == proposal["ticker"] else marks.get(symbol)
                        if not isinstance(mark_snapshot, Mapping):
                            raise UnsafeMarketDataError(
                                f"Fresh execution-quality mark is missing for {symbol}"
                            )
                        mark_price, _, _, _ = _validated_execution_snapshot(
                            mark_snapshot, symbol, account["currency"]
                        )
                        value = float(row["quantity"]) * mark_price
                        current_gross += value
                        if symbol == proposal["ticker"]:
                            target_value = value

                    current_equity = float(account["cash_balance"]) + current_gross
                    if current_equity <= 0:
                        raise ConflictError("Account equity must be positive at approval")
                    daily_pnl, opening_equity = self._mark_daily_equity_in_transaction(
                        db, account["id"], current_equity, now
                    )
                    if daily_pnl < -opening_equity * max_daily_loss_fraction:
                        raise ConflictError("Daily loss limit has been reached")
                    post_target_value = target_value + notional
                    post_gross = current_gross + notional
                    tolerance = max(1e-6, current_equity * 1e-9)
                    if post_target_value > current_equity * max_position_fraction + tolerance:
                        raise ConflictError("Approval would exceed the per-position limit")
                    if post_gross > current_equity * max_gross_fraction + tolerance:
                        raise ConflictError("Approval would exceed the gross-exposure limit")
                    stop_loss = proposal["stop_loss"]
                    if stop_loss is None or not 0 < float(stop_loss) < fill_price:
                        raise ConflictError("BUY proposal requires a valid stop below the fill price")
                    actual_risk = quantity * (fill_price - float(stop_loss))
                    if actual_risk > current_equity * risk_fraction + tolerance:
                        raise ConflictError("Approval would exceed the per-trade risk budget")
                    if float(account["cash_balance"]) + 1e-9 < notional:
                        raise ConflictError("Insufficient paper cash for this order")
                    old_quantity = float(position["quantity"]) if position else 0.0
                    old_cost = float(position["average_cost"]) if position else 0.0
                    new_quantity = old_quantity + quantity
                    average_cost = (old_quantity * old_cost + notional) / new_quantity
                    db.execute(
                        "UPDATE paper_accounts SET cash_balance=cash_balance-?,updated_at=? WHERE id=?",
                        (notional, now, account["id"]),
                    )
                    db.execute(
                        """
                        INSERT INTO paper_positions(account_id,ticker,quantity,average_cost,realized_pnl,updated_at)
                        VALUES (?,?,?,?,0,?)
                        ON CONFLICT(account_id,ticker) DO UPDATE SET
                          quantity=excluded.quantity,
                          average_cost=excluded.average_cost,
                          updated_at=excluded.updated_at
                        """,
                        (account["id"], proposal["ticker"], new_quantity, average_cost, now),
                    )
                else:
                    if position is None or float(position["quantity"]) + 1e-9 < quantity:
                        raise ConflictError("Insufficient paper position for this sell order")
                    old_quantity = float(position["quantity"])
                    average_cost = float(position["average_cost"])
                    remaining = old_quantity - quantity
                    realized = (fill_price - average_cost) * quantity
                    db.execute(
                        """
                        UPDATE paper_accounts SET
                          cash_balance=cash_balance+?,realized_pnl=realized_pnl+?,updated_at=?
                        WHERE id=?
                        """,
                        (notional, realized, now, account["id"]),
                    )
                    if remaining <= 1e-9:
                        db.execute(
                            "DELETE FROM paper_positions WHERE account_id=? AND ticker=?",
                            (account["id"], proposal["ticker"]),
                        )
                    else:
                        db.execute(
                            """
                            UPDATE paper_positions SET quantity=?,realized_pnl=realized_pnl+?,updated_at=?
                            WHERE account_id=? AND ticker=?
                            """,
                            (remaining, realized, now, account["id"], proposal["ticker"]),
                        )

                db.execute(
                    """
                    INSERT INTO paper_orders(
                        id,account_id,proposal_id,ticker,side,quantity,fill_price,notional,
                        idempotency_key,fill_source,snapshot_as_of,quote_currency,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        order_id,
                        account["id"],
                        proposal_id,
                        proposal["ticker"],
                        proposal["side"],
                        quantity,
                        fill_price,
                        notional,
                        key,
                        source,
                        as_of.isoformat(),
                        quote_currency,
                        now,
                    ),
                )
                db.execute(
                    """
                    UPDATE trade_proposals SET
                      status='APPROVED',decided_at=?,decision_note=?,approved_order_id=?
                    WHERE id=?
                    """,
                    (now, note[:2000], order_id, proposal_id),
                )
                order = db.execute("SELECT * FROM paper_orders WHERE id=?", (order_id,)).fetchone()
        if expired:
            raise ConflictError("Proposal has expired")
        assert order is not None
        return self._order(order)

    def reject_trade_proposal(
        self, user_id: str, proposal_id: str, *, note: str = ""
    ) -> dict[str, Any]:
        now = _now()
        expired = False
        with self.transaction() as db:
            proposal = db.execute(
                "SELECT * FROM trade_proposals WHERE id=? AND user_id=?", (proposal_id, user_id)
            ).fetchone()
            if proposal is None:
                raise NotFoundError("Trade proposal not found")
            if proposal["status"] != "PENDING":
                raise ConflictError(f"Proposal is already {proposal['status'].lower()}")
            if int(proposal["expires_at"]) <= now:
                status, expired = "EXPIRED", True
            else:
                status = "REJECTED"
            db.execute(
                """
                UPDATE trade_proposals SET status=?,decided_at=?,decision_note=? WHERE id=?
                """,
                (status, now, note[:2000], proposal_id),
            )
        if expired:
            raise ConflictError("Proposal has expired")
        return self.get_proposal(user_id, proposal_id)

    def list_orders(
        self, user_id: str, *, account_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        with self._connect() as db:
            if account_id:
                rows = db.execute(
                    """
                    SELECT o.* FROM paper_orders o JOIN paper_accounts a ON a.id=o.account_id
                    WHERE a.user_id=? AND a.id=? ORDER BY o.created_at DESC LIMIT ?
                    """,
                    (user_id, account_id, max(1, min(limit, 500))),
                ).fetchall()
            else:
                rows = db.execute(
                    """
                    SELECT o.* FROM paper_orders o JOIN paper_accounts a ON a.id=o.account_id
                    WHERE a.user_id=? ORDER BY o.created_at DESC LIMIT ?
                    """,
                    (user_id, max(1, min(limit, 500))),
                ).fetchall()
        return [self._order(row) for row in rows]

    @staticmethod
    def _order(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "account_id": row["account_id"],
            "proposal_id": row["proposal_id"],
            "ticker": row["ticker"],
            "side": row["side"],
            "quantity": float(row["quantity"]),
            "order_type": row["order_type"],
            "fill_price": float(row["fill_price"]),
            "quote_currency": row["quote_currency"],
            "notional": float(row["notional"]),
            "status": row["status"],
            "idempotency_key": row["idempotency_key"],
            "fill_source": row["fill_source"],
            "snapshot_as_of": row["snapshot_as_of"],
            "created_at": _iso(row["created_at"]),
            "mode": "PAPER",
        }

    def record_agent_run(
        self,
        user_id: str,
        *,
        agent_name: str,
        status: str,
        input_data: Mapping[str, Any],
        output_data: Mapping[str, Any] | None = None,
        error: str = "",
        account_id: str | None = None,
        proposal_id: str | None = None,
        ticker: str | None = None,
        trace_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_status = status.upper()
        if normalized_status not in {"SUCCEEDED", "FAILED"}:
            raise ValidationError("Agent run status must be SUCCEEDED or FAILED")
        run_id, now = str(uuid.uuid4()), _now()
        with self.transaction() as db:
            self._require_active_user(db, user_id)
            if account_id:
                self._require_account(db, user_id, account_id)
            if proposal_id:
                proposal = db.execute(
                    "SELECT 1 FROM trade_proposals WHERE id=? AND user_id=?",
                    (proposal_id, user_id),
                ).fetchone()
                if proposal is None:
                    raise NotFoundError("Trade proposal not found")
            db.execute(
                """
                INSERT INTO agent_runs(
                    id,user_id,account_id,proposal_id,ticker,agent_name,status,input_json,
                    output_json,error,trace_id,created_at,completed_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    user_id,
                    account_id,
                    proposal_id,
                    _ticker(ticker) if ticker else None,
                    agent_name[:128],
                    normalized_status,
                    _json(dict(input_data)),
                    _json(dict(output_data or {})),
                    error[:4000],
                    trace_id,
                    now,
                    now,
                ),
            )
        return self.get_agent_run(user_id, run_id)

    def get_agent_run(self, user_id: str, run_id: str) -> dict[str, Any]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM agent_runs WHERE id=? AND user_id=?", (run_id, user_id)
            ).fetchone()
        if row is None:
            raise NotFoundError("Agent run not found")
        return self._run(row)

    def list_agent_runs(self, user_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM agent_runs WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                (user_id, max(1, min(limit, 500))),
            ).fetchall()
        return [self._run(row) for row in rows]

    @staticmethod
    def _run(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "user_id": row["user_id"],
            "account_id": row["account_id"],
            "proposal_id": row["proposal_id"],
            "ticker": row["ticker"],
            "agent_name": row["agent_name"],
            "status": row["status"],
            "input": _loads(row["input_json"], {}),
            "output": _loads(row["output_json"], {}),
            "error": row["error"],
            "trace_id": row["trace_id"],
            "created_at": _iso(row["created_at"]),
            "completed_at": _iso(row["completed_at"]),
        }

    @staticmethod
    def _require_active_user(db: sqlite3.Connection, user_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM users WHERE id=? AND is_active=1", (user_id,)
        ).fetchone()
        if row is None:
            raise AccessDeniedError("User is not active")
        return row

    @staticmethod
    def _require_account(
        db: sqlite3.Connection, user_id: str, account_id: str
    ) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM paper_accounts WHERE id=? AND user_id=?", (account_id, user_id)
        ).fetchone()
        if row is None:
            raise NotFoundError("Paper account not found")
        return row


_STORE: SQLiteStore | None = None
_STORE_PATH: Path | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> SQLiteStore:
    global _STORE, _STORE_PATH
    path = default_db_path()
    with _STORE_LOCK:
        if _STORE is None or _STORE_PATH != path:
            _STORE = SQLiteStore(path)
            _STORE_PATH = path
        return _STORE


__all__ = [
    "AccessDeniedError",
    "ConflictError",
    "LOCAL_USER_ID",
    "NotFoundError",
    "SQLiteStore",
    "StoreError",
    "UnsafeMarketDataError",
    "UserRecord",
    "ValidationError",
    "default_db_path",
    "get_store",
]
