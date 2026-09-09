"""TradeMind API - trustworthy research, risk-gated proposals and paper trading."""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any, AsyncIterator

import pandas as pd
from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.api import auth as auth_api
from backend.api import portfolio as portfolio_api
from backend.api.auth import get_current_user, require_admin
from backend.core.security import AuthConfig, AuthUser, validate_security_config
from backend.src.agent import financial_agent
from backend.src.cache import cache
from backend.src.intelligence import (
    _history,
    _indicators,
    _quality_meta,
    analyze_technicals,
    detect_anomalies,
    forecast_range,
    get_market_snapshot,
    get_news_intelligence,
    run_screener,
)
from backend.src.market_quality import market_age_limits
from backend.src.observability import (
    ai_limiter,
    finalize_ai_reservation,
    log_ai_usage,
    public_limiter,
    release_ai_reservation,
    reserve_ai_budget,
    trace_store,
    upload_limiter,
    usage_summary,
)
from backend.src.providers import get_provider
from backend.src.rag import FinancialRAG
from backend.src.store import StoreError, get_store
from backend.src.trading import build_trade_decision, run_strategy_backtest

logger = logging.getLogger("trademind.api")
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("DATA_DIR", str(ROOT / "data"))).resolve()
try:
    MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "20"))
except ValueError as exc:
    raise RuntimeError("MAX_UPLOAD_MB must be an integer") from exc
if not 1 <= MAX_UPLOAD_MB <= 200:
    raise RuntimeError("MAX_UPLOAD_MB must be between 1 and 200")
MAX_UPLOAD_BYTES = MAX_UPLOAD_MB * 1024 * 1024
MAX_DEFAULT_REQUEST_BYTES = 2 * 1024 * 1024
MAX_UPLOAD_REQUEST_BYTES = MAX_UPLOAD_BYTES + 256 * 1024
APP_ENVIRONMENT = os.getenv("APP_ENV", "development").strip().lower()
rag = FinancialRAG(DATA_DIR / "rag")


