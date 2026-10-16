# AcceptMatch V3: frontend ↔ FastAPI integration

Branch `feature/customer-v3-agent-integration`. This page covers how the Streamlit customer site (Return and Buy Defective) talks to the AcceptMatch FastAPI backend described in `AcceptMatch_MVP_Implementation_Plan.md`, section 6.

## Status (2026-10-16)

| Area | Status |
|---|---|
| HTTP client, response checks, mapping, api-mode screens, config, diagnostics | IMPLEMENTED |
| Same features against `tests/ui/fake_api.py` (a fake server that follows the plan) | TESTED WITH MOCK HTTP: 99 new tests; all 180 tests pass; checked at 1440 px and 390 px |
| Against the real backend | NOT TESTED. Blocked: no FastAPI backend exists on any branch yet |
| Real AI (Bedrock / AgentCore) | NOT VERIFIED. Blocked on the backend and AWS access |
| Admin / factory dashboard | Not built, on purpose. Admin endpoints stay backend-only, and admin logins are refused |
| Bags in api mode, deleting photos, streaming (SSE) | Not implemented. Bags stay in the mock; the plan has no delete or SSE endpoint |

## Architecture and the two "mode" settings

```text
Browser ── Streamlit server (this repo)
             ├─ ACCEPTMATCH_AGENT=mock → scripted in-process mock (bags, unchanged)
             └─ ACCEPTMATCH_AGENT=api  → HTTPS → FastAPI /api/* → AgentInvoker → Strands / Bedrock / AgentCore
                                                              └→ DynamoDB, S3 (backend only)
```

- `ACCEPTMATCH_AGENT` is the frontend client. `APP_MODE` (mock | bedrock-local | agentcore) is the backend's own setting, reported by `GET /health` and shown in diagnostics. These are two different layers: `ACCEPTMATCH_AGENT=api` combined with `APP_MODE=mock` is still simulated AI.
- Streamlit never calls AWS, and boto3 has been removed from `requirements.txt`. All calls run server-side (Streamlit server → FastAPI). The browser only loads images from hosts listed in `ACCEPTMATCH_IMAGE_HOSTS`, so the frontend doesn't need CORS.
- There is no silent fallback. An api journey with a missing or invalid URL, a backend that's down, or an invalid payload shows an error, never the mock.
- Code:
  - Transport: `ui/client/http_api.py`
  - Contract checks: `ui/client/api_schemas.py`
  - Mapping: `ui/client/api_mapper.py`
  - Operations: `ui/client/api_agent.py`
  - Actions: `ui/api_actions.py`
  - Screens: `ui/screens/api_shop.py`, `ui/screens/api_returns.py`. The existing screens hand off to these when their journey is in api mode.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `ACCEPTMATCH_AGENT` | `mock` | `mock` or `api`. Set one journey with `ACCEPTMATCH_AGENT_SHOP` / `ACCEPTMATCH_AGENT_RETURN`. `agentcore`/`local` now raise a config error (superseded) |
| `ACCEPTMATCH_API_BASE_URL` | none | Required for api mode. https only; plain http is allowed only for localhost/127.0.0.1. No credentials, query or fragment |
| `ACCEPTMATCH_API_TIMEOUT` | `30` | Seconds, for normal calls |
| `ACCEPTMATCH_API_VISION_TIMEOUT` | `60` | Return-mode messages and `/evaluate` |
| `ACCEPTMATCH_API_HEALTH_TIMEOUT` | `5` | `/health` and `/openapi.json` |
| `ACCEPTMATCH_API_MAX_RETRIES` | `1` | 0–3. Only for safe GETs and POSTs that carry an Idempotency-Key, and never after a timeout |
| `ACCEPTMATCH_DEMO_PERSONA[_SHOP\|_RETURN]` | none | Demo persona used to sign in (plan demo: `cust-02` shops, `cust-01` returns) |
| `ACCEPTMATCH_DEMO_PASSWORD[_SHOP\|_RETURN]` | none | Secret. Never logged or shown, and excluded from `repr` |
| `ACCEPTMATCH_API_TOKEN` | none | Secret. A pre-issued bearer token used instead of signing in (no automatic re-login) |
| `ACCEPTMATCH_IMAGE_HOSTS` | none | Comma-separated https hosts whose image URLs the browser may load directly (for example the S3 presign host) |
| `ACCEPTMATCH_DIAGNOSTICS` | off | Operator panel: both mode layers, analysis source, model id, request ids, HTTP call log |

## Running it

