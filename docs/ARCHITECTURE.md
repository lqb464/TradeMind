# Forecasting ML workflow

```text
OHLCV source -> schema/quality validation -> causal feature engineering
            -> direct future log-return labels -> chronological split + purge
            -> quantile regressors -> validation metrics -> checksummed artifact
```

Feature rows use information available through time `t`; a label at `t` is
`log(close[t+h] / close[t])`. The final validation block is chronologically
later than training and the gap is at least the maximum target horizon.

The artifact manifest records the data hash/cutoff, feature schema, split,
purge gap, quantile levels, metrics, and model hash. Compare experiments using
the same point-in-time data and do not interpret a high directional score as a
trading strategy result.
