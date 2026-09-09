"""Trust-aware market data and explainable ML services for TradeMind.

Synthetic data keeps the local interface usable, but every response carries
provenance and an execution eligibility flag. Nothing in this module executes
orders; deterministic risk controls live in the trading layer.
"""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from backend.src.cache import cache
from backend.src.market_quality import market_age_limits

ROOT = Path(__file__).resolve().parents[2]
FORECAST_MODEL_PATH = ROOT / "training" / "outputs" / "quantile_forecaster.joblib"
FORECAST_MODEL_DIR = ROOT / "training" / "outputs" / "forecasters"
SENTIMENT_MODEL_PATH = ROOT / "training" / "outputs" / "sentiment" / "baseline.joblib"
_SENTIMENT_BUNDLE: dict[str, Any] | None = None

PERIOD_DAYS = {"3mo": 70, "6mo": 140, "1y": 270, "2y": 530, "5y": 1260}


def _synthetic(ticker: str, days: int = 260) -> pd.DataFrame:
    """Return deterministic preview data which must never be traded."""
    seed = int(hashlib.sha256(ticker.encode()).hexdigest()[:8], 16)
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(
        end=pd.Timestamp.now(tz="UTC").normalize() - pd.offsets.BDay(1), periods=days
    )
    start = 35 + seed % 180
    returns = rng.normal(0.0006, 0.018, days)
    close = start * np.exp(np.cumsum(returns))
    open_ = close * (1 + rng.normal(0, 0.004, days))
    spread = np.abs(rng.normal(0.009, 0.004, days))
    volume = rng.lognormal(16.2, 0.35, days)
    if days >= 18:
        volume[-18] *= 3.4
        close[-18] *= 1.075
    frame = pd.DataFrame(
        {
            "date": dates,
            "open": open_,
            "high": np.maximum(open_, close) * (1 + spread),
            "low": np.minimum(open_, close) * (1 - spread),
            "close": close,
            "volume": volume,
        }
    )
    frame.attrs["fallback_reason"] = "external market provider unavailable"
    frame.attrs["currency"] = "VND" if ticker.upper().endswith(".VN") else "USD"
    frame.attrs["exchange"] = "DEMO"
    frame.attrs["instrument_type"] = "INDEX" if ticker.startswith("^") else "EQUITY"
    frame.attrs["tradable"] = False
    return frame


def _history(ticker: str, period: str = "1y") -> tuple[pd.DataFrame, str]:
    if period not in PERIOD_DAYS:
        raise ValueError(f"Unsupported period: {period}")
    try:
        import yfinance as yf

        instrument = yf.Ticker(ticker)
        raw = instrument.history(period=period, auto_adjust=True, timeout=4)
        if len(raw) < 40:
            raise ValueError("insufficient market history")
        raw = raw.reset_index().rename(columns=str.lower)
        raw["date"] = pd.to_datetime(raw["date"], utc=True)
        frame = raw[["date", "open", "high", "low", "close", "volume"]].copy()
        try:
            metadata = dict(instrument.history_metadata or {})
        except Exception:
            metadata = {}
        try:
            currency = str(
                metadata.get("currency") or instrument.fast_info.get("currency") or ""
            ).strip().upper()
        except Exception:
            currency = str(metadata.get("currency") or "").strip().upper()
        instrument_type = str(
            metadata.get("instrumentType") or metadata.get("quoteType") or ""
        ).strip().upper()
        exchange = str(
            metadata.get("exchangeName") or metadata.get("exchange") or ""
        ).strip().upper()
        frame.attrs["currency"] = currency if re.fullmatch(r"[A-Z]{3}", currency) else None
        frame.attrs["exchange"] = exchange or None
        frame.attrs["instrument_type"] = instrument_type or None
        frame.attrs["tradable"] = instrument_type in {"EQUITY", "ETF"} and not ticker.startswith("^")
        frame.attrs["fallback_reason"] = None
        return frame, "yfinance"
    except Exception as exc:
        frame = _synthetic(ticker, PERIOD_DAYS[period])
        frame.attrs["fallback_reason"] = f"{type(exc).__name__}: market provider failed"
        return frame, "deterministic-demo"


