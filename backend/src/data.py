"""
data.py — Stock price data fetcher (yfinance) & technical feature engineering module.
"""

from pathlib import Path
import numpy as np
import pandas as pd
import yfinance as yf

# Base dir points to project root
BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "training" / "data"
STOCK_CSV_PATH = DATA_DIR / "stock_prices.csv"

FEATURE_NAMES = [
    "open", "high", "low", "close", "volume",
    "SMA_10", "SMA_30", "RSI_14", "daily_return", "volatility_10",
    "lag_1", "lag_2", "lag_5", "lag_10"
]
TARGET_COL = "target_close"


def fetch_stock_data(ticker: str = "AAPL", period: str = "2y") -> pd.DataFrame:
    """Fetch historical stock price data using yfinance API."""
    print(f"[+] Fetching stock data for ticker='{ticker}', period='{period}'...", flush=True)
    stock = yf.Ticker(ticker)
    df = stock.history(period=period)

    if df.empty:
        raise ValueError(f"No stock price data returned for ticker '{ticker}'")

    df = df[["Open", "High", "Low", "Close", "Volume"]].reset_index()
    df.columns = ["date", "open", "high", "low", "close", "volume"]
    df["date"] = pd.to_datetime(df["date"])

    print(f"  Downloaded {len(df)} daily trading records ({df['date'].min().strftime('%Y-%m-%d')} to {df['date'].max().strftime('%Y-%m-%d')})", flush=True)
    return df


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Relative Strength Index (RSI)."""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / (loss + 1e-8)
    return 100 - (100 / (1 + rs))


def add_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add moving averages, RSI, daily returns, volatility, and lag features."""
    df_out = df.copy().sort_values("date").reset_index(drop=True)

    # 1. Moving Averages
    df_out["SMA_10"] = df_out["close"].rolling(window=10).mean()
    df_out["SMA_30"] = df_out["close"].rolling(window=30).mean()

    # 2. RSI Indicator
    df_out["RSI_14"] = calculate_rsi(df_out["close"], period=14)

    # 3. Daily Return & Volatility
    df_out["daily_return"] = df_out["close"].pct_change()
    df_out["volatility_10"] = df_out["daily_return"].rolling(window=10).std()

    # 4. Lag Features (Historical close prices)
    for lag in [1, 2, 5, 10]:
        df_out[f"lag_{lag}"] = df_out["close"].shift(lag)

    # 5. Target Variable: Next Day Close Price
    df_out[TARGET_COL] = df_out["close"].shift(-1)

    # Drop NaNs created by rolling windows & lag features
    df_clean = df_out.dropna().reset_index(drop=True)
    return df_clean


def generate_synthetic_stock_data(n_days: int = 250) -> pd.DataFrame:
    """Generate synthetic stock price time series for testing."""
    np.random.seed(42)
    dates = pd.date_range(end=pd.Timestamp.today(), periods=n_days, freq="B")

    returns = np.random.normal(loc=0.0005, scale=0.015, size=n_days)
    price_path = 180.0 * np.exp(np.cumsum(returns))

    highs = price_path * (1 + np.abs(np.random.normal(0, 0.005, n_days)))
    lows = price_path * (1 - np.abs(np.random.normal(0, 0.005, n_days)))
    opens = price_path + np.random.normal(0, 0.5, n_days)
    volumes = np.random.randint(20_000_000, 80_000_000, size=n_days).astype(float)

    df = pd.DataFrame({
        "date": dates,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": price_path,
        "volume": volumes,
    })
    return df


def load_and_prepare_stock_data(ticker: str = "AAPL", period: str = "2y") -> pd.DataFrame:
    """Fetch or generate stock data and enrich with technical features."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    try:
        df_raw = fetch_stock_data(ticker=ticker, period=period)
    except Exception as e:
        print(f"[!] Warning: yfinance download failed ({e}). Generating synthetic data...", flush=True)
        df_raw = generate_synthetic_stock_data()

    df_featured = add_technical_indicators(df_raw)
    df_featured.to_csv(STOCK_CSV_PATH, index=False)
    print(f"  Processed dataset saved to {STOCK_CSV_PATH} ({len(df_featured)} records)", flush=True)
    return df_featured
