# TradeMind model workspace

The runtime works without trained artifacts. Training is explicit and never
silently substitutes synthetic data for a requested market download.

- Forecast: direct 10/50/90% quantile models for each horizon, chronological
  holdout, and a purge gap at least as large as the longest label horizon.
- Sentiment: TF-IDF plus balanced logistic regression with a held-out split.
- Retriever: optional multilingual bi-encoder tuning; the command emits a clear
  dependency error until the training extras are installed.

Each joblib artifact is accompanied by `*.manifest.json` and `*.sha256`.
Joblib is pickle-based: load only trusted local artifacts and verify the sidecar
before loading.

Use an explicit CSV for reproducibility:

```powershell
python scripts/train.py --input training/data/prices.csv --ticker AAPL
python -m src.train_model.train_sentiment --data training/data/financial_news.csv
```

`python scripts/train.py --smoke-test` is the only command that deliberately
uses deterministic generated prices; its manifest labels that source.
