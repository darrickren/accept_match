import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

AGENT_ENV = ("ACCEPTMATCH_AGENT", "ACCEPTMATCH_AGENT_SHOP", "ACCEPTMATCH_AGENT_RETURN", "NEW_URL",
             "PAST_SEASON_URL", "PAST_SEASON_CTA", "ACCEPTMATCH_DIAGNOSTICS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Every test starts from the default (mock, no overrides) configuration."""
    for key in AGENT_ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(ROOT)


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
