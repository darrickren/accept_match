"""Fake AcceptMatch FastAPI backend for frontend tests and local UI work.

THIS IS NOT THE BACKEND. It is a small standard-library HTTP server that
follows the plan's section 6 contract (AcceptMatch_MVP_Implementation_Plan.md)
with in-memory synthetic footwear data, so the site's api mode can be tested
without AWS or the real FastAPI service. Its "vision" is a size heuristic and
its chat is a fixed script. Faults (status codes, delays, malformed bodies)
can be injected per path.

Standalone, for local UI work:
    python tests/ui/fake_api.py --port 8765
    ACCEPTMATCH_AGENT=api ACCEPTMATCH_API_BASE_URL=http://127.0.0.1:8765 \
    ACCEPTMATCH_DEMO_PERSONA=cust-02 ACCEPTMATCH_DEMO_PASSWORD=pw-cust-02 streamlit run app.py
"""

from __future__ import annotations

import copy
import io
import json
import re
import secrets
import threading
import time
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from PIL import Image

PERSONAS = {"cust-01": ("pw-cust-01", "customer"), "cust-02": ("pw-cust-02", "customer"),
            "admin": ("pw-admin", "admin")}
SHOE_VIEWS = ("pair_top", "pair_outer", "pair_inner", "pair_soles", "pair_heels", "defect_closeup", "size_label")
REQUIRED_VIEWS = {"DEFECT_CLAIM": ("pair_top", "pair_soles", "pair_heels", "defect_closeup", "size_label"),
                  "STANDARD_RETURN": ("pair_top", "pair_outer", "pair_soles")}
VIEW_REASONS = {"pair_top": "We need to see both shoes together.", "pair_outer": "Outer sides aren't visible yet.",
                "pair_soles": "We can't see both soles.", "pair_heels": "The heels aren't visible yet.",
                "defect_closeup": "We need a close look at the issue.", "size_label": "The size label isn't checked."}
ORDERS = {"ORD-001": {"owner": "cust-01", "lines": {"1"}, "final_sale": False},
          "ORD-002": {"owner": "cust-01", "lines": {"1"}, "final_sale": True},
          "ORD-009": {"owner": "cust-01", "lines": {"1"}, "final_sale": False, "reject": True}}
DEMO_ADDRESS = "FICTIONAL DEMO ADDRESS — DO NOT SHIP\nAcceptMatch Returns (demo)\n100 Example Street, Montréal QC"


def jpeg(colour: Tuple[int, int, int], size: Tuple[int, int] = (480, 360)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, "JPEG")
    return buf.getvalue()


def _defect(defect_id, kind, severity, location, description):
    return {"defect_id": defect_id, "type": kind, "severity": severity, "location": location,
            "description": description, "evidence_photo_ids": [], "uncertainty": "low", "verified_by": "DEMO_SIMULATED"}


