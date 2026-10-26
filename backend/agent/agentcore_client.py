"""APP_MODE=agentcore, local side: Buy Defective turns go to the agent on Amazon Bedrock AgentCore Runtime.

FastAPI -> InvokeAgentRuntime (AWS SDK, standard credential chain) -> Strands agent on Bedrock -> back here.
The request carries a sanitized snapshot of the sellable inventory (AgentCore can't reach DynamoDB Local).
Nothing from the remote answer is trusted as data: its output is schema-validated, its preferences re-validated,
and every item ID it returns is checked against the local database. The items shown are built from DynamoDB,
never from the remote payload. Failures map to the error contract and are never answered by the mock.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..agentcore.remote import snapshot_row
from ..errors import CURRENT_REQUEST
from ..services.inventory import public
from .base import AgentFailure, TurnResult, agent_timeout, ai_output_invalid, ai_throttled, ai_unavailable
from .preferences import Preferences, blocking_constraints
from .providers import failure_chain

LOG = logging.getLogger("acceptmatch.agent")
SOURCE = "AGENTCORE"
REMOTE_CODES = {"AI_UNAVAILABLE": 503, "RATE_LIMITED": 429, "AGENT_TIMEOUT": 504, "AI_OUTPUT_INVALID": 502}


class Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class RemoteOption(Loose):
    value: str = Field(max_length=120)
    label: str = Field(max_length=120)


class RemoteQuestion(Loose):
    question_id: str = Field(max_length=40)
    text: str = Field(min_length=1, max_length=300)
    type: Literal["single_choice", "multi_choice", "free_text"]
    options: List[RemoteOption] = Field(default_factory=list, max_length=12)
    help: Optional[str] = Field(default=None, max_length=200)


class RemoteState(Loose):
    preferences: Dict[str, Any]
    asked: List[str] = Field(default_factory=list, max_length=40)
    offered: List[Dict[str, Any]] = Field(default_factory=list, max_length=4)
    history: List[Dict[str, str]] = Field(default_factory=list, max_length=12)


class RemoteRun(Loose):
    provider: Optional[str] = Field(default=None, max_length=30)
    model_id: Optional[str] = Field(default=None, max_length=120)
    model_calls: Optional[int] = None
    tools_used: List[str] = Field(default_factory=list, max_length=8)
    structured_output: Optional[str] = Field(default=None, max_length=60)
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    latency_ms: Optional[int] = None


class RemoteTurn(Loose):
    message: str = Field(min_length=1, max_length=1200)
    next_action: Literal["ASK_QUESTION", "SHOW_ITEMS"]
    state: RemoteState
    questions: List[RemoteQuestion] = Field(default_factory=list, max_length=3)
    item_ids: List[str] = Field(default_factory=list, max_length=12)
    model_id: Optional[str] = Field(default=None, max_length=120)
    agent: Optional[RemoteRun] = None


AWS_CAUSES = [  # (names, failure)
    ({"NoCredentialsError", "PartialCredentialsError", "CredentialRetrievalError", "TokenRetrievalError",
      "UnauthorizedSSOTokenError", "SSOTokenLoadError", "NoRegionError"},
     lambda: ai_unavailable("no usable AWS credentials for the backend (run aws login or aws sso login)", False)),
    ({"ExpiredTokenException", "ExpiredToken", "UnrecognizedClientException", "InvalidSignatureException"},
     lambda: ai_unavailable("the AWS credentials were rejected or have expired", False)),
    ({"AccessDeniedException"}, lambda: ai_unavailable("access to the AgentCore runtime was denied", False)),
    ({"ResourceNotFoundException"}, lambda: ai_unavailable("the AgentCore runtime wasn't found "
                                                           "(check AGENTCORE_RUNTIME_ARN)", False)),
    ({"ValidationException"}, lambda: ai_unavailable("AgentCore rejected the request", False)),
    ({"ThrottlingException", "ServiceQuotaExceededException"}, lambda: ai_throttled("AgentCore throttled the request")),
    ({"ReadTimeoutError", "ConnectTimeoutError"}, agent_timeout),
    ({"RuntimeClientError"}, lambda: ai_unavailable("the agent on AgentCore failed to answer")),
    ({"EndpointConnectionError", "ConnectionClosedError"}, lambda: ai_unavailable("AgentCore couldn't be reached")),
    ({"InternalServerException", "ServiceUnavailableException", "ConflictException"},
     lambda: ai_unavailable("AgentCore reported a temporary problem")),
]


def classify_agentcore(exc: BaseException) -> AgentFailure:
    found = set(failure_chain(exc))
    for names, failure in AWS_CAUSES:
        if found & names:
            return failure()
    return ai_unavailable(type(exc).__name__)


def remote_failure(error: Any) -> AgentFailure:
    """The remote agent's own error report, mapped to the contract (only known codes, our own wording)."""
    error = error if isinstance(error, dict) else {}
    code = str(error.get("code") or "")
    cause = " ".join(str(error.get("cause") or "").split())[:120]
    if code == "RATE_LIMITED":
        return ai_throttled(cause or "Bedrock throttled the agent")
    if code == "AGENT_TIMEOUT":
        return agent_timeout()
    if code == "AI_OUTPUT_INVALID":
        return ai_output_invalid(["the remote agent's model output failed validation"])
    if code == "AI_UNAVAILABLE":
        return ai_unavailable(f"on AgentCore: {cause}" if cause else "the model on AgentCore is unavailable",
                              bool(error.get("retryable", True)))
    return ai_unavailable(f"the agent on AgentCore reported {code or 'an error'}")


