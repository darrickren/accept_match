# Streamlit and FastAPI integration

The Streamlit site has a typed provider boundary. `ACCEPTMATCH_AGENT=mock` uses the in-process scripted provider.
`ACCEPTMATCH_AGENT=api` uses `ui/client/http_api.py` and `ui/client/api_agent.py` to call the FastAPI service.

The site never calls Gemini, Bedrock, AgentCore, DynamoDB, or an object store directly. Provider credentials belong
only to the backend process.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `ACCEPTMATCH_AGENT` | `mock` | Shared frontend provider, either `mock` or `api`. |
| `ACCEPTMATCH_AGENT_SHOP` | shared value | Optional shopping override. |
| `ACCEPTMATCH_AGENT_RETURN` | shared value | Optional return override. |
| `ACCEPTMATCH_API_BASE_URL` | none | Required for `api`; HTTPS except for localhost. |
| `ACCEPTMATCH_API_TIMEOUT` | 30 seconds | Chat and ordinary API timeout. |
| `ACCEPTMATCH_API_VISION_TIMEOUT` | 60 seconds | Return evaluation timeout. |
| `ACCEPTMATCH_API_MAX_RETRIES` | 1 | Bounded retries for safe operations. |
| `ACCEPTMATCH_DEMO_PERSONA_*` | none | Demo persona used for one journey. |
| `ACCEPTMATCH_DEMO_PASSWORD_*` | none | Demo-only password, excluded from settings representations and diagnostics. |
| `ACCEPTMATCH_API_TOKEN` | none | Optional pre-issued token, also redacted. |
| `ACCEPTMATCH_IMAGE_HOSTS` | none | Additional HTTPS hosts allowed for server-side image fetching. |

The local helper `python -m backend.scripts.dev frontend` supplies the localhost URL and generated demo passwords.
The public default remains `mock` even when the backend code is installed.

## Contract behavior

- The client checks `/health` before protected work and refuses incompatible major versions.
- Demo sign-in is lazy. Tokens stay in the Streamlit session and are never placed in query strings or diagnostics.
- Redirects are refused so authorization headers cannot move to another origin.
- Response envelopes are validated before screen state changes.
- GET requests may retry within the configured limit. Uploads and model turns are not blindly retried.
- Idempotency keys stay stable when checkout or return creation is retried.
- Image URLs must be relative to the API origin or HTTPS on an explicit allowed host.
- Backend errors are mapped to customer-safe messages without exposing stack traces, ARNs, table names, or SDK text.

## Journey mapping

The shopping journey creates a session, sends messages with a context version and client request ID, renders only
validated exact-item results, records acceptance of each disclosed defect, and performs a simulated checkout.

The return journey creates a separate session, uploads declared evidence views, creates one idempotent case, asks
the backend to evaluate it, and supports additional evidence or status lookup. Photo IDs remain bound to the
session that created them.

The frontend independently applies customer-visible guardrails. It will not present an item as verified when the
API payload lacks the evidence needed to reproduce that claim.

## Offline coverage

- Contract tests validate schemas, error mapping, redaction, labels, and configuration.
- A local fake API exercises sign-in, retries, redirects, malformed responses, idempotency, image origins, and
  failure states.
- Streamlit `AppTest` coverage drives complete shopping and return flows against that fake API.
- End-to-end tests run the Streamlit site against the real FastAPI service in `APP_MODE=mock` and DynamoDB Local.

None of these tests contacts a model provider.
