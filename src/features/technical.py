"""Leakage-safe technical features and direct-horizon targets."""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.data.loader import validate_ohlcv


FEATURE_COLUMNS = [
    "return_1d",
    "return_5d",
    "volatility_10",
    "volume_z",
    "rsi_14",
    "macd",
    "macd_signal",
    "price_ma20",
    "price_ma50",
]


def add_technical_indicators(raw: pd.DataFrame) -> pd.DataFrame:
    """Add strictly causal features using information available through row ``t``."""

    frame = validate_ohlcv(raw, min_rows=2)
    close = frame["close"].astype(float)
    volume = frame["volume"].astype(float)
    log_return = np.log(close / close.shift(1))

    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
    relative_strength = gain / loss.replace(0, np.nan)
    ema12 = close.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = close.ewm(span=26, adjust=False, min_periods=26).mean()
    ma20 = close.rolling(20, min_periods=20).mean()
    ma50 = close.rolling(50, min_periods=50).mean()
    volume_mean = volume.rolling(30, min_periods=30).mean()
    volume_std = volume.rolling(30, min_periods=30).std()

    frame["return_1d"] = log_return
    frame["return_5d"] = np.log(close / close.shift(5))
    frame["volatility_10"] = log_return.rolling(10, min_periods=10).std()
    frame["volume_z"] = (volume - volume_mean) / volume_std.replace(0, np.nan)
    frame["rsi_14"] = 100 - 100 / (1 + relative_strength)
    frame["macd"] = ema12 - ema26
    frame["macd_signal"] = frame["macd"].ewm(span=9, adjust=False, min_periods=9).mean()
    frame["price_ma20"] = close / ma20 - 1
    frame["price_ma50"] = close / ma50 - 1

    previous_close = close.shift(1)
    true_range = pd.concat(
        [
            frame["high"] - frame["low"],
            (frame["high"] - previous_close).abs(),
            (frame["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["atr_14"] = true_range.rolling(14, min_periods=14).mean()
    frame["sma_10"] = close.rolling(10, min_periods=10).mean()
    frame["sma_30"] = close.rolling(30, min_periods=30).mean()
    frame["bb_middle"] = ma20
    band_std = close.rolling(20, min_periods=20).std()
    frame["bb_upper"] = ma20 + 2 * band_std
    frame["bb_lower"] = ma20 - 2 * band_std
    frame["log_return"] = log_return
    return frame.replace([np.inf, -np.inf], np.nan)


def generate_targets(
    frame: pd.DataFrame,
    *,
    max_horizon: int = 10,
    drop_incomplete: bool = False,
) -> pd.DataFrame:
    """Attach direct close-to-future-close log-return labels.

    A label at row ``t`` is exactly ``log(close[t+h] / close[t])``.  Targets are
    never used by feature calculations and remain NaN at the unavailable tail.
    """

    if max_horizon < 1:
        raise ValueError("max_horizon must be positive")
    if "close" not in frame:
        raise ValueError("frame must contain close")
    out = frame.copy()
    close = pd.to_numeric(out["close"], errors="raise").astype(float)
    for horizon in range(1, max_horizon + 1):
        out[f"target_return_{horizon}d"] = np.log(close.shift(-horizon) / close)
    if drop_incomplete:
        out = out.dropna(subset=[f"target_return_{max_horizon}d"]).reset_index(
            drop=True
        )
    return out


def build_training_frame(raw: pd.DataFrame, max_horizon: int = 10) -> pd.DataFrame:
    """Build causal features followed by independently computed future labels."""

    return generate_targets(add_technical_indicators(raw), max_horizon=max_horizon)


def latest_feature_row(raw: pd.DataFrame) -> pd.DataFrame:
    """Return the most recent complete feature vector using training column order."""

    features = add_technical_indicators(raw)[FEATURE_COLUMNS].dropna()
    if features.empty:
        raise ValueError("not enough history to build a complete feature row")
    return features.tail(1)
