"""AcceptMatch FastAPI application (plan section 6). Start with the app factory:

    uvicorn backend.main:create_app --factory --port 8000

Customer routes need a customer bearer token; /api/admin/* needs the admin
role. Item listings, item details and their photos are public projections.
"""

from __future__ import annotations

import logging
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import List, Optional

from fastapi import Depends, FastAPI, File, Form, Header, Path, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from . import auth, errors
from .agent.invoker import AgentInvoker
from .config import Settings, load_settings
from .errors import ApiError, request_id
from .repository import DatabaseUnavailable, Repository
from .schemas import (AcceptanceRequest, AcceptanceResponse, AdminItem, ChatResponse, CheckoutRequest, ErrorBody,
                      EvaluateRequest, HealthResponse, InspectRequest, ItemPublic, ItemsPage, LoginRequest,
                      LoginResponse, MessageCreate, PurchaseResponse, ReceiveRequest, ReturnCreate, ReturnResponse,
                      ReviewDecision, ReviewDecisionResponse, ReviewsPage, SessionCreate, ShoeView, UploadResponse)
from .services.admin import AdminService
from .services.inventory import InventoryService
from .services.returns import ReturnService
from .services.sessions import SessionService
from .storage import LocalImageStore, StorageError

LOG = logging.getLogger("acceptmatch.backend")
ID = r"^[A-Za-z0-9-]{1,80}$"
ERRORS = {code: {"model": ErrorBody} for code in (400, 401, 403, 404, 409, 413, 422, 429, 502, 503, 504)}


def _log_to_console() -> None:
    """acceptmatch.* INFO lines (requests with request IDs, ai_call evidence) on the API's console, once."""
    root = logging.getLogger("acceptmatch")
    if not root.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S"))
        root.addHandler(handler)
        root.setLevel(logging.INFO)