class RequestBodyLimitMiddleware:
    """Reject oversized bodies while ASGI is receiving them, before multipart parsing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_limit: int,
        upload_limit: int,
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.upload_limit = upload_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") in {"GET", "HEAD", "OPTIONS"}:
            await self.app(scope, receive, send)
            return
        limit = (
            self.upload_limit
            if scope.get("path") == "/api/documents"
            else self.default_limit
        )
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length")
        try:
            declared_length = int(raw_length) if raw_length is not None else None
        except ValueError:
            declared_length = None
        if declared_length is not None and declared_length > limit:
            await JSONResponse(status_code=413, content={"detail": "Request body is too large"})(
                scope, receive, send
            )
            return

        received = 0
        buffered: list[dict[str, Any]] = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                buffered.append(message)
                continue
            received += len(message.get("body", b""))
            if received > limit:
                await JSONResponse(
                    status_code=413,
                    content={"detail": "Request body is too large"},
                )(scope, receive, send)
                return
            buffered.append(message)
            if not message.get("more_body", False):
                break

        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(buffered):
                message = buffered[index]
                index += 1
                return message
            # Once the buffered request body has been replayed, preserve the
            # real receive channel so streaming responses can block while
            # waiting for a disconnect. Returning an immediate empty request
            # forever makes Starlette's SSE disconnect listener busy-spin and
            # can starve the response stream.
            return await receive()

        await self.app(scope, replay_receive, send)


@asynccontextmanager
async def lifespan(_: FastAPI):
    config = validate_security_config(AuthConfig.from_env())
    market_age_limits()
    if config.environment in {"prod", "production"} and "*" in origins:
        raise RuntimeError("CORS_ORIGINS cannot contain '*' in production")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    get_store().initialize()
    yield


app = FastAPI(
    title="TradeMind Intelligence API",
    description=(
        "Safety-first trading research with provenance, deterministic risk gates, "
        "human approval and paper-only execution."
    ),
    version="3.0.0",
    lifespan=lifespan,
    docs_url=None if APP_ENVIRONMENT in {"prod", "production"} else "/docs",
    redoc_url=None if APP_ENVIRONMENT in {"prod", "production"} else "/redoc",
)
origins = [
    value.strip()
    for value in os.getenv(
        "CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",")
    if value.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "X-Bootstrap-Token",
        "X-Request-ID",
    ],
)
app.add_middleware(GZipMiddleware, minimum_size=1_000)
app.add_middleware(
    RequestBodyLimitMiddleware,
    default_limit=MAX_DEFAULT_REQUEST_BYTES,
    upload_limit=MAX_UPLOAD_REQUEST_BYTES,
)
app.include_router(auth_api.router)
app.include_router(portfolio_api.router)


@app.middleware("http")
async def request_context(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    request.state.request_id = request_id
    started = time.perf_counter()
    if request.url.path.startswith(
        (
            "/api/market/",
            "/api/forecast/",
            "/api/anomalies/",
            "/api/news/",
            "/api/technical-analysis/",
            "/api/screener",
        )
    ):
        client = request.client.host if request.client else "unknown"
        if not public_limiter.allow(f"{client}:{request.url.path.split('/')[2]}"):
            return JSONResponse(
                status_code=429,
                content={"detail": "Quá nhiều yêu cầu dữ liệu; vui lòng thử lại sau"},
                headers={"X-Request-ID": request_id, "Retry-After": "60"},
            )
    response = await call_next(request)
    duration_ms = (time.perf_counter() - started) * 1_000
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Response-Time"] = f"{duration_ms:.1f}ms"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    logger.info(
        "%s %s %s %.1fms request_id=%s",
        request.method,
        request.url.path,
        response.status_code,
        duration_ms,
        request_id,
    )
    return response


class TickerBody(BaseModel):
    ticker: str = Field(min_length=1, max_length=24)


class ScreenerBody(BaseModel):
    query: str = Field(min_length=3, max_length=500)


class AskBody(BaseModel):
    question: str = Field(min_length=3, max_length=2_000)
    document_id: str | None = None
    ticker: str | None = None
    account_id: str | None = None


def clean_ticker(ticker: str) -> str:
    normalized = ticker.strip().upper()
    if not re.fullmatch(r"[A-Z0-9.\-^]{1,24}", normalized):
        raise HTTPException(400, "Mã cổ phiếu không hợp lệ")
    return normalized


def _storage_ok() -> bool:
    try:
        get_store().human_user_count()
        return True
    except Exception:
        return False


def _health_payload() -> dict[str, Any]:
    storage_ok = _storage_ok()
    config = AuthConfig.from_env()
    provider = get_provider()
    return {
        "status": "ok" if storage_ok else "degraded",
        "version": "3.0.0",
        "environment": config.environment,
        "auth_required": config.auth_required,
        "execution_mode": "PAPER",
        "storage": "sqlite" if storage_ok else "unavailable",
        "cache": cache.status(),
        "llm_provider": provider.name,
        "rag_documents": rag.document_count,
        "execution": {
            "paper_enabled": True,
            "live_enabled": False,
            "human_approval_required": True,
            "demo_data_blocked": True,
        },
    }


@app.get("/api/health", tags=["system"])
def health():
    return _health_payload()


@app.get("/health", include_in_schema=False)
def health_alias():
    return _health_payload()


@app.get("/api/config", tags=["system"])
def public_config():
    config = AuthConfig.from_env()
    return {
        "product": "TradeMind",
        "version": "3.0.0",
        "auth_required": config.auth_required,
        "execution_mode": "PAPER",
        "live_trading_available": False,
    }


@app.get("/api/ops/usage", tags=["operations"])
def ai_usage(_: Annotated[AuthUser, Depends(require_admin)]):
    return usage_summary(get_store().path)


@app.get("/api/ops/traces", tags=["operations"])
def recent_traces(
    _: Annotated[AuthUser, Depends(require_admin)],
    limit: int = Query(20, ge=1, le=100),
):
    return {"items": trace_store.recent(limit)}


@app.get("/api/market/{ticker}", tags=["market"])
def market(
    ticker: str,
    period: str = Query("1y", pattern="^(3mo|6mo|1y|2y|5y)$"),
):
    return get_market_snapshot(clean_ticker(ticker), period)


@app.get("/api/forecast/{ticker}", tags=["intelligence"])
def forecast(ticker: str, horizon: int = Query(7, ge=1, le=10)):
    return forecast_range(clean_ticker(ticker), horizon)


@app.get("/api/anomalies/{ticker}", tags=["intelligence"])
def anomalies(ticker: str):
    return detect_anomalies(clean_ticker(ticker))


@app.get("/api/news/{ticker}", tags=["intelligence"])
def news(ticker: str):
    return get_news_intelligence(clean_ticker(ticker))


@app.get("/api/technical-analysis/{ticker}", tags=["intelligence"])
def technical_analysis(ticker: str):
    return analyze_technicals(clean_ticker(ticker))


@app.post("/api/screener", tags=["intelligence"])
def screener(body: ScreenerBody):
    return run_screener(body.query)


def _account_context(user_id: str, account_id: str | None, ticker: str) -> dict[str, Any]:
    account = portfolio_api._marked_account(user_id, account_id)
    position = next(
        (item for item in account["positions"] if item["ticker"] == ticker), None
    )
    return {
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


@app.get("/api/trade/decision/{ticker}", tags=["trading-agent"])
def trade_decision(
    ticker: str,
    user: Annotated[AuthUser, Depends(get_current_user)],
    account_id: str | None = Query(default=None),
    risk_fraction: float = Query(default=0.01, gt=0, le=0.05),
    max_position_fraction: float = Query(default=0.10, gt=0, le=0.50),
    max_gross_exposure_fraction: float = Query(default=0.80, gt=0, le=1.0),
):
    symbol = clean_ticker(ticker)
    try:
        account = _account_context(user.id, account_id, symbol)
        return build_trade_decision(
            symbol,
            account=account,
            risk_limits={
                "risk_per_trade_pct": risk_fraction,
                "max_position_pct": max_position_fraction,
                "max_gross_exposure_pct": max_gross_exposure_fraction,
            },
        )
    except StoreError as exc:
        raise HTTPException(404, str(exc)) from exc


def _strategy_frame(ticker: str, period: str) -> tuple[pd.DataFrame, str]:
    frame, source = _history(ticker, period)
    enriched = _indicators(frame).dropna(subset=["ma20", "ma50", "rsi", "macd_signal"])
    enriched = enriched.copy()
    enriched["signal"] = (
        (enriched.close > enriched.ma20)
        & (enriched.ma20 > enriched.ma50)
        & (enriched.macd > enriched.macd_signal)
        & (enriched.rsi < 72)
    ).astype(float)
    return enriched, source


@app.get("/api/trade/backtest/{ticker}", tags=["trading-agent"])
def strategy_backtest(
    ticker: str,
    _: Annotated[AuthUser, Depends(get_current_user)],
    period: str = Query("2y", pattern="^(1y|2y|5y)$"),
    initial_capital: float = Query(100_000, gt=0, le=1_000_000_000),
    transaction_cost_bps: float = Query(10, ge=0, le=500),
    slippage_bps: float = Query(5, ge=0, le=500),
):
    symbol = clean_ticker(ticker)
    frame, source = _strategy_frame(symbol, period)
    if len(frame) < 40:
        raise HTTPException(422, "Không đủ dữ liệu để backtest")
    result = run_strategy_backtest(
        frame[["date", "close", "signal"]],
        initial_capital=initial_capital,
        transaction_cost_bps=transaction_cost_bps,
        slippage_bps=slippage_bps,
    )
    result["ticker"] = symbol
    result["strategy"] = {
        "name": "trend-momentum-long-only-v1",
        "rules": "close > MA20 > MA50, MACD > signal, RSI < 72",
    }
    result["meta"] = _quality_meta(frame, source)
    result["meta"]["research_only"] = True
    return result


@app.get("/api/documents", tags=["research-rag"])
def documents(user: Annotated[AuthUser, Depends(get_current_user)]):
    return {"items": rag.list_documents(owner_id=user.id)}


@app.post("/api/documents", tags=["research-rag"], status_code=201)
async def upload_document(
    file: UploadFile = File(...),
    user: AuthUser = Depends(get_current_user),
):
    if not upload_limiter.allow(f"upload:{user.id}"):
        await file.close()
        raise HTTPException(429, "Đã vượt giới hạn tải tài liệu; vui lòng thử lại sau")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    await file.close()
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"Tệp vượt quá giới hạn {MAX_UPLOAD_BYTES // 1024 // 1024} MB")
    try:
        import asyncio

        return await asyncio.to_thread(
            rag.ingest, file.filename or "report.pdf", data, user.id
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.delete("/api/documents/{document_id}", tags=["research-rag"])
def delete_document(
    document_id: str,
    user: Annotated[AuthUser, Depends(get_current_user)],
):
    try:
        removed = rag.delete_document(document_id, owner_id=user.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not removed:
        raise HTTPException(404, "Không tìm thấy tài liệu")
    return {"removed": True}


async def sse(
    events: AsyncIterator[dict[str, Any]],
    endpoint: str | None = None,
    *,
    user_id: str = "",
    reservation_id: str | None = None,
) -> AsyncIterator[str]:
    usage_logged = False
    provider_started = False
    provider_model = "interrupted-stream"
    try:
        async for event in events:
            if event.get("type") == "_provider_start":
                provider_started = True
                provider_model = str(event.get("model") or event.get("provider") or provider_model)
                continue
            if endpoint and event.get("type") == "done" and (usage := event.get("usage")):
                log_ai_usage(
                    get_store().path,
                    endpoint=endpoint,
                    model=usage.get("model", "unknown"),
                    input_tokens=int(usage.get("input_tokens", 0)),
                    output_tokens=int(usage.get("output_tokens", 0)),
                    estimated_cost=float(usage.get("estimated_cost", 0)),
                    trace_id=(event.get("trace") or {}).get("request_id", str(uuid.uuid4())),
                    user_id=user_id,
                    reservation_id=reservation_id,
                )
                usage_logged = True
            yield (
                f"event: {event.get('type', 'message')}\n"
                f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            )
    except Exception as exc:
        logger.exception("SSE stream failed")
        event = {
            "type": "error",
            "content": "Luồng AI gặp lỗi; không có lệnh nào được tạo.",
            "error_type": type(exc).__name__,
        }
        yield f"event: error\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
    finally:
        if reservation_id and not usage_logged:
            if endpoint and provider_started:
                finalize_ai_reservation(
                    get_store().path,
                    reservation_id,
                    endpoint=endpoint,
                    model=provider_model,
                )
            else:
                release_ai_reservation(get_store().path, reservation_id)


@app.post("/api/rag/ask", tags=["research-rag"])
async def rag_ask(
    body: AskBody,
    request: Request,
    user: Annotated[AuthUser, Depends(get_current_user)],
):
    if not ai_limiter.allow(f"rag:{user.id}"):
        raise HTTPException(429, "Quá nhiều yêu cầu AI, vui lòng thử lại sau")
    reservation_id = reserve_ai_budget(get_store().path, user.id)
    if reservation_id is None:
        raise HTTPException(429, "Đã đạt ngân sách AI trong ngày")
    return StreamingResponse(
        sse(
            rag.answer_stream(body.question, body.document_id, owner_id=user.id),
            "rag",
            user_id=user.id,
            reservation_id=reservation_id,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _persisted_agent_stream(
    *,
    user: AuthUser,
    ticker: str,
    question: str,
    account_id: str | None,
    request_id: str,
) -> AsyncIterator[dict[str, Any]]:
    account = _account_context(user.id, account_id, ticker)
    completed: dict[str, Any] | None = None
    error = ""
    try:
        async for event in financial_agent.stream(
            ticker,
            question,
            get_store().path,
            account=account,
            request_id=request_id,
        ):
            if event.get("type") == "done":
                completed = event
            yield event
    except Exception as exc:
        error = f"{type(exc).__name__}: agent stream failed"
        raise
    finally:
        try:
            get_store().record_agent_run(
                user.id,
                account_id=account_id or get_store().get_account(user.id)["id"],
                ticker=ticker,
                agent_name="TradeMind Copilot v3",
                status="SUCCEEDED" if completed else "FAILED",
                input_data={"question": question, "ticker": ticker},
                output_data=completed or {},
                error=error,
                trace_id=request_id,
            )
        except StoreError:
            logger.exception("Could not persist agent run")


@app.post("/api/copilot/stream", tags=["trading-agent"])
async def copilot(
    body: AskBody,
    request: Request,
    user: Annotated[AuthUser, Depends(get_current_user)],
):
    ticker = clean_ticker(body.ticker or "AAPL")
    if not ai_limiter.allow(f"copilot:{user.id}"):
        raise HTTPException(429, "Quá nhiều yêu cầu AI, vui lòng thử lại sau")
    reservation_id = reserve_ai_budget(get_store().path, user.id)
    if reservation_id is None:
        raise HTTPException(429, "Đã đạt ngân sách AI trong ngày")
    events = _persisted_agent_stream(
        user=user,
        ticker=ticker,
        question=body.question,
        account_id=body.account_id,
        request_id=request.state.request_id,
    )
    return StreamingResponse(
        sse(
            events,
            "copilot",
            user_id=user.id,
            reservation_id=reservation_id,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
