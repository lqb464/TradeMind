"""Authenticated watchlist, paper portfolio, and human approval endpoints."""

from __future__ import annotations

import re
import time
import uuid
from typing import Annotated, Any, Literal, NoReturn

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.api.auth import get_current_user
from backend.core.security import AuthUser
from backend.src.market_quality import assess_market_snapshot
from backend.src.store import (
    AccessDeniedError,
    ConflictError,
    NotFoundError,
    StoreError,
    UnsafeMarketDataError,
    ValidationError,
    get_store,
)


router = APIRouter(prefix="/api/portfolio", tags=["paper-portfolio"])
_TICKER_RE = re.compile(r"^[A-Z0-9.\-^]{1,24}$")


class TickerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str = Field(min_length=1, max_length=24)

    @field_validator("ticker")
    @classmethod
    def valid_ticker(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not _TICKER_RE.fullmatch(normalized):
            raise ValueError("Invalid ticker")
        return normalized


class AccountCreateBody(BaseModel):
    name: str = Field(default="Paper Account", min_length=1, max_length=80)
    initial_cash: float = Field(default=100_000, ge=0, le=1_000_000_000)
    currency: str = Field(default="USD", min_length=3, max_length=3)


class ProposalCreateBody(TickerBody):
    decision_id: str = Field(pattern=r"^[a-f0-9]{24,64}$")
    account_id: str | None = None
    risk_fraction: float = Field(default=0.01, gt=0, le=0.05)
    max_position_fraction: float = Field(default=0.10, gt=0, le=0.50)
    max_gross_exposure_fraction: float = Field(default=0.80, gt=0, le=1.0)
    expires_in_minutes: int = Field(default=30, ge=5, le=24 * 60)


class ProposalDecisionBody(BaseModel):
    note: str = Field(default="", max_length=2000)


class AccountControlsBody(BaseModel):
    trading_enabled: bool | None = None
    kill_switch: bool | None = None
    reason: str = Field(min_length=3, max_length=500)

    @model_validator(mode="after")
    def has_change(self):
        if self.trading_enabled is None and self.kill_switch is None:
            raise ValueError("At least one account control must be supplied")
        return self


def _store_error(exc: StoreError) -> NoReturn:
    if isinstance(exc, NotFoundError):
        status = 404
    elif isinstance(exc, AccessDeniedError):
        status = 403
    elif isinstance(exc, ValidationError):
        status = 422
    elif isinstance(exc, (ConflictError, UnsafeMarketDataError)):
        status = 409
    else:
        status = 500
    raise HTTPException(status_code=status, detail=str(exc)) from exc


def _quote(ticker: str) -> dict[str, Any]:
    """Fetch a quote only through TradeMind's existing intelligence boundary."""

    from backend.src.intelligence import get_market_snapshot

    try:
        snapshot = get_market_snapshot(ticker, "3mo")
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Market quote is unavailable") from exc
    if not isinstance(snapshot, dict):
        raise HTTPException(status_code=503, detail="Market provider returned an invalid snapshot")
    return snapshot


def _marked_account(user_id: str, account_id: str | None = None) -> dict[str, Any]:
    store = get_store()
    summary = store.account_cost_summary(user_id, account_id)
    marked_positions: list[dict[str, Any]] = []
    market_value = 0.0
    unrealized_pnl = 0.0
    has_demo_marks = False
    marks_complete = True
    marks_execution_eligible = True
    mark_issues: list[dict[str, str]] = []
    for position in summary["positions"]:
        snapshot: dict[str, Any] | None = None
        try:
            snapshot = _quote(position["ticker"])
            price = float(snapshot["price"])
            if price <= 0:
                raise ValueError("non-positive price")
            quality = assess_market_snapshot(snapshot)
            mark_eligible = bool(quality["execution_eligible"])
            quote_currency = str(quality["canonical"].get("currency") or "")
            if quote_currency != summary["currency"]:
                mark_eligible = False
                mark_issues.append(
                    {
                        "ticker": position["ticker"],
                        "code": "CURRENCY_MISMATCH",
                        "message": f"Quote {quote_currency or 'unknown'} != account {summary['currency']}",
                    }
                )
            if not mark_eligible:
                mark_issues.append(
                    {
                        "ticker": position["ticker"],
                        "code": "UNSAFE_MARK",
                        "message": ", ".join(
                            issue["code"] for issue in quality["errors"][:4]
                        ),
                    }
                )
        except (HTTPException, KeyError, TypeError, ValueError):
            price = float(position["average_cost"])
            mark_eligible = False
            marks_complete = False
            mark_issues.append(
                {
                    "ticker": position["ticker"],
                    "code": "QUOTE_UNAVAILABLE",
                    "message": "Using cost basis for display only",
                }
            )
        meta = snapshot.get("meta", {}) if snapshot else {
            "source": "cost-basis-fallback",
            "status": "unavailable",
            "is_demo": True,
            "is_stale": True,
            "execution_eligible": False,
        }
        is_demo = bool(meta.get("is_demo")) or snapshot is None
        has_demo_marks = has_demo_marks or is_demo
        marks_execution_eligible = marks_execution_eligible and mark_eligible
        value = position["quantity"] * price
        pnl = (price - position["average_cost"]) * position["quantity"]
        market_value += value
        unrealized_pnl += pnl
        marked_positions.append(
            {
                **position,
                "market_price": round(price, 6),
                "market_value": round(value, 6),
                "unrealized_pnl": round(pnl, 6),
                "mark_status": "execution-quality" if mark_eligible else "display-only",
                "quote_meta": meta,
            }
        )
    equity = round(summary["cash"] + market_value, 6)
    daily_pnl = summary["daily_pnl"]
    daily_pnl_status = "unavailable"
    if marks_complete and marks_execution_eligible:
        daily_pnl = store.mark_daily_equity(user_id, summary["id"], equity)
        daily_pnl_status = "session-baseline"
    return {
        **{key: value for key, value in summary.items() if key != "positions"},
        "daily_pnl": daily_pnl,
        "positions": marked_positions,
        "market_value": round(market_value, 6),
        "gross_exposure": round(market_value, 6),
        "unrealized_pnl": round(unrealized_pnl, 6),
        "equity": equity,
        "buying_power": round(summary["cash"], 6),
        "marks_include_demo_data": has_demo_marks,
        "marks_complete": marks_complete,
        "portfolio_marks_execution_eligible": marks_execution_eligible,
        "mark_issues": mark_issues,
        "daily_pnl_status": daily_pnl_status,
        "execution_mode": "PAPER",
    }


@router.get("/accounts")
def list_accounts(user: Annotated[AuthUser, Depends(get_current_user)]):
    return {"items": get_store().list_accounts(user.id)}


@router.post("/accounts", status_code=201)
def create_account(
    body: AccountCreateBody,
    user: Annotated[AuthUser, Depends(get_current_user)],
):
    try:
        return get_store().create_paper_account(
            user.id, body.name, body.initial_cash, body.currency
        )
    except StoreError as exc:
        _store_error(exc)


@router.get("/account")
def account_summary(
    user: Annotated[AuthUser, Depends(get_current_user)],
    account_id: str | None = Query(default=None),
):
    try:
        return _marked_account(user.id, account_id)
    except StoreError as exc:
        _store_error(exc)


@router.post("/accounts/{account_id}/controls")
def update_account_controls(
    account_id: str,
    body: AccountControlsBody,
    user: Annotated[AuthUser, Depends(get_current_user)],
):
    try:
        account = get_store().update_account_controls(
            user.id,
            account_id,
            trading_enabled=body.trading_enabled,
            kill_switch=body.kill_switch,
            reason=body.reason,
        )
        account["control_events"] = get_store().list_account_control_events(
            user.id, account_id, limit=10
        )
        return account
    except StoreError as exc:
        _store_error(exc)


@router.get("/accounts/{account_id}/control-events")
def account_control_events(
    account_id: str,
    user: Annotated[AuthUser, Depends(get_current_user)],
    limit: int = Query(default=50, ge=1, le=500),
):
    try:
        return {
            "items": get_store().list_account_control_events(
                user.id, account_id, limit=limit
            )
        }
    except StoreError as exc:
        _store_error(exc)


@router.get("/watchlist")
def list_watchlist(user: Annotated[AuthUser, Depends(get_current_user)]):
    return {"items": get_store().list_watchlist(user.id)}


@router.post("/watchlist", status_code=201)
def add_watchlist(
    body: TickerBody,
    user: Annotated[AuthUser, Depends(get_current_user)],
):
    try:
        return get_store().add_watchlist(user.id, body.ticker)
    except StoreError as exc:
        _store_error(exc)


@router.delete("/watchlist/{ticker}")
def remove_watchlist(
    ticker: str,
    user: Annotated[AuthUser, Depends(get_current_user)],
):
    try:
        return {"removed": get_store().remove_watchlist(user.id, ticker)}
    except StoreError as exc:
        _store_error(exc)


@router.get("/proposals")
def list_proposals(
    user: Annotated[AuthUser, Depends(get_current_user)],
    proposal_status: Literal["PENDING", "APPROVED", "REJECTED", "EXPIRED"] | None = Query(
        default=None, alias="status"
    ),
    limit: int = Query(default=100, ge=1, le=500),
):
    try:
        return {
            "items": get_store().list_proposals(
                user.id, status=proposal_status, limit=limit
            )
        }
    except StoreError as exc:
        _store_error(exc)


@router.post("/proposals", status_code=201)
def create_proposal(
    body: ProposalCreateBody,
    user: Annotated[AuthUser, Depends(get_current_user)],
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
    ],
):
    """Run the proposal-only strategy.  This endpoint never creates an order."""

    store = get_store()
    try:
        account = _marked_account(user.id, body.account_id)
    except StoreError as exc:
        _store_error(exc)
    position = next(
        (item for item in account["positions"] if item["ticker"] == body.ticker), None
    )
    account_context = {
        "equity": account["equity"],
        "currency": account["currency"],
        "cash": account["cash"],
        "gross_exposure": account["gross_exposure"],
        "current_position_quantity": position["quantity"] if position else 0,
        "current_position_value": position["market_value"] if position else 0,
        "daily_pnl": account["daily_pnl"],
        "trading_enabled": account["trading_enabled"],
        "kill_switch": account["kill_switch"],
        "portfolio_marks_execution_eligible": account[
            "portfolio_marks_execution_eligible"
        ],
    }
    risk_limits = {
        "risk_per_trade_pct": body.risk_fraction,
        "max_position_pct": body.max_position_fraction,
        "max_gross_exposure_pct": body.max_gross_exposure_fraction,
    }
    run_input = {
        "ticker": body.ticker,
        "account_id": account["id"],
        "account": account_context,
        "risk_limits": risk_limits,
    }
    trace_id = str(uuid.uuid4())
    existing = store.find_proposal_by_idempotency(
        user.id, account["id"], idempotency_key
    )
    if existing is not None:
        if existing["decision_id"] != body.decision_id:
            _store_error(ConflictError("Idempotency-Key was already used for another decision"))
        return {**existing, "idempotent_replay": True}
    try:
        # Loose, local import keeps strategy and HTTP/storage layers acyclic.
        from backend.src.trading import build_trade_decision

        decision = build_trade_decision(
            body.ticker,
            account=account_context,
            risk_limits=risk_limits,
        )
        if not isinstance(decision, dict):
            raise TypeError("Strategy did not return a decision object")
        if str(decision.get("ticker") or "").upper() != body.ticker:
            raise ValueError("Strategy returned a decision for another ticker")
        if decision.get("decision_id") != body.decision_id:
            raise ConflictError(
                "Decision changed since it was displayed; refresh the decision before proposing"
            )
        proposal = store.create_trade_proposal(
            user.id,
            account["id"],
            decision,
            int(time.time()) + body.expires_in_minutes * 60,
            idempotency_key,
        )
        run = store.record_agent_run(
            user.id,
            account_id=account["id"],
            proposal_id=proposal["id"],
            ticker=body.ticker,
            agent_name=str(decision.get("version") or "trade-decision"),
            status="SUCCEEDED",
            input_data=run_input,
            output_data=decision,
            trace_id=trace_id,
        )
    except StoreError as exc:
        _store_error(exc)
    except Exception as exc:
        try:
            store.record_agent_run(
                user.id,
                account_id=account["id"],
                ticker=body.ticker,
                agent_name="trade-decision",
                status="FAILED",
                input_data=run_input,
                error=str(exc),
                trace_id=trace_id,
            )
        except StoreError:
            pass
        raise HTTPException(status_code=503, detail="Trade decision could not be built") from exc
    return {**proposal, "agent_run_id": run["id"]}


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(
    proposal_id: str,
    body: ProposalDecisionBody,
    user: Annotated[AuthUser, Depends(get_current_user)],
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=128)],
):
    """Human-only PAPER fill boundary; never routes to a live broker."""

    store = get_store()
    try:
        proposal = store.get_proposal(user.id, proposal_id)
        existing = store.find_order_by_idempotency(
            user.id, proposal["account_id"], idempotency_key
        )
        if existing is not None:
            if existing["proposal_id"] != proposal_id:
                raise ConflictError("Idempotency-Key was already used for another proposal")
            return existing
        snapshot = _quote(proposal["ticker"])
        portfolio_marks: dict[str, dict[str, Any]] = {}
        if proposal["side"] == "BUY":
            account = store.account_cost_summary(user.id, proposal["account_id"])
            for position in account["positions"]:
                symbol = position["ticker"]
                portfolio_marks[symbol] = (
                    snapshot if symbol == proposal["ticker"] else _quote(symbol)
                )
        return store.approve_trade_proposal(
            user.id,
            proposal_id,
            idempotency_key,
            snapshot,
            note=body.note,
            portfolio_marks=portfolio_marks,
        )
    except StoreError as exc:
        _store_error(exc)


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(
    proposal_id: str,
    body: ProposalDecisionBody,
    user: Annotated[AuthUser, Depends(get_current_user)],
):
    try:
        return get_store().reject_trade_proposal(user.id, proposal_id, note=body.note)
    except StoreError as exc:
        _store_error(exc)


@router.get("/orders")
def order_history(
    user: Annotated[AuthUser, Depends(get_current_user)],
    account_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
):
    return {"items": get_store().list_orders(user.id, account_id=account_id, limit=limit)}


@router.get("/agent-runs")
def agent_run_history(
    user: Annotated[AuthUser, Depends(get_current_user)],
    limit: int = Query(default=100, ge=1, le=500),
):
    return {"items": get_store().list_agent_runs(user.id, limit=limit)}


__all__ = ["router"]
