"""Deterministic offline retrieval regression gate for TradeMind RAG."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from backend.src.rag import FinancialRAG

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATASET = Path(__file__).with_name("rag_eval.jsonl")
DEFAULT_CORPUS = Path(__file__).with_name("fixtures") / "sample_report.txt"


def token_set(text: str) -> set[str]:
    return set(text.lower().split())


def evaluate(
    rag: FinancialRAG,
    dataset: Path,
    *,
    document_id: str | None = None,
    owner_id: str | None = None,
) -> dict[str, Any]:
    rows = [
        json.loads(line)
        for line in dataset.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    details = []
    for row in rows:
        hits = rag.retrieve(row["question"], document_id, owner_id=owner_id)
        negative = bool(row.get("expect_no_answer"))
        context = " ".join(hit["text"] for hit in hits).lower()
        expected = [term.lower() for term in row.get("expected_terms", [])]
        hit_rate = (
            sum(term in context for term in expected) / len(expected) if expected else 1.0
        )
        relevant_hits = sum(any(term in hit["text"].lower() for term in expected) for hit in hits)
        precision = relevant_hits / max(1, len(hits))
        relevance = len(token_set(row["question"]) & token_set(context)) / max(
            1, len(token_set(row["question"]))
        )
        passed = not hits if negative else bool(hits) and hit_rate >= 0.5
        details.append(
            {
                "question": row["question"],
                "category": row.get("category"),
                "expect_no_answer": negative,
                "retrieval_hit_rate": round(hit_rate, 3),
                "context_precision": round(precision, 3),
                "answer_relevance_proxy": round(relevance, 3),
                "retrieved": len(hits),
                "passed": passed,
            }
        )
    positive = [item for item in details if not item["expect_no_answer"]]
    negatives = [item for item in details if item["expect_no_answer"]]
    pass_rate = sum(item["passed"] for item in details) / max(1, len(details))
    return {
        "samples": len(details),
        "pass_rate": round(pass_rate, 3),
        "retrieval_hit_rate": round(
            sum(item["retrieval_hit_rate"] for item in positive) / max(1, len(positive)), 3
        ),
        "context_precision": round(
            sum(item["context_precision"] for item in positive) / max(1, len(positive)), 3
        ),
        "no_answer_accuracy": round(
            sum(item["passed"] for item in negatives) / max(1, len(negatives)), 3
        ),
        "passed": pass_rate >= 0.8,
        "details": details,
    }


def run(dataset: Path = DEFAULT_DATASET, corpus: Path = DEFAULT_CORPUS) -> dict[str, Any]:
    scratch_root = ROOT / ".eval_tmp"
    scratch_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=scratch_root) as directory:
        rag = FinancialRAG(Path(directory))
        indexed = rag.ingest("sample-report.txt", corpus.read_bytes(), owner_id="evaluation")
        return evaluate(
            rag,
            dataset,
            document_id=indexed["document_id"],
            owner_id="evaluation",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    result = run(args.dataset, args.corpus)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
