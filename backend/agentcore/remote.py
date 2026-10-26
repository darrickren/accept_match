"""Runs inside Amazon Bedrock AgentCore Runtime: the existing Strands shopping agent on Amazon Bedrock.

AgentCore can't reach DynamoDB Local on the demo laptop, so the local FastAPI sends a small, sanitized snapshot of
the sellable inventory with every request. The agent's search_inventory and get_item_details tools read only that
snapshot, through the same deterministic matching the backend uses. Nothing here writes data, and the package
holds no credentials: Bedrock is called with the runtime's IAM execution role.

Request:  {"action": "shopping_turn", "state": {...}, "message": "...", "inventory": [SnapshotItem, ...]}
          {"action": "ping"}  (no model call)
Response: {"ok": true, "turn": {...}}  or  {"ok": false, "error": {"code", "status", "retryable", "cause"}}
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..agent.base import AgentFailure
from ..config import BACKEND, Settings
from ..errors import CURRENT_REQUEST, not_found
from ..schemas import DefectType, Grade, SizeSystem
from ..services.inventory import InventoryService

LOG = logging.getLogger("acceptmatch.agentcore")

MAX_SNAPSHOT = 300
SNAPSHOT_ITEM_FIELDS = ("item_id", "title", "category", "shoe_type", "size_system", "size_value", "color",
                        "price_cents", "currency", "grade", "status", "listing_version", "resale_eligible",
                        "defects", "verified_features")
SNAPSHOT_DEFECT_FIELDS = ("defect_id", "type", "severity", "location", "description", "uncertainty", "verified_by")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SnapshotDefect(Strict):
    defect_id: str = Field(max_length=40)
    type: DefectType
    severity: Grade
    location: Optional[str] = Field(default=None, max_length=80)
    description: Optional[str] = Field(default=None, max_length=300)
    uncertainty: Optional[str] = Field(default=None, max_length=20)
    verified_by: Optional[str] = Field(default=None, max_length=40)


class SnapshotItem(Strict):
    """Only what matching and the public listing need: no orders, returns, customers, photos or storage keys."""

    item_id: str = Field(pattern=r"^[A-Za-z0-9-]{1,40}$")
    title: str = Field(max_length=120)
    category: str = Field(max_length=20)
    shoe_type: Optional[str] = Field(default=None, max_length=40)
    size_system: SizeSystem
    size_value: str = Field(max_length=6)
    color: Optional[str] = Field(default=None, max_length=30)
    price_cents: int = Field(ge=0, le=10_000_000)
    currency: Literal["CAD"] = "CAD"
    grade: Grade
    status: Literal["AVAILABLE"]
    listing_version: int = Field(ge=1)
    resale_eligible: bool
    defects: List[SnapshotDefect] = Field(default_factory=list, max_length=12)
    verified_features: List[str] = Field(default_factory=list, max_length=8)


class ShoppingRequest(Strict):
    action: Literal["shopping_turn"]
    state: Dict[str, Any] = Field(default_factory=dict)
    message: str = Field(min_length=1, max_length=2400)
    inventory: List[SnapshotItem] = Field(max_length=MAX_SNAPSHOT)
    request_id: Optional[str] = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,80}$")  # the local FastAPI's, for logs


def snapshot_row(row: Dict[str, Any]) -> Dict[str, Any]:
    """A DynamoDB inventory row reduced to SnapshotItem fields (used by the local FastAPI before sending)."""
    item = {k: row.get(k) for k in SNAPSHOT_ITEM_FIELDS if row.get(k) is not None}
    item["size_value"] = str(row.get("size_value"))
    item["defects"] = [{k: d.get(k) for k in SNAPSHOT_DEFECT_FIELDS if d.get(k) is not None}
                       for d in row.get("defects") or []]
    return item


class SnapshotInventory(InventoryService):
    """InventoryService over the request's snapshot instead of DynamoDB. Read-only."""

    def __init__(self, items: List[SnapshotItem], settings: Settings) -> None:
        super().__init__(repo=None, settings=settings)
        self._rows = [{**item.model_dump(), "photos": []} for item in items]

    def all_rows(self) -> List[Dict[str, Any]]:
        return list(self._rows)

    def get_row(self, item_id: str) -> Dict[str, Any]:
        for row in self._rows:
            if row["item_id"] == item_id:
                return row
        raise not_found("ITEM_NOT_FOUND", "This piece isn't in the snapshot.")


def settings_from_env(env: Optional[Dict[str, str]] = None) -> Settings:
    """Runtime settings from the AgentCore environment variables (set at deploy time, no secrets)."""
    env = dict(os.environ if env is None else env)
    model = (env.get("BEDROCK_MODEL_ID") or "").strip()
    if not model:
        raise RuntimeError("BEDROCK_MODEL_ID isn't set on the runtime")
    return Settings(app_mode="bedrock-local", bedrock_model_id=model, bedrock_vision_model_id=model,
                    bedrock_region=(env.get("BEDROCK_REGION") or env.get("AWS_REGION") or "us-east-1").strip(),
                    agent_turn_timeout_s=float(env.get("AGENT_TURN_TIMEOUT_S") or 25),
                    rules=json.loads((BACKEND / "settings" / "rules.json").read_text(encoding="utf-8")))


def _error(code: str, status: int, retryable: bool, cause: str = "") -> Dict[str, Any]:
    return {"ok": False, "error": {"code": code, "status": status, "retryable": retryable, "cause": cause[:160]}}


def handle(payload: Any, agent_factory: Optional[Callable[[Settings], Any]] = None,
           settings: Optional[Settings] = None) -> Dict[str, Any]:
    """One AgentCore invocation. Never raises: every outcome is a JSON body the local backend validates."""
    if isinstance(payload, dict) and payload.get("action") == "ping":
        return {"ok": True, "pong": True}
    try:
        request = ShoppingRequest.model_validate(payload)
    except ValidationError as exc:
        return _error("INVALID_REQUEST", 400, False, f"{len(exc.errors())} field(s) failed validation")
    token = CURRENT_REQUEST.set(request.request_id or "agentcore")  # ai_call / tool log lines carry it
    try:
        LOG.info("invocation request=%s snapshot_items=%s", CURRENT_REQUEST.get(), len(request.inventory))
        settings = settings or settings_from_env()
        if agent_factory is None:
            from ..agent.strands_agent import BedrockAgent

            agent_factory = BedrockAgent
        agent = agent_factory(settings)
        turn = agent.shopping_turn(request.state, request.message, SnapshotInventory(request.inventory, settings))
    except AgentFailure as failure:
        return _error(failure.code, failure.status, failure.retryable, (failure.detail or {}).get("cause", "")
                      if isinstance(failure.detail, dict) else "")
    except Exception as exc:  # noqa: BLE001 - reported as a contract error, never as a fake answer
        LOG.exception("invocation request=%s failed", CURRENT_REQUEST.get())
        return _error("AGENT_ERROR", 500, True, type(exc).__name__)
    finally:
        CURRENT_REQUEST.reset(token)
    return {"ok": True, "turn": {
        "message": turn.message, "next_action": turn.next_action, "state": turn.state, "questions": turn.questions,
        "item_ids": [item["item_id"] for item in turn.items], "blocking": turn.blocking,
        "analysis_source": turn.analysis_source, "model_id": turn.model_id, "agent": turn.agent}}
