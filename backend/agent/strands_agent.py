"""The real agent (APP_MODE=gemini-local or bedrock-local): one Strands orchestration layer, two model providers.

    GeminiAgent   Google Gemini API (gemini-3.1-flash-lite by default), see providers.GeminiProvider
    BedrockAgent  Amazon Bedrock Converse, see providers.BedrockProvider

Each customer message is one Strands agent run: the model reads the
conversation state, calls read-only backend tools (search_inventory and
get_item_details for shopping; lookup_demo_order and get_return_policy for
returns), and ends with a Pydantic-validated plan. The model chooses and words
the questions, extracts shopping preferences and return facts from free text,
and describes return photos. It can't change data or decide anything:
- every output is a strict Pydantic model. Invalid output gets exactly one
  repair attempt (Strands sends the validation errors back), then 502
  AI_OUTPUT_INVALID;
- tools are read-only and scoped: public inventory, the caller's own demo
  orders, and the versioned demo policy;
- pairs always come from deterministic matching, question options from what is
  in stock, order IDs only from the customer's own words, photo coverage needs
  our own measurements to pass too, and return decisions come from
  services/rules.py.

Failures map to the error contract and are never answered by the mock:
credentials, key, access, model or region problems -> 503 AI_UNAVAILABLE,
throttling or quota -> 429 RATE_LIMITED, timeouts -> 504 AGENT_TIMEOUT.

Cost guards: bounded max_tokens, a hard cap on model calls per request (Strands
limits), no SDK retries, a deadline per request, images downscaled to 1280 px,
and (Gemini) a per-minute call cap. No model call happens unless a customer
sends a message or submits photos for review.
"""

from __future__ import annotations

import io
import json
import logging
import re
import threading
import time
from typing import Annotated, Any, Callable, Dict, List, Literal, Optional, Tuple, Type

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, model_validator
from strands.hooks import AfterToolCallEvent, AfterToolsEvent, BeforeToolCallEvent, HookProvider

from ..errors import CURRENT_REQUEST
from ..schemas import DefectType, ShoeView, SizeSystem
from ..services.inventory import public
from ..services.photos import hamming
from ..vision import PhotoQuality, VisionDefect, VisionResult
from . import prompts
from .base import AgentFailure, ReturnTools, TurnResult, agent_timeout, ai_output_invalid, question
from .mock import BUDGETS
from .mock import greeting as fixed_greeting
from .preferences import (COLOURS, DEFECT_LABELS, FEATURES, LOCATIONS, SHOE_TYPES, TYPE_PLURALS, Preferences, Size,
                          blocking_constraints, money, size_label)
from .providers import BedrockProvider, GeminiProvider, Provider, failure_chain  # noqa: F401 - re-exported
from .providers import classify_bedrock as classify  # noqa: F401 - re-exported for older imports

LOG = logging.getLogger("acceptmatch.agent")
IMAGE_SIDE = 1280
MAX_IMAGES = 8
MAX_ITEMS = 6
MAX_TOOL_CALLS = 3  # per tool, per customer message
TURNS = {"shopping": 5, "return": 4, "vision": 3, "check": 2}  # hard cap on model calls per request
ModelFactory = Callable[[str, int, float], Any]  # (model_id, max_tokens, read_timeout_s) -> strands Model

ShoeTypeName = Literal[SHOE_TYPES]
ColourName = Literal[tuple(COLOURS)]
LocationName = Literal[tuple(LOCATIONS)]
FeatureName = Literal[tuple(FEATURES)]
Short = Annotated[str, Field(min_length=1, max_length=60)]
Note = Annotated[str, Field(min_length=1, max_length=200)]


# ----------------------------------------------------------------- model-facing schemas

class ModelSchema(BaseModel):
    """Strict (extra=forbid), but explicit nulls mean "not given": Strands marks optional fields nullable."""

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="before")
    @classmethod
    def _nulls_are_missing(cls, data: Any) -> Any:
        return {k: v for k, v in data.items() if v is not None} if isinstance(data, dict) else data


