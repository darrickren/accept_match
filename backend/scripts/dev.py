"""Local development commands. Everything here talks to localhost only.

    python -m backend.scripts.dev secrets      create .local/dev-secrets.env (random, gitignored, mode 600)
    python -m backend.scripts.dev gemini-key   store GEMINI_API_KEY there (hidden prompt; never printed)
    python -m backend.scripts.dev ddb          run DynamoDB Local on :8001 with data in .local/ddb-data
    python -m backend.scripts.dev seed         create the table and load demo-v1 (idempotent)
    python -m backend.scripts.dev reset --confirm-reset-demo-only [--include-runtime]
    python -m backend.scripts.dev api [--mode mock|gemini-local|bedrock-local]
                                               run FastAPI on :8000 (default: APP_MODE from the environment, or mock)
    python -m backend.scripts.dev frontend     run the Streamlit site on :8501 against the local API
    python -m backend.scripts.dev admin receive|inspect ITEM-ID [--outcome PASS|QUARANTINE] [--grade A]
    python -m backend.scripts.dev demo-photos  write 7 synthetic return photos to .local/demo-photos for uploading
    python -m backend.scripts.dev smoke [--i-approve-api-calls]
                                               shopping + photo-review smoke test against the running API
    python -m backend.scripts.dev openapi      write contracts/openapi.json
    python -m backend.scripts.dev check-gemini [--i-approve-api-call [--with-image]]
    python -m backend.scripts.dev check-bedrock [--i-approve-billable-call [--with-image]]
                                               check a provider's setup; nothing is sent without the approval flag

Secrets live in .local/dev-secrets.env (or the process environment, which
wins) and reach child processes through their environment only. They are
never printed, and the Streamlit process never receives model keys.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import urllib.request
from pathlib import Path
from typing import Dict

ROOT = Path(__file__).resolve().parents[2]
LOCAL = ROOT / ".local"
SECRETS = LOCAL / "dev-secrets.env"
DOTENV = ROOT / ".env"  # optional, gitignored; see .env.example
PERSONAS = ("CUST_01", "CUST_02", "CUST_03", "ADMIN")
MODEL_SECRETS = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                 "AWS_BEARER_TOKEN_BEDROCK", "DEMO_TOKEN_SECRET")


def _read_env_file(path: Path) -> Dict[str, str]:
    values = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            if value and not value.startswith("<"):  # skip .env.example placeholders like <your key>
                values[key.strip().removeprefix("export ").strip()] = value
    return values


def read_secrets() -> Dict[str, str]:
    if not SECRETS.exists():
        sys.exit("No dev secrets yet. Run: python -m backend.scripts.dev secrets")
    return _read_env_file(SECRETS)


def backend_env(extra: Dict[str, str] = None) -> Dict[str, str]:
    """Precedence: explicit extras > the process environment > .env > .local/dev-secrets.env."""
    files = {**read_secrets(), **(_read_env_file(DOTENV) if DOTENV.exists() else {})}
    process = {k: v for k, v in os.environ.items() if v or k not in files}  # an empty export doesn't hide a file value
    env = {**files, **process, **(extra or {})}
    env.setdefault("APP_MODE", "mock")
    env.setdefault("DDB_ENDPOINT", "http://localhost:8001")
    env.setdefault("AWS_EC2_METADATA_DISABLED", "true")  # a laptop has no instance metadata to probe
    return env


def cmd_secrets(_args) -> None:
    if SECRETS.exists():
        print(f"{SECRETS.relative_to(ROOT)} already exists; leaving it unchanged.")
        return
    LOCAL.mkdir(exist_ok=True)
    lines = ["# Local demo secrets (generated). Never commit this file.",
             f"DEMO_TOKEN_SECRET={secrets.token_urlsafe(48)}"]
    lines += [f"DEMO_PASSWORD_{p}={secrets.token_urlsafe(18)}" for p in PERSONAS]
    SECRETS.write_text("\n".join(lines) + "\n")
    SECRETS.chmod(0o600)
    print(f"Wrote {SECRETS.relative_to(ROOT)} (mode 600).")


def cmd_ddb(_args) -> None:
    home = LOCAL / "dynamodb-local"
    (LOCAL / "ddb-data").mkdir(parents=True, exist_ok=True)
    os.chdir(home)  # DynamoDB Local writes its metadata file into the working directory
    os.execvp("java", ["java", f"-Djava.library.path={home / 'DynamoDBLocal_lib'}", "-jar",
                       str(home / "DynamoDBLocal.jar"), "-sharedDb", "-dbPath", str(LOCAL / "ddb-data"),
                       "-port", "8001", "-disableTelemetry"])


def _repo_and_store():
    from backend.config import load_settings
    from backend.repository import Repository
    from backend.storage import LocalImageStore

    settings = load_settings(backend_env())
    return settings, Repository.from_settings(settings), LocalImageStore(settings.image_root)


def cmd_seed(_args) -> None:
    from backend.seed.load import counts, seed

    settings, repo, store = _repo_and_store()
    created = repo.ensure_table()
    result = seed(repo, store)
    print(f"table {settings.table} ({'created' if created else 'existing'}); new records: {result}; now: {counts(repo)}")


def cmd_reset(args) -> None:
    if not args.confirm_reset_demo_only:
        sys.exit("Refusing: add --confirm-reset-demo-only (deletes demo-v1 records only).")
    from backend.seed.load import reset

    settings, repo, _ = _repo_and_store()
    removed = reset(repo, include_runtime=args.include_runtime)
    print(f"Deleted {removed} records from {settings.table} at {settings.ddb_endpoint}. Run `seed` to reload.")


def _agentcore_env() -> Dict[str, str]:
    """Optional local-only AgentCore settings; see docs/AGENTCORE_PUBLIC.md."""
    path = LOCAL / "agentcore.json"
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return {"AGENTCORE_RUNTIME_ARN": data.get("runtime_arn", ""), "AGENTCORE_MODEL_ID": data.get("model_id", "")}


def cmd_api(args) -> None:
    from backend.config import ConfigError, load_settings

    extra = {"APP_MODE": args.mode} if args.mode else {}
    if (args.mode or os.environ.get("APP_MODE")) == "agentcore":
        extra = {**{k: v for k, v in _agentcore_env().items() if v and not os.environ.get(k)}, **extra}
    env = backend_env(extra or None)
    try:
        settings = load_settings(env)  # fail here with a clear message instead of a uvicorn traceback
    except ConfigError as exc:
        sys.exit(f"not ready: {exc}")
    model = {"gemini-local": settings.gemini_model_id, "bedrock-local": settings.bedrock_model_id,
             "agentcore": f"{settings.agentcore_model_id} on AgentCore Runtime {settings.agentcore_runtime_arn} "
                          "(Buy Defective; Return stays on the scripted mock)"}.get(
        settings.app_mode, "none (scripted mock)")
    print(f"APP_MODE={settings.app_mode} · model {model} · http://127.0.0.1:{args.port}/docs", flush=True)
    os.execvpe(sys.executable, [sys.executable, "-m", "uvicorn", "backend.main:create_app", "--factory", "--host",
                                "127.0.0.1", "--port", str(args.port)], env)


def cmd_frontend(args) -> None:
    values = backend_env()
    env = {k: v for k, v in os.environ.items() if k not in MODEL_SECRETS}  # the site never gets model keys
    env.setdefault("ACCEPTMATCH_API_TIMEOUT", "75")  # an AgentCore cold start plus two model calls can pass 30 s
    env.update({"ACCEPTMATCH_AGENT": "api", "ACCEPTMATCH_API_BASE_URL": f"http://127.0.0.1:{args.api_port}",
                "ACCEPTMATCH_DEMO_PERSONA_SHOP": "cust-02",
                "ACCEPTMATCH_DEMO_PASSWORD_SHOP": values["DEMO_PASSWORD_CUST_02"],
                "ACCEPTMATCH_DEMO_PERSONA_RETURN": "cust-01",
                "ACCEPTMATCH_DEMO_PASSWORD_RETURN": values["DEMO_PASSWORD_CUST_01"]})
    streamlit = ROOT / ".venv" / "bin" / "streamlit"
    os.chdir(ROOT)
    os.execvpe(str(streamlit), [str(streamlit), "run", "app.py", "--server.port", str(args.port),
                                "--server.headless", "true"], env)


def _call(method: str, path: str, body=None, token: str = None, port: int = 8000):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": f"Bearer {token}"} if token else {})})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def cmd_admin(args) -> None:
    values = read_secrets()
    token = _call("POST", "/api/demo-auth/login", {"persona": "admin", "password": values["DEMO_PASSWORD_ADMIN"]},
                  port=args.port)["access_token"]
    if args.action == "receive":
        result = _call("POST", f"/api/admin/items/{args.item_id}/receive", {"simulated": True}, token, args.port)
    elif args.action == "inspect":
        body = {"outcome": args.outcome, "notes": args.notes, "simulated": True}
        if args.grade:
            body["grade"] = args.grade
        result = _call("POST", f"/api/admin/items/{args.item_id}/inspect", body, token, args.port)
    else:
        result = _call("GET", f"/api/admin/items?status={args.item_id}" if args.item_id else "/api/admin/items",
                       token=token, port=args.port)
    print(json.dumps(result, indent=2))


def cmd_check_bedrock(args) -> None:
    """Check the APP_MODE=bedrock-local setup. Nothing is sent to Bedrock without --i-approve-billable-call."""
    os.environ.setdefault("AWS_EC2_METADATA_DISABLED", "true")  # a laptop has no instance metadata to probe
    import logging
    import time

    import boto3

    from backend.config import ConfigError, load_settings

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    try:
        settings = load_settings(backend_env({"APP_MODE": "bedrock-local"}))
    except ConfigError as exc:
        sys.exit(f"not ready: {exc}")
    print(f"text model    {settings.bedrock_model_id}\nvision model  {settings.bedrock_vision_model_id}\n"
          f"region        {settings.bedrock_region}")
    if os.environ.get("AWS_BEARER_TOKEN_BEDROCK"):
        print("credentials   Bedrock API key (AWS_BEARER_TOKEN_BEDROCK)")
    else:
        found = boto3.Session(region_name=settings.bedrock_region).get_credentials()
        if found is None:
            sys.exit("not ready: no AWS credentials found. Use AWS_PROFILE (after `aws sso login`), the "
                     "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_SESSION_TOKEN variables, or a Bedrock API key "
                     "in AWS_BEARER_TOKEN_BEDROCK. See docs/BACKEND.md.")
        print(f"credentials   found ({found.method})")
    if not args.i_approve_billable_call:
        print("Nothing was sent to Bedrock. To send one small, billable Converse request add "
              "--i-approve-billable-call (and --with-image to include one synthetic photo).")
        return
    from backend.agent.base import AgentFailure
    from backend.agent.strands_agent import BedrockAgent

    started = time.monotonic()
    try:
        answer = BedrockAgent(settings).check(with_image=args.with_image)
    except AgentFailure as failure:
        seen, aws = failure.__context__, {}
        while seen is not None and not aws:  # the AWS error, if any, sits somewhere down the cause chain
            response = getattr(seen, "response", None)
            aws = (response.get("Error") or {}) if isinstance(response, dict) else {}
            seen = seen.__cause__ or seen.__context__
        sys.exit(f"failed: {failure.status} {failure.code}: {(failure.detail or {}).get('cause') or failure.message}"
                 + (f"\nAWS said: {aws.get('Code')}: {aws.get('Message')}" if aws else ""))
    print(f"ok in {time.monotonic() - started:.1f} s: {answer.model_dump()}")


def cmd_gemini_key(args) -> None:
    """Store (or remove) GEMINI_API_KEY in .local/dev-secrets.env. The key is read hidden and never printed."""
    import getpass

    if not SECRETS.exists():
        sys.exit("Run `python -m backend.scripts.dev secrets` first.")
    lines = [line for line in SECRETS.read_text().splitlines() if not line.startswith("GEMINI_API_KEY=")]
    if args.remove:
        SECRETS.write_text("\n".join(lines) + "\n")
        print(f"Removed GEMINI_API_KEY from {SECRETS.relative_to(ROOT)}.")
        return
    key = getpass.getpass("Paste your Gemini API key from Google AI Studio (input is hidden): ").strip()
    if len(key) < 20 or any(c.isspace() for c in key):
        sys.exit("That doesn't look like an API key. Nothing was saved.")
    SECRETS.write_text("\n".join(lines + [f"GEMINI_API_KEY={key}"]) + "\n")
    SECRETS.chmod(0o600)
    print(f"Saved GEMINI_API_KEY to {SECRETS.relative_to(ROOT)} (mode 600, gitignored). It is never printed.")


def _run_line(run: Dict) -> str:
    return (f"{run.get('model_calls')} model call(s), tools {', '.join(run.get('tools_used') or []) or 'none'}, "
            f"tokens in/out {run.get('input_tokens')}/{run.get('output_tokens')}, {run.get('latency_ms')} ms")


def cmd_check_gemini(args) -> None:
    """Check the APP_MODE=gemini-local setup. Nothing is sent to Google without --i-approve-api-call."""
    from backend.config import ConfigError, load_settings

    try:
        settings = load_settings(backend_env({"APP_MODE": "gemini-local"}))
    except ConfigError as exc:
        sys.exit(f"not ready: {exc}")
    print(f"model           {settings.gemini_model_id}\nvision model    {settings.gemini_vision_model_id}\n"
          f"thinking level  {settings.gemini_thinking_level}\nAPI key         set (not shown)\n"
          f"call cap        {settings.agent_max_calls_per_minute} per minute, "
          f"{settings.agent_max_turns_per_session} AI replies per conversation")
    if not args.i_approve_api_call:
        print("Nothing was sent to Google. To send one small request (uses free-tier quota) add "
              "--i-approve-api-call, and --with-image to send one synthetic photo as well.")
        return
    from backend.agent.base import AgentFailure
    from backend.agent.strands_agent import GeminiAgent

    agent, runs = GeminiAgent(settings), []
    try:
        answer = agent.check(evidence=runs)
        print(f"text   ok {answer.model_dump()} · {_run_line(runs[-1])}")
        if args.with_image:
            answer = agent.check(with_image=True, evidence=runs)
            print(f"image  ok {answer.model_dump()} · {_run_line(runs[-1])}")
    except AgentFailure as failure:
        sys.exit(f"failed: {failure.status} {failure.code}: {(failure.detail or {}).get('cause') or failure.message}")


VIEWS = ("pair_top", "pair_outer", "pair_inner", "pair_soles", "pair_heels", "defect_closeup", "size_label")


def cmd_demo_photos(args) -> None:
    """Synthetic photos for a manual Return demo (they match ORD-001: black ankle boots, scratched left heel)."""
    from backend.seed.images import render

    out = Path(args.out).expanduser() if args.out else LOCAL / "demo-photos"
    out.mkdir(parents=True, exist_ok=True)
    for index, view in enumerate(VIEWS, 1):
        (out / f"{index}_{view}.jpg").write_bytes(render(view, "black", "scratch", seed=index))
    (out / "extra_blurry_pair_soles.jpg").write_bytes(render("pair_soles", "black", "scratch", seed=4, blurry=True))
    print(f"Wrote 7 synthetic photos and 1 blurry retake example to {out}. Upload them in the Return journey for "
          "demo order ORD-001. They are drawings marked SYNTHETIC, not photos of real shoes.")


def cmd_smoke(args) -> None:
    """Minimal live check through the running API: one shopping conversation and one photo review."""
    import time
    import uuid

    import httpx

    from backend.seed.images import render

    values = backend_env()
    http = httpx.Client(base_url=f"http://127.0.0.1:{args.port}", timeout=90)
    try:
        health = http.get("/health").json()
    except httpx.HTTPError:
        sys.exit(f"The API isn't running on port {args.port}. Start it with: python -m backend.scripts.dev api")
    agent = health["agent"]
    real = agent["provider"] != "mock"
    print(f"backend   APP_MODE={health['mode']} · provider {agent['provider']} · model {agent['model_id']} · "
          f"database ok={health['database']['ok']}")
    if real and not args.i_approve_api_calls:
        sys.exit("This sends about 6 to 12 requests to the real model and uses free-tier quota. "
                 "Re-run with --i-approve-api-calls.")

    def login(persona: str) -> Dict[str, str]:
        password = values["DEMO_PASSWORD_" + persona.upper().replace("-", "_")]
        token = http.post("/api/demo-auth/login", json={"persona": persona, "password": password}).json()
        return {"Authorization": f"Bearer {token['access_token']}"}

    def post(path: str, headers: Dict[str, str], **kwargs):
        """POST; if AcceptMatch's own per-minute cap stopped it, wait and resend the same request (same keys)."""
        response = http.post(path, headers=headers, **kwargs)
        if response.status_code == 429 and "per minute" in response.text:
            print("          (AcceptMatch's per-minute AI cap reached; waiting 60 s, then resending the same request)")
            time.sleep(60)
            response = http.post(path, headers=headers, **kwargs)
        return response

    def say(headers: Dict[str, str], sid: str, text: str) -> Dict:
        time.sleep(args.pause if real else 0)
        response = post(f"/api/sessions/{sid}/messages", headers,
                        json={"text": text, "photo_ids": [], "client_request_id": "smoke-" + uuid.uuid4().hex[:10]})
        body = response.json()
        if response.status_code != 200:
            sys.exit(f"FAILED  {response.status_code} {body.get('code')}: {body.get('detail') or body.get('message')}"
                     f" (request {body.get('request_id')})")
        run = body.get("agent") or {}
        print(f"  you     {text[:90]}\n  agent   {body['message'][:160]}\n"
              f"          {body['next_action']} · source {body['analysis_source']} · request {body['request_id']}"
              + (f"\n          {_run_line(run)}" if run else ""))
        return body

    verdicts = []
    print("\nTest 2 · shopping conversation (cust-02)")
    buyer = login("cust-02")
    sid = http.post("/api/sessions", json={"mode": "shopping"}, headers=buyer).json()["session_id"]
    turns = [say(buyer, sid, "I'm looking for black women's shoes, US size 7, under CAD 100. A small scratch on "
                             "the back is okay, but I don't want stains.")]
    follow = (turns[0]["questions"] or [{}])[0]
    if follow.get("options"):
        turns.append(say(buyer, sid, follow["options"][0]["label"]))
    elif turns[0]["next_action"] != "SHOW_ITEMS":
        turns.append(say(buyer, sid, "Scuffs are fine too. Show me what you have."))
    tools = sorted({t for body in turns for t in (body.get("agent") or {}).get("tools_used") or []})
    items = [i["item_id"] for body in turns for i in body["items"]]
    stored = [i for i in dict.fromkeys(items) if http.get(f"/api/items/{i}").status_code == 200]
    print(f"  items   {', '.join(dict.fromkeys(items)) or 'none'} · found in the database: {', '.join(stored) or 'none'}")
    ok = bool(stored) and (not real or ("search_inventory" in tools and
                                        all(b["analysis_source"] != "SIMULATED" for b in turns)))
    verdicts.append(("Test 2 shopping", ok))

    print("\nTest 3 · return photo review (cust-01)")
    customer = login("cust-01")
    rsid = http.post("/api/sessions", json={"mode": "return"}, headers=customer).json()["session_id"]
    body = say(customer, rsid, f"Return request for demo order {args.order}, line 1. Type: defect claim. Reason: a "
                               "defect or damage. What happened: There's a scratch on the back of the left heel. I "
                               "noticed it the first time I wore them indoors. They haven't been repaired.")
    for _ in range(2):
        if body["next_action"] == "UPLOAD_PHOTOS":
            break
        body = say(customer, rsid, "Only indoors, they have never been repaired, and I noticed it the first time I "
                                   "wore them.")
    if body["next_action"] != "UPLOAD_PHOTOS":
        verdicts.append(("Test 3 photo review", False))
    else:
        for index, view in enumerate(VIEWS, 1):
            upload = http.post("/api/uploads", headers=customer,
                               files={"file": (f"{view}.jpg", render(view, "black", "scratch", seed=index),
                                               "image/jpeg")},
                               data={"session_id": rsid, "declared_view": view, "purpose": "RETURN_EVIDENCE"})
            if upload.status_code != 200:
                sys.exit(f"FAILED  upload {view}: {upload.status_code} {upload.text[:200]}")
        created = http.post("/api/returns", headers={**customer, "Idempotency-Key": "smoke-" + uuid.uuid4().hex[:12]},
                            json={"session_id": rsid, "order_id": args.order, "order_line": "1",
                                  "claim_type": "DEFECT_CLAIM", "reason": "defect"}).json()
        if created.get("status") not in ("INFO_COLLECTION", "NEEDS_MORE_EVIDENCE"):
            print(f"  {args.order} already has return {created.get('return_id')} ({created.get('status')}), so no new "
                  "review runs. Reset the demo data (dev reset --confirm-reset-demo-only --include-runtime, then "
                  "dev seed) or pass --order.")
            verdicts.append(("Test 3 photo review", False))
        else:
            time.sleep(args.pause if real else 0)
            response = post(f"/api/returns/{created['return_id']}/evaluate", customer,
                            json={"client_request_id": "smoke-" + uuid.uuid4().hex[:10]})
            result = response.json()
            run = result.get("agent") or {}
            print(f"  review  {response.status_code} · {result.get('status') or result.get('code')} · return "
                  f"{created['return_id']} · source {result.get('analysis_source')} · request "
                  f"{result.get('request_id')}")
            if run:
                print(f"          {run.get('images_sent')} images sent · {_run_line(run)}")
            for missing in result.get("missing_views") or []:
                print(f"          missing {missing['view']}: {missing.get('reason')}")
            ok = response.status_code == 200 and (not real or (result.get("analysis_source") != "SIMULATED"
                                                               and (run.get("images_sent") or 0) > 0))
            verdicts.append(("Test 3 photo review", ok))
    print()
    for name, ok in verdicts:
        print(f"{'PASS' if ok else 'FAIL'}  {name}{'' if real else ' (scripted mock: not a real model test)'}")
    sys.exit(0 if all(ok for _, ok in verdicts) else 1)


