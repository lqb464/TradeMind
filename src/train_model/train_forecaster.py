"""Train direct multi-horizon quantile forecasts with purged validation."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error

from src.data.loader import fetch_stock_data, load_price_csv, validate_ohlcv
from src.train_model.artifacts import save_joblib_artifact
from src.train_model.features import FEATURE_COLUMNS, build_training_frame


# Keep the serialized bundle compatible with backend/src/intelligence.py's
# existing loader contract while versioning the richer sidecar independently.
RUNTIME_ARTIFACT_VERSION = 1
MANIFEST_VERSION = 2
MODEL_TYPE = "direct_quantile_hist_gradient_boosting"
QUANTILES = (0.1, 0.5, 0.9)


def _timestamp(value: datetime | None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).isoformat()


def _data_fingerprint(frame: pd.DataFrame) -> str:
    stable = frame[["date", "open", "high", "low", "close", "volume"]].copy()
    stable["date"] = stable["date"].astype(str)
    return hashlib.sha256(stable.to_csv(index=False).encode("utf-8")).hexdigest()


def train_forecaster(
    prices: pd.DataFrame,
    *,
    ticker: str,
    output: str | Path,
    max_horizon: int = 10,
    purge_gap: int | None = None,
    validation_fraction: float = 0.20,
    random_state: int = 42,
    max_iter: int = 180,
    trained_at: datetime | None = None,
    data_source: str = "explicit-frame",
) -> dict[str, Any]:
    """Train one independent 10/50/90% model per requested horizon."""

    symbol = ticker.strip().upper()
    if not symbol:
        raise ValueError("ticker is required")
    if max_horizon < 1 or max_horizon > 60:
        raise ValueError("max_horizon must be between 1 and 60")
    if not 0.05 <= validation_fraction <= 0.5:
        raise ValueError("validation_fraction must be between 0.05 and 0.5")
    if max_iter < 10:
        raise ValueError("max_iter must be at least 10")
    clean_prices = validate_ohlcv(prices, min_rows=max(100, max_horizon * 8))
    frame = build_training_frame(clean_prices, max_horizon=max_horizon)
    gap = max(max_horizon, purge_gap if purge_gap is not None else max_horizon)
    models: dict[str, dict[str, HistGradientBoostingRegressor]] = {}
    metrics: dict[str, dict[str, Any]] = {}

    for horizon in range(1, max_horizon + 1):
        target = f"target_return_{horizon}d"
        usable = (
            frame[FEATURE_COLUMNS + [target, "date"]]
            .dropna()
            .reset_index(drop=True)
        )
        cut = int(len(usable) * (1 - validation_fraction))
        train_end = cut - gap
        train_rows = usable.iloc[:train_end]
        test_rows = usable.iloc[cut:]
        if len(train_rows) < 60 or len(test_rows) < 15:
            raise ValueError(
                f"horizon {horizon} needs at least 60 train and 15 validation "
                "rows after purge; "
                f"received {len(train_rows)} and {len(test_rows)}"
            )

        horizon_models: dict[str, HistGradientBoostingRegressor] = {}
        prediction_columns: list[np.ndarray] = []
        for quantile in QUANTILES:
            model = HistGradientBoostingRegressor(
                loss="quantile",
                quantile=quantile,
                max_iter=max_iter,
                max_leaf_nodes=15,
                learning_rate=0.04,
                l2_regularization=0.2,
                random_state=random_state,
            )
            model.fit(train_rows[FEATURE_COLUMNS], train_rows[target])
            horizon_models[str(quantile)] = model
            prediction_columns.append(model.predict(test_rows[FEATURE_COLUMNS]))

        # Independent quantile models may cross.  Sort each row for evaluation
        # and let runtime consumers apply the same monotonic normalization.
        low, median, high = np.sort(np.vstack(prediction_columns), axis=0)
        actual = test_rows[target].to_numpy(dtype=float)
        metrics[str(horizon)] = {
            "mae_log_return": round(float(mean_absolute_error(actual, median)), 8),
            "rmse_log_return": round(
                float(mean_squared_error(actual, median) ** 0.5), 8
            ),
            "interval_coverage": round(
                float(np.mean((actual >= low) & (actual <= high))), 6
            ),
            "mean_interval_width": round(float(np.mean(high - low)), 8),
            "directional_accuracy": round(
                float(np.mean(np.sign(actual) == np.sign(median))), 6
            ),
            "train_rows": len(train_rows),
            "validation_rows": len(test_rows),
            "purge_gap": gap,
            "validation_start": test_rows["date"].iloc[0].isoformat(),
            "validation_end": test_rows["date"].iloc[-1].isoformat(),
        }
        models[str(horizon)] = horizon_models

    created_at = _timestamp(trained_at)
    manifest = {
        "version": MANIFEST_VERSION,
        "manifest_version": MANIFEST_VERSION,
        "artifact_version": RUNTIME_ARTIFACT_VERSION,
        "model_type": MODEL_TYPE,
        "ticker": symbol,
        "trained_at": created_at,
        "data_start": clean_prices["date"].iloc[0].isoformat(),
        "data_cutoff": clean_prices["date"].iloc[-1].isoformat(),
        "data_rows": len(clean_prices),
        "data_source": data_source,
        "data_sha256": _data_fingerprint(clean_prices),
        "feature_columns": list(FEATURE_COLUMNS),
        "max_horizon": max_horizon,
        "quantiles": list(QUANTILES),
        "validation": {
            "method": "chronological_holdout_with_purge",
            "fraction": validation_fraction,
            "purge_gap": gap,
        },
        "metrics": metrics,
    }
    artifact = {**manifest, "models": models}
    persisted_manifest = save_joblib_artifact(artifact, output, manifest=manifest)
    return {
        **artifact,
        "manifest": persisted_manifest,
        "artifact_path": str(Path(output)),
    }


def train(
    ticker: str,
    period: str = "5y",
    max_horizon: int = 10,
    output: str | Path | None = None,
    purge_gap: int | None = None,
    *,
    prices: pd.DataFrame | None = None,
    data_source: str | None = None,
    max_iter: int = 180,
) -> dict[str, Any]:
    """Compatibility entry point used by the CLI and existing documentation."""

    symbol = ticker.strip().upper()
    source = data_source or (
        "explicit-frame" if prices is not None else f"yfinance:{period}"
    )
    market = prices if prices is not None else fetch_stock_data(symbol, period)
    destination = (
        Path(output)
        if output
        else Path("training/outputs/forecasters")
        / f"{symbol.replace('.', '_')}.joblib"
    )
    return train_forecaster(
        market,
        ticker=symbol,
        output=destination,
        max_horizon=max_horizon,
        purge_gap=purge_gap,
        max_iter=max_iter,
        data_source=source,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--period", default="5y")
    parser.add_argument(
        "--input", type=Path, help="Explicit OHLCV CSV; avoids network access"
    )
    parser.add_argument("--max-horizon", type=int, default=10)
    parser.add_argument("--purge-gap", type=int)
    parser.add_argument("--max-iter", type=int, default=180)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    prices = load_price_csv(args.input, min_rows=100) if args.input else None
    result = train(
        args.ticker,
        args.period,
        args.max_horizon,
        args.output,
        args.purge_gap,
        prices=prices,
        data_source=f"csv:{args.input.name}" if args.input else None,
        max_iter=args.max_iter,
    )
    print(json.dumps(result["manifest"], ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