class ModelPreferences(ModelSchema):
    """Everything the customer has said they want so far. Leave a field out if they haven't said."""

    shoe_types: List[ShoeTypeName] = Field(default_factory=list, max_length=10,
                                           description="Styles wanted. Empty means any style.")
    size_system: Optional[SizeSystem] = Field(default=None, description="Size system, given together with size_value")
    size_value: Optional[str] = Field(default=None, pattern=r"^\d{1,2}(\.5)?$", description='Size, e.g. "8" or "8.5"')
    max_price_cents: Optional[int] = Field(default=None, ge=0, le=500_000,
                                           description="Budget in cents of CAD (CA$80 is 8000)")
    any_price: bool = Field(default=False, description="True if the customer said the price doesn't matter")
    ok_defect_types: List[DefectType] = Field(default_factory=list, max_length=8,
                                              description="Imperfection types the customer said are fine")
    rejected_defect_types: List[DefectType] = Field(default_factory=list, max_length=8,
                                                    description="Imperfection types the customer said aren't OK")
    minor_marks_only: bool = Field(default=False, description="True if only minor marks are acceptable")
    rejected_locations: List[LocationName] = Field(default_factory=list, max_length=5,
                                                   description="Places on the shoe where no mark is acceptable")
    colors: List[ColourName] = Field(default_factory=list, max_length=6,
                                     description="Colours wanted. Empty means any colour.")
    required_features: List[FeatureName] = Field(default_factory=list, max_length=4,
                                                 description="Functions the customer says must work")

    @model_validator(mode="after")
    def _consistent(self) -> "ModelPreferences":
        if (self.size_system is None) != (self.size_value is None):
            raise ValueError("give size_system and size_value together, or neither")
        if set(self.ok_defect_types) & set(self.rejected_defect_types):
            raise ValueError("an imperfection type can't be both fine and not OK")
        if self.any_price and self.max_price_cents is not None:
            raise ValueError("use either any_price or max_price_cents, not both")
        return self

    def to_preferences(self) -> Preferences:
        answers = {**{t: "YES" for t in self.ok_defect_types}, **{t: "NO" for t in self.rejected_defect_types}}
        return Preferences(category="SHOES" if self.shoe_types else None, shoe_types=list(self.shoe_types),
                           size=Size(system=self.size_system, value=self.size_value) if self.size_system else None,
                           max_price_cents=self.max_price_cents, any_price=self.any_price,
                           accepted_defect_types=answers, max_grade="A" if self.minor_marks_only else None,
                           rejected_locations=sorted(set(self.rejected_locations)), colors=list(self.colors),
                           required_features=list(dict.fromkeys(self.required_features)))

    @classmethod
    def from_preferences(cls, prefs: Preferences) -> "ModelPreferences":
        answers = prefs.accepted_defect_types
        return cls(shoe_types=[t for t in prefs.shoe_types if t in SHOE_TYPES],
                   size_system=prefs.size.system if prefs.size else None,
                   size_value=prefs.size.value if prefs.size else None, max_price_cents=prefs.max_price_cents,
                   any_price=prefs.any_price and prefs.max_price_cents is None,
                   ok_defect_types=sorted(t for t, v in answers.items() if v == "YES"),
                   rejected_defect_types=sorted(t for t, v in answers.items() if v == "NO"),
                   minor_marks_only=prefs.max_grade == "A",
                   rejected_locations=[x for x in prefs.rejected_locations if x in LOCATIONS],
                   colors=[c for c in prefs.colors if c in COLOURS],
                   required_features=[f for f in prefs.required_features if f in FEATURES][:4])


QuestionId = Literal["style", "size", "budget", "defects", "locations", "relax", "order", "claim", "worn", "repaired",
                     "onset", "other"]


class PlannedQuestion(ModelSchema):
    question_id: QuestionId
    text: str = Field(min_length=3, max_length=200)
    options: List[Short] = Field(default_factory=list, max_length=8,
                                 description="Short answer choices. Empty for a free-text answer.")
    multi: bool = Field(default=False, description="True if several options can be chosen")


class ShoppingPlan(ModelSchema):
    """Your plan for this shopping turn."""

    preferences: ModelPreferences = Field(description="The customer's full, updated preferences")
    reply: str = Field(min_length=1, max_length=600, description="A short reply to the customer")
    questions: List[PlannedQuestion] = Field(default_factory=list, max_length=2)
    show_items: bool = Field(description="True to show the matching pairs now")


class ReturnFacts(ModelSchema):
    order_id: Optional[str] = Field(default=None, max_length=40,
                                    description="The demo order ID exactly as the customer typed it")
    claim_type: Optional[Literal["DEFECT_CLAIM", "STANDARD_RETURN"]] = None
    description: Optional[str] = Field(default=None, max_length=1500,
                                       description="What happened, in the customer's own words")
    worn: Optional[Literal["NOT_WORN", "INDOORS", "OUTDOORS"]] = None
    repaired: Optional[bool] = None
    onset: Optional[Literal["ON_ARRIVAL", "FIRST_WEAR", "AFTER_WEAR"]] = None


class ReturnPlan(ModelSchema):
    """Your plan for this return turn."""

    facts: ReturnFacts = Field(description="Facts the customer has stated so far. Leave unknown ones out.")
    reply: str = Field(min_length=1, max_length=600, description="A short reply to the customer")
    questions: List[PlannedQuestion] = Field(default_factory=list, max_length=2)
    ready_for_photos: bool = Field(description="True when the order was found and every needed fact is known")


class PhotoObservation(ModelSchema):
    photo_id: str
    detected_view: Optional[ShoeView] = Field(default=None, description="The view the photo really shows, or null")
    looks_like_footwear: bool
    occlusion: Literal["LOW", "MEDIUM", "HIGH"]
    sharpness: Literal["PASS", "FAIL"]
    exposure: Literal["PASS", "FAIL"]


class ModelDefect(ModelSchema, VisionDefect):
    pass


