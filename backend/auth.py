"""Demo authentication: preset personas exchange a password for a short-lived signed bearer token.

Not real SSO. Tokens are HMAC-SHA256 signed with DEMO_TOKEN_SECRET and carry
{sub, role, iat, exp, jti}. Ownership checks always use the token's `sub`,
never a user id sent by the client.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Optional, Tuple

from fastapi import Request

from .config import Settings
from .errors import ApiError

PREFIX = "v1"


@dataclass(frozen=True)
class Principal:
    sub: str
    role: str


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(secret: str, message: str) -> str:
    return _b64(hmac.new(secret.encode(), message.encode(), hashlib.sha256).digest())


def issue_token(settings: Settings, sub: str, role: str, now: Optional[float] = None) -> Tuple[str, int]:
    issued = int(now if now is not None else time.time())
    payload = _b64(json.dumps({"sub": sub, "role": role, "iat": issued, "exp": issued + settings.token_ttl_s,
                               "jti": uuid.uuid4().hex[:12]}, separators=(",", ":")).encode())
    return f"{PREFIX}.{payload}.{_sign(settings.token_secret, PREFIX + '.' + payload)}", settings.token_ttl_s


def verify_token(settings: Settings, token: str, now: Optional[float] = None) -> Principal:
    try:
        prefix, payload, signature = token.split(".")
    except ValueError:
        raise _unauthorized() from None
    expected = _sign(settings.token_secret, f"{prefix}.{payload}")
    if prefix != PREFIX or not hmac.compare_digest(signature, expected):
        raise _unauthorized()
    try:
        claims = json.loads(_unb64(payload))
    except (ValueError, UnicodeDecodeError):
        raise _unauthorized() from None
    if int(claims.get("exp", 0)) <= int(now if now is not None else time.time()):
        raise ApiError(401, "TOKEN_EXPIRED", "Your demo sign-in expired. Please sign in again.")
    sub, role = str(claims.get("sub", "")), str(claims.get("role", ""))
    if settings.role_of(sub) != role:
        raise _unauthorized()
    return Principal(sub=sub, role=role)


def login(settings: Settings, persona: str, password: str) -> Tuple[str, int, str]:
    expected = settings.persona_passwords.get(persona)
    role = settings.role_of(persona)
    # Compare even for unknown personas so timing doesn't reveal which ones exist.
    ok = hmac.compare_digest((expected or "\0" * 32).encode(), password.encode()) and expected is not None
    if not ok or role is None:
        raise ApiError(401, "INVALID_CREDENTIALS", "Demo sign-in failed.")
    token, ttl = issue_token(settings, persona, role)
    return token, ttl, role


def _unauthorized() -> ApiError:
    return ApiError(401, "UNAUTHORIZED", "Sign in first.")


def principal(request: Request) -> Principal:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        raise _unauthorized()
    return verify_token(request.app.state.c.settings, header[7:].strip())


def customer(request: Request) -> Principal:
    user = principal(request)
    if user.role != "customer":
        raise ApiError(403, "FORBIDDEN", "This action is for customer accounts.")
    return user


def admin(request: Request) -> Principal:
    user = principal(request)
    if user.role != "admin":
        raise ApiError(403, "FORBIDDEN", "Admin access is required.")
    return user