def cmd_openapi(_args) -> None:
    os.environ.update({k: v for k, v in backend_env().items() if k.startswith(("DEMO_", "APP_", "DDB_"))})
    from backend.main import create_app

    target = ROOT / "contracts" / "openapi.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(create_app().openapi(), indent=2) + "\n")
    print(f"Wrote {target.relative_to(ROOT)}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m backend.scripts.dev")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("secrets").set_defaults(run=cmd_secrets)
    sub.add_parser("ddb").set_defaults(run=cmd_ddb)
    sub.add_parser("seed").set_defaults(run=cmd_seed)
    reset = sub.add_parser("reset")
    reset.add_argument("--confirm-reset-demo-only", action="store_true")
    reset.add_argument("--include-runtime", action="store_true")
    reset.set_defaults(run=cmd_reset)
    api = sub.add_parser("api")
    api.add_argument("--port", type=int, default=8000)
    api.add_argument("--mode", choices=("mock", "gemini-local", "bedrock-local", "agentcore"))
    api.set_defaults(run=cmd_api)
    key = sub.add_parser("gemini-key")
    key.add_argument("--remove", action="store_true")
    key.set_defaults(run=cmd_gemini_key)
    photos = sub.add_parser("demo-photos")
    photos.add_argument("--out")
    photos.set_defaults(run=cmd_demo_photos)
    smoke = sub.add_parser("smoke")
    smoke.add_argument("--port", type=int, default=8000)
    smoke.add_argument("--order", default="ORD-001")
    smoke.add_argument("--pause", type=float, default=3.0)
    smoke.add_argument("--i-approve-api-calls", action="store_true")
    smoke.set_defaults(run=cmd_smoke)
    gem = sub.add_parser("check-gemini")
    gem.add_argument("--i-approve-api-call", action="store_true")
    gem.add_argument("--with-image", action="store_true")
    gem.set_defaults(run=cmd_check_gemini)
    front = sub.add_parser("frontend")
    front.add_argument("--port", type=int, default=8501)
    front.add_argument("--api-port", type=int, default=8000)
    front.set_defaults(run=cmd_frontend)
    adm = sub.add_parser("admin")
    adm.add_argument("action", choices=("receive", "inspect", "list"))
    adm.add_argument("item_id", nargs="?")
    adm.add_argument("--outcome", choices=("PASS", "QUARANTINE"), default="PASS")
    adm.add_argument("--grade", choices=("A", "B", "C"))
    adm.add_argument("--notes", default="Simulated warehouse inspection")
    adm.add_argument("--port", type=int, default=8000)
    adm.set_defaults(run=cmd_admin)
    sub.add_parser("openapi").set_defaults(run=cmd_openapi)
    check = sub.add_parser("check-bedrock")
    check.add_argument("--i-approve-billable-call", action="store_true")
    check.add_argument("--with-image", action="store_true")
    check.set_defaults(run=cmd_check_bedrock)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
