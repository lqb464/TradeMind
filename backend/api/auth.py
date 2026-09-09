"""Authentication API with refresh-token rotation and optional local mode."""

from __future__ import annotations

import hmac
import os
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.core.security import (
    AuthConfig,
    AuthUser,
    PasswordPolicyError,
    TokenError,
    decode_token,
    hash_password,
    issue_token_pair,
    normalize_email,
    validate_security_config,
    verify_password,
)
from backend.src.store import (
    AccessDeniedError,
    ConflictError,
    StoreError,
    get_store,
)
from backend.src.observability import auth_limiter


router = APIRouter(prefix="/api/auth", tags=["auth"])
_bearer = HTTPBearer(auto_error=False)
_DUMMY_PASSWORD_HASH = hash_password("not-a-real-password-for-timing-only")


class CredentialsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=10, max_length=256)
    name: str | None = Field(default=None, min_length=1, max_length=80)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        return normalize_email(value)


class RefreshBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    refresh_token: str = Field(min_length=32, max_length=16_384)


class UserResponse(BaseModel):
    id: str
    email: str
    name: str = ""
    role: str
    is_local: bool = False
    created_at: str | None = None


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


def _identity(user_record) -> AuthUser:
    return AuthUser(
        id=user_record.id,
        email=user_record.email,
        role=user_record.role,
        is_local=user_record.is_local,
    )


def _token_response(user_record, pair) -> dict:
    return {
        "access_token": pair.access_token,
        "refresh_token": pair.refresh_token,
        "token_type": "bearer",
        "expires_in": pair.expires_in,
        "user": user_record.public(),
    }


def _rate_limit(request: Request, scope: str) -> None:
    client = request.client.host if request.client else "unknown"
    if not auth_limiter.allow(f"{scope}:{client}"):
        raise HTTPException(
            status_code=429,
            detail="Too many authentication attempts",
            headers={"Retry-After": "300"},
        )


def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer)
    ] = None,
) -> AuthUser:
    """Resolve a bearer identity, or the stable local user when auth is off."""

    config = validate_security_config(AuthConfig.from_env())
    store = get_store()
    if credentials is None:
        if config.auth_required:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return _identity(store.ensure_local_user())

    try:
        claims = decode_token(
            credentials.credentials, expected_type="access", config=config
        )
    except TokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc
    user = store.get_user(claims.subject)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User is no longer active",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return _identity(user)


def require_admin(user: Annotated[AuthUser, Depends(get_current_user)]) -> AuthUser:
    if user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Administrator role required")
    return user


@router.post("/register", response_model=TokenResponse, status_code=201)
def register(body: CredentialsBody, request: Request):
    _rate_limit(request, "register")
    config = validate_security_config(AuthConfig.from_env())
    store = get_store()
    human_users = store.human_user_count()
    public_registration = (
        os.getenv("ALLOW_PUBLIC_REGISTRATION", "false").strip().lower()
        in {"1", "true", "yes", "on"}
    )
    if config.environment in {"production", "prod"} and human_users > 0 and not public_registration:
        raise HTTPException(status_code=403, detail="Public registration is disabled")
    if (
        human_users == 0
        and config.environment in {"production", "prod"}
    ):
        supplied_token = request.headers.get("X-Bootstrap-Token", "")
        authorized_email = body.email == config.bootstrap_admin_email
        authorized_token = hmac.compare_digest(
            supplied_token.encode("utf-8"),
            config.bootstrap_admin_token.encode("utf-8"),
        )
        if not authorized_email or not authorized_token:
            raise HTTPException(
                status_code=403,
                detail="Initial administrator bootstrap credentials are invalid",
            )
    try:
        user = store.create_user(
            body.email,
            hash_password(body.password),
            display_name=body.name or "",
            required_first_email=(
                config.bootstrap_admin_email
                if config.environment in {"production", "prod"}
                else None
            ),
        )
        pair = issue_token_pair(_identity(user), config=config)
        store.register_refresh_token(user.id, pair.refresh_token, pair.refresh_expires_at)
    except ConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (PasswordPolicyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except StoreError as exc:
        raise HTTPException(status_code=503, detail="Authentication storage is unavailable") from exc
    return _token_response(user, pair)


@router.post("/login", response_model=TokenResponse)
def login(body: CredentialsBody, request: Request):
    _rate_limit(request, "login")
    config = validate_security_config(AuthConfig.from_env())
    store = get_store()
    user = store.get_user_by_email(body.email)
    password_hash = user.password_hash if user is not None else _DUMMY_PASSWORD_HASH
    password_ok = verify_password(body.password, password_hash)
    if user is None or not password_ok or not user.is_active or user.is_local:
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    pair = issue_token_pair(_identity(user), config=config)
    store.register_refresh_token(user.id, pair.refresh_token, pair.refresh_expires_at)
    return _token_response(user, pair)


@router.post("/refresh", response_model=TokenResponse)
def refresh(body: RefreshBody, request: Request):
    _rate_limit(request, "refresh")
    config = validate_security_config(AuthConfig.from_env())
    try:
        claims = decode_token(body.refresh_token, expected_type="refresh", config=config)
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    store = get_store()
    user = store.get_user(claims.subject)
    if user is None or not user.is_active or user.is_local:
        raise HTTPException(status_code=401, detail="User is no longer active")
    pair = issue_token_pair(_identity(user), config=config)
    try:
        store.rotate_refresh_token(
            user.id,
            body.refresh_token,
            pair.refresh_token,
            pair.refresh_expires_at,
        )
    except AccessDeniedError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return _token_response(user, pair)


@router.post("/logout", status_code=204)
def logout(body: RefreshBody) -> Response:
    config = validate_security_config(AuthConfig.from_env())
    try:
        decode_token(body.refresh_token, expected_type="refresh", config=config)
    except TokenError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    get_store().revoke_refresh_token(body.refresh_token)
    return Response(status_code=204)


@router.get("/me", response_model=UserResponse)
def me(user: Annotated[AuthUser, Depends(get_current_user)]):
    record = get_store().get_user(user.id)
    if record is None:
        raise HTTPException(status_code=401, detail="User is no longer active")
    return record.public()


__all__ = ["get_current_user", "require_admin", "router"]
