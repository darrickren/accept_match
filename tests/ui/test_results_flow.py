"""Results and listing detail through the real app (Task 4)."""
from conftest import page_text
from test_assistant_flow import DEMO, choose, complete_demo, open_assistant, question, version

from ui.client.mock_agent import MockAgentClient


def to_results(app, plan=DEMO):
    open_assistant(app)
    while question(app):
        qid = question(app)
        choose(app, qid, plan[qid])
    app.button(key="v3_s_see_done").click().run()
    assert not app.exception, [e.value for e in app.exception]
    assert app.session_state["v3_route"] == "results"
    return app.session_state["v3_shop"]["results"]


def test_verified_matches_come_first_and_possible_ones_are_labelled(app):
    results = to_results(app)
    levels = [r["match"]["level"] for r in results["results"]]
    assert levels == ["verified", "verified", "possible"]
    text = page_text(app)
    assert "Verified matches" in text and "Possible matches" in text
    assert "Needs inspection before purchase." in text
    assert "Currently unavailable" in text            # AM-006 is published but reserved
    assert "CA$108.00" in text and "Item AM-007" in text


def test_possible_match_detail_shows_missing_evidence_and_no_buy_action(app):
    to_results(app)
    app.button(key="v3_s_view_LS-1007").click().run()
    assert app.session_state["v3_route"] == "listing"
    text = page_text(app)
    assert "Possible match." in text and "Zipper hasn't been checked yet" in text
    assert "Condition report" in text and "Intake inspection" in text
    labels = [b.label for b in app.button if not str(b.key).startswith("am_nav_")]  # nav says "Buy Defective"
    assert not [label for label in labels if any(w in label.lower() for w in ("buy", "reserve", "cart", "checkout"))]


def test_verified_detail_lists_why_it_fits(app):
    to_results(app)
    app.button(key="v3_s_view_LS-1008").click().run()
    text = page_text(app)
    assert "Verified match." in text and "Why it fits you" in text
    assert "Zipper verified working" in text


def test_forged_verified_claim_is_downgraded_and_recorded(app):
    class Forger(MockAgentClient):
        def search_products(self, *args, **kwargs):
            response = super().search_products(*args, **kwargs)
            for item in response["data"]["results"]:
                if item["unit_id"] == "AM-007":
                    item["match"] = {"level": "verified", "reasons": ["Trust me"], "missing_evidence": []}
            return response

    open_assistant(app)
    app.session_state["v3_clients"]["shop"] = Forger("shop", app.session_state["v3_overlay"])
    while question(app):
        choose(app, question(app), DEMO[question(app)])
    app.button(key="v3_s_see_done").click().run()
    am007 = next(r for r in app.session_state["v3_shop"]["results"]["results"] if r["unit_id"] == "AM-007")
    assert am007["match"]["level"] == "possible"
    assert "Zipper hasn't been checked yet" in am007["match"]["missing_evidence"]
    assert any("AM-007" in v["detail"] for v in app.session_state["v3_violations"])


def test_empty_results_offer_suggestions_that_update_preferences_and_results(app):
    plan = dict(DEMO, q_location=["neither"])
    results = to_results(app, plan)
    assert results["total"] == 0 and "No exact matches yet" in page_text(app)
    before = app.session_state["v3_shop"]["turn"]["context"]["context_version"]
    first = results["suggestions"][0]
    app.button(key=f"v3_s_sugg_0_{results['matched_against_context_version']}").click().run()
    after = app.session_state["v3_shop"]
    assert after["turn"]["context"]["context_version"] == before + 1
    assert after["results"]["matched_against_context_version"] == before + 1
    assert after["results"]["total"] == first["adds"] > 0


def test_stale_results_are_not_shown_for_new_preferences(app):
    to_results(app)
    shop = app.session_state["v3_shop"]
    old = shop["results"]["matched_against_context_version"]
    app.button(key="v3_s_edit_prefs").click().run()
    app.button(key="v3_s_edit_q_colour").click().run()   # change a preference: version moves on
    choose(app, "q_colour", ["taupe"])
    while question(app):
        choose(app, question(app), DEMO[question(app)])
    app.button(key="v3_s_see_done").click().run()
    fresh = app.session_state["v3_shop"]["results"]
    assert fresh["matched_against_context_version"] != old
    assert {r["unit_id"] for r in fresh["results"]} == {"AM-008"}
    assert "Corvo crossbody" not in page_text(app)


def test_agent_text_is_rendered_as_plain_text(app):
    class Shouty(MockAgentClient):
        def search_products(self, *args, **kwargs):
            response = super().search_products(*args, **kwargs)
            response["data"]["results"][0]["product"]["name"] = "<img src=x onerror=alert(1)>"
            return response

    open_assistant(app)
    app.session_state["v3_clients"]["shop"] = Shouty("shop", app.session_state["v3_overlay"])
    while question(app):
        choose(app, question(app), DEMO[question(app)])
    app.button(key="v3_s_see_done").click().run()
    from conftest import raw_page

    markup = raw_page(app)
    assert "<img src=x" not in markup and "&lt;img src=x onerror=alert(1)&gt;" in markup
