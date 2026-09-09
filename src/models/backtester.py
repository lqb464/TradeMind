"""Compatibility wrapper around TradeMind's leakage-safe backtest kernel."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from backend.src.trading import run_strategy_backtest


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
