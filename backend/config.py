"""Backend settings from environment variables. Fails fast on unsafe or incomplete configuration.

APP_MODE (backend layer, not the frontend's ACCEPTMATCH_AGENT):
  mock           scripted agent and simulated photo analysis; DynamoDB Local; local image files
  bedrock-local  Strands + real Amazon Bedrock; DynamoDB Local; local image files
  gemini-local   Strands + real Google Gemini API (GEMINI_API_KEY); DynamoDB Local; local image files
  agentcore      Buy Defective runs on the Strands agent deployed to Amazon Bedrock AgentCore Runtime
                 (AGENTCORE_RUNTIME_ARN, AWS credentials from the standard chain); DynamoDB Local stays the
                 source of truth; Return runs on the scripted mock (labelled SIMULATED)

Local modes may only use a localhost DynamoDB endpoint and always use dummy
DynamoDB credentials, so they can never touch a real AWS table. Secrets
(token signing key, persona passwords) are excluded from repr().
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlparse

from . import __version__

ROOT = Path(__file__).resolve().parent.parent
BACKEND = Path(__file__).resolve().parent
APP_MODES = ("mock", "bedrock-local", "gemini-local", "agentcore")
GEMINI_DEFAULT_MODEL = "gemini-3.1-flash-lite"  # the one place the default model is named
THINKING_LEVELS = ("minimal", "low", "medium", "high")
AGENTCORE_ARN = r"^arn:aws:bedrock-agentcore:([a-z0-9-]+):\d{12}:runtime/[a-zA-Z][a-zA-Z0-9_]{0,47}-[a-zA-Z0-9]{10}$"
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
PERSONAS = {"cust-01": "customer", "cust-02": "customer", "cust-03": "customer", "admin": "admin"}


class ConfigError(RuntimeError):
    """The backend can't start safely with this configuration."""


def _json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Settings:
    app_mode: str = "mock"
    version: str = __version__
    table: str = "acceptmatch_demo"
    ddb_endpoint: Optional[str] = "http://localhost:8001"
    aws_region: str = "ca-central-1"
    image_root: Path = ROOT / ".local" / "images"
    token_secret: str = field(default="", repr=False)
    token_ttl_s: int = 3600
    persona_passwords: Mapping[str, str] = field(default_factory=dict, repr=False)
    frontend_origins: Tuple[str, ...] = ()
    bedrock_region: str = "ca-central-1"
    bedrock_model_id: Optional[str] = None
    bedrock_vision_model_id: Optional[str] = None
    bedrock_connect_timeout_s: float = 5.0
    bedrock_read_timeout_s: float = 50.0
    agent_turn_timeout_s: float = 25.0  # below the site's 30 s chat timeout
    agent_vision_timeout_s: float = 55.0  # below the site's 60 s photo-review timeout
    gemini_api_key: str = field(default="", repr=False)
    gemini_model_id: str = GEMINI_DEFAULT_MODEL
    gemini_vision_model_id: Optional[str] = None
    gemini_thinking_level: str = "low"
    gemini_http_timeout_s: float = 50.0
    agent_max_calls_per_minute: int = 12  # Gemini calls per minute from this process; under the free tier (0 = off)
    agent_max_turns_per_session: int = 30  # AI replies per conversation (0 = off)
    agentcore_runtime_arn: Optional[str] = None
    agentcore_region: str = "us-east-1"
    agentcore_qualifier: str = "DEFAULT"
    agentcore_model_id: Optional[str] = None  # what the deployed runtime uses, for labels until it answers
    agentcore_read_timeout_s: float = 60.0  # cold start + the remote agent's own 25 s deadline
    max_image_bytes: int = 10 * 1024 * 1024
    max_inventory_rows: int = 1000
    rules: Mapping[str, Any] = field(default_factory=dict)
    navigation: Mapping[str, Any] = field(default_factory=dict)

    @property
    def local(self) -> bool:
        """DynamoDB Local and local image files. True in every mode of this build, agentcore included."""
        return self.app_mode in ("mock", "bedrock-local", "gemini-local", "agentcore")

    @property
    def policy_version(self) -> str:
        return str(self.rules.get("policy_version", "unknown"))

    @property
    def demo_date(self) -> dt.date:
        return dt.date.fromisoformat(str(self.rules.get("demo_date", "2026-10-15")))

    def role_of(self, persona: str) -> Optional[str]:
        return PERSONAS.get(persona)


