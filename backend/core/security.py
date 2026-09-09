"""Dependency-free password hashing and signed authentication tokens.

The cryptographic implementation deliberately uses only Python's standard
library.  Passwords are encoded with scrypt and bearer tokens use the compact
JWT representation with a fixed HMAC-SHA256 algorithm.  Token persistence and
revocation live in :mod:`backend.src.store`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Literal


Role = Literal["USER", "ADMIN"]
TokenType = Literal["access", "refresh"]

_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
_DEFAULT_DEV_SECRET = "trademind-local-development-secret-change-before-production"
_DEFAULT_BOOTSTRAP_TOKEN = "trademind-local-bootstrap-token-change-before-production"
_JWT_HEADER = {"alg": "HS256", "typ": "JWT"}
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024


class SecurityConfigError(RuntimeError):
    """Raised when production authentication settings are unsafe."""


class PasswordPolicyError(ValueError):
    """Raised when a password does not satisfy the local policy."""


class TokenError(ValueError):
    """Raised for invalid, expired, or incorrectly scoped bearer tokens."""


@dataclass(frozen=True)
class AuthConfig:
    secret: str
    issuer: str = "trademind"
    access_ttl_seconds: int = 30 * 60
    refresh_ttl_seconds: int = 14 * 24 * 60 * 60
    auth_required: bool = True
    environment: str = "development"
    bootstrap_admin_email: str = ""
    bootstrap_admin_token: str = ""

    @classmethod
    def from_env(cls) -> "AuthConfig":
        access_minutes = _bounded_int("JWT_ACCESS_MINUTES", 30, 1, 24 * 60)
        refresh_days = _bounded_int("JWT_REFRESH_DAYS", 14, 1, 90)
        return cls(
            secret=(
                os.getenv("TRADEMIND_JWT_SECRET")
                or os.getenv("JWT_SECRET")
                or _DEFAULT_DEV_SECRET
            ),
            issuer=(os.getenv("JWT_ISSUER") or "trademind").strip(),
            access_ttl_seconds=access_minutes * 60,
            refresh_ttl_seconds=refresh_days * 24 * 60 * 60,
            auth_required=_env_bool("AUTH_REQUIRED", True),
            environment=(os.getenv("APP_ENV") or "development").strip().lower(),
            bootstrap_admin_email=(
                os.getenv("BOOTSTRAP_ADMIN_EMAIL") or ""
            ).strip().lower(),
            bootstrap_admin_token=(
                os.getenv("BOOTSTRAP_ADMIN_TOKEN") or ""
            ).strip(),
        )


@dataclass(frozen=True)
class AuthUser:
    id: str
    email: str
    role: Role
    is_local: bool = False


@dataclass(frozen=True)
class TokenClaims:
    subject: str
    email: str
    role: Role
    token_type: TokenType
    issuer: str
    issued_at: int
    expires_at: int
    token_id: str


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    access_expires_at: int
    refresh_expires_at: int

    @property
    def expires_in(self) -> int:
        return max(0, self.access_expires_at - int(time.time()))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None else default
    except ValueError as exc:
        raise SecurityConfigError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise SecurityConfigError(f"{name} must be between {minimum} and {maximum}")
    return value


def validate_security_config(config: AuthConfig | None = None) -> AuthConfig:
    """Validate settings and fail closed when running in production."""

    config = config or AuthConfig.from_env()
    if not config.issuer or len(config.issuer) > 128:
        raise SecurityConfigError("JWT_ISSUER must contain between 1 and 128 characters")
    if config.environment in {"production", "prod"}:
        if not config.auth_required:
            raise SecurityConfigError("AUTH_REQUIRED must be enabled in production")
        if config.secret == _DEFAULT_DEV_SECRET or len(config.secret.encode("utf-8")) < 32:
            raise SecurityConfigError(
                "TRADEMIND_JWT_SECRET (or JWT_SECRET) must be a unique 32+ byte secret in production"
            )
        if not config.bootstrap_admin_email:
            raise SecurityConfigError(
                "BOOTSTRAP_ADMIN_EMAIL is required in production to protect first-user setup"
            )
        normalize_email(config.bootstrap_admin_email)
        if (
            config.bootstrap_admin_token == _DEFAULT_BOOTSTRAP_TOKEN
            or len(config.bootstrap_admin_token.encode("utf-8")) < 32
        ):
            raise SecurityConfigError(
                "BOOTSTRAP_ADMIN_TOKEN must be a unique 32+ byte secret in production"
            )
    elif len(config.secret.encode("utf-8")) < 32:
        raise SecurityConfigError("JWT secret must contain at least 32 bytes")
    return config


def normalize_email(email: str) -> str:
    normalized = (email or "").strip().lower()
    if len(normalized) > 320 or not _EMAIL_RE.fullmatch(normalized):
        raise ValueError("Invalid email address")
    return normalized


def _validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise PasswordPolicyError("Password must be text")
    if len(password) < 10:
        raise PasswordPolicyError("Password must contain at least 10 characters")
    if len(password) > 256:
        raise PasswordPolicyError("Password must contain at most 256 characters")
    if "\x00" in password:
        raise PasswordPolicyError("Password contains an invalid character")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(value: str) -> bytes:
    if not value or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid base64url value")
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def hash_password(password: str) -> str:
    """Return a versioned scrypt password hash suitable for database storage."""

    _validate_password(password)
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "$".join(
        (
            "scrypt",
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            _b64url_encode(salt),
            _b64url_encode(derived),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    """Verify a stored hash without raising for corrupt or hostile input."""

    try:
        algorithm, n_raw, r_raw, p_raw, salt_raw, digest_raw = encoded.split("$")
        if algorithm != "scrypt":
            return False
        n, r, p = int(n_raw), int(r_raw), int(p_raw)
        if n < 1 << 12 or n > 1 << 18 or n & (n - 1):
            return False
        if not 1 <= r <= 16 or not 1 <= p <= 8 or n * r * p > (1 << 22):
            return False
        salt = _b64url_decode(salt_raw)
        expected = _b64url_decode(digest_raw)
        if not 12 <= len(salt) <= 64 or not 16 <= len(expected) <= 64:
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
            maxmem=_SCRYPT_MAXMEM,
        )
        return hmac.compare_digest(candidate, expected)
    except (AttributeError, TypeError, ValueError, MemoryError):
        return False


def _json_segment(value: dict[str, Any]) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _b64url_encode(raw)


def _sign(signing_input: str, secret: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), signing_input.encode("ascii"), hashlib.sha256
    ).digest()
    return _b64url_encode(digest)


def create_token(
    user: AuthUser,
    token_type: TokenType,
    ttl_seconds: int,
    *,
    config: AuthConfig | None = None,
    now: int | None = None,
) -> tuple[str, TokenClaims]:
    config = validate_security_config(config)
    if token_type not in {"access", "refresh"}:
        raise ValueError("Unsupported token type")
    if ttl_seconds <= 0:
        raise ValueError("Token lifetime must be positive")
    issued_at = int(time.time()) if now is None else int(now)
    expires_at = issued_at + int(ttl_seconds)
    token_id = secrets.token_urlsafe(24)
    payload = {
        "email": normalize_email(user.email),
        "exp": expires_at,
        "iat": issued_at,
        "iss": config.issuer,
        "jti": token_id,
        "role": user.role,
        "sub": user.id,
        "type": token_type,
    }
    header_segment = _json_segment(_JWT_HEADER)
    payload_segment = _json_segment(payload)
    signing_input = f"{header_segment}.{payload_segment}"
    token = f"{signing_input}.{_sign(signing_input, config.secret)}"
    claims = TokenClaims(
        subject=user.id,
        email=payload["email"],
        role=user.role,
        token_type=token_type,
        issuer=config.issuer,
        issued_at=issued_at,
        expires_at=expires_at,
        token_id=token_id,
    )
    return token, claims


def issue_token_pair(
    user: AuthUser,
    *,
    config: AuthConfig | None = None,
    now: int | None = None,
) -> TokenPair:
    config = validate_security_config(config)
    access, access_claims = create_token(
        user, "access", config.access_ttl_seconds, config=config, now=now
    )
    refresh, refresh_claims = create_token(
        user, "refresh", config.refresh_ttl_seconds, config=config, now=now
    )
    return TokenPair(
        access_token=access,
        refresh_token=refresh,
        access_expires_at=access_claims.expires_at,
        refresh_expires_at=refresh_claims.expires_at,
    )


def decode_token(
    token: str,
    expected_type: TokenType | None = None,
    *,
    config: AuthConfig | None = None,
    now: int | None = None,
    leeway_seconds: int = 15,
) -> TokenClaims:
    """Authenticate and validate all security-sensitive JWT claims."""

    config = validate_security_config(config)
    if not isinstance(token, str) or len(token) > 16_384:
        raise TokenError("Malformed token")
    try:
        header_raw, payload_raw, signature = token.split(".")
        signing_input = f"{header_raw}.{payload_raw}"
        expected_signature = _sign(signing_input, config.secret)
        if not hmac.compare_digest(signature, expected_signature):
            raise TokenError("Invalid token signature")
        header = json.loads(_b64url_decode(header_raw))
        payload = json.loads(_b64url_decode(payload_raw))
    except TokenError:
        raise
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TokenError("Malformed token") from exc

    if header != _JWT_HEADER:
        raise TokenError("Unsupported token header")
    if not isinstance(payload, dict):
        raise TokenError("Malformed token payload")

    try:
        subject = str(payload["sub"])
        email = normalize_email(str(payload["email"]))
        role = str(payload["role"])
        token_type = str(payload["type"])
        issuer = str(payload["iss"])
        issued_at = int(payload["iat"])
        expires_at = int(payload["exp"])
        token_id = str(payload["jti"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError("Token is missing required claims") from exc

    if not subject or len(subject) > 128 or not token_id or len(token_id) > 256:
        raise TokenError("Invalid token identity")
    if role not in {"USER", "ADMIN"}:
        raise TokenError("Invalid token role")
    if token_type not in {"access", "refresh"}:
        raise TokenError("Invalid token type")
    if issuer != config.issuer:
        raise TokenError("Invalid token issuer")
    if expected_type is not None and token_type != expected_type:
        raise TokenError(f"Expected a {expected_type} token")

    current_time = int(time.time()) if now is None else int(now)
    leeway = max(0, min(int(leeway_seconds), 300))
    if issued_at > current_time + leeway:
        raise TokenError("Token was issued in the future")
    if expires_at <= current_time - leeway:
        raise TokenError("Token has expired")
    if expires_at <= issued_at:
        raise TokenError("Invalid token lifetime")

    return TokenClaims(
        subject=subject,
        email=email,
        role=role,  # type: ignore[arg-type]
        token_type=token_type,  # type: ignore[arg-type]
        issuer=issuer,
        issued_at=issued_at,
        expires_at=expires_at,
        token_id=token_id,
    )


def token_fingerprint(token: str) -> str:
    """Create a non-reversible identifier used for refresh-token revocation."""

    return hashlib.sha256(token.encode("utf-8")).hexdigest()
