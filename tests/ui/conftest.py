import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AGENT_ENV = ("ACCEPTMATCH_AGENT", "ACCEPTMATCH_AGENT_SHOP", "ACCEPTMATCH_AGENT_RETURN", "NEW_URL",
             "PAST_SEASON_URL", "PAST_SEASON_CTA", "ACCEPTMATCH_DIAGNOSTICS", "ACCEPTMATCH_API_BASE_URL",
             "ACCEPTMATCH_API_TIMEOUT", "ACCEPTMATCH_API_VISION_TIMEOUT", "ACCEPTMATCH_API_HEALTH_TIMEOUT",
             "ACCEPTMATCH_API_MAX_RETRIES", "ACCEPTMATCH_API_TOKEN", "ACCEPTMATCH_IMAGE_HOSTS",
             "ACCEPTMATCH_DEMO_PERSONA", "ACCEPTMATCH_DEMO_PERSONA_SHOP", "ACCEPTMATCH_DEMO_PERSONA_RETURN",
             "ACCEPTMATCH_DEMO_PASSWORD", "ACCEPTMATCH_DEMO_PASSWORD_SHOP", "ACCEPTMATCH_DEMO_PASSWORD_RETURN")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts from the default (mock, no overrides) configuration."""
    for key in AGENT_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(ROOT)


@pytest.fixture
def fake_api():
    """A fake FastAPI backend (tests/ui/fake_api.py) on a free local port: (backend, base_url)."""
    from fake_api import start

    server, backend, url = start()
    yield backend, url
    server.shutdown()
    server.server_close()


def api_env(monkeypatch, url, **extra):
    """api mode for both journeys: cust-02 shops, cust-01 returns (the plan's demo personas)."""
    env = {"ACCEPTMATCH_AGENT": "api", "ACCEPTMATCH_API_BASE_URL": url,
           "ACCEPTMATCH_DEMO_PERSONA_SHOP": "cust-02", "ACCEPTMATCH_DEMO_PASSWORD_SHOP": "pw-cust-02",
           "ACCEPTMATCH_DEMO_PERSONA_RETURN": "cust-01", "ACCEPTMATCH_DEMO_PASSWORD_RETURN": "pw-cust-01"}
    env.update(extra)
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)


def api_app(monkeypatch, url, **extra):
    from streamlit.testing.v1 import AppTest

    api_env(monkeypatch, url, **extra)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


@pytest.fixture
def app():
    from streamlit.testing.v1 import AppTest

    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def raw_page(at) -> str:
    """Raw markup of markdown, html and caption elements (escaping preserved)."""
    parts = []
    for kind in ("markdown", "html", "caption"):
        try:
            parts += [str(getattr(el, "value", "")) for el in at.get(kind)]
        except Exception:  # noqa: BLE001 - element type absent
            pass
    return "\n".join(parts)


def page_text(at) -> str:
    """Visible text for content assertions (HTML entities decoded)."""
    import html

    return html.unescape(raw_page(at))
