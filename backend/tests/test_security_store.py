from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core.security import (
    AuthConfig,
    AuthUser,
    SecurityConfigError,
    TokenError,
    decode_token,
    hash_password,
    issue_token_pair,
    validate_security_config,
    verify_password,
)
from backend.src.store import (
    AccessDeniedError,
    ConflictError,
    NotFoundError,
    SQLiteStore,
    UnsafeMarketDataError,
    ValidationError,
)


CONFIG = AuthConfig(
    secret="unit-test-secret-that-is-longer-than-thirty-two-bytes",
    issuer="trademind-test",
    access_ttl_seconds=60,
    refresh_ttl_seconds=3600,
    environment="test",
)


def _decision(ticker: str = "AAPL") -> dict:
    return {
        "decision_id": "a" * 24,
        "ticker": ticker,
        "action": "BUY",
        "execution_eligible": True,
        "score": 0.8,
        "confidence": 0.75,
        "vetoes": [],
        "evidence": [{"kind": "market_snapshot", "source": "yfinance"}],
        "risk_policy": {
            "risk_per_trade_pct": 0.01,
            "max_position_pct": 0.10,
            "max_gross_exposure_pct": 0.80,
            "max_daily_loss_pct": 0.03,
        },
        "market_quality": {"canonical": {"currency": "USD"}},
        "position_size": {
            "quantity": "10",
            "price": "100.00",
            "stop_price": "95.00",
            "estimated_risk": "50.00",
        },
    }


def _live_snapshot(ticker: str = "AAPL") -> dict:
    now = datetime.now(timezone.utc)
    return {
        "ticker": ticker,
        "price": 101.0,
        "volume": 1_000_000,
        "candles": [
            {
                "date": (now - timedelta(days=40 - index)).isoformat(),
                "open": 99.0,
                "high": 102.0,
                "low": 98.0,
                "close": 101.0,
                "volume": 1_000_000,
            }
            for index in range(40)
        ],
        "meta": {
            "source": "yfinance",
            "as_of": now.isoformat(),
            "is_demo": False,
            "execution_eligible": True,
            "currency": "USD",
            "exchange": "NASDAQ",
            "instrument_type": "EQUITY",
            "tradable": True,
        },
    }


def test_scrypt_password_hash_round_trip_and_corrupt_input():
    encoded = hash_password("a-strong-test-password")

    assert encoded.startswith("scrypt$")
    assert verify_password("a-strong-test-password", encoded)
    assert not verify_password("wrong-password", encoded)
    assert not verify_password("a-strong-test-password", "scrypt$bad")


def test_hmac_jwt_enforces_signature_type_issuer_and_expiry():
    user = AuthUser(id="user-1", email="person@example.com", role="USER")
    pair = issue_token_pair(user, config=CONFIG, now=1_000)

    assert decode_token(pair.access_token, "access", config=CONFIG, now=1_010).subject == "user-1"
    with pytest.raises(TokenError, match="Expected"):
        decode_token(pair.refresh_token, "access", config=CONFIG, now=1_010)
    with pytest.raises(TokenError, match="issuer"):
        decode_token(
            pair.access_token,
            "access",
            config=AuthConfig(**{**CONFIG.__dict__, "issuer": "another-service"}),
            now=1_010,
        )
    with pytest.raises(TokenError, match="expired"):
        decode_token(pair.access_token, "access", config=CONFIG, now=1_080)

    parts = pair.access_token.split(".")
    parts[1] = ("A" if parts[1][0] != "A" else "B") + parts[1][1:]
    with pytest.raises(TokenError, match="signature"):
        decode_token(".".join(parts), "access", config=CONFIG, now=1_010)


