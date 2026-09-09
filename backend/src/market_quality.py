"""Canonical market-snapshot validation for safety-sensitive decisions.

The research product may deliberately use deterministic demo data when a market
provider is unavailable.  That is useful for the UI, but it must never be
confused with an execution-quality quote.  This module centralises the boundary
between those two use cases.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


QUALITY_VERSION = "market-snapshot-quality-v1"
_TICKER_RE = re.compile(r"^[A-Z0-9.\-^]{1,24}$")
_DEMO_SOURCE_MARKERS = (
    "demo",
    "synthetic",
    "fallback",
    "preview",
    "mock",
    "sample",
    "offline",
)


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _parse_timestamp(value: Any, *, require_timezone: bool = False) -> datetime | None:
    if isinstance(value, datetime):
        if require_timezone and (value.tzinfo is None or value.utcoffset() is None):
            return None
        return _utc(value)
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if require_timezone and (parsed.tzinfo is None or parsed.utcoffset() is None):
        return None
    return _utc(parsed)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _valid_candle(candle: Any) -> bool:
    if not isinstance(candle, Mapping):
        return False
    open_ = _decimal(candle.get("open"))
    high = _decimal(candle.get("high"))
    low = _decimal(candle.get("low"))
    close = _decimal(candle.get("close"))
    volume = _decimal(candle.get("volume"))
    if None in (open_, high, low, close, volume) or _parse_timestamp(candle.get("date")) is None:
        return False
    assert open_ is not None and high is not None and low is not None
    assert close is not None and volume is not None
    return (
        open_ > 0
        and high > 0
        and low > 0
        and close > 0
        and volume >= 0
        and high >= max(open_, close)
        and low <= min(open_, close)
        and high >= low
    )


def _issue(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _bounded_env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def market_age_limits() -> tuple[int, int]:
    """Return the canonical freshness and future-skew limits."""

    return (
        _bounded_env_int(
            "MAX_MARKET_SNAPSHOT_AGE_SECONDS",
            72 * 60 * 60,
            1,
            31 * 24 * 60 * 60,
        ),
        _bounded_env_int("MARKET_MAX_FUTURE_SKEW_SECONDS", 300, 0, 3600),
    )


def assess_market_snapshot(
    snapshot: Mapping[str, Any],
    *,
    now: datetime | None = None,
    max_age_seconds: int | None = None,
    future_skew_seconds: int | None = None,
    min_candles: int = 30,
    min_completeness: float = 0.85,
) -> dict[str, Any]:
    """Assess provenance, freshness and completeness of one market snapshot.

    The result is JSON-safe and intentionally contains both a canonical summary
    and machine-readable issues.  A demo/synthetic/fallback provenance always
    produces ``execution_eligible=False``, irrespective of all other checks.
    ``now`` is injectable so decisions and tests are reproducible.
    """

    if not isinstance(snapshot, Mapping):
        raise TypeError("snapshot must be a mapping")
    configured_max_age, configured_future_skew = market_age_limits()
    max_age_seconds = configured_max_age if max_age_seconds is None else max_age_seconds
    future_skew_seconds = (
        configured_future_skew if future_skew_seconds is None else future_skew_seconds
    )
    if max_age_seconds <= 0:
        raise ValueError("max_age_seconds must be positive")
    if future_skew_seconds < 0:
        raise ValueError("future_skew_seconds cannot be negative")
    if min_candles < 1:
        raise ValueError("min_candles must be at least one")
    if not 0 <= min_completeness <= 1:
        raise ValueError("min_completeness must be between zero and one")

    checked_at = _utc(now)
    meta = snapshot.get("meta") if isinstance(snapshot.get("meta"), Mapping) else {}
    ticker = str(snapshot.get("ticker") or "").strip().upper()
    source = str(meta.get("source") or snapshot.get("source") or "").strip()
    currency = str(meta.get("currency") or snapshot.get("currency") or "").strip().upper()
    exchange = str(meta.get("exchange") or snapshot.get("exchange") or "").strip().upper()
    instrument_type = str(
        meta.get("instrument_type") or snapshot.get("instrument_type") or ""
    ).strip().upper()
    declared_tradable = meta.get("tradable", snapshot.get("tradable"))
    as_of = _parse_timestamp(
        meta.get("as_of") or snapshot.get("as_of"), require_timezone=True
    )
    price = _decimal(snapshot.get("price"))
    volume = _decimal(snapshot.get("volume"))
    raw_candles = snapshot.get("candles")
    candles: Sequence[Any] = (
        raw_candles
        if isinstance(raw_candles, Sequence)
        and not isinstance(raw_candles, (str, bytes, bytearray))
        else []
    )
    valid_candles = sum(_valid_candle(item) for item in candles)
    candle_valid_ratio = valid_candles / len(candles) if candles else 0.0

    source_lower = source.lower()
    declared_demo = bool(meta.get("is_demo") or snapshot.get("is_demo"))
    inferred_demo = any(marker in source_lower for marker in _DEMO_SOURCE_MARKERS)
    is_demo = declared_demo or inferred_demo
    declared_stale = bool(meta.get("is_stale") or snapshot.get("is_stale"))
    declared_eligible = meta.get("execution_eligible", snapshot.get("execution_eligible"))
    upstream_status = str(meta.get("status") or snapshot.get("status") or "").strip().lower()
    halted = bool(meta.get("is_halted") or meta.get("halted") or snapshot.get("is_halted"))
    delayed = bool(meta.get("is_delayed") or meta.get("delayed") or snapshot.get("is_delayed"))
    declared_stale = declared_stale or upstream_status in {"stale", "expired"}
    halted = halted or upstream_status in {"halted", "suspended"}
    delayed = delayed or upstream_status == "delayed"

    checks = {
        "ticker": bool(_TICKER_RE.fullmatch(ticker)),
        "positive_price": price is not None and price > 0,
        "positive_volume": volume is not None and volume > 0,
        "provenance": bool(source),
        "quote_currency": bool(re.fullmatch(r"[A-Z]{3}", currency)),
        "exchange": bool(exchange),
        "supported_instrument": instrument_type in {"EQUITY", "ETF"},
        "tradable": declared_tradable is True,
        "timestamp": as_of is not None,
        "candles": len(candles) >= min_candles and candle_valid_ratio >= 0.95,
    }
    completeness_score = sum(checks.values()) / len(checks)
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    if not checks["ticker"]:
        errors.append(_issue("INVALID_TICKER", "Snapshot ticker is missing or invalid."))
    if not checks["positive_price"]:
        errors.append(_issue("INVALID_PRICE", "Snapshot price must be finite and positive."))
    if not checks["positive_volume"]:
        errors.append(_issue("INVALID_VOLUME", "Snapshot volume must be finite and positive."))
    if not checks["provenance"]:
        errors.append(_issue("MISSING_PROVENANCE", "Snapshot does not identify its data source."))
    if not checks["quote_currency"]:
        errors.append(_issue("MISSING_QUOTE_CURRENCY", "Snapshot quote currency is missing or invalid."))
    if not checks["exchange"]:
        errors.append(_issue("MISSING_EXCHANGE", "Snapshot exchange is missing."))
    if not checks["supported_instrument"]:
        errors.append(
            _issue("UNSUPPORTED_INSTRUMENT", "Only provider-confirmed equities and ETFs are supported.")
        )
    if not checks["tradable"]:
        errors.append(_issue("NOT_TRADABLE", "The provider did not explicitly mark this instrument tradable."))
    if not checks["timestamp"]:
        errors.append(_issue("MISSING_AS_OF", "Snapshot does not contain a valid as-of timestamp."))
    if not checks["candles"]:
        errors.append(
            _issue(
                "INCOMPLETE_CANDLES",
                f"Need at least {min_candles} candles with at least 95% valid OHLCV rows.",
            )
        )
    if completeness_score < min_completeness:
        errors.append(
            _issue(
                "INCOMPLETE_SNAPSHOT",
                f"Completeness {completeness_score:.2f} is below {min_completeness:.2f}.",
            )
        )

    age_seconds: float | None = None
    stale = True
    future_dated = False
    if as_of is not None:
        age_seconds = (checked_at - as_of).total_seconds()
        future_dated = age_seconds < -future_skew_seconds
        stale = age_seconds > max_age_seconds
        if future_dated:
            errors.append(_issue("FUTURE_DATED", "Snapshot timestamp is unexpectedly in the future."))
        elif stale:
            errors.append(
                _issue(
                    "STALE_DATA",
                    f"Snapshot age exceeds the {max_age_seconds}-second freshness limit.",
                )
            )
    if is_demo:
        errors.append(
            _issue(
                "DEMO_DATA",
                "Demo, synthetic or fallback market data is research-only.",
            )
        )
    if declared_stale:
        errors.append(_issue("UPSTREAM_STALE", "The upstream provider marked this snapshot stale."))
    if declared_eligible is not True:
        errors.append(
            _issue(
                "UPSTREAM_INELIGIBLE",
                "The upstream provider did not explicitly mark this snapshot execution-eligible.",
            )
        )
    if halted:
        errors.append(_issue("MARKET_HALTED", "The upstream provider marked this market halted."))
    if delayed:
        errors.append(_issue("DELAYED_DATA", "Delayed market data is research-only."))
    if candle_valid_ratio < 1 and candles:
        warnings.append(
            _issue(
                "INVALID_CANDLE_ROWS",
                f"{len(candles) - valid_candles} candle rows failed OHLCV validation.",
            )
        )

    if age_seconds is None or future_dated:
        freshness_score = 0.0
    else:
        freshness_score = max(0.0, 1.0 - max(0.0, age_seconds) / max_age_seconds)
    quality_score = completeness_score * (0.5 + 0.5 * freshness_score)
    if is_demo:
        quality_score = 0.0

    canonical = {
        "ticker": ticker,
        "price": str(price) if price is not None else None,
        "volume": str(volume) if volume is not None else None,
        "source": source or None,
        "currency": currency or None,
        "exchange": exchange or None,
        "instrument_type": instrument_type or None,
        "tradable": declared_tradable is True,
        "as_of": as_of.isoformat() if as_of else None,
        "is_demo": is_demo,
        "candle_count": len(candles),
        "valid_candle_count": valid_candles,
    }
    digest_payload = {
        **canonical,
        "last_candles": list(candles[-3:]) if candles else [],
    }
    snapshot_id = hashlib.sha256(
        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]

    execution_eligible = (
        not is_demo
        and not stale
        and not future_dated
        and completeness_score >= min_completeness
        and not errors
    )
    return {
        "version": QUALITY_VERSION,
        "snapshot_id": snapshot_id,
        "status": "PASS" if execution_eligible else "FAIL",
        "execution_eligible": execution_eligible,
        "quality_score": round(float(quality_score), 6),
        "completeness_score": round(float(completeness_score), 6),
        "freshness": {
            "checked_at": checked_at.isoformat(),
            "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
            "max_age_seconds": max_age_seconds,
            "future_skew_seconds": future_skew_seconds,
            "stale": stale,
            "future_dated": future_dated,
        },
        "provenance": {
            "source": source or None,
            "declared_demo": declared_demo,
            "inferred_demo": inferred_demo,
            "is_demo": is_demo,
            "declared_stale": declared_stale,
            "declared_execution_eligible": declared_eligible,
            "upstream_status": upstream_status or None,
            "halted": halted,
            "delayed": delayed,
        },
        "checks": checks,
        "canonical": canonical,
        "errors": errors,
        "warnings": warnings,
    }
