"""Safe forecast-training CLI replacing the former XGBoost/RNN all-in demo."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.data.loader import deterministic_price_frame, fetch_stock_data, load_price_csv
from src.train_model.train_forecaster import train_forecaster


def train(
    ticker: str = "AAPL",
    period: str = "5y",
    smoke_test: bool = False,
    *,
    input_path: str | Path | None = None,
    output: str | Path | None = None,
    max_horizon: int = 10,
    purge_gap: int | None = None,
    max_iter: int = 180,
):
    """Train from an explicit CSV, an explicit smoke frame, or a requested feed."""

    symbol = ticker.strip().upper()
    if not symbol:
        raise ValueError("ticker is required")
    if smoke_test and input_path is not None:
        raise ValueError("--smoke-test and --input are mutually exclusive")
    if smoke_test:
        prices = deterministic_price_frame(240, ticker=symbol)
        source = "deterministic-smoke-test"
        max_horizon = min(max_horizon, 3)
        max_iter = min(max_iter, 30)
    elif input_path is not None:
        path = Path(input_path)
        prices = load_price_csv(path, min_rows=100)
        source = f"csv:{path.name}"
    else:
        prices = fetch_stock_data(symbol, period, min_rows=120)
        source = f"yfinance:{period}"

    destination = (
        Path(output)
        if output is not None
        else PROJECT_ROOT
        / "training"
        / "outputs"
        / "forecasters"
        / f"{symbol.replace('.', '_')}.joblib"
    )
    result = train_forecaster(
        prices,
        ticker=symbol,
        output=destination,
        max_horizon=max_horizon,
        purge_gap=purge_gap,
        max_iter=max_iter,
        data_source=source,
    )
    return result, result["manifest"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticker", default="AAPL")
    parser.add_argument("--period", default="5y")
    parser.add_argument(
        "--input",
        type=Path,
        help="Explicit OHLCV CSV; recommended for reproducibility",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-horizon", type=int, default=10)
    parser.add_argument("--purge-gap", type=int)
    parser.add_argument("--max-iter", type=int, default=180)
    parser.add_argument(
        "--smoke-test",
        action="store_true",
        help="Use deterministic generated data; never for release artifacts",
    )
    args = parser.parse_args(argv)
    _, manifest = train(
        args.ticker,
        args.period,
        args.smoke_test,
        input_path=args.input,
        output=args.output,
        max_horizon=args.max_horizon,
        purge_gap=args.purge_gap,
        max_iter=args.max_iter,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