def _indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ma20"] = out.close.rolling(20).mean()
    out["ma50"] = out.close.rolling(50).mean()
    delta = out.close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    out["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))
    out["macd"] = out.close.ewm(span=12, adjust=False).mean() - out.close.ewm(
        span=26, adjust=False
    ).mean()
    out["macd_signal"] = out.macd.ewm(span=9, adjust=False).mean()
    previous_close = out.close.shift(1)
    true_range = pd.concat(
        [
            out.high - out.low,
            (out.high - previous_close).abs(),
            (out.low - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    out["atr14"] = true_range.rolling(14).mean()
    out["return"] = out.close.pct_change()
    out["volatility20"] = out["return"].rolling(20).std() * math.sqrt(252)
    out["volume_z"] = (out.volume - out.volume.rolling(30).mean()) / out.volume.rolling(
        30
    ).std().replace(0, np.nan)
    out["return_z"] = (
        out["return"] - out["return"].rolling(30).mean()
    ) / out["return"].rolling(30).std().replace(0, np.nan)
    out.attrs.update(df.attrs)
    return out


def _num(value: float | int | None) -> float | None:
    if value is None or pd.isna(value) or not math.isfinite(float(value)):
        return None
    return round(float(value), 4)


def _quality_meta(frame: pd.DataFrame, source: str) -> dict[str, Any]:
    latest = pd.Timestamp(frame.date.iloc[-1])
    latest = latest.tz_localize("UTC") if latest.tzinfo is None else latest.tz_convert("UTC")
    age_seconds = int((pd.Timestamp.now(tz="UTC") - latest).total_seconds())
    max_age_seconds, future_skew_seconds = market_age_limits()
    is_demo = source != "yfinance"
    is_future_dated = age_seconds < -max(0, future_skew_seconds)
    is_stale = age_seconds > max_age_seconds or max_age_seconds <= 0
    completeness = float(
        frame[["open", "high", "low", "close", "volume"]].notna().mean().mean()
    )
    execution_eligible = (
        not is_demo
        and not is_stale
        and not is_future_dated
        and completeness >= 0.995
        and len(frame) >= 40
        and bool(re.fullmatch(r"[A-Z]{3}", str(frame.attrs.get("currency") or "")))
        and bool(frame.attrs.get("exchange"))
        and frame.attrs.get("instrument_type") in {"EQUITY", "ETF"}
        and frame.attrs.get("tradable") is True
    )
    status = (
        "demo"
        if is_demo
        else "future-dated"
        if is_future_dated
        else "stale"
        if is_stale
        else "live"
    )
    return {
        "source": source,
        "currency": frame.attrs.get("currency"),
        "exchange": frame.attrs.get("exchange"),
        "instrument_type": frame.attrs.get("instrument_type"),
        "tradable": frame.attrs.get("tradable") is True,
        "as_of": latest.isoformat(),
        "status": status,
        "is_demo": is_demo,
        "is_stale": is_stale,
        "is_future_dated": is_future_dated,
        "age_seconds": age_seconds,
        "max_age_seconds": max_age_seconds,
        "bars": len(frame),
        "completeness": round(completeness, 4),
        "execution_eligible": execution_eligible,
        "fallback_reason": frame.attrs.get("fallback_reason"),
        "interval": "1d",
    }


def get_market_snapshot(ticker: str, period: str = "1y") -> dict[str, Any]:
    def build() -> dict[str, Any]:
        frame, source = _history(ticker, period)
        frame = _indicators(frame)
        latest, previous = frame.iloc[-1], frame.iloc[-2]
        candles = [
            {
                "date": row.date.strftime("%Y-%m-%d"),
                "open": _num(row.open),
                "high": _num(row.high),
                "low": _num(row.low),
                "close": _num(row.close),
                "volume": int(row.volume),
                "ma20": _num(row.ma20),
                "ma50": _num(row.ma50),
            }
            for _, row in frame.tail(180).iterrows()
        ]
        change = float(latest.close - previous.close)
        trailing_year = frame.tail(252)
        return {
            "ticker": ticker,
            "currency": frame.attrs.get("currency"),
            "exchange": frame.attrs.get("exchange"),
            "instrument_type": frame.attrs.get("instrument_type"),
            "tradable": frame.attrs.get("tradable") is True,
            "price": round(float(latest.close), 2),
            "change": round(change, 2),
            "change_pct": round(change / float(previous.close) * 100, 2),
            "volume": int(latest.volume),
            "indicators": {
                "rsi": _num(latest.rsi),
                "macd": _num(latest.macd),
                "macd_signal": _num(latest.macd_signal),
                "ma20": _num(latest.ma20),
                "ma50": _num(latest.ma50),
                "atr14": _num(latest.atr14),
                "atr_14": _num(latest.atr14),
                "volatility20": _num(latest.volatility20),
                "high_52w": _num(trailing_year.high.max()),
                "low_52w": _num(trailing_year.low.min()),
            },
            "candles": candles,
            "meta": _quality_meta(frame, source),
        }

    return cache.get_or_set(f"market:v2:{ticker}:{period}", 300, build)


def _ewma_backtest(frame: pd.DataFrame, horizon: int) -> dict[str, Any]:
    """Calculate honest point-in-time calibration for the statistical fallback."""
    prices = frame.close.astype(float).reset_index(drop=True)
    log_returns = np.log(prices / prices.shift(1))
    coverages: list[bool] = []
    errors: list[float] = []
    start = max(80, len(frame) - 140)
    for index in range(start, len(frame) - horizon):
        history = log_returns.iloc[max(1, index - 252) : index + 1].dropna()
        if len(history) < 60:
            continue
        mu = float(history.ewm(span=60).mean().iloc[-1])
        sigma = float(history.ewm(span=60).std().iloc[-1])
        actual = float(np.log(prices.iloc[index + horizon] / prices.iloc[index]))
        median = mu * horizon
        width = 1.2815515655 * sigma * math.sqrt(horizon)
        coverages.append(median - width <= actual <= median + width)
        errors.append(abs(math.exp(median) - math.exp(actual)) * 100)
    if not errors:
        return {
            "status": "not_evaluated",
            "samples": 0,
            "coverage": None,
            "mae_pct": None,
        }
    return {
        "status": "point_in_time",
        "samples": len(errors),
        "coverage": round(float(np.mean(coverages)), 4),
        "mae_pct": round(float(np.mean(errors)), 4),
    }


def forecast_range(ticker: str, horizon: int = 7) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        frame, source = _history(ticker, "2y")
        last = float(frame.close.iloc[-1])
        dates = pd.bdate_range(frame.date.iloc[-1] + pd.Timedelta(days=1), periods=horizon)
        artifact_path = FORECAST_MODEL_DIR / f"{ticker.upper().replace('.', '_')}.joblib"
        if not artifact_path.exists():
            artifact_path = FORECAST_MODEL_PATH
        if artifact_path.exists():
            try:
                import joblib
                from src.train_model.features import latest_feature_row

                artifact = joblib.load(artifact_path)
                if (
                    artifact.get("artifact_version") == 1
                    and artifact.get("ticker", "").upper() == ticker.upper()
                    and artifact.get("max_horizon", 0) >= horizon
                ):
                    features = latest_feature_row(frame)
                    points = []
                    for index, date in enumerate(dates, 1):
                        models = artifact["models"][str(index)]
                        values = sorted(
                            last * np.exp(float(models[quantile].predict(features)[0]))
                            for quantile in ("0.1", "0.5", "0.9")
                        )
                        points.append(
                            {
                                "date": date.strftime("%Y-%m-%d"),
                                "low": round(float(values[0]), 2),
                                "median": round(float(values[1]), 2),
                                "high": round(float(values[2]), 2),
                            }
                        )
                    metric = artifact.get("metrics", {}).get(str(horizon), {})
                    return {
                        "ticker": ticker,
                        "horizon": horizon,
                        "confidence": 0.8,
                        "method": artifact["model_type"],
                        "forecast": points,
                        "backtest": {
                            "status": "artifact_holdout",
                            "coverage": metric.get("interval_coverage"),
                            "mae_log_return": metric.get("mae_log_return"),
                            "samples": metric.get("test_rows"),
                        },
                        "meta": {
                            **_quality_meta(frame, source),
                            "model_artifact": artifact_path.name,
                            "trained_at": artifact.get("trained_at"),
                        },
                    }
            except Exception as exc:
                frame.attrs["model_fallback_reason"] = type(exc).__name__

        returns = np.log(frame.close / frame.close.shift()).dropna().tail(252)
        mu = float(returns.ewm(span=60).mean().iloc[-1])
        sigma = float(returns.ewm(span=60).std().iloc[-1])
        seed = int(hashlib.md5(ticker.encode(), usedforsecurity=False).hexdigest()[:8], 16)
        rng = np.random.default_rng(seed)
        paths = last * np.exp(np.cumsum(rng.normal(mu, sigma, (2500, horizon)), axis=1))
        points = [
            {
                "date": date.strftime("%Y-%m-%d"),
                "low": round(float(np.quantile(paths[:, index], 0.1)), 2),
                "median": round(float(np.quantile(paths[:, index], 0.5)), 2),
                "high": round(float(np.quantile(paths[:, index], 0.9)), 2),
            }
            for index, date in enumerate(dates)
        ]
        return {
            "ticker": ticker,
            "horizon": horizon,
            "confidence": 0.8,
            "method": "EWMA Monte Carlo (2,500 paths) - statistical fallback",
            "forecast": points,
            "backtest": _ewma_backtest(frame, horizon),
            "meta": {
                **_quality_meta(frame, source),
                "model_artifact": None,
                "model_fallback_reason": frame.attrs.get("model_fallback_reason"),
            },
        }

    return cache.get_or_set(f"forecast:v2:{ticker}:{horizon}", 1800, build)


def detect_anomalies(ticker: str) -> dict[str, Any]:
    frame, source = _history(ticker, "1y")
    data = _indicators(frame)
    hits = data[(data.volume_z.abs() >= 2.4) | (data.return_z.abs() >= 2.6)].tail(8)
    items = []
    for _, row in hits.iterrows():
        kind = "Volume spike" if abs(row.volume_z) >= abs(row.return_z) else "Price shock"
        items.append(
            {
                "date": row.date.strftime("%Y-%m-%d"),
                "type": kind,
                "severity": round(float(max(abs(row.volume_z), abs(row.return_z))), 2),
                "change_pct": round(float(row["return"] * 100), 2),
                "explanation": (
                    "Khối lượng/biến động lệch đáng kể khỏi phân phối 30 phiên; "
                    "cần đối chiếu công bố doanh nghiệp và tin tức cùng ngày."
                ),
            }
        )
    return {
        "ticker": ticker,
        "items": items[::-1],
        "method": "rolling z-score",
        "meta": _quality_meta(frame, source),
    }


DEMO_NEWS = [
    ("Ví dụ: kết quả kinh doanh vượt kỳ vọng, biên lợi nhuận cải thiện", 0.72),
    ("Ví dụ: ban lãnh đạo cập nhật kế hoạch tăng trưởng thận trọng", 0.12),
    ("Ví dụ: thị trường chờ dữ liệu lạm phát và quyết định lãi suất", -0.18),
]
POSITIVE = {
    "beat", "growth", "gain", "surge", "record", "profit", "upgrade", "strong",
    "improve", "tăng", "tích", "cực", "lợi", "nhuận",
}
NEGATIVE = {
    "miss", "loss", "drop", "fall", "risk", "downgrade", "weak", "lawsuit",
    "giảm", "rủi", "ro", "thua", "lỗ",
}


def _headline_score(title: str) -> float:
    global _SENTIMENT_BUNDLE
    if SENTIMENT_MODEL_PATH.exists():
        try:
            if _SENTIMENT_BUNDLE is None:
                import joblib

                _SENTIMENT_BUNDLE = joblib.load(SENTIMENT_MODEL_PATH)
            label = str(
                _SENTIMENT_BUNDLE["model"].predict(
                    _SENTIMENT_BUNDLE["vectorizer"].transform([title])
                )[0]
            ).lower()
            return {
                "negative": -1.0,
                "neutral": 0.0,
                "positive": 1.0,
                "-1": -1.0,
                "0": 0.0,
                "1": 1.0,
            }.get(label, 0.0)
        except Exception:
            pass
    words = set(re.findall(r"[\wÀ-ỹ]+", title.lower()))
    return round(max(-1, min(1, (len(words & POSITIVE) - len(words & NEGATIVE)) / 3)), 2)


def get_news_intelligence(ticker: str) -> dict[str, Any]:
    def build() -> dict[str, Any]:
        articles: list[dict[str, Any]] = []
        source = "demo-curated"
        try:
            import yfinance as yf

            for item in (yf.Ticker(ticker).news or [])[:8]:
                content = item.get("content", item)
                title = content.get("title") or item.get("title")
                if not title:
                    continue
                provider = (
                    content.get("provider", {}).get("displayName")
                    or item.get("publisher")
                    or "News"
                )
                articles.append(
                    {
                        "title": title,
                        "score": _headline_score(title),
                        "source": provider,
                        "published_at": content.get("pubDate") or item.get("providerPublishTime"),
                        "is_demo": False,
                    }
                )
            if articles:
                source = "yfinance-news"
        except Exception:
            pass
        if not articles:
            articles = [
                {
                    "title": title,
                    "score": score,
                    "source": "TradeMind example corpus",
                    "published_at": None,
                    "is_demo": True,
                }
                for title, score in DEMO_NEWS
            ]
        sentiment = round(sum(item["score"] for item in articles) / len(articles), 2)
        label = "Tích cực" if sentiment > 0.25 else "Tiêu cực" if sentiment < -0.25 else "Trung tính"
        ranked = sorted(articles, key=lambda item: abs(item["score"]), reverse=True)[:3]
        model_name = (
            "trained TF-IDF classifier"
            if SENTIMENT_MODEL_PATH.exists()
            else "transparent lexical fallback"
        )
        return {
            "ticker": ticker,
            "sentiment": sentiment,
            "label": label,
            "key_impacts": [item["title"] for item in ranked],
            "articles": articles,
            "model": model_name,
            "meta": {
                "source": source,
                "status": "live" if source == "yfinance-news" else "demo",
                "is_demo": source != "yfinance-news",
                "execution_eligible": source == "yfinance-news",
                "cached_seconds": 900,
                "model_artifact": SENTIMENT_MODEL_PATH.name if SENTIMENT_MODEL_PATH.exists() else None,
            },
        }

    return cache.get_or_set(f"news:v2:{ticker}", 900, build)


def analyze_technicals(ticker: str) -> dict[str, Any]:
    market = get_market_snapshot(ticker, "6mo")
    indicators = market["indicators"]
    price = float(market["price"])
    rsi = float(indicators.get("rsi") or 50)
    macd = float(indicators.get("macd") or 0)
    signal_line = float(indicators.get("macd_signal") or 0)
    ma20 = float(indicators.get("ma20") or price)
    ma50 = float(indicators.get("ma50") or price)
    trend_score = int(price > ma20) + int(ma20 > ma50) - int(price < ma20) - int(ma20 < ma50)
    trend = "tăng" if trend_score > 0 else "giảm" if trend_score < 0 else "đi ngang"
    momentum = "tích cực" if macd > signal_line else "suy yếu"
    rsi_zone = "quá mua" if rsi > 70 else "quá bán" if rsi < 30 else "trung tính"
    score = max(-1.0, min(1.0, trend_score * 0.3 + (0.25 if macd > signal_line else -0.25)))
    score += -0.15 if rsi > 75 else 0.15 if rsi < 25 else 0.0
    text = (
        f"{ticker} đang ở xu hướng {trend}: giá đóng cửa {price:.2f} so với "
        f"MA20 {ma20:.2f} và MA50 {ma50:.2f}. Động lượng MACD {momentum}, "
        f"trong khi RSI {rsi:.1f} nằm ở vùng {rsi_zone}. Tín hiệu cần được "
        "xác nhận bằng dữ liệu thật, thanh khoản và giới hạn rủi ro."
    )
    label = "BULLISH" if score >= 0.35 else "BEARISH" if score <= -0.35 else "NEUTRAL"
    return {
        "ticker": ticker,
        "signal": label,
        "score": round(score, 4),
        "analysis": text,
        "evidence": indicators,
        "meta": market["meta"],
        "disclaimer": "Phân tích tự động, không phải khuyến nghị đầu tư.",
    }


UNIVERSE = [
    {"ticker": "VCB.VN", "sector": "ngân hàng", "pe": 15.1, "revenue_growth": 18.4, "roe": 20.2},
    {"ticker": "MBB.VN", "sector": "ngân hàng", "pe": 8.7, "revenue_growth": 17.8, "roe": 23.6},
    {"ticker": "TCB.VN", "sector": "ngân hàng", "pe": 9.4, "revenue_growth": 16.3, "roe": 15.7},
    {"ticker": "FPT.VN", "sector": "công nghệ", "pe": 22.6, "revenue_growth": 20.1, "roe": 28.4},
    {"ticker": "HPG.VN", "sector": "thép", "pe": 12.8, "revenue_growth": 14.2, "roe": 11.3},
]


def run_screener(query: str) -> dict[str, Any]:
    normalized = query.lower().replace(",", ".")
    sector = next((value for value in ("ngân hàng", "công nghệ", "thép") if value in normalized), None)
    pe_match = re.search(r"p/?e\s*(?:<|dưới|nhỏ hơn)\s*(\d+(?:\.\d+)?)", normalized)
    growth_match = re.search(
        r"(?:doanh thu|revenue).*?(?:>|trên|lớn hơn)\s*(\d+(?:\.\d+)?)",
        normalized,
    )
    pe_max = float(pe_match.group(1)) if pe_match else None
    growth_min = float(growth_match.group(1)) if growth_match else None
    rows = [
        row
        for row in UNIVERSE
        if (not sector or row["sector"] == sector)
        and (pe_max is None or row["pe"] < pe_max)
        and (growth_min is None or row["revenue_growth"] > growth_min)
    ]
    return {
        "query": query,
        "interpreted_filters": {
            "sector": sector,
            "pe_max": pe_max,
            "revenue_growth_min": growth_min,
        },
        "sql_preview": "SELECT * FROM fundamentals WHERE filters are allow-listed",
        "results": rows,
        "count": len(rows),
        "meta": {
            "source": "embedded-demo-universe",
            "is_demo": True,
            "execution_eligible": False,
        },
    }