def seed_items() -> Dict[str, Dict[str, Any]]:
    def item(item_id, title, shoe, system, size, color, cents, grade, status, defects, colour):
        return {"item_id": item_id, "title": title, "description": f"Returned {title.lower()}, inspected (demo).",
                "category": "SHOES", "shoe_type": shoe, "size_system": system, "size_value": size, "color": color,
                "price_cents": cents, "currency": "CAD", "grade": grade, "status": status, "listing_version": 1,
                "defects": defects,
                "photos": [{"photo_id": f"P-{item_id[-3:]}", "url": f"/media/items/{item_id}.jpg",
                            "alt": f"{title}, both shoes", "view": "pair_top"}],
                "_colour": colour}
    rows = [
        item("ITM-001", "Black Ankle Boots", "Ankle boot", "US_WOMEN", "7", "black", 6900, "A", "AVAILABLE",
             [_defect("D-001", "scratch", "A", "back of the left heel", "Small cosmetic mark on the heel counter")],
             (30, 30, 34)),
        item("ITM-002", "Cognac Loafers", "Loafer", "US_MEN", "9", "brown", 8500, "B", "AVAILABLE",
             [_defect("D-002", "scuff", "B", "left toe", "Visible scuff on the toe cap"),
              _defect("D-003", "stain", "A", "right inner side", "Faint water mark")], (150, 90, 50)),
        item("ITM-003", "Black Ankle Boots", "Ankle boot", "US_WOMEN", "7", "black", 7400, "A", "SOLD",
             [_defect("D-004", "scratch", "A", "right toe", "Light scratch")], (40, 40, 40)),
        item("ITM-004", "Beige Pumps", "Pump", "EU", "38", "beige", 7900, "A", "AWAITING_RECEIPT",
             [_defect("D-007", "scuff", "A", "left heel", "Small scuff")], (220, 200, 170)),
        item("ITM-005", "Black Sneakers", "Sneaker", "US_WOMEN", "7", "black", 5900, "A", "AVAILABLE",
             [_defect("D-005", "scuff", "A", "right toe", "Light scuff on the toe"),
              _defect("D-006", "discoloration", "A", "left heel", "Slight yellowing")], (20, 20, 20)),
        item("ITM-006", "Black Heeled Sandals", "Sandal", "US_WOMEN", "7", "black", 9900, "C", "QUARANTINED",
             [_defect("D-008", "broken_heel", "C", "left heel", "Heel is loose")], (60, 60, 60)),
    ]
    return {row["item_id"]: row for row in rows}


ROUTES = [
    ("POST", r"/api/demo-auth/login"), ("POST", r"/api/sessions"), ("POST", r"/api/sessions/{id}/messages"),
    ("POST", r"/api/uploads"), ("POST", r"/api/returns"), ("POST", r"/api/returns/{id}/evaluate"),
    ("GET", r"/api/returns/{id}"), ("GET", r"/api/items"), ("GET", r"/api/items/{id}"),
    ("POST", r"/api/items/{id}/acceptances"), ("POST", r"/api/demo-checkout"), ("GET", r"/api/navigation"),
    ("GET", r"/health"),
]