```bash
cd accept_match-customer-v3
.venv/bin/streamlit run app.py                                   # mock (default)

# UI work without a backend: the fake server (NOT the backend; scripted chat, size-based "vision")
.venv/bin/python tests/ui/fake_api.py --port 8765
ACCEPTMATCH_AGENT=api ACCEPTMATCH_API_BASE_URL=http://127.0.0.1:8765 \
ACCEPTMATCH_DEMO_PERSONA_SHOP=cust-02 ACCEPTMATCH_DEMO_PASSWORD_SHOP=pw-cust-02 \
ACCEPTMATCH_DEMO_PERSONA_RETURN=cust-01 ACCEPTMATCH_DEMO_PASSWORD_RETURN=pw-cust-01 \
.venv/bin/streamlit run app.py

# Real backend on this machine: same, with ACCEPTMATCH_API_BASE_URL=http://localhost:8000 and the team's credentials
```

The fake server's demo order IDs:
- `ORD-001`: approved after all photos arrive.
- `ORD-002`: final-sale defect, goes to human review.
- `ORD-009`: rejected after review.

A photo narrower than 300 px counts as "blurry", which triggers NEEDS_MORE_EVIDENCE.

## Streamlit Cloud (not changed; apply only when the backend URL exists)

Streamlit turns root-level secrets into environment variables. In *App settings → Secrets*:

```toml
ACCEPTMATCH_AGENT = "api"
ACCEPTMATCH_API_BASE_URL = "https://<backend-host>"
ACCEPTMATCH_DEMO_PERSONA_SHOP = "cust-02"
ACCEPTMATCH_DEMO_PASSWORD_SHOP = "<from the backend team>"
ACCEPTMATCH_DEMO_PERSONA_RETURN = "cust-01"
ACCEPTMATCH_DEMO_PASSWORD_RETURN = "<from the backend team>"
```

- The backend must be reachable from the public internet over HTTPS. Streamlit Cloud can't reach a laptop's localhost.
- Every visitor signs in as the same demo personas, so all visitors share those personas' returns and purchases. That's acceptable for the demo, but it isn't user isolation.

## Authentication

- **Sign-in:** the site signs in lazily with `POST /api/demo-auth/login {persona, password}` before the first protected call. It keeps the token in memory for that browser session and sends it as `Authorization: Bearer …`.
- **Token refresh:** it signs in again 30 s before `expires_in`, and once after a 401.
- **Refused logins:** a role other than `customer` is refused, so an admin token can never drive the customer site.
- **Public calls:** `GET /api/items`, `GET /api/items/{id}` and `/health` go out without a token when none is configured. The backend decides what is public.
- **Not exposed:** tokens never go in query strings, logs, diagnostics or the page. Redirects are refused, so the token can't leave the API host.
- **Images:** server-side image fetches attach the token only on the API's own origin.

## Endpoints used

Every call sends `X-Request-ID` (= `client_request_id`).

| Endpoint | When | Timeout | Automatic retry | Idempotency |
|---|---|---|---|---|
| `GET /health` | First call of each journey per browser session. A 503, or a `version` whose major isn't 0 or 1, stops the call | 5 s | once | — |
| `GET /openapi.json` | Listing page. A missing acceptance/checkout path shows "Buy · pending integration" | 5 s | once | — |
| `POST /api/demo-auth/login` | Lazily, and once after a 401 | 30 s | no | — |
| `POST /api/sessions` `{mode}` | First message of a journey (`shopping` / `return`) | 30 s | no | — |
| `POST /api/sessions/{id}/messages` `{text, photo_ids: [], client_request_id}` | Every customer message or option tap. Try again resends the same id | 30 s shop / 60 s return | 503 or connection only | `Idempotency-Key` = `client_request_id` |
| `POST /api/uploads` multipart `session_id, file, declared_view, purpose` | Each photo (cleaned locally: EXIF removed, re-encoded as JPEG ≤ 2048 px) | 30 s | no | — |
| `POST /api/returns` `{session_id, order_id, order_line, claim_type, reason}` | Submit, once per return | 30 s | no | `Idempotency-Key`, the same on every retry |
| `POST /api/returns/{id}/evaluate` `{client_request_id}` | After creation, after more photos, Try again | 60 s | 503 or connection only | `Idempotency-Key`, stable per round |
| `GET /api/returns/{id}` | Status page, Check again | 30 s | once | — |
| `GET /api/items` (plan filters + `cursor`) | Browse all pieces, Show more | 30 s | once | — |
| `GET /api/items/{id}` | View details, and a reload after a 409 or a purchase | 30 s | once | — |
| `POST /api/items/{id}/acceptances` `{listing_version, acceptances: [{defect_id, accepted: true}]}` | "Confirm these imperfections", only after the customer ticks every defect | 30 s | 503 or connection only | `Idempotency-Key` |
| `POST /api/demo-checkout` `{item_id, listing_version, idempotency_key}` | Buy | 30 s | 503 or connection only | Header and body share one key per item + listing version, reused on every retry |

