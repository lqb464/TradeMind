"""HTTP routers that extend the TradeMind application."""

from backend.api.auth import router as auth_router
from backend.api.portfolio import router as portfolio_router

__all__ = ["auth_router", "portfolio_router"]
