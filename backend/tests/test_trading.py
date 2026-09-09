from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd

from backend.src.trading import build_trade_decision, run_strategy_backtest


NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def _snapshot(*, source: str = "primary-market-feed", is_demo: bool = False, age_hours: int = 1):
    candles = []
    for index in range(40):
        close = 92.0 + index * 0.2
        candles.append(
            {
                "date": (NOW - timedelta(days=40 - index)).date().isoformat(),
                "open": close - 0.2,
                "high": close + 0.8,
                "low": close - 0.8,
                "close": close,
                "volume": 1_000_000 + index * 1_000,
            }
        )
    return {
        "ticker": "TEST",
        "price": 100.0,
        "volume": 1_500_000,
        "indicators": {
            "rsi": 60.0,
            "macd": 1.2,
            "macd_signal": 0.6,
            "ma20": 98.0,
            "ma50": 95.0,
            "atr_14": 2.0,
        },
        "candles": candles,
        "meta": {
            "source": source,
            "as_of": (NOW - timedelta(hours=age_hours)).isoformat(),
            "is_demo": is_demo,
            "execution_eligible": not is_demo,
            "currency": "USD",
            "exchange": "TEST",
            "instrument_type": "EQUITY",
            "tradable": True,
        },
    }


def _bundle():
    return {
        "technicals": {
            "ticker": "TEST",
            "signal": "BULLISH",
            "evidence": {
                "rsi": 60.0,
                "macd": 1.2,
                "macd_signal": 0.6,
                "ma20": 98.0,
                "ma50": 95.0,
                "atr_14": 2.0,
            },
            "meta": {
                "source": "primary-market-feed",
                "execution_eligible": True,
            },
        },
        "forecast": {
            "ticker": "TEST",
            "confidence": 0.8,
            "method": "offline-test-model",
            "forecast": [{"low": 101.0, "median": 105.0, "high": 109.0}],
            "meta": {
                "source": "primary-market-feed",
                "execution_eligible": True,
                "model_artifact": "test-v1",
            },
        },
        "news": {
            "ticker": "TEST",
            "sentiment": 0.55,
            "label": "positive",
            "key_impacts": ["earnings improved"],
            "meta": {"source": "licensed-news-feed", "execution_eligible": True},
        },
        "anomalies": {
            "ticker": "TEST",
            "method": "z-score",
            "items": [],
            "meta": {"source": "primary-market-feed", "execution_eligible": True},
        },
    }


def _account():
    return {
        "equity": "100000.00",
        "currency": "USD",
        "cash": "50000.00",
        "gross_exposure": "0.00",
        "current_position_quantity": "0",
        "daily_pnl": "0.00",
        "trading_enabled": True,
        "portfolio_marks_execution_eligible": True,
    }


def _decision(snapshot=None, **overrides):
    inputs = _bundle()
    account = overrides.pop("account", _account())
    inputs.update(overrides)
    return build_trade_decision(
        snapshot=snapshot or _snapshot(),
        account=account,
        now=NOW,
        risk_limits={
            "risk_per_trade_pct": "0.01",
            "max_position_pct": "0.10",
            "max_gross_exposure_pct": "0.50",
            "min_stop_pct": "0.02",
            "max_stop_pct": "0.10",
        },
        **inputs,
    )


def test_demo_snapshot_is_always_observe_and_ineligible():
    result = _decision(_snapshot(source="deterministic-demo", is_demo=True))

    assert result["action"] == "OBSERVE"
    assert result["execution_eligible"] is False
    assert result["execution_performed"] is False
    assert "DEMO_DATA" in {item["code"] for item in result["vetoes"]}


def test_stale_snapshot_vetoes_a_bullish_signal():
    result = _decision(_snapshot(age_hours=100))

    assert result["score"] > 0
    assert result["action"] == "OBSERVE"
    assert result["execution_eligible"] is False
    assert "STALE_DATA" in {item["code"] for item in result["vetoes"]}


def test_decision_uses_configured_snapshot_age(monkeypatch):
    monkeypatch.setenv("MAX_MARKET_SNAPSHOT_AGE_SECONDS", "1800")

    result = _decision(_snapshot(age_hours=1))

    assert result["action"] == "OBSERVE"
    assert "STALE_DATA" in {item["code"] for item in result["vetoes"]}


