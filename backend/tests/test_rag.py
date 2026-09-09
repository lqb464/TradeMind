from __future__ import annotations

import asyncio

import pytest

from backend.src.rag import FinancialRAG, reciprocal_rank_fusion, rewrite_queries


def test_rrf_and_financial_query_rewrite():
    assert reciprocal_rank_fusion([[2, 1], [1, 3]])[0][0] == 1
    assert len(rewrite_queries("Biên lợi nhuận thay đổi thế nào so với cùng kỳ?")) >= 2


def test_rag_is_tenant_scoped_and_rejects_unrelated_context(tmp_path):
    rag = FinancialRAG(tmp_path)
    document = rag.ingest(
        "report.txt",
        (
            "Biên lợi nhuận gộp tăng từ 18% lên 22% so với cùng kỳ. "
            "Dòng tiền từ hoạt động kinh doanh dương 120 tỷ đồng."
        ).encode("utf-8"),
        owner_id="user-a",
    )
    assert len(rag.list_documents("user-a")) == 1
    assert rag.list_documents("user-b") == []
    assert rag.retrieve(
        "Biên lợi nhuận so với cùng kỳ?",
        document["document_id"],
        owner_id="user-a",
    )
    assert rag.retrieve(
        "quantum penguin volcano",
        document["document_id"],
        owner_id="user-a",
    ) == []
    assert rag.retrieve(
        "Biên lợi nhuận",
        document["document_id"],
        owner_id="user-b",
    ) == []
    assert rag.delete_document(document["document_id"], owner_id="user-b") is False
    assert rag.delete_document(document["document_id"], owner_id="user-a") is True


def test_single_incidental_token_does_not_defeat_no_answer(tmp_path):
    rag = FinancialRAG(tmp_path)
    document = rag.ingest(
        "liquidity.txt",
        b"Liquidity risk decreased because operating cash flow remained stable.",
        owner_id="user-a",
    )
    assert rag.retrieve(
        "What is the volcano risk on Mars?",
        document["document_id"],
        owner_id="user-a",
    ) == []


def test_per_user_document_quota_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_RAG_DOCUMENTS_PER_USER", "1")
    rag = FinancialRAG(tmp_path)
    rag.ingest("one.txt", b"Revenue increased strongly.", owner_id="user-a")
    with pytest.raises(ValueError, match="giới hạn"):
        rag.ingest("two.txt", b"Cash flow remained positive.", owner_id="user-a")
    assert rag.ingest("other.txt", b"Another tenant.", owner_id="user-b")


def test_document_id_is_validated(tmp_path):
    rag = FinancialRAG(tmp_path)
    with pytest.raises(ValueError):
        rag.retrieve("question", "../../secret", owner_id="user-a")


def test_no_answer_stream_is_explicit(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "offline")
    rag = FinancialRAG(tmp_path)

    async def collect():
        return [event async for event in rag.answer_stream("missing", None, owner_id="u")]

    events = asyncio.run(collect())
    assert events[-1]["type"] == "done"
    assert events[-1]["grounded"] is False
    assert events[-1]["reason"] == "no_relevant_context"