Not used:
- `GET /api/navigation`: the home page links come from `NEW_URL` and `PAST_SEASON_URL`, so the home page works even when the backend is down.
- The `/api/admin/*` endpoints.

## Response shapes the site relies on

Checks fail closed. An invalid ChatResponse, return or purchase is rejected as a whole. An invalid item is dropped. Both are logged as contract violations and shown as "We couldn't read the service's response."

- **ChatResponse:**
  - All 13 plan keys are required.
  - `next_action` must be in the plan's enum.
  - `UPLOAD_PHOTOS` needs a non-empty `required_photos` (`[{view, reason}]`).
  - `return_status` is in the enum or null.
  - Optional extras the site uses: `model_id` and `blocking_constraints` (for no-match cases).
- **Question:** either a string, or `{question_id|id, text|question|prompt, options|choices: [string | {value, label}], type: "multi_choice"?, help?}`. At most 3 questions per turn are shown.
- **ItemPublic:**
  - Required: `item_id`, `title`, `category`, `price_cents` (int, shown as CA$), `currency == "CAD"`, `grade` (A/B/C/UNKNOWN), `defects[]`, `status` (inventory lifecycle), `listing_version` (int ≥ 1).
  - `SHOES` also needs `size_system` (US_WOMEN/US_MEN/EU) and `size_value`.
  - Each defect needs a unique `defect_id`, a `type` from the plan's 8 types, and a `severity` of A/B/C/UNKNOWN.
  - Optional: `photos` (a URL string, or `{photo_id, url, alt, view}`), `match_status`, `match_reasons`, `pending_confirmations` and `shoe_type`.
- **Lists:**
  - `GET /api/items` returns `{items, next_cursor}` (a bare list is accepted).
  - `GET /api/items/{id}` returns the item, or `{item: {...}}`.
- **Return:**
  - Required: `return_id` (or `id`) and a `status` from the enum.
  - `NEEDS_MORE_EVIDENCE` needs `missing_views` (view names or `{view, reason}`).
  - Optional: `retake_reason`, `next_step`, `demo_address` (a string with newlines, a list, or `{lines}`), `inventory_status`, `analysis_source`, `model_id`, `rule_decision {code, final_merchant_decision}` and `request_id`.
- **Other responses:**
  - Upload: `{photo_id}`.
  - Login: `{access_token|token, role, expires_in?}`.
  - Acceptances: an object, with `listing_version` (if present) equal to the request's.
  - Checkout: `{purchase_id, item_id, amount_cents?, currency: CAD, …}`.
- **Errors:** `{request_id, code, message, retryable, detail}`. FastAPI's `{"detail": {...}}` wrapper is also accepted.

**Guardrails on the frontend side.** None of these change backend data.
- Grade C/UNKNOWN items, and anything not `AVAILABLE`, can never be bought.
- A `MATCH` that still has `pending_confirmations` is shown as "Needs your confirmation".
- Checkboxes start unticked and are keyed by listing version. What the customer said in chat never counts as accepting a specific defect.
- An approved return shows "This is not an actual ALDO refund approval", and real AI is never claimed (see below).

## Error handling

| Backend | What the customer sees |
|---|---|
| 400 / 422 with an error body | Our message. The backend's message is shown only if it's short and non-technical |
| 422 FastAPI validation list | "Couldn't read…". Diagnostics keep only the field locations, never input values |
| 401 | One silent re-login, then "The demo sign-in isn't working right now" |
| 403 | "This demo account isn't allowed to do that" |
| 404 with an error body | "We couldn't find that" / the backend message |
| 404/405 `{"detail":"Not Found"}` | "Pending integration" (route missing) |
| 409 `STALE_LISTING_VERSION` | Listing reloads, boxes untick: "details changed… confirm again" |
| 409 `INCOMPLETE_ACCEPTANCES` | "Please confirm each imperfection" |
| 409 `ITEM_UNAVAILABLE` | Listing reloads (shows Sold), Buy disappears |
| 413 | Photo rejected (too large) |
| 429 | "Busy". Automatic retry only if `Retry-After` ≤ 2 s |
| 502 `AI_OUTPUT_INVALID` | "…a result we couldn't use, so nothing was decided" + Try again |
| 503 `AI_UNAVAILABLE` / database down | "…unavailable right now" + Try again. Technical text such as ARNs or Bedrock errors is never shown |
| 504 / client timeout | "…took too long" + Try again, reusing the same keys. For checkout: "Trying again is safe…" |
| 3xx | Refused; the redirect is never followed |
| Unreachable | "…isn't reachable right now" + Try again |