class VisionObservation(ModelSchema):
    """Your observations of the photos."""

    photos: List[PhotoObservation] = Field(min_length=1, max_length=MAX_IMAGES)
    defects: List[ModelDefect] = Field(default_factory=list, max_length=12)
    possible_structural_or_safety_issue: bool
    text_photo_conflicts: List[Note] = Field(default_factory=list, max_length=10)
    cannot_verify: List[Note] = Field(default_factory=list, max_length=10)


class ConnectivityCheck(ModelSchema):
    """Your answer to the connectivity check."""

    ok: bool
    shoes_visible: Optional[bool] = Field(default=None, description="Only when an image was sent")


def observation_model(photo_ids: List[str]) -> Type[VisionObservation]:
    """VisionObservation for exactly these photos: the schema lists the ids, validation checks each appears once."""
    ids = tuple(photo_ids)
    photo = create_model("PhotoObservation", __base__=PhotoObservation, photo_id=(Literal[ids], ...))

    def every_photo_once(self):
        if sorted(p.photo_id for p in self.photos) != sorted(ids):
            raise ValueError("report on every photo exactly once: " + ", ".join(ids))
        cited = sorted({pid for d in self.defects for pid in d.photo_ids} - set(ids))
        if cited:
            raise ValueError("defects may only cite these photos: " + ", ".join(ids))
        return self

    return create_model("VisionObservation", __base__=VisionObservation, __doc__=VisionObservation.__doc__,
                        photos=(List[photo], Field(min_length=len(ids), max_length=len(ids))),
                        __validators__={"every_photo_once": model_validator(mode="after")(every_photo_once)})


# ----------------------------------------------------------------- one repair attempt

class OneRepair(HookProvider):
    """Exactly one repair attempt: after a second invalid structured output the loop stops."""

    def __init__(self, output_name: str) -> None:
        self.output_name = output_name
        self.problems: List[str] = []

    def register_hooks(self, registry: Any, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._after_call)
        registry.add_callback(AfterToolsEvent, self._after_tools)

    def _after_call(self, event: AfterToolCallEvent) -> None:
        result = event.result
        if event.tool_use.get("name") == self.output_name and isinstance(result, dict) and \
                result.get("status") == "error" and not event.cancel_message:  # a SearchFirst note isn't a failure
            text = " ".join(str(c.get("text", "")) for c in result.get("content") or [] if isinstance(c, dict))
            self.problems.append(" ".join(text.split())[:300])

    def _after_tools(self, event: AfterToolsEvent) -> None:
        if len(self.problems) >= 2:
            event.end_turn = "Stopped: the structured output was still invalid after one repair attempt."


class SearchFirst(HookProvider):
    """A shopping plan must rest on a real inventory search: a plan sent before any search is turned back once,
    with a note to call search_inventory first. Once only, so a forced final answer still goes through."""

    def __init__(self, output_name: str, searched: List[Any]) -> None:
        self.output_name, self.searched, self.turned_back = output_name, searched, False

    def register_hooks(self, registry: Any, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_call)

    def _before_call(self, event: BeforeToolCallEvent) -> None:
        if event.tool_use.get("name") == self.output_name and not self.searched and not self.turned_back:
            self.turned_back = True
            event.cancel_tool = ("Call search_inventory with the customer's updated preferences first, then return "
                                 "your ShoppingPlan based on what it finds.")


# ----------------------------------------------------------------- controlled tools (read-only)

def _result(tool_use: Dict[str, Any], payload: Dict[str, Any], ok: bool = True) -> Dict[str, Any]:
    return {"toolUseId": tool_use["toolUseId"], "status": "success" if ok else "error",
            "content": [{"text": json.dumps(payload, ensure_ascii=False)}]}


