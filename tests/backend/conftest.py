"""Backend tests: the real FastAPI app against a real DynamoDB Local (in-memory, its own port).

Run with the backend virtualenv:
    .venv-backend/bin/python -m pytest tests/backend -q -p no:cacheprovider
Each test gets its own table, so tests never see each other's data.
"""

import os
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DDB_HOME = ROOT / ".local" / "dynamodb-local"
PASSWORDS = {"cust-01": "test-pass-c01", "cust-02": "test-pass-c02", "cust-03": "test-pass-c03",
             "admin": "test-pass-admin"}


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_ddb(port: int, db_path: Path = None) -> subprocess.Popen:
    if not (DDB_HOME / "DynamoDBLocal.jar").exists():
        pytest.fail("DynamoDB Local isn't installed in .local/dynamodb-local (see docs/BACKEND.md)")
    storage = ["-dbPath", str(db_path)] if db_path else ["-inMemory"]
    proc = subprocess.Popen(["java", f"-Djava.library.path={DDB_HOME / 'DynamoDBLocal_lib'}", "-jar",
                             str(DDB_HOME / "DynamoDBLocal.jar"), "-sharedDb", *storage, "-port", str(port),
                             "-disableTelemetry"], cwd=db_path or DDB_HOME, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    for _ in range(100):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return proc
        time.sleep(0.1)
    proc.kill()
    pytest.fail("DynamoDB Local didn't start")


def env_for(endpoint: str, table: str, image_root: Path, **extra) -> dict:
    env = {"APP_MODE": "mock", "DDB_ENDPOINT": endpoint, "ACCEPTMATCH_TABLE": table, "LOCAL_IMAGE_ROOT": str(image_root),
           "DEMO_TOKEN_SECRET": "test-secret-" + "x" * 40}
    env.update({"DEMO_PASSWORD_" + p.upper().replace("-", "_"): v for p, v in PASSWORDS.items()})
    env.update(extra)
    return env


@pytest.fixture(scope="session")
def ddb():
    port = free_port()
    proc = start_ddb(port)
    yield f"http://127.0.0.1:{port}"
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="session")
def image_root(tmp_path_factory):
    return tmp_path_factory.mktemp("images")


@pytest.fixture
def backend(ddb, image_root):
    """(TestClient, services container) on a fresh, seeded table."""
    from fastapi.testclient import TestClient

    from backend.config import load_settings
    from backend.main import create_app
    from backend.seed.load import seed

    settings = load_settings(env_for(ddb, "t_" + uuid.uuid4().hex[:10], image_root))
    app = create_app(settings)
    with TestClient(app) as client:
        seed(app.state.c.repo, app.state.c.store)
        yield client, app.state.c
        app.state.c.repo.drop_table()


def login(client, persona: str) -> dict:
    response = client.post("/api/demo-auth/login", json={"persona": persona, "password": PASSWORDS[persona]})
    assert response.status_code == 200, response.text
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def frontend_checks():
    """The V3 frontend's own contract checks (ui/client/api_schemas.py), loaded without Streamlit."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("frontend_api_schemas", ROOT / "ui" / "client" / "api_schemas.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def shoe_photo(view: str, **kwargs) -> bytes:
    from backend.seed.images import render

    return render(view, kwargs.pop("colour", "black"), kwargs.pop("defect", "scratch"), **kwargs)
