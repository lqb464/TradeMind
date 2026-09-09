from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from scripts import train as training_cli
from src.data.loader import deterministic_price_frame
from src.models.backtester import TradingBacktester
from src.train_model.artifacts import load_joblib_artifact, verify_artifact
from src.train_model.features import FEATURE_COLUMNS
from src.train_model.finetune_retriever import train as train_retriever
from src.train_model.train_forecaster import train_forecaster
from src.train_model.train_sentiment import train_baseline


def test_forecaster_artifact_has_reproducible_manifest_and_checksum(tmp_path: Path):
    output = tmp_path / "TEST.joblib"
    prices = deterministic_price_frame(220, ticker="MANIFEST")
    result = train_forecaster(
        prices,
        ticker="TEST",
        output=output,
        max_horizon=2,
        max_iter=12,
        trained_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        data_source="unit-test-frame",
    )

    manifest = verify_artifact(output)
    loaded = load_joblib_artifact(output)
    assert output.is_file()
    assert output.with_suffix(".joblib.manifest.json").is_file()
    assert output.with_suffix(".joblib.sha256").is_file()
    assert manifest["artifact_sha256"]
    assert manifest["version"] == 2
    assert manifest["artifact_version"] == 1
    assert manifest["ticker"] == "TEST"
    assert manifest["trained_at"] == "2026-01-02T00:00:00+00:00"
    assert manifest["data_cutoff"] == prices["date"].iloc[-1].isoformat()
    assert manifest["feature_columns"] == FEATURE_COLUMNS
    assert set(manifest["metrics"]) == {"1", "2"}
    assert loaded["max_horizon"] == 2
    assert set(loaded["models"]["1"]) == {"0.1", "0.5", "0.9"}
    assert result["manifest"]["artifact_sha256"] == manifest["artifact_sha256"]


def test_sentiment_baseline_uses_same_verified_artifact_contract(tmp_path: Path):
    rows = pd.DataFrame(
        {
            "text": [
                "profit growth beat estimates",
                "revenue and margin improved",
                "strong cash flow and upgrade",
                "record earnings this quarter",
                "positive outlook from management",
                "demand growth remains strong",
                "loss widened below estimates",
                "weak demand and downgrade",
                "cash flow declined sharply",
                "lawsuit creates material risk",
                "negative outlook from management",
                "revenue miss and margin pressure",
                "profit rose with stable demand",
                "guidance improved after strong sales",
                "earnings miss as costs increased",
                "sales declined in weak market",
            ],
            "label": ["positive"] * 6
            + ["negative"] * 6
            + ["positive", "positive", "negative", "negative"],
        }
    )
    output = tmp_path / "sentiment.joblib"
    result = train_baseline(
        rows,
        output,
        trained_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )

    manifest = verify_artifact(output)
    assert manifest["model_type"] == "tfidf_logistic_regression"
    assert set(manifest["labels"]) == {"negative", "positive"}
    assert 0 <= manifest["metrics"]["accuracy"] <= 1
    prediction = result["model"].predict(
        result["vectorizer"].transform(["profit growth"])
    )
    assert prediction.shape == (1,)


def test_smoke_cli_is_offline_and_marks_generated_source(tmp_path: Path, monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("network fetch must not run in smoke mode")

    monkeypatch.setattr(training_cli, "fetch_stock_data", fail_if_called)
    _, manifest = training_cli.train(
        "SMOKE",
        smoke_test=True,
        output=tmp_path / "smoke.joblib",
        max_horizon=1,
        max_iter=10,
    )

    assert manifest["data_source"] == "deterministic-smoke-test"
    assert manifest["max_horizon"] == 1


def test_legacy_backtester_wrapper_keeps_next_period_alignment():
    frame = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=3, tz="UTC"),
            "close": [100.0, 200.0, 200.0],
            "pred_log_return": [-0.01, 0.01, 0.01],
        }
    )
    result = TradingBacktester(
        transaction_cost=0,
        slippage=0,
        max_exposure=0.10,
    ).run(frame)

    assert result["alignment"] == "signal_at_t_applied_to_return_t_plus_1"
    assert result["metrics"]["total_return"] == 0
    assert result["benchmark"]["total_return"] == 1


def test_retriever_module_import_does_not_require_optional_training_stack():
    assert callable(train_retriever)