class AgentCoreShopping:
    def __init__(self, settings: Any, client: Any = None) -> None:
        self.settings = settings
        self.model_id = settings.agentcore_model_id
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3
            from botocore.config import Config

            self._client = boto3.client("bedrock-agentcore", region_name=self.settings.agentcore_region,
                                        config=Config(connect_timeout=5, read_timeout=self.settings.
                                                      agentcore_read_timeout_s, retries={"total_max_attempts": 1}))
        return self._client

    def invoke(self, payload: Dict[str, Any], session_id: Optional[str]) -> Dict[str, Any]:
        """One InvokeAgentRuntime call. Returns the parsed body and the AgentCore request metadata."""
        kwargs: Dict[str, Any] = {"agentRuntimeArn": self.settings.agentcore_runtime_arn,
                                  "qualifier": self.settings.agentcore_qualifier,
                                  "contentType": "application/json", "accept": "application/json",
                                  "payload": json.dumps(payload).encode("utf-8")}
        if session_id and len(session_id) >= 33:
            kwargs["runtimeSessionId"] = session_id
        started = time.monotonic()
        try:
            response = self.client.invoke_agent_runtime(**kwargs)
            raw = response["response"].read()
        except Exception as exc:  # noqa: BLE001 - classified; never answered by the mock
            failure = classify_agentcore(exc)
            LOG.warning("agentcore_call request=%s FAILED %s -> %s %s", CURRENT_REQUEST.get(),
                        " <- ".join(failure_chain(exc)), failure.status, failure.code)
            raise failure from None
        try:
            body = json.loads(raw)
        except (ValueError, TypeError):
            raise ai_output_invalid(["the AgentCore runtime didn't return JSON"]) from None
        meta = {"request_id": (response.get("ResponseMetadata") or {}).get("RequestId"),
                "runtime_session_id": response.get("runtimeSessionId"), "trace_id": response.get("traceId"),
                "status_code": response.get("statusCode"), "round_trip_ms": int((time.monotonic() - started) * 1000)}
        return {"body": body, "meta": meta}

    def shopping_turn(self, state: Dict[str, Any], text: str, inventory: Any,
                      session_id: Optional[str] = None) -> TurnResult:
        snapshot = [snapshot_row(row) for row in inventory.sellable()]
        request_id = CURRENT_REQUEST.get()
        result = self.invoke({"action": "shopping_turn", "state": state or {}, "message": text, "inventory": snapshot,
                              **({"request_id": request_id} if request_id and request_id != "-" else {})},
                             session_id)
        body, meta = result["body"], result["meta"]
        if not isinstance(body, dict) or body.get("ok") is not True:
            failure = remote_failure((body or {}).get("error") if isinstance(body, dict) else None)
            LOG.warning("agentcore_call request=%s agentcore_request=%s REMOTE ERROR -> %s %s", CURRENT_REQUEST.get(),
                        meta["request_id"], failure.status, failure.code)
            raise failure
        try:
            remote = RemoteTurn.model_validate(body.get("turn"))
            prefs = Preferences(**remote.state.preferences)
        except (ValidationError, TypeError, ValueError) as exc:
            problems = len(exc.errors()) if isinstance(exc, ValidationError) else 1
            raise ai_output_invalid([f"the AgentCore answer failed validation ({problems} problem(s))"]) from None
        found = inventory.find_for(prefs)  # the local database decides what exists and what fits
        local = {row["item_id"]: (row, m) for row, m in found}
        verified = [i for i in dict.fromkeys(remote.item_ids) if i in local]
        unverified = [i for i in dict.fromkeys(remote.item_ids) if i not in local]
        message, next_action = remote.message, remote.next_action
        if next_action == "SHOW_ITEMS" and not verified:
            next_action = "ASK_QUESTION"
            message = "I couldn't confirm those pairs against the live inventory. Could you tell me a bit more?"
        run = remote.agent.model_dump() if remote.agent else {}
        agent = {**run, "provider": "agentcore", "model_id": remote.model_id or run.get("model_id"),
                 "step": "shopping", "runtime": "bedrock-agentcore", "agentcore_request_id": meta["request_id"],
                 "round_trip_ms": meta["round_trip_ms"], "snapshot_items": len(snapshot),
                 "verified_item_ids": verified, "unverified_item_ids": unverified}
        LOG.info("agentcore_call request=%s agentcore_request=%s ok model=%s calls=%s tools=%s tokens_in=%s "
                 "tokens_out=%s round_trip_ms=%s snapshot=%s verified=%s unverified=%s", CURRENT_REQUEST.get(),
                 meta["request_id"], agent["model_id"], run.get("model_calls"),
                 ",".join(run.get("tools_used") or []) or "-", run.get("input_tokens"), run.get("output_tokens"),
                 meta["round_trip_ms"], len(snapshot), ",".join(verified) or "-", ",".join(unverified) or "-")
        new_state = {**remote.state.model_dump(), "preferences": prefs.model_dump()}
        return TurnResult(message, next_action, new_state, [q.model_dump() for q in remote.questions],
                          items=[public(*local[i]) for i in verified] if next_action == "SHOW_ITEMS" else [],
                          blocking=blocking_constraints(prefs) if not found else [], analysis_source=SOURCE,
                          model_id=agent["model_id"], agent=agent)

    def ping(self, session_id: Optional[str] = None) -> Dict[str, Any]:
        """Wakes the runtime without a model call (used by the deploy script's check)."""
        return self.invoke({"action": "ping"}, session_id)
