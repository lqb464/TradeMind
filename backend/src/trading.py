"""Deterministic, proposal-only trading intelligence and backtesting.

Nothing in this module talks to a broker or submits an order.  The intelligence
layer produces an explainable proposal; a separate policy/approval/execution
boundary must consume it if TradeMind gains paper or live execution later.
"""

from __future__ import annotations

import hashlib
import json
import math
import statistics
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any, Mapping, Sequence

import numpy as np
import pandas as pd

from backend.src.market_quality import assess_market_snapshot


DECISION_VERSION = "trademind-explainable-ensemble-v1"
BACKTEST_VERSION = "trademind-leakage-safe-backtest-v1"
_ZERO = Decimal("0")
_ONE = Decimal("1")

__all__ = ["build_trade_decision", "run_strategy_backtest"]


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _decimal(value: Any, default: Decimal | None = None) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return default
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return default
    return result if result.is_finite() else default


def _number(value: Any) -> float | None:
    result = _decimal(value)
    return float(result) if result is not None else None


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _fraction(value: Any, default: str) -> Decimal:
    result = _decimal(value, Decimal(default))
    assert result is not None
    if result > 1:
        result /= Decimal("100")
    return max(_ZERO, min(_ONE, result))


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


def _quantity(value: Decimal, step: Decimal) -> Decimal:
    if value <= 0 or step <= 0:
        return _ZERO
    return (value / step).to_integral_value(rounding=ROUND_FLOOR) * step


