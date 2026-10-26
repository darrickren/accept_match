"""Customer sessions and chat turns.

Each turn is saved with optimistic locking (session version) together with an
idempotent reply record in one transaction: retrying a client_request_id
returns exactly the same ChatResponse, and two racing messages can't both win.
"""

from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from ..agent.base import ReturnTools, TurnResult
from ..agent.preferences import Preferences, progress, summary
from ..errors import ApiError, conflict, not_found
from ..repository import Repository, TransactionCancelled, now_iso, reply_key, session_key

MAX_MESSAGES = 20


def chat_response(session_id: str, mode: str, turn: TurnResult, request_id: str,
                  extras: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"session_id": session_id, "mode": mode, "message": turn.message, "next_action": turn.next_action,
            "questions": turn.questions, "required_photos": turn.required_photos, "return_status": turn.return_status,
            "items": turn.items, "pending_acceptances": [], "can_purchase": False,
            "analysis_source": turn.analysis_source, "request_id": request_id, "errors": turn.errors,
            "model_id": turn.model_id, "blocking_constraints": turn.blocking, "agent": turn.agent,
            "preferences": [], "progress": None, "match_count": None, **(extras or {})}


class SessionService:
    def __init__(self, repo: Repository, invoker: Any, inventory: Any) -> None:
        self.repo, self.invoker, self.inventory = repo, invoker, inventory

    def create(self, user: str, mode: str, request_id: str) -> Dict[str, Any]:
        session_id = "sess-" + uuid.uuid4().hex
        opening = self.invoker.greeting(mode, self.inventory)
        stamp = now_iso()
        message = opening["message"]
        self.repo.put({"PK": session_key(session_id)[0], "SK": "META", "entity_type": "session",
                       "session_id": session_id, "owner": user, "mode": mode, "state": opening["state"],
                       "messages": [{"role": "assistant", "text": message, "at": stamp}], "turns": 0, "version": 1,
                       "created_at": stamp, "updated_at": stamp, "dataset_id": "runtime"}, if_absent=True)
        real = self.invoker.real_for(mode)  # a real agent's opening is a fixed text: no model has answered yet
        turn = TurnResult(message=message, next_action="ASK_QUESTION", state=opening["state"],
                          questions=opening["questions"], return_status="INFO_COLLECTION" if mode == "return" else None,
                          analysis_source=None if real else self.invoker.source_for(mode), model_id=None)
        return {**chat_response(session_id, mode, turn, request_id), "created_at": stamp}

    def _shopping_extras(self, turn: TurnResult) -> Dict[str, Any]:
        """The site's preference panel, progress and match count, from the validated preferences (every mode)."""
        try:
            prefs = Preferences(**((turn.state or {}).get("preferences") or {}))
        except ValueError:
            return {}
        return {"preferences": summary(prefs), "progress": progress(prefs),
                "match_count": len(self.inventory.find_for(prefs))}

    def get_owned(self, user: str, session_id: str) -> Dict[str, Any]:
        session = self.repo.get(session_key(session_id))
        if not session or session.get("owner") != user:
            raise not_found("SESSION_NOT_FOUND", "This conversation wasn't found. Please start again.")
        return session

    def photos(self, session_id: str) -> List[Dict[str, Any]]:
        return self.repo.query(f"SESSION#{session_id}", "PHOTO#")

    def post(self, user: str, session_id: str, text: str, photo_ids: List[str], client_request_id: str,
             request_id: str) -> Dict[str, Any]:
        session = self.get_owned(user, session_id)
        stored = self.repo.get(reply_key(session_id, client_request_id))
        if stored:
            return stored["response"]
        if photo_ids:
            known = {p["photo_id"] for p in self.photos(session_id)}
            if not set(photo_ids) <= known:
                raise ApiError(422, "UNKNOWN_PHOTO", "One of the photos doesn't belong to this conversation.")
        clean = " ".join(text.split())[:2400]
        if not clean and not photo_ids:
            raise ApiError(422, "EMPTY_MESSAGE", "Please type a message.")
        limit = int(getattr(self.invoker.settings, "agent_max_turns_per_session", 0) or 0)
        if self.invoker.real_for(session["mode"]) and limit and int(session.get("turns") or 0) >= limit:
            raise ApiError(429, "SESSION_LIMIT_REACHED", "This conversation reached its demo limit for AI replies. "
                           "Start over to keep going.", False, {"limit": limit})
        if session["mode"] == "shopping":
            turn = self.invoker.shopping_turn(session.get("state") or {}, clean, self.inventory,
                                              session_id=session_id)
        else:
            turn = self.invoker.return_turn(session.get("state") or {}, clean, ReturnTools(self.repo, user))
        response = chat_response(session_id, session["mode"], turn, request_id,
                                 self._shopping_extras(turn) if session["mode"] == "shopping" else None)
        stamp = now_iso()
        messages = (list(session.get("messages") or []) + [{"role": "user", "text": clean, "at": stamp},
                                                           {"role": "assistant", "text": turn.message, "at": stamp}])
        pk, sk = session_key(session_id)
        rpk, rsk = reply_key(session_id, client_request_id)
        try:
            self.repo.transact([
                {"Update": {"Key": {"PK": pk, "SK": sk},
                            "UpdateExpression": "SET #st = :st, messages = :m, turns = turns + :one, "
                                                "version = version + :one, updated_at = :at",
                            "ConditionExpression": "version = :v",
                            "ExpressionAttributeNames": {"#st": "state"},
                            "ExpressionAttributeValues": {":st": turn.state, ":m": messages[-MAX_MESSAGES:],
                                                          ":one": 1, ":at": stamp, ":v": int(session["version"])}}},
                {"Put": {"Item": {"PK": rpk, "SK": rsk, "entity_type": "chat_reply", "response": response,
                                  "created_at": stamp, "dataset_id": "runtime"},
                         "ConditionExpression": "attribute_not_exists(PK)"}},
            ])
        except TransactionCancelled:
            stored = self.repo.get(reply_key(session_id, client_request_id))
            if stored:  # the same request finished first (a retry raced the original)
                return stored["response"]
            raise conflict("SESSION_CONFLICT", "This conversation changed in another window. Please try again.",
                           retryable=True) from None
        return response
