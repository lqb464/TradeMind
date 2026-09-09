"""Small, deterministic model factories for the offline workspace."""

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


def build_xgboost_pipeline(random_state: int = 42):
    """Compatibility factory with an actionable optional-dependency error."""

    try:
        from sklearn.pipeline import Pipeline
        from sklearn.preprocessing import StandardScaler
        from xgboost import XGBRegressor
    except ImportError as exc:  # pragma: no cover - depends on optional install
        raise RuntimeError(
            "XGBoost requires the project's training dependencies"
        ) from exc
    return Pipeline(
        [
            ("scaler", StandardScaler()),
            (
                "regressor",
                XGBRegressor(
                    n_estimators=200,
                    learning_rate=0.03,
                    max_depth=4,
                    subsample=0.8,
                    colsample_bytree=0.8,
                    random_state=random_state,
                    n_jobs=-1,
                ),
            ),
        ]
    )