def _qstr(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    return "0" if text in {"-0", ""} else text


def _nested(mapping: Mapping[str, Any] | None, *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = _number(value)
        if number is not None and math.isfinite(number):
            return number
    return None


def _auxiliary_usable(
    value: Mapping[str, Any] | None,
    *,
    expected_ticker: str,
) -> tuple[bool, dict[str, Any]]:
    if not isinstance(value, Mapping):
        return False, {"source": None, "reason": "missing"}
    meta = value.get("meta") if isinstance(value.get("meta"), Mapping) else {}
    source = str(meta.get("source") or value.get("source") or "").strip()
    status = str(meta.get("status") or value.get("status") or "").strip().lower()
    ticker = str(value.get("ticker") or "").strip().upper()
    is_demo = bool(meta.get("is_demo") or value.get("is_demo")) or any(
        marker in source.lower()
        for marker in ("demo", "synthetic", "fallback", "mock", "sample", "offline")
    )
    is_stale = bool(meta.get("is_stale") or value.get("is_stale")) or status in {
        "stale",
        "expired",
    }
    declared_eligible = meta.get("execution_eligible", value.get("execution_eligible"))
    ticker_mismatch = bool(ticker and expected_ticker and ticker != expected_ticker)
    unsafe_status = status in {"unavailable", "error", "halted", "suspended", "delayed"}
    reason = None
    if not source:
        reason = "missing_provenance"
    elif not ticker:
        reason = "missing_ticker"
    elif ticker_mismatch:
        reason = "ticker_mismatch"
    elif is_demo:
        reason = "demo_or_fallback"
    elif is_stale:
        reason = "stale"
    elif declared_eligible is not True:
        reason = "upstream_unverified"
    elif unsafe_status:
        reason = status
    return reason is None, {
        "source": source or None,
        "status": status or None,
        "ticker": ticker or None,
        "is_demo": is_demo,
        "is_stale": is_stale,
        "declared_execution_eligible": declared_eligible,
        "reason": reason,
    }


def _technical_contribution(
    snapshot: Mapping[str, Any], technicals: Mapping[str, Any] | None
) -> dict[str, Any]:
    indicators = snapshot.get("indicators") if isinstance(snapshot.get("indicators"), Mapping) else {}
    external_usable, external_provenance = _auxiliary_usable(
        technicals,
        expected_ticker=str(snapshot.get("ticker") or "").strip().upper(),
    )
    evidence = (
        technicals.get("evidence")
        if external_usable
        and isinstance(technicals, Mapping)
        and isinstance(technicals.get("evidence"), Mapping)
        else {}
    )
    price = _number(snapshot.get("price"))
    ma20 = _first_number(_nested(evidence, "ma20"), _nested(indicators, "ma20"))
    ma50 = _first_number(_nested(evidence, "ma50"), _nested(indicators, "ma50"))
    rsi = _first_number(_nested(evidence, "rsi"), _nested(indicators, "rsi"))
    macd = _first_number(_nested(evidence, "macd"), _nested(indicators, "macd"))
    macd_signal = _first_number(
        _nested(evidence, "macd_signal"), _nested(indicators, "macd_signal")
    )
    parts: list[tuple[str, float]] = []
    signal = str((technicals or {}).get("signal") or "").upper() if external_usable else ""
    if signal:
        parts.append(
            (
                "declared signal",
                {"BULLISH": 0.8, "BUY": 0.8, "BEARISH": -0.8, "SELL": -0.8}.get(signal, 0.0),
            )
        )
    if price and ma20 and ma20 > 0:
        parts.append(("price vs MA20", math.tanh(((price / ma20) - 1) / 0.035)))
    if ma20 and ma50 and ma50 > 0:
        parts.append(("MA20 vs MA50", math.tanh(((ma20 / ma50) - 1) / 0.05)))
    if price and macd is not None and macd_signal is not None:
        parts.append(("MACD spread", math.tanh((macd - macd_signal) / max(price * 0.01, 1e-9))))
    if rsi is not None:
        rsi_score = math.tanh((rsi - 50) / 22)
        if rsi >= 75:
            rsi_score -= min(0.6, (rsi - 75) / 20)
        parts.append(("RSI", _clamp(rsi_score)))
    raw_score = statistics.fmean(value for _, value in parts) if parts else 0.0
    return {
        "name": "technicals",
        "weight": 0.40,
        "available": bool(parts),
        "raw_score": round(_clamp(raw_score), 6),
        "rationale": "; ".join(f"{name}={value:+.2f}" for name, value in parts)
        or "No usable technical indicators were supplied.",
        "evidence": {
            "signal": signal or None,
            "price": price,
            "ma20": ma20,
            "ma50": ma50,
            "rsi": rsi,
            "macd": macd,
            "macd_signal": macd_signal,
            "external_provenance": external_provenance,
        },
    }


def _forecast_contribution(
    snapshot: Mapping[str, Any], forecast: Mapping[str, Any] | None
) -> dict[str, Any]:
    price = _number(snapshot.get("price"))
    provenance_usable, provenance = _auxiliary_usable(
        forecast,
        expected_ticker=str(snapshot.get("ticker") or "").strip().upper(),
    )
    points = forecast.get("forecast") if isinstance(forecast, Mapping) else None
    point = points[-1] if isinstance(points, Sequence) and points else None
    median = _number(point.get("median")) if isinstance(point, Mapping) else None
    low = _number(point.get("low")) if isinstance(point, Mapping) else None
    high = _number(point.get("high")) if isinstance(point, Mapping) else None
    confidence = _first_number((forecast or {}).get("confidence"), 0.5) or 0.5
    confidence = _clamp(confidence, 0.0, 1.0)
    available = bool(provenance_usable and price and median and median > 0)
    expected_return = (median / price - 1) if available and price else None
    interval_width = (
        max(0.0, (high - low) / price)
        if available and low is not None and high is not None and price
        else None
    )
    uncertainty_factor = max(0.20, 1.0 - min(0.80, interval_width or 0.25))
    raw_score = (
        math.tanh((expected_return or 0.0) / 0.05) * confidence * uncertainty_factor
        if available
        else 0.0
    )
    return {
        "name": "forecast",
        "weight": 0.35,
        "available": available,
        "raw_score": round(_clamp(raw_score), 6),
        "rationale": (
            f"median expected return={expected_return:+.2%}, interval width={interval_width:.2%}"
            if expected_return is not None and interval_width is not None
            else "No usable forecast median was supplied."
        ),
        "evidence": {
            "method": (forecast or {}).get("method"),
            "confidence": confidence,
            "low": low,
            "median": median,
            "high": high,
            "expected_return": round(expected_return, 8) if expected_return is not None else None,
            "interval_width": round(interval_width, 8) if interval_width is not None else None,
            "model_artifact": _nested(forecast, "meta", "model_artifact"),
            "provenance": provenance,
        },
    }


def _news_contribution(
    news: Mapping[str, Any] | None,
    *,
    expected_ticker: str,
) -> dict[str, Any]:
    sentiment = _number((news or {}).get("sentiment"))
    provenance_usable, provenance = _auxiliary_usable(news, expected_ticker=expected_ticker)
    available = sentiment is not None and provenance_usable
    raw_score = _clamp(sentiment or 0.0) if available else 0.0
    return {
        "name": "news",
        "weight": 0.15,
        "available": available,
        "raw_score": round(raw_score, 6),
        "rationale": (
            f"provider sentiment={raw_score:+.2f}"
            if available
            else "News signal is missing or marked as fallback/demo."
        ),
        "evidence": {
            "sentiment": sentiment,
            "label": (news or {}).get("label"),
            "source": _nested(news, "meta", "source"),
            "model": (news or {}).get("model"),
            "key_impacts": list((news or {}).get("key_impacts") or [])[:3],
            "provenance": provenance,
        },
    }


def _anomaly_contribution(
    anomalies: Mapping[str, Any] | None,
    *,
    expected_ticker: str,
) -> dict[str, Any]:
    provenance_usable, provenance = _auxiliary_usable(
        anomalies,
        expected_ticker=expected_ticker,
    )
    raw_items = anomalies.get("items") if isinstance(anomalies, Mapping) else None
    items = raw_items if isinstance(raw_items, Sequence) else []
    severities = [
        value
        for value in (_number(item.get("severity")) for item in items if isinstance(item, Mapping))
        if value is not None
    ]
    max_severity = max(severities, default=0.0)
    negative_shock = any(
        (_number(item.get("change_pct")) or 0.0) < 0
        for item in items
        if isinstance(item, Mapping)
    )
    penalty = min(1.0, max_severity / 5.0) * (1.0 if negative_shock else 0.35)
    available = anomalies is not None and provenance_usable
    return {
        "name": "anomalies",
        "weight": 0.10,
        "available": available,
        "raw_score": round(-penalty, 6),
        "rationale": (
            f"maximum anomaly severity={max_severity:.2f}; negative shock={negative_shock}"
            if available
            else "Anomaly assessment is missing or marked as untrusted."
        ),
        "evidence": {
            "method": (anomalies or {}).get("method"),
            "count": len(items),
            "max_severity": max_severity,
            "negative_shock": negative_shock,
            "provenance": provenance,
        },
    }


def _volatility_stop_pct(
    snapshot: Mapping[str, Any], technicals: Mapping[str, Any] | None, limits: Mapping[str, Any]
) -> tuple[Decimal, str]:
    price = _decimal(snapshot.get("price"), _ZERO) or _ZERO
    min_stop = _fraction(limits.get("min_stop_pct"), "0.02")
    max_stop = _fraction(limits.get("max_stop_pct"), "0.12")
    if max_stop < min_stop:
        max_stop = min_stop
    atr_multiple = _decimal(limits.get("atr_multiple"), Decimal("2")) or Decimal("2")
    volatility_multiple = _decimal(limits.get("volatility_multiple"), Decimal("2")) or Decimal("2")
    evidence = (
        technicals.get("evidence")
        if isinstance(technicals, Mapping) and isinstance(technicals.get("evidence"), Mapping)
        else technicals or {}
    )
    indicators = snapshot.get("indicators") if isinstance(snapshot.get("indicators"), Mapping) else {}
    atr = _decimal(
        evidence.get("atr_14")
        or evidence.get("atr")
        or indicators.get("atr_14")
        or indicators.get("atr")
    )
    candidates: list[tuple[Decimal, str]] = [(min_stop, "minimum policy stop")]
    if atr is not None and atr > 0 and price > 0:
        candidates.append((atr * atr_multiple / price, "ATR stop"))

    candles = snapshot.get("candles")
    closes: list[float] = []
    if isinstance(candles, Sequence):
        for candle in candles[-31:]:
            if isinstance(candle, Mapping):
                close = _number(candle.get("close"))
                if close is not None and close > 0:
                    closes.append(close)
    if len(closes) >= 10:
        returns = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))]
        volatility = Decimal(str(statistics.stdev(returns)))
        candidates.append((volatility * volatility_multiple, "realized-volatility stop"))

    selected, basis = max(candidates, key=lambda pair: pair[0])
    return min(max_stop, max(min_stop, selected)), basis