def _number(env: Mapping[str, str], key: str, default, cast, allow_zero: bool = False):
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = cast(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number") from exc
    if value < 0 or (value == 0 and not allow_zero):
        raise ConfigError(f"{key} must be positive")
    return value


def load_settings(env: Optional[Mapping[str, str]] = None) -> Settings:
    env = os.environ if env is None else env
    mode = (env.get("APP_MODE") or "mock").strip().lower()
    if mode not in APP_MODES:
        raise ConfigError(f"APP_MODE must be one of {', '.join(APP_MODES)}")
    runtime_arn = (env.get("AGENTCORE_RUNTIME_ARN") or "").strip() or None
    arn_match = re.match(AGENTCORE_ARN, runtime_arn or "")
    if mode == "agentcore" and not arn_match:
        raise ConfigError("APP_MODE=agentcore needs AGENTCORE_RUNTIME_ARN; keep it in the process environment or "
                          ".local/agentcore.json (see docs/AGENTCORE_PUBLIC.md)")
    secret = env.get("DEMO_TOKEN_SECRET") or ""
    if len(secret) < 32:
        raise ConfigError("DEMO_TOKEN_SECRET must be set (at least 32 characters). "
                          "Run: python -m backend.scripts.dev secrets")
    passwords = {}
    for persona in PERSONAS:
        value = env.get("DEMO_PASSWORD_" + persona.upper().replace("-", "_")) or ""
        if value:
            if len(value) < 8:
                raise ConfigError(f"The password for {persona} must be at least 8 characters")
            passwords[persona] = value
    if not any(PERSONAS[p] == "customer" for p in passwords):
        raise ConfigError("Set at least one customer password, e.g. DEMO_PASSWORD_CUST_01")
    endpoint = (env.get("DDB_ENDPOINT") or "http://localhost:8001").strip()
    host = (urlparse(endpoint).hostname or "").lower()
    if host not in LOCAL_HOSTS:
        raise ConfigError("Local modes only use DynamoDB Local: DDB_ENDPOINT must point to localhost")
    origins = tuple(o.strip() for o in (env.get("FRONTEND_ORIGINS") or "").split(",") if o.strip())
    if "*" in origins:
        raise ConfigError("FRONTEND_ORIGINS can't contain '*'")
    model = (env.get("BEDROCK_MODEL_ID") or "").strip() or None
    if mode == "bedrock-local" and not model:
        raise ConfigError("APP_MODE=bedrock-local needs BEDROCK_MODEL_ID (for example us.amazon.nova-2-lite-v1:0)")
    gemini_key = (env.get("GEMINI_API_KEY") or "").strip()
    if mode == "gemini-local" and not gemini_key:
        raise ConfigError("APP_MODE=gemini-local needs GEMINI_API_KEY (server-side only). Store it with: "
                          "python -m backend.scripts.dev gemini-key   (see docs/BACKEND.md)")
    thinking = (env.get("GEMINI_THINKING_LEVEL") or "low").strip().lower()
    if thinking not in THINKING_LEVELS:
        raise ConfigError(f"GEMINI_THINKING_LEVEL must be one of {', '.join(THINKING_LEVELS)}")
    gemini_model = (env.get("GEMINI_MODEL_ID") or "").strip() or GEMINI_DEFAULT_MODEL
    image_root = Path(env.get("LOCAL_IMAGE_ROOT") or ROOT / ".local" / "images").expanduser().resolve()
    return Settings(
        app_mode=mode,
        table=(env.get("ACCEPTMATCH_TABLE") or "acceptmatch_demo").strip(),
        ddb_endpoint=endpoint,
        aws_region=(env.get("AWS_REGION") or "ca-central-1").strip(),
        image_root=image_root,
        token_secret=secret,
        token_ttl_s=_number(env, "DEMO_TOKEN_TTL_S", 3600, int),
        persona_passwords=passwords,
        frontend_origins=origins,
        bedrock_region=(env.get("BEDROCK_REGION") or env.get("AWS_REGION") or "ca-central-1").strip(),
        bedrock_model_id=model,
        bedrock_vision_model_id=(env.get("BEDROCK_VISION_MODEL_ID") or "").strip() or model,
        bedrock_connect_timeout_s=_number(env, "BEDROCK_CONNECT_TIMEOUT_S", 5.0, float),
        bedrock_read_timeout_s=_number(env, "BEDROCK_READ_TIMEOUT_S", 50.0, float),
        agent_turn_timeout_s=_number(env, "AGENT_TURN_TIMEOUT_S", 25.0, float),
        agent_vision_timeout_s=_number(env, "AGENT_VISION_TIMEOUT_S", 55.0, float),
        gemini_api_key=gemini_key,
        gemini_model_id=gemini_model,
        gemini_vision_model_id=(env.get("GEMINI_VISION_MODEL_ID") or "").strip() or gemini_model,
        gemini_thinking_level=thinking,
        gemini_http_timeout_s=_number(env, "GEMINI_HTTP_TIMEOUT_S", 50.0, float),
        agent_max_calls_per_minute=_number(env, "AGENT_MAX_CALLS_PER_MINUTE", 12, int, allow_zero=True),
        agent_max_turns_per_session=_number(env, "AGENT_MAX_TURNS_PER_SESSION", 30, int, allow_zero=True),
        agentcore_runtime_arn=runtime_arn,
        agentcore_region=arn_match.group(1) if arn_match else (env.get("AGENTCORE_REGION") or "us-east-1").strip(),
        agentcore_qualifier=(env.get("AGENTCORE_QUALIFIER") or "DEFAULT").strip(),
        agentcore_model_id=(env.get("AGENTCORE_MODEL_ID") or "").strip() or None,
        agentcore_read_timeout_s=_number(env, "AGENTCORE_READ_TIMEOUT_S", 60.0, float),
        max_image_bytes=_number(env, "MAX_IMAGE_BYTES", 10 * 1024 * 1024, int),
        rules=_json(BACKEND / "settings" / "rules.json"),
        navigation=_json(BACKEND / "settings" / "navigation.json"),
    )