class FakeBackend:
    """In-memory state. Tests call fault(), bump_listing(), sell() and read .requests."""

    def __init__(self, analysis_source: Optional[str] = "SIMULATED", model_id: Optional[str] = None) -> None:
        self.lock = threading.RLock()
        self.analysis_source = analysis_source
        self.model_id = model_id
        self.routes = list(ROUTES)
        self.tokens: Dict[str, str] = {}
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.photos: Dict[str, Dict[str, Any]] = {}
        self.returns: Dict[str, Dict[str, Any]] = {}
        self.items = seed_items()
        self.acceptances: Dict[Tuple[str, str, int], set] = {}
        self.purchases: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self.replies: Dict[Tuple[str, str], Tuple[int, Any]] = {}
        self.faults: List[Dict[str, Any]] = []
        self.requests: List[Dict[str, Any]] = []
        self.counter = 0
        self.health_version = "0.1.0"

    # ------------------------------------------------------------ test controls

    def fault(self, method: str, path: str, status: Optional[int] = None, body: Any = None, delay: float = 0.0,
              times: int = 1, raw: Optional[bytes] = None, content_type: str = "application/json",
              headers: Optional[Dict[str, str]] = None) -> None:
        self.faults.append({"method": method, "path": re.compile(path), "status": status, "body": body,
                            "delay": delay, "times": times, "raw": raw, "content_type": content_type,
                            "headers": headers or {}})

    def remove_route(self, method: str, path: str) -> None:
        self.routes = [r for r in self.routes if r != (method, path)]

    def bump_listing(self, item_id: str) -> None:
        with self.lock:
            self.items[item_id]["listing_version"] += 1

    def sell(self, item_id: str) -> None:
        with self.lock:
            self.items[item_id]["status"] = "SOLD"

    def expire_tokens(self) -> None:
        with self.lock:
            self.tokens.clear()

    def calls(self, method: str, prefix: str) -> List[Dict[str, Any]]:
        return [r for r in self.requests if r["method"] == method and r["path"].startswith(prefix)]

    # ------------------------------------------------------------ helpers

    def next_id(self, prefix: str) -> str:
        with self.lock:
            self.counter += 1
            return f"{prefix}-{self.counter:03d}"

    def request_id(self) -> str:
        return self.next_id("req")

    def error(self, status: int, code: str, message: str, retryable: bool = False, detail: Any = None):
        return status, {"request_id": self.request_id(), "code": code, "message": message, "retryable": retryable,
                        "detail": detail}

    def public(self, item: Dict[str, Any]) -> Dict[str, Any]:
        return {k: copy.deepcopy(v) for k, v in item.items() if not k.startswith("_")}

    def chat(self, session_id: str, mode: str, message: str, next_action: str, **extra) -> Dict[str, Any]:
        body = {"session_id": session_id, "mode": mode, "message": message, "next_action": next_action,
                "questions": [], "required_photos": [], "return_status": None, "items": [], "pending_acceptances": [],
                "can_purchase": False, "analysis_source": self.analysis_source, "request_id": self.request_id(),
                "errors": []}
        if self.model_id:
            body["model_id"] = self.model_id
        body.update(extra)
        return body

    # ------------------------------------------------------------ shopping script

    @staticmethod
    def parse_preferences(prefs: Dict[str, Any], text: str) -> None:
        low = text.lower()
        size = re.search(r"us\s*(women'?s|men'?s)\s*(\d{1,2}(\.5)?)|eu\s*(\d{2})", low)
        if size:
            if size.group(4):
                prefs["size"] = ("EU", size.group(4))
            else:
                prefs["size"] = ("US_WOMEN" if size.group(1).startswith("women") else "US_MEN", size.group(2))
        budget = re.search(r"under\s*(ca)?\$?\s*(\d{2,4})", low)
        if budget:
            prefs["max_price_cents"] = int(budget.group(2)) * 100
        if "any price" in low:
            prefs["max_price_cents"] = None
        for kind, pattern in (("scratch", r"scratch"), ("scuff", r"scuff"), ("stain", r"stain"),
                              ("discoloration", r"discolou?r")):
            if re.search(rf"no {pattern}|not? (ok|fine) with {pattern}", low):
                prefs.setdefault("defects", {})[kind] = "NO"
            elif re.search(pattern, low):
                prefs.setdefault("defects", {})[kind] = "YES"
        if "defects" in prefs or "none of these" in low:
            prefs["asked_defects"] = True

    def shopping_turn(self, session: Dict[str, Any], sid: str, text: str) -> Dict[str, Any]:
        prefs = session.setdefault("prefs", {})
        self.parse_preferences(prefs, text)
        if "size" not in prefs:
            questions = [{"question_id": "size", "text": "Which size do you wear?",
                          "options": [{"value": "US_WOMEN:7", "label": "US women's 7"},
                                      {"value": "US_MEN:9", "label": "US men's 9"},
                                      {"value": "EU:38", "label": "EU 38"}]}]
            if "max_price_cents" not in prefs:
                questions.append({"question_id": "budget", "text": "What's your budget?",
                                  "options": ["Under CA$100", "Any price"]})
            return self.chat(sid, "shopping", "Thanks. A quick question so I only show pairs that fit.",
                             "ASK_QUESTION", questions=questions)
        if not prefs.get("asked_defects"):
            return self.chat(sid, "shopping", "Which imperfections are fine with you?", "ASK_QUESTION",
                             questions=[{"question_id": "defects", "text": "Choose all that are fine.",
                                         "type": "multi_choice", "options": ["Scratches", "Scuffs", "Stains"]}])
        system, value = prefs["size"]
        limit = prefs.get("max_price_cents")
        answers = prefs.get("defects", {})
        items = []
        for item in self.items.values():
            if item["status"] != "AVAILABLE" or item["grade"] not in ("A", "B"):
                continue
            if (item["size_system"], item["size_value"]) != (system, value):
                continue
            if limit is not None and item["price_cents"] > limit:
                continue
            kinds = {d["type"] for d in item["defects"]}
            if any(answers.get(k) == "NO" for k in kinds):
                continue  # NO is a hard exclusion
            unknown = sorted(k for k in kinds if answers.get(k) != "YES")
            card = self.public(item)
            size_name = {"US_WOMEN": "US women's", "US_MEN": "US men's", "EU": "EU"}[system]
            card["match_reasons"] = [f"Size {size_name} {value}"] + \
                [f"{k.title()} is fine with you" for k in sorted(kinds) if answers.get(k) == "YES"]
            card["pending_confirmations"] = [f"You haven't said whether {k} is OK" for k in unknown]
            card["match_status"] = "NEEDS_CONFIRMATION" if unknown else "MATCH"
            items.append(card)
        if not items:
            blocking = [f"Size {value}"] + ([f"Under CA${limit // 100}"] if limit else []) + \
                [f"No {k}" for k, v in sorted(answers.items()) if v == "NO"]
            return self.chat(sid, "shopping", "Nothing available fits all of that right now.", "ASK_QUESTION",
                             blocking_constraints=blocking,
                             questions=[{"question_id": "relax", "text": "Would a higher budget be OK?",
                                         "options": ["Yes, any price", "No, keep my budget"]}])
        return self.chat(sid, "shopping", f"I found {len(items)} pairs that fit.", "SHOW_ITEMS", items=items,
                         can_purchase=False)

    # ------------------------------------------------------------ return script

    def return_turn(self, session: Dict[str, Any], sid: str, text: str) -> Dict[str, Any]:
        session["turns"] = session.get("turns", 0) + 1
        if "DEFECT" in text.upper() or "defect claim" in text.lower():
            session["claim"] = "DEFECT_CLAIM"
        elif "standard return" in text.lower():
            session["claim"] = "STANDARD_RETURN"
        if session["turns"] == 1:
            return self.chat(sid, "return", "Thanks. Have the shoes been worn outdoors?", "ASK_QUESTION",
                             return_status="INFO_COLLECTION",
                             questions=[{"question_id": "worn", "text": "Have they been worn outdoors?",
                                         "options": ["Only indoors", "Yes, outdoors", "Not worn"]}])
        views = REQUIRED_VIEWS[session.get("claim", "DEFECT_CLAIM")]
        return self.chat(sid, "return", "Please add these photos of both shoes.", "UPLOAD_PHOTOS",
                         return_status="INFO_COLLECTION",
                         required_photos=[{"view": v, "reason": VIEW_REASONS.get(v)} for v in views])

    def evaluate(self, record: Dict[str, Any]) -> None:
        session_photos = [p for p in self.photos.values() if p["session_id"] == record["session_id"]]
        latest: Dict[str, Dict[str, Any]] = {}
        for photo in session_photos:
            latest[photo["view"]] = photo  # the newest photo of each view counts
        missing = []
        for view in REQUIRED_VIEWS[record["claim_type"]]:
            photo = latest.get(view)
            if not photo:
                missing.append({"view": view, "reason": VIEW_REASONS.get(view)})
            elif photo["width"] < 300:  # stand-in for the real vision check: tiny photos count as blurry
                missing.append({"view": view, "reason": "The photo is too blurry to check."})
        order = ORDERS[record["order_id"]]
        record.update({"analysis_source": self.analysis_source, "model_id": self.model_id,
                       "photo_ids": [p["photo_id"] for p in session_photos], "request_id": self.request_id()})
        if missing:
            record.update(status="NEEDS_MORE_EVIDENCE", missing_views=missing,
                          retake_reason="Some views are missing or unclear.", next_step="Add the photos listed.")
        elif order["final_sale"]:
            record.update(status="HUMAN_REVIEW_REQUIRED", missing_views=[], retake_reason=None,
                          next_step="A specialist will look at this final-sale defect claim (simulated).")
        elif order.get("reject"):
            record.update(status="REJECTED_AFTER_REVIEW", missing_views=[], retake_reason=None,
                          next_step="The demo reviewer couldn't accept this request.")
        else:
            if not record.get("item_id"):
                record["item_id"] = f"ITM-{record['return_id']}"  # exactly one inventory record per return
            record.update(status="APPROVED_DEMO", missing_views=[], retake_reason=None,
                          inventory_status="AWAITING_RECEIPT", demo_address=DEMO_ADDRESS,
                          next_step="Pack both shoes and write your return ID on the parcel (demo).",
                          rule_decision={"code": "DEMO_TRIAGE_APPROVAL", "final_merchant_decision": False})