def test_production_config_fails_closed():
    with pytest.raises(SecurityConfigError):
        validate_security_config(
            AuthConfig(
                secret="short",
                environment="production",
                bootstrap_admin_email="admin@example.com",
                bootstrap_admin_token="b" * 40,
            )
        )
    with pytest.raises(SecurityConfigError, match="BOOTSTRAP"):
        validate_security_config(
            AuthConfig(
                secret="x" * 40,
                environment="production",
                bootstrap_admin_email="",
                bootstrap_admin_token="b" * 40,
            )
        )
    with pytest.raises(SecurityConfigError, match="BOOTSTRAP_ADMIN_TOKEN"):
        validate_security_config(
            AuthConfig(
                secret="x" * 40,
                environment="production",
                bootstrap_admin_email="admin@example.com",
            )
        )
    assert validate_security_config(
        AuthConfig(
            secret="x" * 40,
            environment="production",
            bootstrap_admin_email="admin@example.com",
            bootstrap_admin_token="b" * 40,
        )
    ).environment == "production"
    with pytest.raises(SecurityConfigError, match="AUTH_REQUIRED"):
        validate_security_config(
            AuthConfig(
                secret="x" * 40,
                environment="production",
                bootstrap_admin_email="admin@example.com",
                bootstrap_admin_token="b" * 40,
                auth_required=False,
            )
        )


def test_first_user_admin_refresh_rotation_and_user_scoping(tmp_path):
    store = SQLiteStore(tmp_path / "trademind.db")
    first = store.create_user("admin@example.com", hash_password("first-user-password"))
    second = store.create_user("user@example.com", hash_password("second-user-password"))

    assert first.role == "ADMIN"
    assert second.role == "USER"

    pair = issue_token_pair(
        AuthUser(first.id, first.email, "ADMIN"), config=CONFIG
    )
    store.register_refresh_token(first.id, pair.refresh_token, pair.refresh_expires_at)
    replacement = issue_token_pair(
        AuthUser(first.id, first.email, "ADMIN"), config=CONFIG
    )
    store.rotate_refresh_token(
        first.id,
        pair.refresh_token,
        replacement.refresh_token,
        replacement.refresh_expires_at,
    )
    with pytest.raises(AccessDeniedError):
        store.rotate_refresh_token(
            first.id,
            pair.refresh_token,
            replacement.refresh_token + "x",
            replacement.refresh_expires_at,
        )
    with pytest.raises(AccessDeniedError):
        store.rotate_refresh_token(
            first.id,
            replacement.refresh_token,
            replacement.refresh_token + "-next",
            replacement.refresh_expires_at,
        )

    store.add_watchlist(first.id, "aapl")
    assert [item["ticker"] for item in store.list_watchlist(first.id)] == ["AAPL"]
    assert store.list_watchlist(second.id) == []
    first_account = store.get_account(first.id)
    with pytest.raises(NotFoundError):
        store.get_account(second.id, first_account["id"])

    with sqlite3.connect(tmp_path / "trademind.db") as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_paper_approval_blocks_demo_and_is_idempotent(tmp_path):
    store = SQLiteStore(tmp_path / "trademind.db")
    user = store.create_user("person@example.com", hash_password("very-secure-password"))
    account = store.get_account(user.id)
    proposal = store.create_trade_proposal(
        user.id, account["id"], _decision(), int(time.time()) + 600, "proposal-create-0001"
    )

    demo = _live_snapshot()
    demo["meta"]["is_demo"] = True
    with pytest.raises(UnsafeMarketDataError):
        store.approve_trade_proposal(
            user.id, proposal["id"], "approval-0001", demo
        )

    order = store.approve_trade_proposal(
        user.id, proposal["id"], "approval-0001", _live_snapshot()
    )
    repeated = store.approve_trade_proposal(
        user.id, proposal["id"], "approval-0001", demo
    )

    assert order["mode"] == "PAPER"
    assert order["status"] == "FILLED"
    assert repeated["id"] == order["id"]
    assert store.get_proposal(user.id, proposal["id"])["status"] == "APPROVED"
    summary = store.account_cost_summary(user.id, account["id"])
    assert summary["cash"] == pytest.approx(100_000 - 1_010)
    assert summary["positions"][0]["quantity"] == 10