def _empty_sizing(price: Decimal) -> dict[str, Any]:
    return {
        "quantity": "0",
        "notional": "0.00",
        "price": _money(price),
        "stop_price": None,
        "stop_distance": None,
        "stop_pct": None,
        "risk_budget": None,
        "estimated_risk": "0.00",
        "binding_constraint": None,
        "constraints": {},
        "calculation": "No position change proposed.",
    }


def _size_buy(
    price: Decimal,
    snapshot: Mapping[str, Any],
    technicals: Mapping[str, Any] | None,
    account: Mapping[str, Any],
    limits: Mapping[str, Any],
) -> dict[str, Any]:
    equity = _decimal(account.get("equity"), _ZERO) or _ZERO
    cash = _decimal(account.get("cash") or account.get("buying_power"), _ZERO) or _ZERO
    current_quantity = _decimal(account.get("current_position_quantity"), _ZERO) or _ZERO
    current_position_value = _decimal(account.get("current_position_value"))
    if current_position_value is None:
        current_position_value = max(_ZERO, current_quantity * price)
    gross_exposure = _decimal(account.get("gross_exposure"), current_position_value) or current_position_value
    risk_pct = _fraction(limits.get("risk_per_trade_pct"), "0.01")
    max_position_pct = _fraction(limits.get("max_position_pct"), "0.10")
    max_exposure_pct = _fraction(limits.get("max_gross_exposure_pct"), "0.80")
    quantity_step = _decimal(limits.get("quantity_step"), Decimal("1")) or Decimal("1")
    if not bool(limits.get("allow_fractional", False)):
        quantity_step = max(Decimal("1"), quantity_step)

    stop_pct, stop_basis = _volatility_stop_pct(snapshot, technicals, limits)
    stop_distance = price * stop_pct
    risk_budget = equity * risk_pct
    risk_capacity = risk_budget / stop_distance if stop_distance > 0 else _ZERO
    position_room = max(_ZERO, equity * max_position_pct - current_position_value)
    exposure_room = max(_ZERO, equity * max_exposure_pct - gross_exposure)
    max_order_notional = _decimal(limits.get("max_order_notional"))
    order_room = max_order_notional if max_order_notional is not None else equity
    capacity_by_name = {
        "risk_budget": risk_capacity,
        "position_limit": position_room / price if price > 0 else _ZERO,
        "portfolio_exposure": exposure_room / price if price > 0 else _ZERO,
        "cash": max(_ZERO, cash) / price if price > 0 else _ZERO,
        "order_notional": max(_ZERO, order_room) / price if price > 0 else _ZERO,
    }
    binding_constraint, raw_quantity = min(capacity_by_name.items(), key=lambda item: item[1])
    quantity = _quantity(max(_ZERO, raw_quantity), quantity_step)
    notional = quantity * price
    estimated_risk = quantity * stop_distance
    stop_price = max(_ZERO, price - stop_distance)
    return {
        "quantity": _qstr(quantity),
        "notional": _money(notional),
        "price": _money(price),
        "stop_price": _money(stop_price),
        "stop_distance": _money(stop_distance),
        "stop_pct": format(stop_pct.normalize(), "f"),
        "stop_basis": stop_basis,
        "risk_budget": _money(risk_budget),
        "estimated_risk": _money(estimated_risk),
        "binding_constraint": binding_constraint,
        "constraints": {name: _qstr(_quantity(value, quantity_step)) for name, value in capacity_by_name.items()},
        "calculation": (
            "quantity = floor(min(risk budget / stop distance, position room / price, "
            "exposure room / price, cash / price, order cap / price), quantity step)"
        ),
    }