def search_tool(inventory: Any, searched: List[Preferences]):
    """search_inventory: deterministic matching over the shared inventory. Its input schema is ModelPreferences."""
    from strands.tools import PythonAgentTool
    from strands.tools.structured_output import convert_pydantic_to_tool_spec

    spec = convert_pydantic_to_tool_spec(ModelPreferences, "Search the shared AcceptMatch inventory with the "
                                         "customer's full, updated preferences. Returns the matching pairs and what "
                                         "is in stock (sizes, prices, imperfection types).")
    spec["name"] = "search_inventory"

    def run(tool_use: Dict[str, Any], **_: Any) -> Dict[str, Any]:
        if len(searched) >= MAX_TOOL_CALLS:
            return _result(tool_use, {"error": "search limit reached for this turn; return your plan now"}, False)
        try:
            prefs = ModelPreferences.model_validate(tool_use.get("input") or {}).to_preferences()
        except ValidationError as exc:
            return _result(tool_use, {"error": "invalid preferences",
                                      "problems": [f"{'.'.join(map(str, e['loc']))}: {e['msg']}"
                                                   for e in exc.errors()][:5]}, False)
        searched.append(prefs)
        try:
            found = inventory.find_for(prefs)
            stock = inventory.catalogue_facts(prefs)
        except Exception:  # noqa: BLE001 - the model only learns that the search failed
            LOG.warning("search_inventory failed", exc_info=True)
            return _result(tool_use, {"error": "the inventory couldn't be searched right now"}, False)
        LOG.info("tool search_inventory request=%s matched=%s item_ids=%s", CURRENT_REQUEST.get(), len(found),
                 ",".join(row["item_id"] for row, _ in found[:10]) or "-")
        return _result(tool_use, {
            "count": len(found), "full_matches": sum(1 for _, m in found if m.status == "MATCH"),
            "matching": [{"item_id": row["item_id"], "shoe_type": row.get("shoe_type"), "color": row.get("color"),
                          "size": size_label(row.get("size_system"), str(row.get("size_value"))),
                          "price": money(row.get("price_cents")), "match_status": m.status,
                          "imperfections": sorted({d["type"] for d in row.get("defects") or []}),
                          "why_it_fits": m.reasons[:3], "not_yet_confirmed": m.pending[:3]}
                         for row, m in found[:10]],
            "in_stock": {"sizes": [size_label(s, v) for s, v in stock["sizes"][:12]],
                         "prices": [money(p) for p in stock["prices"][:1] + stock["prices"][-1:]],
                         "imperfection_types": stock["defect_types"], "shoe_types": stock["shoe_types"],
                         "pairs_ignoring_size_and_budget": stock["count"]}})

    return PythonAgentTool("search_inventory", spec, run)


def item_tool(inventory: Any, looked_up: List[str]):
    """get_item_details: the public listing of one pair (the same view the site shows)."""
    from strands import tool

    @tool
    def get_item_details(item_id: str) -> Dict[str, Any]:
        """Get the public listing of one pair: price, size, grade and every disclosed imperfection.

        Args:
            item_id: an item ID returned by search_inventory, for example ITM-001
        """
        if len(looked_up) >= MAX_TOOL_CALLS:
            return {"error": "lookup limit reached for this turn; return your plan now"}
        looked_up.append(str(item_id))
        try:
            item = inventory.get_public(str(item_id).strip().upper()[:40])
        except Exception:  # noqa: BLE001 - not listed or not public: the model only learns that
            return {"found": False}
        return {"found": True, "item": {
            "item_id": item["item_id"], "title": item["title"], "status": item["status"],
            "price": money(item["price_cents"]), "size": size_label(item["size_system"], item["size_value"]),
            "color": item["color"], "grade": item["grade"], "listing_version": item["listing_version"],
            "imperfections": [{"type": d["type"], "severity": d["severity"], "location": d["location"],
                               "description": d["description"], "verified_by": d["verified_by"]}
                              for d in item["defects"]],
            "photo_count": len(item["photos"]),
            "note": "Buying needs the customer's own confirmation of each imperfection on the item page."}}

    return get_item_details


def policy_tool(rules: Dict[str, Any]):
    """get_return_policy: the versioned demo return rules. No customer data."""
    from strands import tool

    @tool
    def get_return_policy(claim_type: str) -> Dict[str, Any]:
        """Get the demo return rules for a claim type: time window, required photos and who decides.

        Args:
            claim_type: DEFECT_CLAIM or STANDARD_RETURN
        """
        claim = "STANDARD_RETURN" if str(claim_type).strip().upper() == "STANDARD_RETURN" else "DEFECT_CLAIM"
        reasons = rules.get("view_reasons", {})
        days = rules["standard_return_days"] if claim == "STANDARD_RETURN" else rules["defect_claim_days"]
        return {"policy_version": rules.get("policy_version"), "claim_type": claim, "window_days": days,
                "required_photos": [{"view": v, "why": reasons.get(v)} for v in rules["required_views"][claim]],
                "decided_by": "AcceptMatch's deterministic demo rules after the photo review, never the assistant",
                "note": "Demo only: not an ALDO decision. No refund is promised."}

    return get_return_policy


def order_tool(tools: ReturnTools, looked_up: List[str]):
    """lookup_demo_order: only the signed-in customer's own demo orders."""
    from strands import tool

    @tool
    def lookup_demo_order(order_id: str) -> Dict[str, Any]:
        """Look up one of this customer's demo orders. Returns the order, or found=false.

        Args:
            order_id: the demo order ID exactly as the customer typed it, e.g. ORD-001
        """
        if len(looked_up) >= MAX_TOOL_CALLS:
            return {"error": "lookup limit reached for this turn; return your plan now"}
        looked_up.append(str(order_id))
        order = tools.lookup_demo_order(str(order_id)[:40])
        return {"found": bool(order), "order": order}

    return lookup_demo_order


# ----------------------------------------------------------------- helpers

def _jpeg_for_model(data: bytes) -> bytes:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as img:
        img = img.convert("RGB")
        img.thumbnail((IMAGE_SIDE, IMAGE_SIDE))
        out = io.BytesIO()
        img.save(out, "JPEG", quality=85)
        return out.getvalue()