def test_atomic_approval_rechecks_stacked_position_exposure(tmp_path):
    store = SQLiteStore(tmp_path / "trademind.db")
    user = store.create_user("stack@example.com", hash_password("very-secure-password"))
    account = store.get_account(user.id)
    decision = _decision()
    decision["position_size"].update(
        quantity="50", notional="5000.00", estimated_risk="250.00"
    )
    first = store.create_trade_proposal(
        user.id,
        account["id"],
        decision,
        int(time.time()) + 600,
        "stacked-proposal-first",
    )
    second = store.create_trade_proposal(
        user.id,
        account["id"],
        decision,
        int(time.time()) + 600,
        "stacked-proposal-second",
    )
    snapshot = _live_snapshot()
    store.approve_trade_proposal(
        user.id, first["id"], "stacked-approval-first", snapshot
    )
    with pytest.raises(ConflictError, match="per-position"):
        store.approve_trade_proposal(
            user.id,
            second["id"],
            "stacked-approval-second",
            snapshot,
            portfolio_marks={"AAPL": snapshot},
        )
    assert len(store.list_orders(user.id)) == 1


def test_quote_currency_must_match_account(tmp_path):
    store = SQLiteStore(tmp_path / "trademind.db")
    user = store.create_user("currency@example.com", hash_password("very-secure-password"))
    account = store.create_paper_account(user.id, "VND Paper", 2_000_000_000, "VND")
    decision = _decision("FPT.VN")
    decision["market_quality"]["canonical"]["currency"] = "VND"
    proposal = store.create_trade_proposal(
        user.id,
        account["id"],
        decision,
        int(time.time()) + 600,
        "currency-proposal",
    )
    usd_snapshot = _live_snapshot("FPT.VN")
    with pytest.raises(UnsafeMarketDataError, match="currency"):
        store.approve_trade_proposal(
            user.id, proposal["id"], "currency-approval", usd_snapshot
        )


def test_daily_pnl_keeps_the_first_session_baseline(tmp_path):
    store = SQLiteStore(tmp_path / "trademind.db")
    user = store.create_user("daily-risk@example.com", hash_password("very-secure-password"))
    account = store.get_account(user.id)

    assert store.mark_daily_equity(user.id, account["id"], 98_000) == pytest.approx(-2_000)
    assert store.mark_daily_equity(user.id, account["id"], 97_000) == pytest.approx(-3_000)
    assert store.get_account(user.id, account["id"])["daily_pnl"] == pytest.approx(-3_000)

    second = store.create_paper_account(user.id, "No profit buffer", 100_000, "USD")
    assert store.mark_daily_equity(user.id, second["id"], 120_000) == pytest.approx(0)
    assert store.mark_daily_equity(user.id, second["id"], 110_000) == pytest.approx(-10_000)


def test_approval_rejects_legacy_unbound_proposal(tmp_path):
    store = SQLiteStore(tmp_path / "trademind.db")
    user = store.create_user("legacy@example.com", hash_password("very-secure-password"))
    account = store.get_account(user.id)
    proposal = store.create_trade_proposal(
        user.id,
        account["id"],
        _decision(),
        int(time.time()) + 600,
        "legacy-proposal-key",
    )
    with store.transaction() as db:
        db.execute(
            "UPDATE trade_proposals SET decision_id='',idempotency_key='',quote_currency='' WHERE id=?",
            (proposal["id"],),
        )

    with pytest.raises(ConflictError, match="decision-binding"):
        store.approve_trade_proposal(
            user.id,
            proposal["id"],
            "legacy-approval-key",
            _live_snapshot(),
        )
    assert store.list_orders(user.id) == []


def test_account_controls_are_owned_safe_and_audited(tmp_path):
    store = SQLiteStore(tmp_path / "trademind.db")
    owner = store.create_user("owner@example.com", hash_password("very-secure-password"))
    outsider = store.create_user("outsider@example.com", hash_password("very-secure-password"))
    account = store.get_account(owner.id)

    disabled = store.update_account_controls(
        owner.id,
        account["id"],
        kill_switch=True,
        reason="Unexpected market conditions",
    )
    assert disabled["kill_switch"] is True
    assert disabled["trading_enabled"] is False
    events = store.list_account_control_events(owner.id, account["id"])
    assert len(events) == 1
    assert events[0]["reason"] == "Unexpected market conditions"

    with pytest.raises(ConflictError, match="kill switch"):
        store.update_account_controls(
            owner.id,
            account["id"],
            trading_enabled=True,
            reason="Unsafe enable attempt",
        )

    with pytest.raises(NotFoundError):
        store.update_account_controls(
            outsider.id,
            account["id"],
            kill_switch=False,
            reason="Not my account",
        )

    enabled = store.update_account_controls(
        owner.id,
        account["id"],
        kill_switch=False,
        trading_enabled=True,
        reason="Risk review completed",
    )
    assert enabled["kill_switch"] is False
    assert enabled["trading_enabled"] is True
    assert len(store.list_account_control_events(owner.id, account["id"])) == 2