class Handler(BaseHTTPRequestHandler):
    backend: FakeBackend = None  # set by start()
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    # ------------------------------------------------------------ plumbing

    def _send(self, status: int, body: Any = None, raw: Optional[bytes] = None,
              content_type: str = "application/json", headers: Optional[Dict[str, str]] = None) -> None:
        data = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client gave up (timeout tests)

    def _user(self) -> Optional[str]:
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            return None
        return self.backend.tokens.get(header[7:])

    def _dispatch(self, method: str) -> None:
        backend = self.backend
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        raw_body = self.rfile.read(length) if length else b""
        backend.requests.append({"method": method, "path": path, "query": parse_qs(parsed.query),
                                 "authorized": bool(self.headers.get("Authorization")),
                                 "idempotency_key": self.headers.get("Idempotency-Key"),
                                 "x_request_id": self.headers.get("X-Request-ID"),
                                 "content_type": self.headers.get("Content-Type", "")})
        for fault in list(backend.faults):
            if fault["method"] == method and fault["path"].fullmatch(path):
                fault["times"] -= 1
                if fault["times"] <= 0:
                    backend.faults.remove(fault)
                if fault["delay"]:
                    time.sleep(fault["delay"])
                if fault["raw"] is not None:
                    return self._send(fault["status"] or 200, raw=fault["raw"], content_type=fault["content_type"],
                                      headers=fault["headers"])
                if fault["status"]:
                    body = fault["body"] if fault["body"] is not None else {
                        "request_id": backend.request_id(), "code": "INJECTED", "message": "Injected fault",
                        "retryable": fault["status"] >= 500, "detail": None}
                    return self._send(fault["status"], body, headers=fault["headers"])
        if path.startswith("/media/items/"):
            item = backend.items.get(path.rsplit("/", 1)[-1].replace(".jpg", ""))
            if not item:
                return self._send(404, {"detail": "Not Found"})
            return self._send(200, raw=jpeg(item["_colour"]), content_type="image/jpeg")
        template = re.sub(r"/(?:api/(?:sessions|returns|items))/[^/]+", lambda m: m.group(0).rsplit("/", 1)[0] + "/{id}",
                          path)
        if path == "/openapi.json" and method == "GET":
            paths: Dict[str, Dict[str, Any]] = {}
            for m, p in backend.routes:
                paths.setdefault(p, {})[m.lower()] = {"summary": "fake"}
            return self._send(200, {"openapi": "3.1.0", "info": {"title": "AcceptMatch (fake)", "version": "0.1.0"},
                                    "paths": paths})
        if (method, template) not in backend.routes:
            return self._send(404 if not any(p == template for _, p in backend.routes) else 405,
                              {"detail": "Not Found" if not any(p == template for _, p in backend.routes)
                               else "Method Not Allowed"})
        try:
            payload = json.loads(raw_body.decode()) if raw_body and "json" in self.headers.get("Content-Type", "") \
                else None
        except ValueError:
            return self._send(*backend.error(400, "BAD_JSON", "Request body is not valid JSON."))
        with backend.lock:
            status, body = self._route(method, template, path, payload, raw_body)
        self._send(status, body)

    def do_GET(self):  # noqa: N802 - http.server API
        self._dispatch("GET")

    def do_POST(self):  # noqa: N802 - http.server API
        self._dispatch("POST")

    # ------------------------------------------------------------ routes

    def _route(self, method: str, template: str, path: str, payload: Any, raw_body: bytes) -> Tuple[int, Any]:
        b = self.backend
        if template == "/health":
            return 200, {"mode": "mock", "database": {"ok": True}, "agent": {"configured": True},
                         "version": b.health_version}
        if template == "/api/navigation":
            return 200, {"new": "https://www.aldoshoes.com/en-ca", "return": "/return", "buy_defective": "/shop",
                         "out_of_season": "https://www.aldoshoes.com/en-ca/collections/sale"}
        if template == "/api/demo-auth/login":
            persona, password = (payload or {}).get("persona"), (payload or {}).get("password")
            if persona not in PERSONAS or PERSONAS[persona][0] != password:
                return b.error(401, "INVALID_CREDENTIALS", "Demo sign-in failed.")
            token = "tok-" + secrets.token_hex(12)
            b.tokens[token] = persona
            return 200, {"access_token": token, "token_type": "bearer", "role": PERSONAS[persona][1],
                         "expires_in": 3600}
        if template == "/api/items" and method == "GET":
            query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            rows = [i for i in b.items.values() if i["status"] == "AVAILABLE" and i["grade"] in ("A", "B")]
            if query.get("size_system"):
                rows = [i for i in rows if i["size_system"] == query["size_system"]]
            if query.get("size_value"):
                rows = [i for i in rows if i["size_value"] == query["size_value"]]
            if query.get("max_price_cents"):
                rows = [i for i in rows if i["price_cents"] <= int(query["max_price_cents"])]
            start = int(query.get("cursor") or 0)
            page = rows[start:start + 2]
            nxt = str(start + 2) if start + 2 < len(rows) else None
            return 200, {"items": [b.public(i) for i in page], "next_cursor": nxt, "request_id": b.request_id()}
        if template == "/api/items/{id}" and method == "GET":
            item = b.items.get(path.rsplit("/", 1)[-1])
            if not item or item["status"] in ("AWAITING_RECEIPT", "INSPECTION_PENDING", "QUARANTINED"):
                return b.error(404, "ITEM_NOT_FOUND", "This item isn't listed.")
            return 200, b.public(item)
        user = self._user()
        if user is None:
            return b.error(401, "UNAUTHORIZED", "Sign in first.")
        if template == "/api/sessions":
            mode = (payload or {}).get("mode")
            if mode not in ("return", "shopping"):
                return b.error(422, "INVALID_MODE", "mode must be return or shopping")
            sid = "sess-" + secrets.token_hex(6)
            b.sessions[sid] = {"owner": user, "mode": mode}
            return 200, {"session_id": sid, "mode": mode}
        if template == "/api/sessions/{id}/messages":
            sid = path.split("/")[3]
            session = b.sessions.get(sid)
            if not session or session["owner"] != user:
                return b.error(404, "SESSION_NOT_FOUND", "Session not found.")
            rid = (payload or {}).get("client_request_id")
            if rid and (sid, rid) in b.replies:
                return b.replies[(sid, rid)]
            text = str((payload or {}).get("text") or "")
            reply = b.shopping_turn(session, sid, text) if session["mode"] == "shopping" else \
                b.return_turn(session, sid, text)
            if rid:
                b.replies[(sid, rid)] = (200, reply)
            return 200, reply
        if template == "/api/uploads":
            return self._upload(user, raw_body)
        if template == "/api/returns" and method == "POST":
            body = payload or {}
            session = b.sessions.get(body.get("session_id") or "")
            if not session or session["owner"] != user:
                return b.error(404, "SESSION_NOT_FOUND", "Session not found.")
            order = ORDERS.get(body.get("order_id") or "")
            if not order or order["owner"] != user or str(body.get("order_line")) not in order["lines"]:
                return b.error(404, "ORDER_NOT_FOUND", f"Order {str(body.get('order_id'))[:20]} was not found.")
            if body.get("claim_type") not in REQUIRED_VIEWS:
                return b.error(422, "INVALID_CLAIM", "Unknown claim type.")
            for record in b.returns.values():  # one return per order line
                if (record["order_id"], record["order_line"]) == (body["order_id"], str(body["order_line"])):
                    return 200, self._return_public(record)
            return_id = b.next_id("RET")
            record = {"return_id": return_id, "status": "INFO_COLLECTION", "order_id": body["order_id"],
                      "order_line": str(body["order_line"]), "claim_type": body["claim_type"],
                      "reason": body.get("reason"), "session_id": body["session_id"], "owner": user,
                      "missing_views": [], "analysis_source": b.analysis_source, "request_id": b.request_id(),
                      "policy_version": "aldo-ca-2026-10-15-v1"}
            b.returns[return_id] = record
            return 200, self._return_public(record)
        if template == "/api/returns/{id}/evaluate":
            record = b.returns.get(path.split("/")[3])
            if not record or record["owner"] != user:
                return b.error(404, "RETURN_NOT_FOUND", "Return not found.")
            rid = (payload or {}).get("client_request_id")
            key = (record["return_id"], "eval:" + str(rid))
            if rid and key in b.replies:
                return b.replies[key]
            b.evaluate(record)
            reply = (200, self._return_public(record))
            b.replies[key] = reply
            return reply
        if template == "/api/returns/{id}" and method == "GET":
            record = b.returns.get(path.split("/")[3])
            if not record or record["owner"] != user:
                return b.error(404, "RETURN_NOT_FOUND", "Return not found.")
            return 200, self._return_public(record)
        if template == "/api/items/{id}/acceptances":
            item = b.items.get(path.split("/")[3])
            if not item:
                return b.error(404, "ITEM_NOT_FOUND", "This item isn't listed.")
            body = payload or {}
            if body.get("listing_version") != item["listing_version"]:
                return b.error(409, "STALE_LISTING_VERSION", "The listing changed.", detail={
                    "current_listing_version": item["listing_version"]})
            ids = {d["defect_id"] for d in item["defects"]}
            accepted = set()
            for row in body.get("acceptances") or []:
                if row.get("defect_id") not in ids or row.get("accepted") is not True:
                    return b.error(422, "INVALID_ACCEPTANCE", "Each acceptance must name a disclosed defect.")
                accepted.add(row["defect_id"])
            store = b.acceptances.setdefault((user, item["item_id"], item["listing_version"]), set())
            store |= accepted
            return 200, {"item_id": item["item_id"], "listing_version": item["listing_version"],
                         "accepted_defect_ids": sorted(store), "complete": store == ids,
                         "request_id": b.request_id()}
        if template == "/api/demo-checkout":
            body = payload or {}
            key = self.headers.get("Idempotency-Key")
            if not key or key != body.get("idempotency_key"):
                return b.error(400, "IDEMPOTENCY_KEY_MISMATCH", "Idempotency-Key header and body must match.")
            if (user, key) in b.purchases:
                return 200, b.purchases[(user, key)]
            item = b.items.get(body.get("item_id") or "")
            if not item:
                return b.error(404, "ITEM_NOT_FOUND", "This item isn't listed.")
            if item["status"] != "AVAILABLE" or item["grade"] not in ("A", "B"):
                return b.error(409, "ITEM_UNAVAILABLE", "This item is no longer available.")
            if body.get("listing_version") != item["listing_version"]:
                return b.error(409, "STALE_LISTING_VERSION", "The listing changed.")
            needed = {d["defect_id"] for d in item["defects"]}
            if not needed <= b.acceptances.get((user, item["item_id"], item["listing_version"]), set()):
                return b.error(409, "INCOMPLETE_ACCEPTANCES", "Every defect must be accepted first.")
            item["status"] = "SOLD"
            purchase = {"purchase_id": b.next_id("PUR"), "item_id": item["item_id"],
                        "listing_version": item["listing_version"], "amount_cents": item["price_cents"],
                        "currency": "CAD", "status": "CONFIRMED_DEMO", "payment": "SIMULATED",
                        "created_at": "2026-10-16T10:00:00Z", "request_id": b.request_id()}
            b.purchases[(user, key)] = purchase
            return 200, purchase
        return 404, {"detail": "Not Found"}

    def _return_public(self, record: Dict[str, Any]) -> Dict[str, Any]:
        return {k: copy.deepcopy(v) for k, v in record.items() if k not in ("owner", "session_id")}

    def _upload(self, user: str, raw_body: bytes) -> Tuple[int, Any]:
        b = self.backend
        content_type = self.headers.get("Content-Type", "")
        if not content_type.startswith("multipart/form-data"):
            return b.error(400, "NOT_MULTIPART", "Use multipart/form-data.")
        if len(raw_body) > 10 * 1024 * 1024:
            return b.error(413, "PAYLOAD_TOO_LARGE", "Images must be 10 MB or smaller.")
        message = BytesParser(policy=email_policy).parsebytes(
            b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + raw_body)
        fields: Dict[str, str] = {}
        file_part = None
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            if part.get_filename() is not None:
                file_part = part
            else:
                fields[name] = part.get_content().strip()
        session = b.sessions.get(fields.get("session_id") or "")
        if not session or session["owner"] != user:
            return b.error(404, "SESSION_NOT_FOUND", "Session not found.")
        if fields.get("declared_view") not in SHOE_VIEWS or not fields.get("purpose"):
            return b.error(422, "INVALID_UPLOAD", "declared_view and purpose are required.")
        if file_part is None or file_part.get_content_type() not in ("image/jpeg", "image/png", "image/webp"):
            return b.error(422, "INVALID_IMAGE", "Upload a JPEG, PNG or WebP image.")
        data = file_part.get_payload(decode=True) or b""
        try:
            with Image.open(io.BytesIO(data)) as probe:
                width, height = probe.size
        except Exception:  # noqa: BLE001 - fake backend
            return b.error(422, "INVALID_IMAGE", "That file isn't a readable image.")
        photo_id = b.next_id("P")
        b.photos[photo_id] = {"photo_id": photo_id, "owner": user, "session_id": fields["session_id"],
                              "view": fields["declared_view"], "width": width, "height": height,
                              "purpose": fields["purpose"], "bytes": len(data)}
        return 200, {"photo_id": photo_id, "declared_view": fields["declared_view"], "request_id": b.request_id()}


class QuietServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):  # clients that time out on purpose aren't errors here
        pass


def start(backend: Optional[FakeBackend] = None, port: int = 0):
    backend = backend or FakeBackend()
    handler = type("BoundHandler", (Handler,), {"backend": backend})
    server = QuietServer(("127.0.0.1", port), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, backend, f"http://127.0.0.1:{server.server_address[1]}"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Fake AcceptMatch API for local UI work (not the backend).")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--real-ai-label", action="store_true",
                        help="Report a non-simulated analysis_source (label testing only; no model is called).")
    args = parser.parse_args()
    fake = FakeBackend(analysis_source="BEDROCK" if args.real_ai_label else "SIMULATED",
                       model_id="us.amazon.nova-2-lite-v1:0" if args.real_ai_label else None)
    srv, _, url = start(fake, args.port)
    print(f"Fake AcceptMatch API (NOT the backend) on {url}. Personas: cust-01/pw-cust-01, cust-02/pw-cust-02.")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        srv.shutdown()