def _history(state: Dict[str, Any], text: str, reply: str) -> List[Dict[str, str]]:
    turn = [{"role": "user", "text": text[:600]}, {"role": "assistant", "text": reply[:600]}]
    return (list(state.get("history") or []) + turn)[-12:]


def _offered(questions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """What the customer was just asked, so the model can read a chip answer like "Scratches, Scuffs"."""
    return [{"question_id": q["question_id"], "text": q["text"], "options": [o["label"] for o in q["options"]]}
            for q in questions]


def _kind(q: PlannedQuestion) -> str:
    return "multi_choice" if q.multi else ("single_choice" if q.options else "free_text")


def _relax_question(prefs: Preferences) -> Dict[str, Any]:
    options = (["Any price"] if prefs.max_price_cents is not None else []) + \
        [f"{DEFECT_LABELS[t]} are fine" for t, v in sorted(prefs.accepted_defect_types.items()) if v == "NO"][:2] + \
        ["Keep my preferences"]
    return question("relax", "Would you like to relax one of these?", options)


def shopping_questions(planned: List[PlannedQuestion], prefs: Preferences, stock: Dict[str, Any],
                       unknown_types: List[str]) -> List[Dict[str, Any]]:
    """The model's questions with options taken from stock; questions already answered are dropped."""
    out: List[Dict[str, Any]] = []
    for q in planned:
        qid = q.question_id
        if qid == "size":
            if prefs.size or not stock["sizes"]:
                continue
            out.append(question("size", q.text, [{"value": f"{s}:{v}", "label": size_label(s, v)}
                                                 for s, v in stock["sizes"][:9]], help="Sizes in stock right now."))
        elif qid == "style":
            if prefs.shoe_types:
                continue
            styles = [{"value": t, "label": TYPE_PLURALS.get(t, t)} for t in stock["shoe_types"]]
            out.append(question("style", q.text, styles + [{"value": "any", "label": "Any style"}]))
        elif qid == "budget":
            if prefs.max_price_cents is not None or prefs.any_price:
                continue
            out.append(question("budget", q.text, q.options or [f"Under {money(b)}" for b in BUDGETS] + ["Any price"]))
        elif qid == "defects":
            if not unknown_types:
                continue
            out.append(question("defects", q.text, [DEFECT_LABELS[t] for t in unknown_types] + ["None of these"],
                                "multi_choice", help="Choose all that are fine. Anything you don't choose counts as "
                                                     "not OK."))
        elif qid in ("locations", "relax", "other"):
            out.append(question(qid, q.text, list(q.options), _kind(q)))
    return out[:2]


RETURN_QUESTIONS = {
    "order": ("What's the demo order ID?", []),
    "worn": ("Have the shoes been worn?", ["Not worn", "Only indoors", "Yes, outdoors"]),
    "repaired": ("Have they been repaired or altered?", ["No", "Yes"]),
    "onset": ("When did you notice the issue?", ["When I unboxed them", "The first time I wore them",
                                                 "After a few wears"]),
}


def _return_question(qid: str, planned: Optional[PlannedQuestion] = None) -> Dict[str, Any]:
    text, options = RETURN_QUESTIONS[qid]
    if planned is not None:
        return question(qid, planned.text, list(planned.options) or options,
                        _kind(planned) if planned.options else ("free_text" if not options else "single_choice"))
    return question(qid, text, options, "single_choice" if options else "free_text")


def _run_evidence(provider: str, model_id: str, step: str, output_name: str, result: Any,
                  started: float) -> Dict[str, Any]:
    """What one agent run did, without prompts, customer text, images or secrets."""
    metrics = getattr(result, "metrics", None)
    usage = dict(getattr(metrics, "accumulated_usage", None) or {})
    tools = list(getattr(metrics, "tool_metrics", None) or {})
    return {"provider": provider, "model_id": model_id, "step": step,
            "model_calls": int(getattr(metrics, "cycle_count", 0) or 0),
            "tools_used": [name for name in tools if name != output_name], "structured_output": output_name,
            "input_tokens": usage.get("inputTokens"), "output_tokens": usage.get("outputTokens"),
            "latency_ms": int((time.monotonic() - started) * 1000)}


# ----------------------------------------------------------------- the agent

class StrandsAgent:
    """The shopping, return and photo-review agent. The provider decides which model API answers."""

    def __init__(self, settings: Any, provider: Provider, model_factory: Optional[ModelFactory] = None) -> None:
        self.settings = settings
        self.provider = provider
        self.source = provider.source
        self.model_id = provider.model_id
        self.vision_model_id = provider.vision_model_id
        self._factory = model_factory or provider.build

    def _run(self, step: str, system: str, prompt: Any, output: Type[BaseModel], tools: List[Any],
             model_id: str, hooks: Tuple[Any, ...] = ()) -> Tuple[Any, Dict[str, Any]]:
        """One Strands invocation: validated output, one repair, capped calls, a deadline, classified failures.

        Returns the validated output and redacted run evidence (provider, model, calls, tools, tokens, latency).
        """
        from strands import Agent
        from strands.tools.executors import SequentialToolExecutor

        deadline = float(self.settings.agent_vision_timeout_s if step == "vision" else
                         self.settings.agent_turn_timeout_s)
        guard = OneRepair(output.__name__)
        cancel = threading.Event()
        timer = threading.Timer(deadline, cancel.set)
        timer.daemon = True
        started = time.monotonic()
        try:
            model = self._factory(model_id, self.provider.max_tokens[step], self.provider.read_timeout(deadline))
            agent = Agent(model=model, tools=tools, system_prompt=system, callback_handler=None, retry_strategy=None,
                          tool_executor=SequentialToolExecutor(), hooks=[guard, *hooks], name=f"acceptmatch-{step}")
            timer.start()
            result = agent(prompt, structured_output_model=output, limits={"turns": TURNS[step]},
                           cancel_signal=cancel)
        except AgentFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - every failure is classified; none is answered by the mock
            failure = self.provider.classify(exc)
            LOG.warning("ai_call request=%s provider=%s model=%s step=%s FAILED %s -> %s %s", CURRENT_REQUEST.get(),
                        self.provider.name, model_id, step, self.provider.describe(exc), failure.status, failure.code)
            raise failure from None
        finally:
            timer.cancel()
        run = _run_evidence(self.provider.name, model_id, step, output.__name__, result, started)
        if result.stop_reason == "cancelled":
            LOG.warning("ai_call request=%s provider=%s model=%s step=%s TIMEOUT after %s ms", CURRENT_REQUEST.get(),
                        self.provider.name, model_id, step, run["latency_ms"])
            raise agent_timeout()
        value = result.structured_output
        if isinstance(value, output):
            LOG.info("ai_call request=%s provider=%s model=%s step=%s ok calls=%s tools=%s tokens_in=%s "
                     "tokens_out=%s latency_ms=%s", CURRENT_REQUEST.get(), self.provider.name, model_id, step,
                     run["model_calls"], ",".join(run["tools_used"]) or "-", run["input_tokens"],
                     run["output_tokens"], run["latency_ms"])
            return value, run
        why = guard.problems[-1] if guard.problems else \
            f"no {output.__name__} was produced (stop reason {result.stop_reason})"
        LOG.warning("ai_call request=%s provider=%s model=%s step=%s INVALID %s", CURRENT_REQUEST.get(),
                    self.provider.name, model_id, step, why[:200])
        raise ai_output_invalid([why])

    def check(self, with_image: bool = False, evidence: Optional[List[Dict[str, Any]]] = None) -> ConnectivityCheck:
        """One small request through the production path. Uses quota: only `dev check-*` with approval calls it."""
        prompt: Any = "Connectivity check: return ok=true."
        model_id = self.model_id
        if with_image:
            from ..seed.images import render

            prompt = [{"text": "Connectivity check: return ok=true and say whether shoes are visible."},
                      {"image": {"format": "jpeg", "source": {"bytes": _jpeg_for_model(render("pair_top", "black",
                                                                                              None, seed=1))}}}]
            model_id = self.vision_model_id
        value, run = self._run("check", "You answer connectivity checks for a demo backend.", prompt,
                               ConnectivityCheck, [], model_id)
        if evidence is not None:
            evidence.append(run)
        return value

    # ------------------------------------------------------------ shopping

    def greeting(self, inventory: Any) -> Dict[str, Any]:
        """A fixed opening in every mode: the first model call happens on the customer's first message."""
        return fixed_greeting(inventory)

    def shopping_turn(self, state: Dict[str, Any], text: str, inventory: Any) -> TurnResult:
        state = {"preferences": {}, "asked": [], "history": [], **(state or {})}
        current = ModelPreferences.from_preferences(Preferences(**state["preferences"]))
        searched: List[Preferences] = []
        prompt = json.dumps({"current_preferences": current.model_dump(), "asked_questions": state["asked"],
                             "questions_just_asked": state.get("offered") or [],
                             "recent_conversation": state["history"][-6:], "customer_message": text},
                            ensure_ascii=False)
        plan, run = self._run("shopping", prompts.SHOPPING, prompt, ShoppingPlan,
                              [search_tool(inventory, searched), item_tool(inventory, [])], self.model_id,
                              hooks=(SearchFirst(ShoppingPlan.__name__, searched),))
        prefs = plan.preferences.to_preferences()
        found = inventory.find_for(prefs)  # deterministic: the model can't add, drop or upgrade a pair
        stock = inventory.catalogue_facts(prefs)
        unknown = sorted({d["type"] for row, _ in found for d in row.get("defects") or []
                          if prefs.answer(d["type"]) == "UNKNOWN"})
        questions = shopping_questions(plan.questions, prefs, stock, unknown)
        message = plan.reply
        if not found and (plan.show_items or not questions):
            questions = [_relax_question(prefs)]
            message = "Nothing in stock fits all of that right now. I won't change your preferences unless you say so."
        show = bool(found) and (plan.show_items or not questions)
        if show and not plan.show_items:
            message = f"Here {'is the pair' if len(found) == 1 else 'are the pairs'} that fit what you've told me."
        shown = questions[:1] if show else questions
        new_state = {"preferences": prefs.model_dump(),
                     "asked": list(dict.fromkeys(state["asked"] + [q["question_id"] for q in shown])),
                     "offered": _offered(shown), "history": _history(state, text, message)}
        common = {"analysis_source": self.source, "model_id": self.model_id, "agent": run}
        if show:
            return TurnResult(message, "SHOW_ITEMS", new_state, shown,
                              items=[public(row, m) for row, m in found[:MAX_ITEMS]], **common)
        return TurnResult(message, "ASK_QUESTION", new_state, shown,
                          blocking=blocking_constraints(prefs) if not found else [], **common)

    # ------------------------------------------------------------ returns

    def return_turn(self, state: Dict[str, Any], text: str, tools: ReturnTools, rules: Dict[str, Any]) -> TurnResult:
        state = {"facts": {}, "asked": [], "history": [], **(state or {})}
        prompt = json.dumps({"known_facts": state["facts"], "asked_questions": state["asked"],
                             "questions_just_asked": state.get("offered") or [],
                             "recent_conversation": state["history"][-6:], "customer_message": text},
                            ensure_ascii=False)
        plan, run = self._run("return", prompts.RETURN, prompt, ReturnPlan,
                              [order_tool(tools, []), policy_tool(rules)], self.model_id)
        new = {k: v for k, v in plan.facts.model_dump().items() if v is not None}
        proposed = str(new.pop("order_id", "") or "").strip().upper()
        if proposed and proposed != state["facts"].get("order_id") and \
                re.search(rf"(?<![A-Z0-9-]){re.escape(proposed)}(?![A-Z0-9-])", text.upper()):
            new["order_id"] = proposed  # only an order ID the customer typed in this message
        happened = re.search(r"what happened:\s*(.+)$", text, re.I | re.S)
        if happened:
            new["description"] = happened.group(1).strip()[:1500]  # the customer's own words, verbatim
        facts = {**state["facts"], **new}
        order = tools.lookup_demo_order(facts["order_id"]) if facts.get("order_id") else None  # always our check
        claim = facts.get("claim_type") or "DEFECT_CLAIM"
        missing = [f for f in ["worn", "repaired"] + (["onset"] if claim == "DEFECT_CLAIM" else [])
                   if facts.get(f) is None]
        planned = {q.question_id: q for q in plan.questions}
        common = {"return_status": "INFO_COLLECTION", "analysis_source": self.source, "model_id": self.model_id,
                  "agent": run}
        if not order:
            message = (f"I couldn't find demo order {facts['order_id']} for your account. " if facts.get("order_id")
                       else "Which demo order is this about? ") + \
                "Please check the demo order ID (for example ORD-001)."
            questions = [_return_question("order", planned.get("order"))]
            return TurnResult(message, "ASK_QUESTION", self._return_state(state, facts, questions, text, message),
                              questions, **common)
        if not missing and (plan.ready_for_photos or not [q for q in plan.questions if q.question_id == "claim"]):
            message = plan.reply if plan.ready_for_photos else \
                "Thanks, that's everything I need to know. Please add the photos listed below."
            reasons = rules.get("view_reasons", {})
            return TurnResult(message, "UPLOAD_PHOTOS", self._return_state(state, facts, [], text, message),
                              required_photos=[{"view": v, "reason": reasons.get(v)}
                                               for v in rules["required_views"][claim]], **common)
        if not missing:  # every fact is known but the model wants the claim type confirmed
            claim_q = planned["claim"]
            questions = [question("claim", claim_q.text, list(claim_q.options), _kind(claim_q))]
            message = plan.reply
        elif [q for q in plan.questions if q.question_id in missing]:  # the model's choice and wording
            questions = [_return_question(q.question_id, q) for q in plan.questions if q.question_id in missing]
            message = plan.reply
        else:  # the model asked about something else: ask for the missing facts instead
            questions = [_return_question(f) for f in missing[:2]]
            message = f"Thanks. I found {order['order_id']}. " \
                f"{'Two quick questions' if len(questions) > 1 else 'One quick question'}."
        return TurnResult(message, "ASK_QUESTION", self._return_state(state, facts, questions, text, message),
                          questions, **common)

    @staticmethod
    def _return_state(state: Dict[str, Any], facts: Dict[str, Any], questions: List[Dict[str, Any]], text: str,
                      message: str) -> Dict[str, Any]:
        return {"facts": facts, "asked": list(dict.fromkeys(state["asked"] + [q["question_id"] for q in questions])),
                "offered": _offered(questions), "history": _history(state, text, message)}

    # ------------------------------------------------------------ photos (multimodal)

    def analyze_photos(self, photos: List[Dict[str, Any]], expected_views: List[str], facts: Dict[str, Any],
                       line: Dict[str, Any], images: Dict[str, bytes],
                       rules: Dict[str, Any]) -> Tuple[VisionResult, Optional[Dict[str, Any]]]:
        """The stored image bytes go to the model; returns the merged result and the run evidence."""
        newest: Dict[str, Dict[str, Any]] = {}
        for photo in sorted(photos, key=lambda p: p.get("uploaded_at", "")):
            newest[photo["declared_view"]] = photo  # a retake replaces the earlier photo of that view
        content: List[Dict[str, Any]] = [{"text": json.dumps({
            "order_line": {k: line.get(k) for k in ("product_name", "shoe_type", "size_system", "size_value", "color")},
            "customer_description": (facts.get("description") or "")[:1500], "expected_views": expected_views},
            ensure_ascii=False)}]
        chosen, unreadable = [], []
        for photo in list(newest.values())[:MAX_IMAGES]:
            try:
                jpeg = _jpeg_for_model(images.get(photo["photo_id"]) or b"")
            except Exception:  # noqa: BLE001 - a missing or broken file just can't count as evidence
                unreadable.append(photo["photo_id"])
                continue
            chosen.append(photo)
            label = f"Photo {photo['photo_id']} (the customer says it shows: {photo['declared_view']})"
            content.append({"text": label})
            content.append({"image": {"format": "jpeg", "source": {"bytes": jpeg}}})
        notes = [f"photo {pid} couldn't be read" for pid in unreadable][:3]
        if not chosen:
            return VisionResult(photo_quality=[], covered_views=[], missing_views=list(expected_views), defects=[],
                                possible_structural_or_safety_issue=False, text_photo_conflicts=[],
                                cannot_verify=(notes or ["no photos were provided"])), None
        schema = observation_model([p["photo_id"] for p in chosen])
        observed, run = self._run("vision", prompts.VISION, content, schema, [], self.vision_model_id)
        run["images_sent"] = len(chosen)
        result = merge(observed, chosen, expected_views, rules)
        if notes:
            result = result.model_copy(update={"cannot_verify": (notes + result.cannot_verify)[:10]})
        return result, run


class BedrockAgent(StrandsAgent):
    """APP_MODE=bedrock-local: Strands + Amazon Bedrock."""

    def __init__(self, settings: Any, model_factory: Optional[ModelFactory] = None) -> None:
        super().__init__(settings, BedrockProvider(settings), model_factory)

    def _bedrock_model(self, model_id: str, max_tokens: int, read_timeout_s: float):
        return self.provider.build(model_id, max_tokens, read_timeout_s)


class GeminiAgent(StrandsAgent):
    """APP_MODE=gemini-local: Strands + the Google Gemini API (GEMINI_API_KEY, server-side only)."""

    def __init__(self, settings: Any, model_factory: Optional[ModelFactory] = None, transport: Any = None) -> None:
        super().__init__(settings, GeminiProvider(settings, transport=transport), model_factory)


def merge(observed: VisionObservation, photos: List[Dict[str, Any]], expected_views: List[str],
          rules: Dict[str, Any]) -> VisionResult:
    """Combine the model's observations with our own measurements. The model alone can't pass a photo."""
    by_id = {p["photo_id"]: p for p in photos}
    unknown = [o.photo_id for o in observed.photos if o.photo_id not in by_id]
    if unknown:
        raise ai_output_invalid([f"observation for unknown photo {unknown[0]}"])
    limit = int(rules.get("quality", {}).get("duplicate_max_distance", 12))
    quality, covered, seen = [], [], []
    for obs in observed.photos:
        photo = by_id[obs.photo_id]
        measured = photo.get("verdict") or {}
        sharp = "FAIL" if "FAIL" in (obs.sharpness, measured.get("sharpness", "FAIL")) else "PASS"
        exposure = "FAIL" if "FAIL" in (obs.exposure, measured.get("exposure", "FAIL")) else "PASS"
        duplicate = next((other["photo_id"] for other in seen if other.get("dhash") and photo.get("dhash") and
                          hamming(other["dhash"], photo["dhash"]) <= limit), None)
        seen.append(photo)
        quality.append(PhotoQuality(photo_id=obs.photo_id, sharpness=sharp, exposure=exposure, occlusion=obs.occlusion,
                                    looks_like_footwear=obs.looks_like_footwear, duplicate_of=duplicate,
                                    detected_view=obs.detected_view))
        usable = sharp == "PASS" and exposure == "PASS" and measured.get("resolution") == "PASS" and \
            obs.occlusion != "HIGH" and obs.looks_like_footwear and not duplicate
        if usable and obs.detected_view and obs.detected_view not in covered:
            covered.append(obs.detected_view)  # coverage comes from what the model saw, not the declared view
    covered = [v for v in expected_views if v in covered]
    return VisionResult(photo_quality=quality, covered_views=covered,
                        missing_views=[v for v in expected_views if v not in covered],
                        defects=[VisionDefect(**d.model_dump()) for d in observed.defects],
                        possible_structural_or_safety_issue=observed.possible_structural_or_safety_issue,
                        text_photo_conflicts=list(observed.text_photo_conflicts),
                        cannot_verify=list(observed.cannot_verify))
