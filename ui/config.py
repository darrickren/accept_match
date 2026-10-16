"""Runtime settings for the customer site (V3).

Values come from environment variables. Defaults are suggestions agreed in the
V3.1 plan and can be changed without touching page code. Nothing here is a
secret: AWS credentials always come from boto3's default chain, never from
this module or the repository.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional, Tuple
from urllib.parse import urlparse

DEFAULT_NEW_URL = "https://www.aldoshoes.com/en-ca"
DEFAULT_PAST_SEASON_URL = "https://www.aldoshoes.com/en-ca/collections/sale"
DEFAULT_PAST_SEASON_CTA = "Shop the ALDO Sale ↗"
ALLOWED_EXTERNAL_DOMAIN = "aldoshoes.com"

BACKENDS = ("mock", "agentcore", "local")
JOURNEYS = ("shop", "return")


class ConfigError(RuntimeError):
    """A setting is missing or invalid for the selected mode."""


@dataclass(frozen=True)
class Settings:
    new_url: str = DEFAULT_NEW_URL
    past_season_url: str = DEFAULT_PAST_SEASON_URL
    past_season_cta: str = DEFAULT_PAST_SEASON_CTA
    backend_default: str = "mock"
    backend_overrides: Mapping[str, str] = field(default_factory=dict)
    agentcore_runtime_arn: Optional[str] = None
    agentcore_qualifier: Optional[str] = None
    agentcore_local_url: str = "http://localhost:8080/invocations"
    diagnostics: bool = False
    connect_timeout_s: float = 5.0
    turn_timeout_s: float = 30.0
    search_timeout_s: float = 15.0
    upload_timeout_s: float = 30.0
    assessment_timeout_s: float = 90.0
    max_agent_sessions_per_browser: int = 10
    return_photos_min: int = 3
    return_photos_max: int = 8
    photo_max_mb: float = 10.0
    photo_max_px: int = 2048
    session_photo_budget_mb: float = 40.0
    draft_photo_ttl_h: int = 24
    context_max_answers: int = 20
    context_max_bytes: int = 16_000
    warnings: Tuple[str, ...] = ()

    def backend_for(self, journey: str) -> str:
        """Backend selected for a journey: per-journey override, else the default."""
        return self.backend_overrides.get(journey, self.backend_default)


def _flag(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _number(env: Mapping[str, str], key: str, default, cast, warnings: list):
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError:
        warnings.append(f"{key}={raw!r} is not a number; using {default}.")
        return default
    if value <= 0:
        warnings.append(f"{key} must be positive; using {default}.")
        return default
    return value


def validate_external_url(url: Optional[str], default: str, key: str, warnings: list) -> str:
    """Only https links to the official ALDO domain are allowed for the external entries."""
    value = (url or "").strip()
    if not value:
        return default
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    official = host == ALLOWED_EXTERNAL_DOMAIN or host.endswith("." + ALLOWED_EXTERNAL_DOMAIN)
    if parsed.scheme != "https" or not official:
        warnings.append(f"{key} must be an https link on {ALLOWED_EXTERNAL_DOMAIN}; using the default.")
        return default
    return value


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    env = os.environ if env is None else env
    warnings: list = []
    overrides: Dict[str, str] = {}
    for journey in JOURNEYS:
        raw = (env.get(f"ACCEPTMATCH_AGENT_{journey.upper()}") or "").strip().lower()
        if raw:
            overrides[journey] = raw
    cta = (env.get("PAST_SEASON_CTA") or "").strip() or DEFAULT_PAST_SEASON_CTA
    return Settings(
        new_url=validate_external_url(env.get("NEW_URL"), DEFAULT_NEW_URL, "NEW_URL", warnings),
        past_season_url=validate_external_url(env.get("PAST_SEASON_URL"), DEFAULT_PAST_SEASON_URL,
                                              "PAST_SEASON_URL", warnings),
        past_season_cta=cta[:40],
        backend_default=(env.get("ACCEPTMATCH_AGENT") or "mock").strip().lower(),
        backend_overrides=overrides,
        agentcore_runtime_arn=(env.get("AGENTCORE_RUNTIME_ARN") or "").strip() or None,
        agentcore_qualifier=(env.get("AGENTCORE_QUALIFIER") or "").strip() or None,
        agentcore_local_url=(env.get("AGENTCORE_LOCAL_URL") or "").strip() or "http://localhost:8080/invocations",
        diagnostics=_flag(env.get("ACCEPTMATCH_DIAGNOSTICS")),
        connect_timeout_s=_number(env, "AGENT_CONNECT_TIMEOUT_S", 5.0, float, warnings),
        turn_timeout_s=_number(env, "AGENT_TURN_TIMEOUT_S", 30.0, float, warnings),
        search_timeout_s=_number(env, "AGENT_SEARCH_TIMEOUT_S", 15.0, float, warnings),
        upload_timeout_s=_number(env, "PHOTO_UPLOAD_TIMEOUT_S", 30.0, float, warnings),
        assessment_timeout_s=_number(env, "AGENT_ASSESSMENT_TIMEOUT_S", 90.0, float, warnings),
        max_agent_sessions_per_browser=_number(env, "MAX_AGENT_SESSIONS_PER_BROWSER", 10, int, warnings),
        return_photos_min=_number(env, "RETURN_PHOTOS_MIN", 3, int, warnings),
        return_photos_max=_number(env, "RETURN_PHOTOS_MAX", 8, int, warnings),
        photo_max_mb=_number(env, "PHOTO_MAX_MB", 10.0, float, warnings),
        photo_max_px=_number(env, "PHOTO_MAX_PX", 2048, int, warnings),
        session_photo_budget_mb=_number(env, "SESSION_PHOTO_BUDGET_MB", 40.0, float, warnings),
        draft_photo_ttl_h=_number(env, "DRAFT_PHOTO_TTL_H", 24, int, warnings),
        warnings=tuple(warnings),
    )