def test_order_key_cannot_cross_proposals_and_expiry_is_enforced(tmp_path):
    store = SQLiteStore(tmp_path / "trademind.db")
    user = store.create_user("person@example.com", hash_password("very-secure-password"))
    account = store.get_account(user.id)
    first = store.create_trade_proposal(
        user.id, account["id"], _decision(), int(time.time()) + 600, "proposal-create-first"
    )
    store.approve_trade_proposal(
        user.id, first["id"], "approval-shared", _live_snapshot()
    )
    second = store.create_trade_proposal(
        user.id, account["id"], _decision(), int(time.time()) + 600, "proposal-create-second"
    )
    with pytest.raises(ConflictError, match="another proposal"):
        store.approve_trade_proposal(
            user.id, second["id"], "approval-shared", _live_snapshot()
        )

    with store.transaction() as db:
        db.execute(
            "UPDATE trade_proposals SET expires_at=? WHERE id=?",
            (int(time.time()) - 1, second["id"]),
        )
    with pytest.raises(ConflictError, match="expired"):
        store.approve_trade_proposal(
            user.id, second["id"], "approval-expired", _live_snapshot()
        )
    assert store.get_proposal(user.id, second["id"])["status"] == "EXPIRED"


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda snapshot: snapshot["meta"].pop("as_of"), "MISSING_AS_OF"),
        (lambda snapshot: snapshot["meta"].update(as_of="not-a-date"), "MISSING_AS_OF"),
        (
            lambda snapshot: snapshot["meta"].update(as_of="2026-09-08T12:00:00"),
            "MISSING_AS_OF",
        ),
        (
            lambda snapshot: snapshot["meta"].update(
                as_of=(datetime.now(timezone.utc) - timedelta(hours=73)).isoformat()
            ),
            "STALE_DATA",
        ),
        (
            lambda snapshot: snapshot["meta"].update(
                as_of=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            ),
            "FUTURE_DATED",
        ),
        (lambda snapshot: snapshot["meta"].update(execution_eligible=False), "UPSTREAM_INELIGIBLE"),
        (lambda snapshot: snapshot.update(price=120.0), "deviation"),
    ],
)
def test_paper_approval_rejects_unsafe_or_unbounded_quote(
    tmp_path, mutation, error
):
    store = SQLiteStore(tmp_path / "trademind.db")
    user = store.create_user("person@example.com", hash_password("very-secure-password"))
    account = store.get_account(user.id)
    proposal = store.create_trade_proposal(
        user.id, account["id"], _decision(), int(time.time()) + 600, "proposal-create-unsafe"
    )
    snapshot = _live_snapshot()
    mutation(snapshot)

    with pytest.raises((UnsafeMarketDataError, ValidationError), match=error):
        store.approve_trade_proposal(
            user.id, proposal["id"], "unsafe-approval", snapshot
        )
    assert store.list_orders(user.id) == []


def _api_client(tmp_path, monkeypatch, *, auth_required: bool) -> TestClient:
    monkeypatch.setenv("TRADEMIND_DB_PATH", str(tmp_path / "api.db"))
    monkeypatch.setenv("JWT_SECRET", CONFIG.secret)
    monkeypatch.setenv("JWT_ISSUER", CONFIG.issuer)
    monkeypatch.setenv("APP_ENV", "test")
    monkeypatch.setenv("AUTH_REQUIRED", "true" if auth_required else "false")
    import backend.src.store as store_module

    store_module._STORE = None
    store_module._STORE_PATH = None
    from backend.api import auth_router, portfolio_router

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(portfolio_router)
    return TestClient(app)


