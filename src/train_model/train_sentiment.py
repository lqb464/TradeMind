"""Train a transparent TF-IDF financial-news sentiment baseline."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import accuracy_score, f1_score
from sklearn.model_selection import train_test_split

from src.train_model.artifacts import save_joblib_artifact


ARTIFACT_VERSION = 2


def _load_rows(data: str | Path | pd.DataFrame) -> pd.DataFrame:
    if isinstance(data, pd.DataFrame):
        return data.copy()
    source = Path(data)
    if not source.is_file():
        raise FileNotFoundError(f"sentiment dataset not found: {source}")
    return pd.read_csv(source)


def train_baseline(
    data: str | Path | pd.DataFrame,
    output: str | Path,
    *,
    text_column: str = "text",
    label_column: str = "label",
    random_state: int = 42,
    trained_at: datetime | None = None,
) -> dict[str, Any]:
    """Fit and persist a deterministic lexical baseline with held-out metrics."""

    rows = _load_rows(data)
    if text_column not in rows and text_column == "text":
        text_column = next(
            (
                name
                for name in ("headline", "title", "sentence")
                if name in rows
            ),
            text_column,
        )
    missing = [column for column in (text_column, label_column) if column not in rows]
    if missing:
        raise ValueError(f"sentiment dataset is missing columns: {', '.join(missing)}")
    clean = rows[[text_column, label_column]].dropna().copy()
    clean[text_column] = clean[text_column].astype(str).str.strip()
    clean[label_column] = clean[label_column].astype(str).str.strip().str.lower()
    clean = clean[(clean[text_column] != "") & (clean[label_column] != "")]
    counts = clean[label_column].value_counts()
    if len(clean) < 12:
        raise ValueError("sentiment baseline needs at least 12 labeled rows")
    if len(counts) < 2 or int(counts.min()) < 2:
        raise ValueError(
            "sentiment baseline needs at least two classes with two rows each"
        )

    stratify = clean[label_column] if int(counts.min()) >= 2 else None
    train_text, test_text, train_label, test_label = train_test_split(
        clean[text_column],
        clean[label_column],
        test_size=0.25,
        random_state=random_state,
        stratify=stratify,
    )
    vectorizer = TfidfVectorizer(
        lowercase=True,
        ngram_range=(1, 2),
        min_df=1,
        max_features=30_000,
        sublinear_tf=True,
    )
    train_matrix = vectorizer.fit_transform(train_text)
    test_matrix = vectorizer.transform(test_text)
    model = LogisticRegression(
        max_iter=1_000,
        class_weight="balanced",
        random_state=random_state,
    )
    model.fit(train_matrix, train_label)
    predicted = model.predict(test_matrix)
    created_at = trained_at or datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    metrics = {
        "accuracy": round(float(accuracy_score(test_label, predicted)), 6),
        "macro_f1": round(
            float(
                f1_score(
                    test_label,
                    predicted,
                    average="macro",
                    zero_division=0,
                )
            ),
            6,
        ),
        "train_rows": len(train_text),
        "validation_rows": len(test_text),
    }
    manifest = {
        "artifact_version": ARTIFACT_VERSION,
        "model_type": "tfidf_logistic_regression",
        "trained_at": created_at.astimezone(timezone.utc).isoformat(),
        "data_rows": len(clean),
        "text_column": text_column,
        "label_column": label_column,
        "labels": sorted(clean[label_column].unique().tolist()),
        "feature_count": len(vectorizer.vocabulary_),
        "metrics": metrics,
    }
    artifact = {**manifest, "vectorizer": vectorizer, "model": model}
    persisted_manifest = save_joblib_artifact(artifact, output, manifest=manifest)
    return {
        **artifact,
        "manifest": persisted_manifest,
        "artifact_path": str(Path(output)),
    }


def finetune_transformer(*args, **kwargs):
    """Fail clearly instead of silently pretending that a transformer was tuned."""

    del args, kwargs
    raise RuntimeError(
        "Transformer fine-tuning is not part of the safe baseline pipeline. "
        "Use a reviewed training job with transformers/datasets and a model card."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("training/outputs/sentiment/baseline.joblib"),
    )
    parser.add_argument("--text-column", default="text")
    parser.add_argument("--label-column", default="label")
    parser.add_argument(
        "--mode", choices=("baseline", "transformer"), default="baseline"
    )
    args = parser.parse_args(argv)
    if args.mode == "transformer":
        finetune_transformer()
    result = train_baseline(
        args.data,
        args.output,
        text_column=args.text_column,
        label_column=args.label_column,
    )
    print(json.dumps(result["manifest"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