## Labels: real AI is never assumed

The source pill uses the latest successful backend answer:

| Backend reports | Pill |
|---|---|
| Nothing yet | "AcceptMatch service" |
| `SIMULATED` | "Live service · simulated AI" |
| Another source with a `model_id` | "AI-assisted · <model>" |
| Another source without a `model_id` | "Live service · AI not confirmed" |

Return outcomes add a line:
- "Photo analysis in this environment is simulated…", or
- "Photos were analysed by <model>. Photo analysis is an observation…"

## Assumptions the Agent team should confirm (each is a one-line change in `api_mapper.py` or `http_api.py`)

1. **Login:** the body is `{persona, password}`. The response has `access_token` (or `token`), `role` and optional `expires_in`.
2. **Session creation:** `POST /api/sessions` returns `{session_id}`. If it returns a full ChatResponse, the site shows it as the greeting; otherwise the site shows its own opening prompt.
3. **Answers:** tapping an option sends the option label as `text`, because the message body has no structured-answer field.
4. **Photos and returns:**
   - Photos are linked to the return through `session_id`, because `POST /api/returns` has no `photo_ids`.
   - There's no delete endpoint, so a replaced photo stays in the session. The newest photo per view should count.
5. **Upload `purpose`:** the value is `RETURN_EVIDENCE`.
6. **Return conversation:** the site's first message carries the demo order ID, line, claim type, reason and description. The backend then asks its questions until it returns `UPLOAD_PHOTOS`.
7. **AI labels:** please return `model_id` and `request_id` on ChatResponse and return records whenever a real model runs. Without them the site won't label the result as AI.
8. **Item photos:** `photos[].url` is either relative to the API (fetched server-side with the token) or https on a host listed in `ACCEPTMATCH_IMAGE_HOSTS`. Photos without a URL show "Photo coming soon".
9. **No match:** `blocking_constraints` is an optional ChatResponse key, sent with an empty `items`.
10. **Versions:** `/health.version` is semver with major version 0 or 1.

## Tests

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ui -q -p no:cacheprovider
```

- `test_api_contract.py`:
  - Plan-shaped fixtures in `tests/ui/fixtures/api/`, written by the frontend; no backend fixtures exist yet.
  - Malformed payloads, the error mapping, request bodies, labels, statuses, config validation, secret redaction and Markdown-safe labels.
- `test_api_client.py`, against the fake server:
  - Sign-in and re-login, admin refusal, health failure, version refusal, unreachable host.
  - Timeouts are not retried; GETs retry; uploads never retry; checkout retries keep the same key and produce one purchase.
  - Malformed and non-JSON bodies, refused redirects, pending integration, same-origin images, no secrets in logs.
- `test_api_flows.py`, through the real app (AppTest) against the fake server:
  - Home makes no backend calls; config errors are visible; mock and api can run side by side.
  - Backend questions are rendered and answers sent; items come from the API in CAD with footwear fields.
  - Honest no-match; no defect is pre-accepted; buy once.
  - Stale version, sold elsewhere, and checkout timeout with retry each still end with a single purchase.
  - Return: questions → requested views → photo IDs → NEEDS_MORE_EVIDENCE → approval with the disclaimer → status page.
  - No duplicate cases; human review and rejection; 503/504 during review; unknown order; diagnostics without secrets.
- All 81 earlier mock tests still pass.

## What the Agent/backend team needs to provide

1. The HTTPS URL of the deployed FastAPI service, or a local run on `:8000` for joint testing.
2. `contracts/openapi.yaml` or the live `/openapi.json`, plus `fixtures/api/*.json`, to replace the frontend-written fixtures.
3. Demo customer credentials for `cust-02` (shop) and `cust-01` (return), delivered through Streamlit secrets, never the repo.
4. Answers to the assumptions above, especially 3, 4 and 7.
5. Before anyone says "real AI": a run in `bedrock-local` or `agentcore` mode that returns `model_id` and request ids, using the plan's 7-view photo set.