def _fetch_inputs(ticker: str) -> tuple[dict, dict, dict, dict]:
    """Fetch one market snapshot and the non-snapshot intelligence inputs."""

    from backend.src.intelligence import (
        detect_anomalies,
        forecast_range,
        get_market_snapshot,
        get_news_intelligence,
    )

    market = get_market_snapshot(ticker, "1y")
    forecast = forecast_range(ticker, 7)
    news = get_news_intelligence(ticker)
    anomalies = detect_anomalies(ticker)
    return market, forecast, news, anomalies


def build_trade_decision(
    ticker: str | Mapping[str, Any] | None = None,
    *,
    snapshot: Mapping[str, Any] | None = None,
    technicals: Mapping[str, Any] | None = None,
    forecast: Mapping[str, Any] | None = None,
    news: Mapping[str, Any] | None = None,
    anomalies: Mapping[str, Any] | None = None,
    account: Mapping[str, Any] | None = None,
    risk_limits: Mapping[str, Any] | None = None,
    now: datetime | None = None,
    max_snapshot_age_seconds: int | None = None,
) -> dict[str, Any]:
    """Build a deterministic, explainable and non-executing trade proposal.

    Callers may supply a complete research bundle for a reproducible/offline
    decision, or call with only ``ticker`` to use the existing intelligence
    providers.  When a snapshot is supplied, missing auxiliary inputs are not
    fetched implicitly.  Technical indicators are read from that same snapshot
    (and optionally enriched by ``technicals``), avoiding a second market fetch.

    The function never submits an order.  Its output is a proposal whose
    ``execution_eligible`` flag is only a guardrail signal for a later, separate
    approval and execution boundary.
    """

    if isinstance(ticker, Mapping):
        if snapshot is not None:
            raise ValueError("provide the snapshot either positionally or by keyword, not both")
        snapshot = ticker
        ticker = None
    fetched = snapshot is None
    requested_ticker = str(ticker or (snapshot or {}).get("ticker") or "").strip().upper()
    if fetched:
        if not requested_ticker:
            raise ValueError("ticker is required when snapshot is not supplied")
        snapshot, fetched_forecast, fetched_news, fetched_anomalies = _fetch_inputs(requested_ticker)
        forecast = forecast or fetched_forecast
        news = news or fetched_news
        anomalies = anomalies or fetched_anomalies
    assert snapshot is not None
    snapshot_ticker = str(snapshot.get("ticker") or "").strip().upper()
    if requested_ticker and snapshot_ticker and requested_ticker != snapshot_ticker:
        raise ValueError("ticker does not match snapshot ticker")
    requested_ticker = snapshot_ticker or requested_ticker

    decision_time = _utc(now)
    limits = dict(risk_limits or {})
    risk_policy = {
        "risk_per_trade_pct": float(_fraction(limits.get("risk_per_trade_pct"), "0.01")),
        "max_position_pct": float(_fraction(limits.get("max_position_pct"), "0.10")),
        "max_gross_exposure_pct": float(
            _fraction(limits.get("max_gross_exposure_pct"), "0.80")
        ),
        "max_daily_loss_pct": float(_fraction(limits.get("max_daily_loss_pct"), "0.03")),
    }
    quality = assess_market_snapshot(
        snapshot,
        now=decision_time,
        max_age_seconds=max_snapshot_age_seconds,
        min_candles=int(limits.get("min_candles", 30)),
        min_completeness=float(limits.get("min_completeness", 0.85)),
    )
    contributions = [
        _technical_contribution(snapshot, technicals),
        _forecast_contribution(snapshot, forecast),
        _news_contribution(news, expected_ticker=requested_ticker),
        _anomaly_contribution(anomalies, expected_ticker=requested_ticker),
    ]
    available_weight = sum(item["weight"] for item in contributions if item["available"])
    for item in contributions:
        item["weighted_score"] = round(
            item["weight"] * item["raw_score"] if item["available"] else 0.0,
            6,
        )
    weighted_sum = sum(item["weighted_score"] for item in contributions)
    ensemble_score = weighted_sum / available_weight if available_weight else 0.0
    ensemble_score = round(_clamp(ensemble_score), 6)

    vetoes = [dict(issue) for issue in quality["errors"]]
    minimum_evidence_weight = float(limits.get("minimum_evidence_weight", 0.65))
    if available_weight < minimum_evidence_weight:
        vetoes.append(
            {
                "code": "INSUFFICIENT_EVIDENCE",
                "message": (
                    f"Available evidence weight {available_weight:.2f} is below "
                    f"the {minimum_evidence_weight:.2f} policy threshold."
                ),
            }
        )
    anomaly_signal = next(item for item in contributions if item["name"] == "anomalies")
    max_anomaly = anomaly_signal["evidence"]["max_severity"]
    if anomaly_signal["available"] and max_anomaly >= float(
        limits.get("severe_anomaly_threshold", 4.0)
    ):
        vetoes.append(
            {
                "code": "SEVERE_ANOMALY",
                "message": "A severe market anomaly requires review before increasing exposure.",
            }
        )

    price = _decimal(snapshot.get("price"), _ZERO) or _ZERO
    current_quantity = _decimal((account or {}).get("current_position_quantity"), _ZERO) or _ZERO
    current_position_value = _decimal((account or {}).get("current_position_value"))
    if current_position_value is None:
        current_position_value = max(_ZERO, current_quantity * price)
    has_position = current_quantity > 0 or current_position_value > 0

    if account is not None:
        if bool(account.get("kill_switch", False)):
            vetoes.append({"code": "KILL_SWITCH", "message": "Account kill switch is active."})
        if account.get("trading_enabled") is False:
            vetoes.append({"code": "TRADING_DISABLED", "message": "Trading is disabled for this account."})
        equity = _decimal(account.get("equity"), _ZERO) or _ZERO
        daily_pnl = _decimal(account.get("daily_pnl"), _ZERO) or _ZERO
        max_daily_loss = equity * _fraction(limits.get("max_daily_loss_pct"), "0.03")
        if equity <= 0:
            vetoes.append({"code": "INVALID_EQUITY", "message": "Account equity must be positive."})
        if daily_pnl < -max_daily_loss:
            vetoes.append(
                {
                    "code": "DAILY_LOSS_LIMIT",
                    "message": "Account daily loss limit has been reached.",
                }
            )
        account_currency = str(account.get("currency") or "").strip().upper()
        quote_currency = str(quality["canonical"].get("currency") or "").strip().upper()
        if not account_currency or quote_currency != account_currency:
            vetoes.append(
                {
                    "code": "CURRENCY_MISMATCH",
                    "message": "Account currency must match the trusted market quote currency.",
                }
            )

    buy_threshold = float(limits.get("buy_threshold", 0.25))
    reduce_threshold = float(limits.get("reduce_threshold", -0.20))
    quality_veto = not quality["execution_eligible"]
    evidence_veto = any(
        veto["code"] in {"INSUFFICIENT_EVIDENCE", "SEVERE_ANOMALY"} for veto in vetoes
    )
    if quality_veto or evidence_veto:
        action = "OBSERVE"
    elif has_position and ensemble_score <= reduce_threshold:
        action = "REDUCE"
    elif ensemble_score >= buy_threshold:
        action = "BUY"
    else:
        action = "HOLD"

    if (
        action == "BUY"
        and account is not None
        and account.get("portfolio_marks_execution_eligible") is not True
    ):
        vetoes.append(
            {
                "code": "UNSAFE_PORTFOLIO_MARKS",
                "message": "Fresh, execution-quality marks are required before increasing exposure.",
            }
        )
        action = "OBSERVE"

    sizing = _empty_sizing(price)
    if action == "BUY":
        if account is None:
            vetoes.append(
                {
                    "code": "MISSING_ACCOUNT_CONTEXT",
                    "message": "Position sizing requires account equity and exposure.",
                }
            )
            action = "OBSERVE"
        elif any(
            veto["code"]
            in {
                "KILL_SWITCH",
                "TRADING_DISABLED",
                "DAILY_LOSS_LIMIT",
                "INVALID_EQUITY",
                "CURRENCY_MISMATCH",
            }
            for veto in vetoes
        ):
            action = "OBSERVE"
        else:
            sizing = _size_buy(price, snapshot, technicals, account, limits)
            if _decimal(sizing["quantity"], _ZERO) == 0:
                vetoes.append(
                    {
                        "code": "NO_RISK_CAPACITY",
                        "message": "Risk, cash or exposure limits leave no capacity for a new position.",
                    }
                )
                action = "OBSERVE"
                sizing = _empty_sizing(price)
    elif action == "REDUCE":
        step = _decimal(limits.get("quantity_step"), Decimal("1")) or Decimal("1")
        if not bool(limits.get("allow_fractional", False)):
            step = max(Decimal("1"), step)
        reduce_fraction = _fraction(limits.get("reduce_fraction"), "0.50")
        reduce_quantity = _quantity(current_quantity * reduce_fraction, step)
        if reduce_quantity <= 0 and current_quantity > 0:
            reduce_quantity = _quantity(current_quantity, step)
        sizing = {
            **_empty_sizing(price),
            "quantity": _qstr(reduce_quantity),
            "notional": _money(reduce_quantity * price),
            "calculation": "Reduce the existing long position; never open a short position.",
        }

    quality_factor = float(quality["quality_score"])
    directional_conviction = abs(ensemble_score)
    confidence = (0.35 + 0.65 * directional_conviction) * available_weight * quality_factor
    confidence = round(_clamp(confidence, 0.0, 1.0), 6)
    execution_eligible = (
        action in {"BUY", "REDUCE"}
        and quality["execution_eligible"]
        and not vetoes
        and (_decimal(sizing.get("quantity"), _ZERO) or _ZERO) > 0
    )

    evidence = [
        {
            "kind": "market_snapshot",
            "snapshot_id": quality["snapshot_id"],
            "ticker": requested_ticker,
            "source": quality["provenance"]["source"],
            "as_of": quality["canonical"]["as_of"],
            "quality_status": quality["status"],
        }
    ]
    evidence.extend(
        {
            "kind": item["name"],
            "available": item["available"],
            "evidence": item["evidence"],
        }
        for item in contributions
    )
    fingerprint = {
        "version": DECISION_VERSION,
        "snapshot_id": quality["snapshot_id"],
        "ticker": requested_ticker,
        "score": ensemble_score,
        "confidence": confidence,
        "action": action,
        "vetoes": [item["code"] for item in vetoes],
        "contributions": [
            {
                "name": item["name"],
                "available": item["available"],
                "weighted_score": item["weighted_score"],
            }
            for item in contributions
        ],
        "position_size": sizing,
        "risk_policy": risk_policy,
        "account_state": {
            key: (account or {}).get(key)
            for key in (
                "currency",
                "equity",
                "cash",
                "gross_exposure",
                "current_position_quantity",
                "current_position_value",
                "daily_pnl",
                "trading_enabled",
                "kill_switch",
                "portfolio_marks_execution_eligible",
            )
        },
    }
    decision_id = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:24]
    return {
        "decision_id": decision_id,
        "version": DECISION_VERSION,
        "created_at": decision_time.isoformat(),
        "ticker": requested_ticker,
        "action": action,
        "long_only": True,
        "proposal_only": True,
        "execution_performed": False,
        "execution_eligible": execution_eligible,
        "score": ensemble_score,
        "confidence": confidence,
        "available_evidence_weight": round(available_weight, 6),
        "contributions": contributions,
        "vetoes": vetoes,
        "position_size": sizing,
        "risk_policy": risk_policy,
        "market_quality": quality,
        "evidence": evidence,
    }


