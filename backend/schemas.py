"""Pydantic request and response models: the public contract (also exported as /openapi.json).

Requests forbid unknown fields so contract drift fails loudly. Responses always
include every documented key (null/[]/false when empty), matching the plan's
ChatResponse rule and the V3 HTTP client's checks.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator

NextAction = Literal["ASK_QUESTION", "UPLOAD_PHOTOS", "SHOW_ITEMS", "REQUEST_ACCEPTANCE", "SHOW_RETURN_STATUS",
                     "HUMAN_REVIEW", "COMPLETE"]
Mode = Literal["return", "shopping"]
ReturnStatus = Literal["DRAFT", "INFO_COLLECTION", "NEEDS_MORE_EVIDENCE", "APPROVED_DEMO", "HUMAN_REVIEW_REQUIRED",
                       "REJECTED_AFTER_REVIEW"]
InventoryStatus = Literal["AWAITING_RECEIPT", "INSPECTION_PENDING", "AVAILABLE", "QUARANTINED", "SOLD"]
Grade = Literal["A", "B", "C", "UNKNOWN"]
DefectType = Literal["stain", "scratch", "scuff", "discoloration", "stitching_issue", "sole_separation", "broken_heel",
                     "other"]
SizeSystem = Literal["US_WOMEN", "US_MEN", "EU"]
ShoeView = Literal["pair_top", "pair_outer", "pair_inner", "pair_soles", "pair_heels", "defect_closeup", "size_label"]
ClaimType = Literal["STANDARD_RETURN", "DEFECT_CLAIM"]
MatchStatus = Literal["MATCH", "NEEDS_CONFIRMATION", "NO_MATCH"]
SHOE_VIEWS = ("pair_top", "pair_outer", "pair_inner", "pair_soles", "pair_heels", "defect_closeup", "size_label")
DEFECT_TYPES = ("stain", "scratch", "scuff", "discoloration", "stitching_issue", "sole_separation", "broken_heel",
                "other")
REQUEST_ID = r"^[A-Za-z0-9_.:-]{1,100}$"


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ----------------------------------------------------------------- requests

class LoginRequest(Strict):
    persona: str = Field(pattern=r"^[a-z0-9-]{3,20}$")
    password: str = Field(min_length=1, max_length=200)


class SessionCreate(Strict):
    mode: Mode


class MessageCreate(Strict):
    text: str = Field(default="", max_length=2400)
    photo_ids: List[str] = Field(default_factory=list, max_length=10)
    client_request_id: str = Field(pattern=REQUEST_ID)


class ReturnCreate(Strict):
    session_id: str = Field(min_length=1, max_length=120)
    order_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9-]{2,39}$")
    order_line: Union[int, str]
    claim_type: ClaimType
    reason: str = Field(min_length=1, max_length=200)

    @field_validator("order_line")
    @classmethod
    def _line(cls, value):
        text = str(value).strip()
        if not text.isdigit() or not 1 <= int(text) <= 50:
            raise ValueError("order_line must be a line number from 1 to 50")
        return str(int(text))


class EvaluateRequest(Strict):
    client_request_id: str = Field(pattern=REQUEST_ID)


class Acceptance(Strict):
    defect_id: str = Field(min_length=1, max_length=60)
    accepted: bool


class AcceptanceRequest(Strict):
    listing_version: int = Field(ge=1)
    acceptances: List[Acceptance] = Field(min_length=1, max_length=20)


class CheckoutRequest(Strict):
    item_id: str = Field(min_length=1, max_length=60)
    listing_version: int = Field(ge=1)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{8,100}$")


class DefectLabel(Strict):
    type: DefectType
    severity: Grade
    location: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=300)


class ReviewDecision(Strict):
    conclusion: Literal["APPROVE", "REJECT"]
    reason: str = Field(min_length=3, max_length=500)
    corrected_labels: Optional[List[DefectLabel]] = Field(default=None, max_length=8)
    simulated: Literal[True]


class ReceiveRequest(Strict):
    simulated: Literal[True]


class InspectRequest(Strict):
    outcome: Literal["PASS", "QUARANTINE"]
    notes: str = Field(default="", max_length=500)
    simulated: Literal[True]
    grade: Optional[Literal["A", "B", "C"]] = None  # the inspector's grade; required for UNKNOWN units
    publish_photo_ids: Optional[List[str]] = Field(default=None, max_length=8)


# ----------------------------------------------------------------- responses

class ErrorBody(BaseModel):
    request_id: str
    code: str
    message: str
    retryable: bool
    detail: Any = None


class LoginResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    role: str
    persona: str
    expires_in: int


class Option(BaseModel):
    value: str
    label: str


class Question(BaseModel):
    question_id: str
    text: str
    type: Literal["single_choice", "multi_choice", "free_text"] = "single_choice"
    options: List[Option] = Field(default_factory=list)
    help: Optional[str] = None


class RequiredPhoto(BaseModel):
    view: ShoeView
    reason: Optional[str] = None


class DefectPublic(BaseModel):
    defect_id: str
    type: DefectType
    severity: Grade
    location: Optional[str] = None
    description: Optional[str] = None
    evidence_photo_ids: List[str] = Field(default_factory=list)
    uncertainty: Optional[str] = None
    verified_by: Optional[str] = None


class PhotoPublic(BaseModel):
    photo_id: str
    url: str
    alt: Optional[str] = None
    view: Optional[str] = None


class ItemPublic(BaseModel):
    """Public projection: never order ids, source return, customer, reviewer or storage keys."""
    item_id: str
    title: str
    description: Optional[str] = None
    category: str
    shoe_type: Optional[str] = None
    size_system: Optional[SizeSystem] = None
    size_value: Optional[str] = None
    color: Optional[str] = None
    price_cents: int
    currency: Literal["CAD"] = "CAD"
    grade: Grade
    defects: List[DefectPublic]
    photos: List[PhotoPublic]
    status: InventoryStatus
    listing_version: int
    match_status: Optional[MatchStatus] = None
    match_reasons: Optional[List[str]] = None
    pending_confirmations: Optional[List[str]] = None


class AgentRun(BaseModel):
    """Redacted evidence of one real model run: no prompts, customer text, images or secrets."""

    provider: str
    model_id: Optional[str] = None
    step: str
    model_calls: int
    tools_used: List[str]
    structured_output: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    latency_ms: Optional[int] = None
    images_sent: Optional[int] = None
    runtime: Optional[str] = None  # "bedrock-agentcore" when the agent ran on AgentCore Runtime
    agentcore_request_id: Optional[str] = None
    round_trip_ms: Optional[int] = None
    snapshot_items: Optional[int] = None
    verified_item_ids: Optional[List[str]] = None  # item IDs the remote agent returned that exist in DynamoDB Local
    unverified_item_ids: Optional[List[str]] = None  # returned but not found locally: dropped


class PreferenceRow(BaseModel):
    key: str
    title: str
    value: str


class Progress(BaseModel):
    answered: int
    estimated_total: int


class ChatResponse(BaseModel):
    session_id: str
    mode: Mode
    message: str
    next_action: NextAction
    questions: List[Question]
    required_photos: List[RequiredPhoto]
    return_status: Optional[ReturnStatus]
    items: List[ItemPublic]
    pending_acceptances: List[str]
    can_purchase: bool
    analysis_source: Optional[str]
    request_id: str
    errors: List[str]
    model_id: Optional[str] = None
    blocking_constraints: List[str] = Field(default_factory=list)
    preferences: List[PreferenceRow] = Field(default_factory=list)
    progress: Optional[Progress] = None
    match_count: Optional[int] = None
    agent: Optional[AgentRun] = None


class ItemsPage(BaseModel):
    items: List[ItemPublic]
    next_cursor: Optional[str]
    request_id: str


class AcceptanceResponse(BaseModel):
    item_id: str
    listing_version: int
    accepted_defect_ids: List[str]
    complete: bool
    request_id: str


class PurchaseResponse(BaseModel):
    purchase_id: str
    item_id: str
    listing_version: int
    amount_cents: int
    currency: Literal["CAD"] = "CAD"
    status: str
    payment: str
    shipping: str
    created_at: str
    request_id: str


class PhotoQualityPublic(BaseModel):
    width: int
    height: int
    sharpness: float
    brightness: float
    verdict: Dict[str, str]


class UploadResponse(BaseModel):
    photo_id: str
    declared_view: ShoeView
    session_id: str
    quality: PhotoQualityPublic
    request_id: str


class RuleDecision(BaseModel):
    code: Optional[str] = None
    reasons: List[str] = Field(default_factory=list)
    final_merchant_decision: Literal[False] = False


class ReturnResponse(BaseModel):
    return_id: str
    status: ReturnStatus
    order_id: str
    order_line: str
    claim_type: ClaimType
    reason: Optional[str] = None
    policy_version: str
    missing_views: List[RequiredPhoto]
    retake_reason: Optional[str] = None
    next_step: Optional[str] = None
    demo_address: Optional[str] = None
    inventory_status: Optional[InventoryStatus] = None
    item_id: Optional[str] = None
    analysis_source: Optional[str] = None
    model_id: Optional[str] = None
    rule_decision: RuleDecision
    photo_ids: List[str]
    created_at: str
    updated_at: str
    agent: Optional[AgentRun] = None
    request_id: str


class HealthResponse(BaseModel):
    mode: str
    database: Dict[str, Any]
    agent: Dict[str, Any]
    version: str
    policy_version: str


class ReviewPublic(BaseModel):
    review_id: str
    return_id: str
    review_stage: Literal["CLAIM", "RECEIPT"]
    status: Literal["OPEN", "DECIDED"]
    reason_codes: List[str]
    summary: Optional[str] = None
    conclusion: Optional[str] = None
    decided_by: Optional[str] = None
    decided_at: Optional[str] = None
    simulated: bool = True
    created_at: str


class ReviewsPage(BaseModel):
    reviews: List[ReviewPublic]
    next_cursor: Optional[str]
    request_id: str


class AdminItem(BaseModel):
    item_id: str
    status: InventoryStatus
    grade: Grade
    resale_eligible: bool
    listing_version: int
    source_return_id: Optional[str] = None
    inspection: Optional[Dict[str, Any]] = None
    request_id: str


class ReviewDecisionResponse(BaseModel):
    review: ReviewPublic
    return_status: ReturnStatus
    item_id: Optional[str] = None
    request_id: str
