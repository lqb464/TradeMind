"""Leakage-safe, long-only strategy backtesting for model research."""
from __future__ import annotations
import math
from typing import Any, Mapping, Sequence
import numpy as np
import pandas as pd
BACKTEST_VERSION = "trademind-leakage-safe-backtest-v1"

def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


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

def backtest_predictions(
    frame: pd.DataFrame,
    *,
    prediction_col: str = "pred_log_return",
    price_col: str = "close",
    threshold: float = 0.0,
    max_exposure: float = 0.10,
    initial_capital: float = 10_000.0,
    transaction_cost_bps: float = 10.0,
    slippage_bps: float = 5.0,
) -> dict[str, Any]:
    """Turn predictions at ``t`` into bounded exposure for return ``t+1``."""

    if prediction_col not in frame:
        raise ValueError(f"frame must contain prediction column {prediction_col!r}")
    if not 0 <= max_exposure <= 1:
        raise ValueError("max_exposure must be between zero and one")
    predictions = pd.to_numeric(frame[prediction_col], errors="coerce").to_numpy(
        dtype=float
    )
    if not np.isfinite(predictions).all():
        raise ValueError("predictions must be finite")
    prepared = frame.copy()
    prepared["target_exposure"] = np.where(predictions > threshold, max_exposure, 0.0)
    return run_strategy_backtest(
        prepared,
        signal_col="target_exposure",
        price_col=price_col,
        initial_capital=initial_capital,
        transaction_cost_bps=transaction_cost_bps,
        slippage_bps=slippage_bps,
    )


class TradingBacktester:
    """Legacy class name backed by bounded, next-period execution semantics."""

    def __init__(
        self,
        initial_capital: float = 10_000.0,
        transaction_cost: float = 0.001,
        *,
        slippage: float = 0.0005,
        max_exposure: float = 0.10,
        threshold: float = 0.0,
    ) -> None:
        if transaction_cost < 0 or slippage < 0:
            raise ValueError("costs cannot be negative")
        self.initial_capital = initial_capital
        self.transaction_cost = transaction_cost
        self.slippage = slippage
        self.max_exposure = max_exposure
        self.threshold = threshold

    def run(
        self, frame: pd.DataFrame, pred_col: str = "pred_log_return"
    ) -> dict[str, Any]:
        result = backtest_predictions(
            frame,
            prediction_col=pred_col,
            threshold=self.threshold,
            max_exposure=self.max_exposure,
            initial_capital=self.initial_capital,
            transaction_cost_bps=self.transaction_cost * 10_000,
            slippage_bps=self.slippage * 10_000,
        )
        metrics = result["metrics"]
        # Preserve old report keys while exposing the complete audited result.
        return {
            **result,
            "Total ROI (%)": metrics["total_return_pct"],
            "Sharpe Ratio": metrics["sharpe"],
            "Max Drawdown (%)": metrics["max_drawdown_pct"],
            "Final Portfolio Value": result["final_equity"],
        }
