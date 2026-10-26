"""End-to-end: the real Streamlit V3 site (AppTest) against the real FastAPI backend and DynamoDB Local.

Not the fake server. Runs in the frontend virtualenv and starts:
  DynamoDB Local (in-memory) -> backend seed -> uvicorn backend.main:create_app (APP_MODE=mock)
Run separately from tests/ui (each folder has its own conftest):
    .venv/bin/python -m pytest tests/e2e -q -p no:cacheprovider
"""

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
BACKEND_PY = ROOT / ".venv-backend" / "bin" / "python"
DDB_HOME = ROOT / ".local" / "dynamodb-local"
FRONTEND_ENV = ("ACCEPTMATCH_AGENT", "ACCEPTMATCH_AGENT_SHOP", "ACCEPTMATCH_AGENT_RETURN", "ACCEPTMATCH_API_BASE_URL",
                "ACCEPTMATCH_DEMO_PERSONA_SHOP", "ACCEPTMATCH_DEMO_PASSWORD_SHOP", "ACCEPTMATCH_DEMO_PERSONA_RETURN",
                "ACCEPTMATCH_DEMO_PASSWORD_RETURN", "ACCEPTMATCH_DIAGNOSTICS", "ACCEPTMATCH_API_TOKEN")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait(url: str, seconds: float = 30) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2):
                return
        except Exception:  # noqa: BLE001 - not up yet
            time.sleep(0.2)
    raise RuntimeError(f"{url} didn't come up")


@pytest.fixture(scope="module")
def stack(tmp_path_factory):
    """A fresh backend per test module: (base_url, passwords, admin_call)."""
    for path in (BACKEND_PY, DDB_HOME / "DynamoDBLocal.jar"):
        if not path.exists():
            pytest.fail(f"missing {path.relative_to(ROOT)}: see docs/BACKEND.md")
    ddb_port, api_port = _free_port(), _free_port()
    passwords = {p: secrets.token_urlsafe(12) for p in ("cust-01", "cust-02", "cust-03", "admin")}
    env = {**os.environ, "APP_MODE": "mock", "DDB_ENDPOINT": f"http://127.0.0.1:{ddb_port}",
           "ACCEPTMATCH_TABLE": "e2e_demo", "LOCAL_IMAGE_ROOT": str(tmp_path_factory.mktemp("images")),
           "DEMO_TOKEN_SECRET": secrets.token_urlsafe(48), "PYTHONDONTWRITEBYTECODE": "1"}
    env.update({"DEMO_PASSWORD_" + p.upper().replace("-", "_"): v for p, v in passwords.items()})
    ddb = subprocess.Popen(["java", f"-Djava.library.path={DDB_HOME / 'DynamoDBLocal_lib'}", "-jar",
                            str(DDB_HOME / "DynamoDBLocal.jar"), "-sharedDb", "-inMemory", "-port", str(ddb_port),
                            "-disableTelemetry"], cwd=DDB_HOME, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    api = None
    try:
        time.sleep(1.5)
        subprocess.run([str(BACKEND_PY), "-m", "backend.seed.load"], cwd=ROOT, env=env, check=True,
                       capture_output=True, timeout=120)
        api = subprocess.Popen([str(BACKEND_PY), "-m", "uvicorn", "backend.main:create_app", "--factory", "--host",
                                "127.0.0.1", "--port", str(api_port)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
        base = f"http://127.0.0.1:{api_port}"
        _wait(base + "/health")

        def admin_call(method: str, path: str, body=None):
            token = _post(base, "/api/demo-auth/login", {"persona": "admin", "password": passwords["admin"]})
            return _post(base, path, body, token["access_token"], method)

        yield base, passwords, admin_call
    finally:
        if api:
            api.terminate()
            api.wait(timeout=10)
        ddb.terminate()
        ddb.wait(timeout=10)


def _post(base, path, body=None, token=None, method="POST"):
    request = urllib.request.Request(base + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json",
                                              **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in FRONTEND_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(ROOT)


def site(monkeypatch, stack, shop_persona="cust-02", return_persona="cust-01"):
    """The real V3 app in api mode, signed in as the given demo personas."""
    from streamlit.testing.v1 import AppTest

    base, passwords, _ = stack
    for key, value in {"ACCEPTMATCH_AGENT": "api", "ACCEPTMATCH_API_BASE_URL": base,
                       "ACCEPTMATCH_DEMO_PERSONA_SHOP": shop_persona,
                       "ACCEPTMATCH_DEMO_PASSWORD_SHOP": passwords[shop_persona],
                       "ACCEPTMATCH_DEMO_PERSONA_RETURN": return_persona,
                       "ACCEPTMATCH_DEMO_PASSWORD_RETURN": passwords[return_persona]}.items():
        monkeypatch.setenv(key, value)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=90)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def page_text(at) -> str:
    import html

    parts = []
    for kind in ("markdown", "html", "caption"):
        try:
            parts += [str(getattr(el, "value", "")) for el in at.get(kind)]
        except Exception:  # noqa: BLE001
            pass
    return html.unescape("\n".join(parts))