def _target_exposures(values: Sequence[Any]) -> np.ndarray:
    exposures: list[float] = []
    current = 0.0
    for value in values:
        if isinstance(value, str):
            action = value.strip().upper()
            if action == "BUY":
                current = 1.0
            elif action == "REDUCE":
                current = 0.0
            elif action not in {"HOLD", "OBSERVE"}:
                raise ValueError(f"unsupported long-only action: {value}")
        else:
            numeric = _number(value)
            if numeric is None or numeric < 0 or numeric > 1:
                raise ValueError("numeric signals must be finite target exposures between zero and one")
            current = numeric
        exposures.append(current)
    return np.asarray(exposures, dtype=float)


def _safe_ratio(mean: float, denominator: float, annualization: int) -> float:
    if not math.isfinite(denominator) or denominator <= 1e-15:
        return 0.0
    return math.sqrt(annualization) * mean / denominator


def _cagr(final_value: float, initial_value: float, years: float) -> float:
    if initial_value <= 0 or final_value <= 0 or years <= 0:
        return -1.0 if final_value <= 0 else 0.0
    return (final_value / initial_value) ** (1.0 / years) - 1.0


def run_strategy_backtest(
    data: pd.DataFrame | Sequence[Mapping[str, Any]],
    *,
    signal_col: str = "signal",
    price_col: str = "close",
    date_col: str = "date",
    initial_capital: float = 10_000.0,
    transaction_cost_bps: float = 10.0,
    slippage_bps: float = 5.0,
    annualization: int = 252,
    cost_bps: float | None = None,
) -> dict[str, Any]:
    """Backtest long-only target exposures without look-ahead leakage.

    Row ``t``'s signal is applied to the interval from close ``t`` to close
    ``t+1``.  The final row therefore supplies an ending price but contributes
    no decision period.  Costs and slippage are charged on absolute changes in
    target exposure.  String actions follow state semantics: BUY targets 100%,
    REDUCE targets 0%, while HOLD/OBSERVE leave the prior exposure unchanged.
    """

    frame = data.copy(deep=True) if isinstance(data, pd.DataFrame) else pd.DataFrame(list(data))
    if len(frame) < 2:
        raise ValueError("backtest needs at least two chronologically ordered rows")
    if signal_col not in frame or price_col not in frame:
        raise ValueError(f"data must contain {signal_col!r} and {price_col!r}")
    if initial_capital <= 0:
        raise ValueError("initial_capital must be positive")
    if annualization <= 0:
        raise ValueError("annualization must be positive")
    effective_cost_bps = transaction_cost_bps if cost_bps is None else cost_bps
    if not 0 <= effective_cost_bps < 10_000 or not 0 <= slippage_bps < 10_000:
        raise ValueError("cost and slippage basis points must be in [0, 10000)")

    if date_col in frame:
        frame[date_col] = pd.to_datetime(frame[date_col], utc=True, errors="raise")
        frame = frame.sort_values(date_col, kind="stable").reset_index(drop=True)
        if frame[date_col].duplicated().any():
            raise ValueError("backtest dates must be unique")
    prices = pd.to_numeric(frame[price_col], errors="coerce").to_numpy(dtype=float)
    if not np.isfinite(prices).all() or (prices <= 0).any():
        raise ValueError("prices must be finite and positive")
    target = _target_exposures(frame[signal_col].tolist())

    # Signal at row t earns only the subsequently observed t -> t+1 return.
    forward_returns = prices[1:] / prices[:-1] - 1.0
    positions = target[:-1]
    previous_positions = np.concatenate(([0.0], positions[:-1]))
    turnover = np.abs(positions - previous_positions)
    cost_rate = (float(effective_cost_bps) + float(slippage_bps)) / 10_000.0
    costs = turnover * cost_rate
    gross_returns = positions * forward_returns
    net_returns = gross_returns - costs

    growth = np.cumprod(1.0 + net_returns)
    benchmark_growth = np.cumprod(1.0 + forward_returns)
    equity = float(initial_capital) * growth
    benchmark_equity = float(initial_capital) * benchmark_growth
    running_peak = np.maximum.accumulate(np.concatenate(([float(initial_capital)], equity)))
    drawdowns = np.concatenate(([float(initial_capital)], equity)) / running_peak - 1.0

    periods = len(net_returns)
    if date_col in frame:
        elapsed_days = max(1.0, (frame[date_col].iloc[-1] - frame[date_col].iloc[0]).total_seconds() / 86400)
        years = elapsed_days / 365.25
    else:
        years = periods / annualization
    final_equity = float(equity[-1])
    final_benchmark = float(benchmark_equity[-1])
    total_return = final_equity / float(initial_capital) - 1.0
    benchmark_return = final_benchmark / float(initial_capital) - 1.0
    mean_return = float(np.mean(net_returns))
    std_return = float(np.std(net_returns, ddof=1)) if periods > 1 else 0.0
    downside = np.minimum(net_returns, 0.0)
    downside_deviation = float(np.sqrt(np.mean(np.square(downside)))) if periods else 0.0
    active_returns = net_returns[positions > 0]
    win_rate = float(np.mean(active_returns > 0)) if len(active_returns) else 0.0
    trade_count = int(np.count_nonzero(turnover > 1e-12))

    if date_col in frame:
        period_dates = [value.isoformat() for value in frame[date_col].iloc[:-1]]
        next_dates = [value.isoformat() for value in frame[date_col].iloc[1:]]
    else:
        period_dates = list(range(periods))
        next_dates = list(range(1, periods + 1))
    curve = [
        {
            "date": period_dates[i],
            "next_date": next_dates[i],
            "target_exposure": round(float(positions[i]), 8),
            "next_return": round(float(forward_returns[i]), 10),
            "turnover": round(float(turnover[i]), 8),
            "cost": round(float(costs[i]), 10),
            "gross_return": round(float(gross_returns[i]), 10),
            "net_return": round(float(net_returns[i]), 10),
            "equity": round(float(equity[i]), 6),
            "benchmark_equity": round(float(benchmark_equity[i]), 6),
        }
        for i in range(periods)
    ]
    return {
        "version": BACKTEST_VERSION,
        "alignment": "signal_at_t_applied_to_return_t_plus_1",
        "periods": periods,
        "initial_capital": round(float(initial_capital), 6),
        "final_equity": round(final_equity, 6),
        "metrics": {
            "total_return": round(total_return, 10),
            "total_return_pct": round(total_return * 100, 6),
            "cagr": round(_cagr(final_equity, float(initial_capital), years), 10),
            "cagr_pct": round(_cagr(final_equity, float(initial_capital), years) * 100, 6),
            "sharpe": round(_safe_ratio(mean_return, std_return, annualization), 8),
            "sortino": round(_safe_ratio(mean_return, downside_deviation, annualization), 8),
            "max_drawdown": round(float(np.min(drawdowns)), 10),
            "max_drawdown_pct": round(float(np.min(drawdowns)) * 100, 6),
            "win_rate": round(win_rate, 8),
            "turnover": round(float(np.sum(turnover)), 8),
            "trades": trade_count,
        },
        "benchmark": {
            "final_equity": round(final_benchmark, 6),
            "total_return": round(benchmark_return, 10),
            "total_return_pct": round(benchmark_return * 100, 6),
            "cagr": round(_cagr(final_benchmark, float(initial_capital), years), 10),
            "cagr_pct": round(_cagr(final_benchmark, float(initial_capital), years) * 100, 6),
            "excess_total_return": round(total_return - benchmark_return, 10),
        },
        "costs": {
            "transaction_cost_bps": float(effective_cost_bps),
            "slippage_bps": float(slippage_bps),
            "total_cost_fraction": round(float(np.sum(costs)), 10),
        },
        "equity_curve": curve,
    }
