from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


def test_health_config_and_user_scoped_portfolio(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setenv("TRADEMIND_DB_PATH", str(tmp_path / "api.db"))
    from backend.main import app

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        payload = health.json()
        assert payload["status"] == "ok"
        assert payload["execution"] == {
            "paper_enabled": True,
            "live_enabled": False,
            "human_approval_required": True,
            "demo_data_blocked": True,
        }
        assert payload["execution_mode"] == "PAPER"
        assert client.get("/api/config").json()["product"] == "TradeMind"
        portfolio = client.get("/api/portfolio/account")
        assert portfolio.status_code == 200
        assert portfolio.json()["execution_mode"] == "PAPER"


def test_market_endpoint_exposes_quality_contract(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_REQUIRED", "false")
    monkeypatch.setenv("TRADEMIND_DB_PATH", str(tmp_path / "market.db"))
    from backend import main

    snapshot = {
        "ticker": "TEST",
        "price": 100,
        "change": 1,
        "change_pct": 1,
        "volume": 1000,
        "indicators": {},
        "candles": [],
        "meta": {
            "source": "deterministic-demo",
            "as_of": datetime.now(timezone.utc).isoformat(),
            "status": "demo",
            "is_demo": True,
            "is_stale": False,
            "execution_eligible": False,
        },
    }
    monkeypatch.setattr(main, "get_market_snapshot", lambda ticker, period: {**snapshot, "ticker": ticker})
    with TestClient(main.app) as client:
        response = client.get("/api/market/test")
    assert response.status_code == 200
    assert response.json()["ticker"] == "TEST"
    assert response.json()["meta"]["execution_eligible"] is False


def test_protected_operations_require_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("TRADEMIND_DB_PATH", str(tmp_path / "protected.db"))
    from backend.main import app

    with TestClient(app) as client:
        assert client.get("/api/ops/traces").status_code == 401
        assert client.get("/api/documents").status_code == 401
        assert client.get("/api/trade/backtest/AAPL").status_code == 401


def test_request_body_limit_rejects_before_endpoint_parsing():
    from backend.main import RequestBodyLimitMiddleware

    inner = FastAPI()

    @inner.post("/api/documents")
    async def upload_handler(request: Request):
        await request.body()
        return {"unexpected": True}

    limited = RequestBodyLimitMiddleware(inner, default_limit=8, upload_limit=8)
    with TestClient(limited) as client:
        response = client.post(
            "/api/documents",
            content=b"123456789",
            headers={"content-type": "application/octet-stream"},
        )
        streamed = client.post(
            "/api/documents",
            content=(chunk for chunk in (b"1234", b"5678", b"9")),
            headers={"content-type": "application/octet-stream"},
        )
    assert response.status_code == 413
    assert streamed.status_code == 413


def test_request_body_limit_preserves_disconnect_after_replay():
    from backend.main import RequestBodyLimitMiddleware

    seen: list[dict[str, object]] = []

    async def inner(scope, receive, send):
        seen.append(await receive())
        seen.append(await asyncio.wait_for(receive(), timeout=0.1))
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    incoming = iter(
        [
            {"type": "http.request", "body": b"{}", "more_body": False},
            {"type": "http.disconnect"},
        ]
    )

    async def receive():
        return next(incoming)

    sent: list[dict[str, object]] = []

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/copilot/stream",
        "headers": [],
    }
    limited = RequestBodyLimitMiddleware(inner, default_limit=8, upload_limit=8)
    asyncio.run(limited(scope, receive, send))

    assert seen == [
        {"type": "http.request", "body": b"{}", "more_body": False},
        {"type": "http.disconnect"},
    ]
    assert sent[0]["status"] == 204
