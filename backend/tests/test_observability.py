from __future__ import annotations

from backend.src.observability import (
    finalize_ai_reservation,
    log_ai_usage,
    release_ai_reservation,
    reserve_ai_budget,
    usage_summary,
)


def test_ai_budget_reservation_is_atomic_and_releasable(tmp_path, monkeypatch):
    database = tmp_path / "budget.db"
    monkeypatch.setenv("MAX_AI_TOKENS_PER_USER_PER_DAY", "17000")
    monkeypatch.setenv("MAX_AI_CALLS_PER_USER_PER_DAY", "1")
    first = reserve_ai_budget(database, "user-a")
    assert first
    assert reserve_ai_budget(database, "user-a") is None

    release_ai_reservation(database, first)
    replacement = reserve_ai_budget(database, "user-a")
    assert replacement
    log_ai_usage(
        database,
        endpoint="rag",
        model="offline-grounded",
        input_tokens=100,
        output_tokens=50,
        estimated_cost=0,
        trace_id="trace-1",
        user_id="user-a",
        reservation_id=replacement,
    )
    assert reserve_ai_budget(database, "user-a") is None
    assert usage_summary(database)["calls"] == 1


def test_started_interrupted_stream_consumes_its_reservation(tmp_path, monkeypatch):
    database = tmp_path / "interrupted.db"
    monkeypatch.setenv("MAX_AI_TOKENS_PER_USER_PER_DAY", "17000")
    monkeypatch.setenv("MAX_AI_CALLS_PER_USER_PER_DAY", "1")
    reservation = reserve_ai_budget(database, "user-a")
    assert reservation

    assert finalize_ai_reservation(
        database,
        reservation,
        endpoint="rag",
        model="test-interrupted",
        trace_id="interrupted-test",
    )
    assert finalize_ai_reservation(
        database,
        reservation,
        endpoint="rag",
    ) is False
    summary = usage_summary(database)
    assert summary["calls"] == 1
    assert summary["input_tokens"] == 16_200
    assert reserve_ai_budget(database, "user-a") is None
