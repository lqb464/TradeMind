import numpy as np

from src.data.loader import deterministic_price_frame
from src.features.technical import (
    FEATURE_COLUMNS,
    build_training_frame,
    latest_feature_row,
)


def test_direct_horizon_target_is_future_close_return():
    raw = deterministic_price_frame(140, ticker="TARGET")
    frame = build_training_frame(raw, max_horizon=3)
    index = 80

    expected = np.log(raw.loc[index + 3, "close"] / raw.loc[index, "close"])
    assert np.isclose(frame.loc[index, "target_return_3d"], expected)


def test_features_are_causal_when_distant_future_changes():
    raw = deterministic_price_frame(160, ticker="CAUSAL")
    changed = raw.copy()
    changed.loc[120:, "close"] *= 1.8
    changed.loc[120:, "open"] *= 1.8
    changed.loc[120:, "high"] *= 1.8
    changed.loc[120:, "low"] *= 1.8

    baseline = (
        build_training_frame(raw, max_horizon=2)
        .loc[90, FEATURE_COLUMNS]
        .to_numpy(dtype=float)
    )
    altered = (
        build_training_frame(changed, max_horizon=2)
        .loc[90, FEATURE_COLUMNS]
        .to_numpy(dtype=float)
    )
    assert np.allclose(baseline, altered, equal_nan=True)


def test_latest_feature_row_has_stable_runtime_column_order():
    row = latest_feature_row(deterministic_price_frame(120, ticker="LATEST"))

    assert list(row.columns) == FEATURE_COLUMNS
    assert len(row) == 1
    assert np.isfinite(row.to_numpy(dtype=float)).all()
