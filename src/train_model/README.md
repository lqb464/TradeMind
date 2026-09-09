# Training workflows

- Forecasting: direct 10/50/90% quantile models for multiple horizons, causal features, chronological holdout, and a purge gap at least as large as the longest target horizon.
- Sentiment: TF-IDF plus balanced logistic regression with held-out metrics.
- Retrieval: optional multilingual bi-encoder fine-tuning for query/passage pairs; install the `[nlp]` extra when needed.

Every joblib artifact is accompanied by a JSON manifest and SHA256 sidecar.
Load artifacts only from trusted sources and verify their checksum first.

```powershell
python scripts/train.py --input data/raw/prices.csv --ticker AAPL
python -m src.train_model.train_sentiment --data data/raw/financial_news.csv
python -m src.train_model.finetune_retriever --data data/raw/pairs.jsonl
```

`python scripts/train.py --smoke-test` is the only workflow that intentionally
generates OHLCV data; its manifest marks the source as a smoke test.
