"""Explicit OHLCV loaders used by the training workspace.

Production training must fail when its requested source is unavailable.  The
deterministic generator is intentionally separate and only intended for tests
and the explicitly requested ``--smoke-test`` CLI mode.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pandas as pd


OHLCV_COLUMNS = ("date", "open", "high", "low", "close", "volume")


def validate_ohlcv(frame: pd.DataFrame, *, min_rows: int = 2) -> pd.DataFrame:
    """Return a sorted, UTC-normalized OHLCV frame or raise a clear error."""

    if not isinstance(frame, pd.DataFrame):
        raise TypeError("OHLCV data must be a pandas DataFrame")
    missing = [column for column in OHLCV_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"OHLCV data is missing columns: {', '.join(missing)}")

    out = frame.loc[:, OHLCV_COLUMNS].copy()
    out["date"] = pd.to_datetime(out["date"], utc=True, errors="raise")
    for column in OHLCV_COLUMNS[1:]:
        out[column] = pd.to_numeric(out[column], errors="coerce")
    if out[list(OHLCV_COLUMNS[1:])].isna().any().any():
        raise ValueError("OHLCV data contains non-numeric or missing values")
    if not np.isfinite(out[list(OHLCV_COLUMNS[1:])].to_numpy(dtype=float)).all():
        raise ValueError("OHLCV data contains non-finite values")
    if (out[["open", "high", "low", "close"]] <= 0).any().any():
        raise ValueError("OHLC prices must be positive")
    if (out["volume"] < 0).any():
        raise ValueError("volume must be non-negative")
    if (out["high"] < out[["open", "close", "low"]].max(axis=1)).any():
        raise ValueError("high must be at least open, close and low")
    if (out["low"] > out[["open", "close", "high"]].min(axis=1)).any():
        raise ValueError("low must be at most open, close and high")

    out = out.sort_values("date", kind="stable").reset_index(drop=True)
    if out["date"].duplicated().any():
        raise ValueError("OHLCV timestamps must be unique")
    if len(out) < min_rows:
        raise ValueError(
            f"OHLCV data needs at least {min_rows} rows; received {len(out)}"
        )
    return out


def load_price_csv(path: str | Path, *, min_rows: int = 2) -> pd.DataFrame:
    """Load an explicit local CSV without performing any network fallback."""

    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"price CSV not found: {source}")
    return validate_ohlcv(pd.read_csv(source), min_rows=min_rows)


def fetch_stock_data(
    ticker: str, period: str = "5y", *, min_rows: int = 120
) -> pd.DataFrame:
    """Fetch adjusted daily bars from yfinance and fail closed on provider errors."""

    symbol = ticker.strip().upper()
    if not symbol:
        raise ValueError("ticker is required")
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - depends on optional runtime install
        raise RuntimeError(
            "yfinance is required for live data; install the project dependencies"
        ) from exc
    try:
        raw = yf.Ticker(symbol).history(period=period, auto_adjust=True, timeout=15)
    except Exception as exc:  # provider errors must not become synthetic training data
        raise RuntimeError(f"market-data download failed for {symbol}: {exc}") from exc
    if raw is None or raw.empty:
        raise RuntimeError(f"market-data provider returned no rows for {symbol}")
    normalized = raw.reset_index().rename(columns=str.lower)
    return validate_ohlcv(normalized, min_rows=min_rows)


def deterministic_price_frame(
    rows: int = 360,
    *,
    ticker: str = "SMOKE",
    start: str = "2020-01-02",
) -> pd.DataFrame:
    """Create reproducible OHLCV exclusively for tests and explicit smoke runs."""

    if rows < 80:
        raise ValueError("deterministic smoke data needs at least 80 rows")
    seed = int(hashlib.sha256(ticker.upper().encode("utf-8")).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    returns = rng.normal(0.00035, 0.012, rows)
    close = 100.0 * np.exp(np.cumsum(returns))
    open_ = close * (1 + rng.normal(0, 0.0025, rows))
    spread = np.abs(rng.normal(0.006, 0.0015, rows))
    high = np.maximum(open_, close) * (1 + spread)
    low = np.minimum(open_, close) * (1 - spread)
    volume = rng.lognormal(mean=14.5, sigma=0.25, size=rows)
    frame = pd.DataFrame(
        {
            "date": pd.bdate_range(start=start, periods=rows, tz="UTC"),
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )
    return validate_ohlcv(frame, min_rows=rows)
