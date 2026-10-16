"""Backend selection per journey: no silent fallback to the mock; diagnostics stay opt-in."""
from conftest import ROOT, page_text
from streamlit.testing.v1 import AppTest


def run_app(monkeypatch, **env):
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.run()
    return at


def test_live_backend_without_client_shows_unavailable_instead_of_mock(monkeypatch):
    at = run_app(monkeypatch, ACCEPTMATCH_AGENT_SHOP="agentcore")
    at.button(key="am_home_buy").click().run()
    assert not at.exception
    assert "isn't available right now" in page_text(at)
    assert "shop" not in at.session_state["v3_clients"]             # no mock was created instead
    assert any(c["operation"] == "configure" for c in at.session_state["v3_calls"])


def test_journeys_can_use_different_backends_and_are_labelled(monkeypatch):
    at = run_app(monkeypatch, ACCEPTMATCH_AGENT_SHOP="agentcore", ACCEPTMATCH_AGENT_RETURN="mock")
    at.button(key="am_home_return").click().run()
    assert "Demo assistant · scripted" in page_text(at)
    at.segmented_control(key="v3_r_category").set_value("tote").run()
    at.selectbox(key="v3_r_reason").set_value("damaged").run()
    at.button(key="v3_r_next_1").click().run()
    at.text_area(key="v3_r_desc").input("A handle is coming loose").run()
    at.button(key="v3_r_next_2").click().run()
    assert at.session_state["v3_clients"]["return"].source == "mock"   # created lazily, on the first call
    assert "shop" not in at.session_state["v3_clients"]


def test_diagnostics_are_off_by_default_and_render_when_enabled(monkeypatch, app):
    assert "Diagnostics" not in [e.label for e in app.expander]
    at = run_app(monkeypatch, ACCEPTMATCH_DIAGNOSTICS="1")
    at.button(key="am_home_buy").click().run()
    assert not at.exception
    assert "Diagnostics (demo operators)" in [e.label for e in at.expander]
    assert "Scripted mock trace" in page_text(at)
