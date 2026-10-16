from conftest import page_text

from ui.config import DEFAULT_NEW_URL, DEFAULT_PAST_SEASON_URL, load_settings, validate_external_url


def _home_links(at):
    return {lb.proto.label: lb.proto.url for lb in at.get("link_button")
            if lb.proto.label in ("Shop ALDO ↗", "Shop the ALDO Sale ↗")}


def test_home_has_exactly_four_primary_entries(app):
    tiles = [key for key in ("am_home_new", "am_home_return", "am_home_buy", "am_home_past")]
    buttons = {b.key for b in app.button if str(b.key).startswith("am_home_")}
    links = _home_links(app)
    assert buttons == {"am_home_return", "am_home_buy"}
    assert set(links) == {"Shop ALDO ↗", "Shop the ALDO Sale ↗"}
    assert len(buttons) + len(links) == len(tiles) == 4


def test_external_entries_use_configured_urls(app):
    links = _home_links(app)
    assert links["Shop ALDO ↗"] == DEFAULT_NEW_URL == "https://www.aldoshoes.com/en-ca"
    assert links["Shop the ALDO Sale ↗"] == DEFAULT_PAST_SEASON_URL
    assert "Opens the official ALDO website." in page_text(app)


def test_past_season_url_comes_from_config(monkeypatch):
    from streamlit.testing.v1 import AppTest

    from conftest import ROOT

    monkeypatch.setenv("PAST_SEASON_URL", "https://www.aldoshoes.com/en-ca/collections/outlet-test")
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    at.run()
    assert "https://www.aldoshoes.com/en-ca/collections/outlet-test" in _home_links(at).values()


def test_home_never_creates_an_agent_client_or_session_data(app):
    assert app.session_state["v3_clients"] == {}
    assert "v3_overlay" not in app.session_state
    assert app.session_state["v3_calls"] == []


def test_there_is_no_internal_past_season_route(app):
    from ui import state

    assert not [r for r in state.V3_ROUTES if "past" in r or "season" in r]
    app.session_state["v3_route"] = "past_season"
    app.run()
    assert {b.key for b in app.button if str(b.key).startswith("am_home_")} == {"am_home_return", "am_home_buy"}


def test_internal_entries_route_inside_the_site(app):
    app.button(key="am_home_buy").click().run()
    assert app.session_state["v3_route"] == "shop"
    app.button(key="v3_s_back_home").click().run()
    assert app.session_state["v3_route"] == "home"
    app.button(key="am_home_return").click().run()
    assert app.session_state["v3_route"] == "return"


def test_no_v1_staff_or_factory_chrome_on_home(app):
    content = page_text(app).lower()
    for phrase in ("staff panel", "inspection station", "factory", "agent activity", "reset demo"):
        assert phrase not in content


def test_external_url_validation():
    warnings = []
    assert validate_external_url("http://www.aldoshoes.com/en-ca", "d", "K", warnings) == "d"
    assert validate_external_url("https://evil.example/aldoshoes.com", "d", "K", warnings) == "d"
    assert validate_external_url("https://www.aldoshoes.com.evil.example/", "d", "K", warnings) == "d"
    assert validate_external_url("https://www.aldoshoes.com/en-ca/x", "d", "K", warnings) == \
        "https://www.aldoshoes.com/en-ca/x"
    assert len(warnings) == 3
    settings = load_settings({"PAST_SEASON_URL": "javascript:alert(1)"})
    assert settings.past_season_url == DEFAULT_PAST_SEASON_URL and settings.warnings