def test_auth_api_rotates_refresh_tokens(tmp_path, monkeypatch):
    client = _api_client(tmp_path, monkeypatch, auth_required=True)
    registered = client.post(
        "/api/auth/register",
        json={"email": "admin@example.com", "password": "very-secure-password"},
    )
    assert registered.status_code == 201
    payload = registered.json()
    assert payload["user"]["role"] == "ADMIN"

    me = client.get(
        "/api/auth/me",
        headers={"Authorization": f"Bearer {payload['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["email"] == "admin@example.com"

    refreshed = client.post(
        "/api/auth/refresh", json={"refresh_token": payload["refresh_token"]}
    )
    assert refreshed.status_code == 200
    replay = client.post(
        "/api/auth/refresh", json={"refresh_token": payload["refresh_token"]}
    )
    assert replay.status_code == 401


def test_production_bootstrap_requires_secret_header(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADEMIND_DB_PATH", str(tmp_path / "bootstrap.db"))
    monkeypatch.setenv("TRADEMIND_JWT_SECRET", "s" * 40)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "admin@example.com")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_TOKEN", "b" * 40)
    monkeypatch.setenv("ALLOW_PUBLIC_REGISTRATION", "false")
    import backend.src.store as store_module

    store_module._STORE = None
    store_module._STORE_PATH = None
    from backend.api import auth_router

    app = FastAPI()
    app.include_router(auth_router)
    client = TestClient(app)
    credentials = {
        "email": "admin@example.com",
        "password": "very-secure-password",
    }

    assert client.post("/api/auth/register", json=credentials).status_code == 403
    assert client.post(
        "/api/auth/register",
        json=credentials,
        headers={"X-Bootstrap-Token": "wrong-token"},
    ).status_code == 403
    created = client.post(
        "/api/auth/register",
        json=credentials,
        headers={"X-Bootstrap-Token": "b" * 40},
    )
    assert created.status_code == 201
    assert created.json()["user"]["role"] == "ADMIN"
    assert client.post(
        "/api/auth/register",
        json={"email": "another@example.com", "password": "very-secure-password"},
    ).status_code == 403


def test_optional_auth_uses_scoped_local_paper_account(tmp_path, monkeypatch):
    client = _api_client(tmp_path, monkeypatch, auth_required=False)

    me = client.get("/api/auth/me")
    account = client.get("/api/portfolio/account")

    assert me.status_code == 200
    assert me.json()["is_local"] is True
    assert account.status_code == 200
    assert account.json()["mode"] == "PAPER"
    assert account.json()["execution_mode"] == "PAPER"


def test_portfolio_api_requires_human_approval_and_blocks_demo_fill(tmp_path, monkeypatch):
    client = _api_client(tmp_path, monkeypatch, auth_required=False)
    import backend.api.portfolio as portfolio_api
    import backend.src.trading as trading

    monkeypatch.setattr(trading, "build_trade_decision", lambda *args, **kwargs: _decision("AAPL"))
    created = client.post(
        "/api/portfolio/proposals",
        json={"ticker": "AAPL", "decision_id": "a" * 24},
        headers={"Idempotency-Key": "proposal-api-create-001"},
    )
    assert created.status_code == 201
    proposal = created.json()
    assert proposal["status"] == "PENDING"
    assert client.get("/api/portfolio/orders").json()["items"] == []
    replay = client.post(
        "/api/portfolio/proposals",
        json={"ticker": "AAPL", "decision_id": "a" * 24},
        headers={"Idempotency-Key": "proposal-api-create-001"},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == proposal["id"]
    assert replay.json()["idempotent_replay"] is True

    demo = _live_snapshot()
    demo["meta"]["is_demo"] = True
    monkeypatch.setattr(portfolio_api, "_quote", lambda ticker: demo)
    blocked = client.post(
        f"/api/portfolio/proposals/{proposal['id']}/approve",
        json={"note": "human reviewed"},
        headers={"Idempotency-Key": "human-approval-001"},
    )
    assert blocked.status_code == 409
    assert client.get("/api/portfolio/orders").json()["items"] == []

    monkeypatch.setattr(portfolio_api, "_quote", lambda ticker: _live_snapshot(ticker))
    approved = client.post(
        f"/api/portfolio/proposals/{proposal['id']}/approve",
        json={"note": "human reviewed"},
        headers={"Idempotency-Key": "human-approval-001"},
    )
    assert approved.status_code == 200
    assert approved.json()["mode"] == "PAPER"
    assert len(client.get("/api/portfolio/orders").json()["items"]) == 1