def test_upstream_ineligible_snapshot_and_demo_auxiliary_are_not_trusted():
    snapshot = _snapshot()
    snapshot["meta"]["execution_eligible"] = False
    blocked = _decision(snapshot)
    assert blocked["execution_eligible"] is False
    assert "UPSTREAM_INELIGIBLE" in {item["code"] for item in blocked["vetoes"]}

    demo_forecast = _bundle()["forecast"]
    demo_forecast["meta"].update(
        source="deterministic-demo", is_demo=True, execution_eligible=False
    )
    decision = _decision(forecast=demo_forecast)
    forecast_signal = next(
        item for item in decision["contributions"] if item["name"] == "forecast"
    )
    assert forecast_signal["available"] is False
    assert forecast_signal["weighted_score"] == 0

    demo_anomalies = _bundle()["anomalies"]
    demo_anomalies["items"] = [{"severity": 5.0, "change_pct": -12.0}]
    demo_anomalies["meta"].update(
        source="deterministic-demo", is_demo=True, execution_eligible=False
    )
    anomaly_decision = _decision(anomalies=demo_anomalies)
    assert "SEVERE_ANOMALY" not in {
        item["code"] for item in anomaly_decision["vetoes"]
    }


def test_non_tradable_instrument_is_never_execution_eligible():
    snapshot = _snapshot()
    snapshot["meta"].update(instrument_type="INDEX", tradable=False)
    decision = _decision(snapshot)
    assert decision["action"] == "OBSERVE"
    assert "UNSUPPORTED_INSTRUMENT" in {item["code"] for item in decision["vetoes"]}


def test_currency_mismatch_vetoes_before_position_sizing():
    account = _account()
    account["currency"] = "VND"

    decision = _decision(account=account)

    assert decision["action"] == "OBSERVE"
    assert decision["position_size"]["quantity"] == "0"
    assert "CURRENCY_MISMATCH" in {item["code"] for item in decision["vetoes"]}


def test_position_size_respects_risk_position_cash_and_exposure_bounds():
    result = _decision()
    sizing = result["position_size"]
    quantity = Decimal(sizing["quantity"])
    notional = Decimal(sizing["notional"])
    estimated_risk = Decimal(sizing["estimated_risk"])

    assert result["action"] == "BUY"
    assert result["proposal_only"] is True
    assert quantity == quantity.to_integral_value()
    assert Decimal("0") < notional <= Decimal("10000.00")
    assert estimated_risk <= Decimal("1000.00")
    assert sizing["binding_constraint"] in {
        "risk_budget",
        "position_limit",
        "portfolio_exposure",
        "cash",
        "order_notional",
    }


def test_backtest_uses_signal_at_t_only_for_next_return():
    # The only large move occurs after a flat signal.  A leaky/same-row
    # implementation would incorrectly capture the 100% jump.
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC"),
            "close": [100.0, 200.0, 200.0],
            "signal": [0.0, 1.0, 1.0],
        }
    )
    result = run_strategy_backtest(
        frame,
        transaction_cost_bps=0,
        slippage_bps=0,
    )

    assert result["alignment"] == "signal_at_t_applied_to_return_t_plus_1"
    assert result["metrics"]["total_return"] == 0
    assert result["benchmark"]["total_return"] == 1
    assert result["equity_curve"][0]["target_exposure"] == 0
    assert result["equity_curve"][0]["next_return"] == 1


def test_backtest_metrics_are_derived_and_sane():
    frame = pd.DataFrame(
        {
            "date": pd.bdate_range("2025-01-01", periods=7, tz="UTC"),
            "close": [100.0, 102.0, 101.0, 104.0, 106.0, 105.0, 109.0],
            "signal": [1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0],
        }
    )
    free = run_strategy_backtest(frame, transaction_cost_bps=0, slippage_bps=0)
    costly = run_strategy_backtest(frame, transaction_cost_bps=20, slippage_bps=10)

    metrics = free["metrics"]
    assert free["periods"] == len(frame) - 1
    assert -1 <= metrics["max_drawdown"] <= 0
    assert 0 <= metrics["win_rate"] <= 1
    assert metrics["turnover"] >= 1
    assert metrics["trades"] >= 1
    assert all(math_value == math_value for math_value in (metrics["sharpe"], metrics["sortino"]))
    assert costly["final_equity"] < free["final_equity"]
