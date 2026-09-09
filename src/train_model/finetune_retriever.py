"""Optional bi-encoder fine-tuning entry point for financial retrieval pairs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _dependencies():
    try:
        from sentence_transformers import InputExample, SentenceTransformer, losses
        from torch.utils.data import DataLoader
    except ImportError as exc:
        raise RuntimeError(
            "Retriever fine-tuning requires optional dependencies. Install "
            "`pip install -r backend/requirements-train.txt` before running "
            "this command."
        ) from exc
    return InputExample, SentenceTransformer, losses, DataLoader


def _load_pairs(path: str | Path) -> list[dict[str, str]]:
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"retriever dataset not found: {source}")
    pairs: list[dict[str, str]] = []
    lines = source.read_text(encoding="utf-8").splitlines()
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row: Any = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on line {line_number}") from exc
        query = str(row.get("query") or row.get("question") or "").strip()
        positive = str(
            row.get("positive") or row.get("passage") or row.get("text") or ""
        ).strip()
        if not query or not positive:
            raise ValueError(
                f"line {line_number} needs non-empty query and positive text"
            )
        pairs.append({"query": query, "positive": positive})
    if len(pairs) < 8:
        raise ValueError("retriever fine-tuning needs at least eight positive pairs")
    return pairs


def train(
    data: str | Path,
    output: str | Path,
    model_name: str = "intfloat/multilingual-e5-small",
    epochs: int = 1,
    *,
    batch_size: int = 16,
) -> None:
    """Fine-tune with in-batch negatives when optional dependencies are present."""

    if epochs < 1 or batch_size < 2:
        raise ValueError("epochs must be positive and batch_size must be at least two")
    pairs = _load_pairs(data)
    InputExample, SentenceTransformer, losses, DataLoader = _dependencies()
    model = SentenceTransformer(model_name)
    examples = [InputExample(texts=[row["query"], row["positive"]]) for row in pairs]
    loader = DataLoader(
        examples, shuffle=True, batch_size=min(batch_size, len(examples))
    )
    loss = losses.MultipleNegativesRankingLoss(model)
    warmup_steps = max(1, int(len(loader) * epochs * 0.1))
    model.fit(
        train_objectives=[(loader, loss)],
        epochs=epochs,
        warmup_steps=warmup_steps,
        output_path=str(Path(output)),
        show_progress_bar=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument(
        "--output", type=Path, default=Path("training/outputs/retriever")
    )
    parser.add_argument("--model-name", default="intfloat/multilingual-e5-small")
    parser.add_argument("--epochs", type=int, default=1)
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args(argv)
    train(
        args.data,
        args.output,
        args.model_name,
        args.epochs,
        batch_size=args.batch_size,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
