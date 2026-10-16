"""Buy Defective assistant through the real Streamlit app (AppTest)."""
from conftest import page_text

DEMO = {"q_category": ["crossbody"], "q_location": ["back"], "q_types": ["scratch"], "q_severity": ["minor"],
        "q_components": ["zipper"], "q_colour": ["any"], "q_style": ["any"], "q_price": ["any"]}


def turn(at):
    return at.session_state["v3_shop"]["turn"]


def question(at):
    q = turn(at)["question"]
    return q["question_id"] if q else None


def version(at):
    return turn(at)["context"]["context_version"]


def choose(at, qid, values):
    q = turn(at)["question"]
    v = version(at)
    for value in values:
        at.button(key=f"v3_s_opt_{qid}_{value}_{v}").click().run()
    if q["type"] == "multi_choice":
        at.button(key=f"v3_s_continue_{qid}_{v}").click().run()
    assert not at.exception, [e.value for e in at.exception]


def open_assistant(app):
    app.button(key="am_home_buy").click().run()
    assert question(app) == "q_category"
    return app


def complete_demo(app):
    open_assistant(app)
    asked = []
    while question(app):
        qid = question(app)
        assert qid not in asked
        asked.append(qid)
        choose(app, qid, DEMO[qid])
    return asked


def test_one_question_at_a_time_through_to_results(app):
    asked = complete_demo(app)
    assert asked[:5] == ["q_category", "q_location", "q_types", "q_severity", "q_components"]
    assert "Demo assistant · scripted" in page_text(app)
    app.button(key="v3_s_see_done").click().run()
    assert app.session_state["v3_route"] == "results"


def test_free_text_is_confirmed_before_it_counts(app):
    open_assistant(app)
    choose(app, "q_category", ["crossbody"])
    v = version(app)
    app.text_input(key=f"v3_s_text_q_location_{v}").input("A minor scratch on the back is fine. The front must be "
                                                          "intact.").run()
    app.button(key=f"v3_s_send_q_location_{v}").click().run()
    assert question(app) == "q_location"  # nothing applied yet
    assert "We understood:" in page_text(app) and "Back only" in page_text(app)
    app.button(key=f"v3_s_use_q_location_{v}").click().run()
    assert question(app) == "q_types"
    assert turn(app)["context"]["answers"]["q_location"]["values"] == ["back"]


def test_unparseable_text_asks_to_pick_an_option(app):
    open_assistant(app)
    v = version(app)
    app.text_input(key=f"v3_s_text_q_category_{v}").input("something purple").run()
    app.button(key=f"v3_s_send_q_category_{v}").click().run()
    assert "couldn't match that" in page_text(app)
    assert question(app) == "q_category"


def test_edit_reasks_and_clears_dependent_answers(app):
    complete_demo(app)
    app.button(key="v3_s_edit_q_location").click().run()
    assert question(app) == "q_location"
    answers = turn(app)["context"]["answers"]
    assert "q_types" not in answers and "q_severity" not in answers and "q_location" not in answers
    assert answers["q_components"]["values"] == ["zipper"]
    choose(app, "q_location", ["both"])
    assert question(app) == "q_types"


def test_back_returns_to_the_previous_question(app):
    open_assistant(app)
    choose(app, "q_category", ["crossbody"])
    choose(app, "q_location", ["back"])
    assert question(app) == "q_types"
    app.button(key=f"v3_s_prev_{version(app)}").click().run()
    assert question(app) == "q_location"


def test_errors_keep_the_customers_input(app):
    class Broken:
        source = "mock"

        def interpret_answer(self, *args, **kwargs):
            raise RuntimeError("backend down")

    open_assistant(app)
    v = version(app)
    key = f"v3_s_text_q_category_{v}"
    app.text_input(key=key).input("a tote please").run()
    app.session_state["v3_clients"]["shop"] = Broken()
    app.button(key=f"v3_s_send_q_category_{v}").click().run()
    assert "Something went wrong" in page_text(app)
    assert app.text_input(key=key).value == "a tote please"
    assert question(app) == "q_category"


def test_stale_turn_is_discarded(app):
    from ui.client.mock_agent import MockAgentClient

    class Stale(MockAgentClient):
        def submit_answer(self, *args, **kwargs):
            response = super().submit_answer(*args, **kwargs)
            response["data"]["turn"]["context"]["context_version"] += 5
            return response

    open_assistant(app)
    real = app.session_state["v3_clients"]["shop"]
    app.session_state["v3_clients"]["shop"] = Stale("shop", app.session_state["v3_overlay"])
    choose(app, "q_category", ["crossbody"])
    assert question(app) == "q_category"
    assert any(v["kind"] == "stale_response" for v in app.session_state["v3_violations"])
    app.session_state["v3_clients"]["shop"] = real
