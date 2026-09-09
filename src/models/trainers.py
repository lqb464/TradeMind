"""Model factories used by the forecasting pipeline."""
from __future__ import annotations

from sklearn.ensemble import HistGradientBoostingRegressor


def build_quantile_regressor(
    quantile: float,
    *,
    random_state: int = 42,
    max_iter: int = 180,
) -> HistGradientBoostingRegressor:
    if not 0 < quantile < 1:
        raise ValueError("quantile must be between zero and one")
    return HistGradientBoostingRegressor(
        loss="quantile",
        quantile=quantile,
        max_iter=max_iter,
        max_leaf_nodes=15,
        learning_rate=0.04,
        l2_regularization=0.2,
        random_state=random_state,
    )