def create_app(settings: Optional[Settings] = None, repo: Optional[Repository] = None, store=None,
               invoker: Optional[AgentInvoker] = None) -> FastAPI:
    _log_to_console()
    settings = settings or load_settings()
    repo = repo or Repository.from_settings(settings)
    store = store or LocalImageStore(settings.image_root)
    invoker = invoker or AgentInvoker(settings)
    inventory = InventoryService(repo, settings)
    sessions = SessionService(repo, invoker, inventory)
    returns = ReturnService(repo, store, invoker, sessions, settings)
    admin = AdminService(repo, store, returns, settings)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        try:
            if settings.local and repo.ensure_table():
                LOG.info("created table %s", settings.table)
        except DatabaseUnavailable:
            LOG.warning("DynamoDB isn't reachable at startup; /health reports 503 until it is")
        yield

    app = FastAPI(title="AcceptMatch API", version=settings.version, lifespan=lifespan,
                  description="Backend for the AcceptMatch demo. Synthetic data; demo decisions are never ALDO "
                              "decisions; payments, shipping and refunds are simulated.")
    app.state.c = SimpleNamespace(settings=settings, repo=repo, store=store, invoker=invoker, inventory=inventory,
                                  sessions=sessions, returns=returns, admin=admin)
    errors.install(app)

    @app.exception_handler(DatabaseUnavailable)
    async def _db_down(request: Request, exc: DatabaseUnavailable):
        return JSONResponse(errors.body(request_id(request), "DB_UNAVAILABLE", "The database isn't reachable right now.",
                                        True), status_code=503)

    @app.exception_handler(StorageError)
    async def _storage(request: Request, exc: StorageError):
        return JSONResponse(errors.body(request_id(request), "STORAGE_ERROR", "The image store rejected the request.",
                                        False), status_code=400)

    if settings.frontend_origins:
        app.add_middleware(CORSMiddleware, allow_origins=list(settings.frontend_origins), allow_credentials=False,
                           allow_methods=["GET", "POST"], allow_headers=["Authorization", "Content-Type",
                                                                         "Idempotency-Key", "X-Request-ID"])

    @app.middleware("http")
    async def _request_log(request: Request, call_next):
        request.state.request_id = errors.new_request_id()
        errors.CURRENT_REQUEST.set(request.state.request_id)  # ties ai_call log lines to the request
        started = time.perf_counter()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        LOG.info("%s %s -> %s %.0fms %s", request.method, request.url.path, response.status_code,
                 (time.perf_counter() - started) * 1000, request.state.request_id)
        return response

    # ------------------------------------------------------------ health, navigation, auth

    @app.get("/health", response_model=HealthResponse, tags=["system"])
    def health(request: Request):
        ok = repo.ping()
        body = {"mode": settings.app_mode, "database": {"ok": ok, "table": settings.table},
                "agent": {"configured": True, "provider": invoker.provider, "model_id": invoker.model_id,
                          "vision_model_id": invoker.vision_model_id},
                "version": settings.version, "policy_version": settings.policy_version}
        return body if ok else JSONResponse(body, status_code=503)

    @app.get("/api/navigation", tags=["system"])
    def navigation():
        return dict(settings.navigation)

    @app.post("/api/demo-auth/login", response_model=LoginResponse, responses=ERRORS, tags=["auth"])
    def login(body: LoginRequest):
        token, ttl, role = auth.login(settings, body.persona, body.password)
        return {"access_token": token, "token_type": "bearer", "role": role, "persona": body.persona,
                "expires_in": ttl}

    # ------------------------------------------------------------ sessions

    @app.post("/api/sessions", response_model=ChatResponse, responses=ERRORS, tags=["sessions"])
    def create_session(body: SessionCreate, request: Request, user: auth.Principal = Depends(auth.customer)):
        return sessions.create(user.sub, body.mode, request_id(request))

    @app.post("/api/sessions/{session_id}/messages", response_model=ChatResponse, responses=ERRORS,
              tags=["sessions"])
    def post_message(body: MessageCreate, request: Request, session_id: str = Path(pattern=ID),
                     user: auth.Principal = Depends(auth.customer)):
        return sessions.post(user.sub, session_id, body.text, body.photo_ids, body.client_request_id,
                             request_id(request))

    # ------------------------------------------------------------ uploads and returns

    @app.post("/api/uploads", response_model=UploadResponse, responses=ERRORS, tags=["returns"])
    async def upload(request: Request, session_id: str = Form(pattern=ID), declared_view: ShoeView = Form(),
                     purpose: str = Form(max_length=40), file: UploadFile = File(),
                     user: auth.Principal = Depends(auth.customer)):
        length = int(request.headers.get("content-length") or 0)
        if length > settings.max_image_bytes + 1024 * 1024:
            raise ApiError(413, "PAYLOAD_TOO_LARGE", "Images must be 10 MB or smaller.")
        data = await file.read(settings.max_image_bytes + 1)
        return returns.upload(user.sub, session_id, declared_view, purpose, data, file.content_type or "",
                              request_id(request))

    @app.post("/api/returns", response_model=ReturnResponse, responses=ERRORS, tags=["returns"])
    def create_return(body: ReturnCreate, request: Request, user: auth.Principal = Depends(auth.customer),
                      idempotency_key: Optional[str] = Header(default=None, max_length=100)):
        return returns.create(user.sub, body.model_dump(), idempotency_key, request_id(request))

    @app.post("/api/returns/{return_id}/evaluate", response_model=ReturnResponse, responses=ERRORS,
              tags=["returns"])
    def evaluate_return(body: EvaluateRequest, request: Request, return_id: str = Path(pattern=ID),
                        user: auth.Principal = Depends(auth.customer)):
        return returns.evaluate(user.sub, return_id, body.client_request_id, request_id(request))

    @app.get("/api/returns/{return_id}", response_model=ReturnResponse, responses=ERRORS, tags=["returns"])
    def get_return(request: Request, return_id: str = Path(pattern=ID), user: auth.Principal = Depends(auth.customer)):
        return returns.view(returns.get_owned(user.sub, return_id), request_id(request))

    # ------------------------------------------------------------ inventory, acceptances, checkout

    @app.get("/api/items", response_model=ItemsPage, responses=ERRORS, tags=["inventory"])
    def list_items(request: Request, category: Optional[str] = Query(default=None, max_length=20),
                   size_system: Optional[str] = Query(default=None, pattern=r"^(US_WOMEN|US_MEN|EU)$"),
                   size_value: Optional[str] = Query(default=None, pattern=r"^\d{1,2}(\.5)?$"),
                   color: Optional[str] = Query(default=None, max_length=20),
                   min_price_cents: Optional[int] = Query(default=None, ge=0),
                   max_price_cents: Optional[int] = Query(default=None, ge=0),
                   defect_type: Optional[str] = Query(default=None, max_length=30),
                   max_grade: Optional[str] = Query(default=None, pattern=r"^[AB]$"),
                   cursor: Optional[str] = Query(default=None, max_length=40),
                   limit: int = Query(default=12, ge=1, le=50)):
        filters = {"category": category, "size_system": size_system, "size_value": size_value, "color": color,
                   "min_price_cents": min_price_cents, "max_price_cents": max_price_cents,
                   "defect_type": defect_type, "max_grade": max_grade}
        items, nxt = inventory.search(filters, cursor, limit)
        return {"items": items, "next_cursor": nxt, "request_id": request_id(request)}

    @app.get("/api/items/{item_id}", response_model=ItemPublic, responses=ERRORS, tags=["inventory"])
    def get_item(item_id: str = Path(pattern=ID)):
        return inventory.get_public(item_id)

    @app.get("/api/items/{item_id}/photos/{photo_id}", tags=["inventory"],
             responses={200: {"content": {"image/jpeg": {}}}, 404: {"model": ErrorBody}})
    def item_photo(item_id: str = Path(pattern=ID), photo_id: str = Path(pattern=ID)):
        row = inventory.get_row(item_id)
        photo = next((p for p in row.get("photos") or [] if p["photo_id"] == photo_id), None)
        if row.get("status") not in ("AVAILABLE", "SOLD") or not photo:
            raise errors.not_found("PHOTO_NOT_FOUND", "That photo isn't available.")
        data = store.get(photo["storage_key"])
        if not data:
            raise errors.not_found("PHOTO_NOT_FOUND", "That photo isn't available.")
        return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=300"})

    @app.post("/api/items/{item_id}/acceptances", response_model=AcceptanceResponse, responses=ERRORS,
              tags=["inventory"])
    def accept_defects(body: AcceptanceRequest, request: Request, item_id: str = Path(pattern=ID),
                       user: auth.Principal = Depends(auth.customer)):
        return inventory.accept(user.sub, item_id, body.listing_version, [a.model_dump() for a in body.acceptances],
                                request_id(request))

    @app.post("/api/demo-checkout", response_model=PurchaseResponse, responses=ERRORS, tags=["inventory"])
    def demo_checkout(body: CheckoutRequest, request: Request, user: auth.Principal = Depends(auth.customer),
                      idempotency_key: Optional[str] = Header(default=None, max_length=100)):
        return inventory.checkout(user.sub, body.item_id, body.listing_version, body.idempotency_key,
                                  idempotency_key, request_id(request))

    # ------------------------------------------------------------ admin (protected)

    @app.get("/api/admin/reviews", response_model=ReviewsPage, responses=ERRORS, tags=["admin"])
    def list_reviews(request: Request, stage: Optional[str] = Query(default=None, pattern=r"^(CLAIM|RECEIPT)$"),
                     status: Optional[str] = Query(default=None, pattern=r"^(OPEN|DECIDED)$"),
                     cursor: Optional[str] = Query(default=None, max_length=40),
                     _admin: auth.Principal = Depends(auth.admin)):
        rows, nxt = admin.reviews(stage, status, cursor)
        return {"reviews": rows, "next_cursor": nxt, "request_id": request_id(request)}

    @app.post("/api/admin/reviews/{review_id}/decision", response_model=ReviewDecisionResponse, responses=ERRORS,
              tags=["admin"])
    def decide_review(body: ReviewDecision, request: Request, review_id: str = Path(pattern=ID),
                      user: auth.Principal = Depends(auth.admin)):
        return {**admin.decide(user.sub, review_id, body.model_dump()), "request_id": request_id(request)}

    @app.get("/api/admin/items", response_model=List[AdminItem], responses=ERRORS, tags=["admin"])
    def admin_items(request: Request, status: Optional[str] = Query(default=None, max_length=30),
                    _admin: auth.Principal = Depends(auth.admin)):
        return [{**row, "request_id": request_id(request)} for row in admin.items(status)]

    @app.post("/api/admin/items/{item_id}/receive", response_model=AdminItem, responses=ERRORS, tags=["admin"])
    def receive_item(body: ReceiveRequest, request: Request, item_id: str = Path(pattern=ID),
                     user: auth.Principal = Depends(auth.admin)):
        return {**admin.receive(user.sub, item_id), "request_id": request_id(request)}

    @app.post("/api/admin/items/{item_id}/inspect", response_model=AdminItem, responses=ERRORS, tags=["admin"])
    def inspect_item(body: InspectRequest, request: Request, item_id: str = Path(pattern=ID),
                     user: auth.Principal = Depends(auth.admin)):
        return {**admin.inspect(user.sub, item_id, body.model_dump()), "request_id": request_id(request)}

    return app
