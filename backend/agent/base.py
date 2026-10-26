"""Shared agent types: turn results, controlled tools, and the failures that map to the error contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..errors import ApiError


class AgentFailure(ApiError):
    """A model/agent problem. Never answered by quietly switching to the mock."""


def ai_unavailable(detail: str = "", retryable: bool = True) -> AgentFailure:
    """retryable=False for configuration problems (missing or rejected key, no access): retrying can't help."""
    return AgentFailure(503, "AI_UNAVAILABLE", "The AI service is unavailable right now. Nothing was changed.",
                        retryable, {"cause": detail[:120]} if detail else None)


def agent_timeout() -> AgentFailure:
    return AgentFailure(504, "AGENT_TIMEOUT", "The AI agent took too long to answer.", True)


def ai_throttled(detail: str = "") -> AgentFailure:
    return AgentFailure(429, "RATE_LIMITED", "The AI service is busy. Please try again shortly.", True,
                        {"cause": detail[:120]} if detail else None)


def ai_output_invalid(problems: List[str]) -> AgentFailure:
    return AgentFailure(502, "AI_OUTPUT_INVALID", "The AI returned a result that failed validation.", False,
                        {"problems": [p[:160] for p in problems[:5]]})


@dataclass
class TurnResult:
    message: str
    next_action: str
    state: Dict[str, Any]
    questions: List[Dict[str, Any]] = field(default_factory=list)
    required_photos: List[Dict[str, Any]] = field(default_factory=list)
    return_status: Optional[str] = None
    items: List[Dict[str, Any]] = field(default_factory=list)
    blocking: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    analysis_source: str = "SIMULATED"
    model_id: Optional[str] = None
    agent: Optional[Dict[str, Any]] = None  # redacted run evidence from a real model (provider, calls, tools, usage)


def question(question_id: str, text: str, options: List[Any], kind: str = "single_choice",
             help: Optional[str] = None) -> Dict[str, Any]:
    opts = [o if isinstance(o, dict) else {"value": str(o), "label": str(o)} for o in options]
    return {"question_id": question_id, "text": text, "type": kind, "options": opts, "help": help}


class ReturnTools:
    """What the return conversation may read: the caller's own demo orders, nothing else."""

    def __init__(self, repo: Any, user: str) -> None:
        self.repo, self.user = repo, user

    def lookup_demo_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        from ..repository import order_key

        order = self.repo.get(order_key(str(order_id).strip().upper()))
        if not order or order.get("demo_user_id") != self.user:
            return None
        lines = self.repo.query(f"ORDER#{order['order_id']}", "LINE#")
        return {"order_id": order["order_id"], "purchased_at": order["purchased_at"], "channel": order["channel"],
                "is_final_sale": bool(order.get("is_final_sale")),
                "lines": [{k: line.get(k) for k in ("line", "product_name", "shoe_type", "size_system", "size_value",
                                                    "color")} for line in lines]}
