"""Mock Agent contract behaviour for Buy Defective (Task 2/3)."""
from ui.client.contracts import (compact_context, context_size, new_request_id, new_session_id, validate_envelope,
                                 SESSION_ID_RE)
from ui.client.mock_agent import MockAgentClient
from ui.client.mock_store import new_overlay


def client():
    return MockAgentClient("shop", new_overlay())


def answer(c, turn, values, sid="am-shop-" + "0" * 32):
    q = turn["question"]["question_id"]
    resp = c.submit_answer(sid, q, {"values": values}, turn["context"], new_request_id())
    assert not validate_envelope(resp) and resp["ok"], resp["error"]
    return resp["data"]["turn"]


def start(c):
    resp = c.start_conversation("am-shop-" + "0" * 32, new_request_id())
    assert resp["ok"]
    return resp["data"]["turn"]


def run_demo(c):
    """Crossbody, back only, minor scratches, zipper must work, any colour/style/price."""
    t = start(c)
    seen = []
    plan = {"q_category": ["crossbody"], "q_location": ["back"], "q_types": ["scratch"], "q_severity": ["minor"],
            "q_components": ["zipper"], "q_colour": ["any"], "q_style": ["any"], "q_price": ["any"]}
    while t["question"]:
        q = t["question"]["question_id"]
        assert q not in seen, f"asked {q} twice"
        seen.append(q)
        t = answer(c, t, plan[q])
    return t, seen


def test_session_ids_meet_agentcore_format():
    for kind in ("shop", "return", "status"):
        sid = new_session_id(kind)
        assert len(sid) == 40 and SESSION_ID_RE.fullmatch(sid)


def test_conversation_asks_each_question_once_and_ends_searchable():
    final, seen = run_demo(client())
    assert seen[:5] == ["q_category", "q_location", "q_types", "q_severity", "q_components"]
    assert final["question"] is None and final["can_search"]
    assert final["context"]["context_version"] == len(seen) + 1


def test_neither_skips_type_and_severity_questions():
    c = client()
    t = answer(c, start(c), ["crossbody"])
    t = answer(c, t, ["neither"])
    assert t["question"]["question_id"] == "q_components"


def test_responses_echo_request_id_and_bump_context_version():
    c = client()
    t = start(c)
    rid = new_request_id()
    resp = c.submit_answer(t["session_id"], "q_category", {"values": ["crossbody"]}, t["context"], rid)
    assert resp["meta"]["client_request_id"] == rid
    assert resp["data"]["turn"]["context"]["context_version"] == t["context"]["context_version"] + 1
    assert resp["data"]["turn"]["turn_id"] == "t_02"


def test_same_context_and_answer_give_the_same_turn():
    c1, c2 = client(), client()
    t1, t2 = start(c1), start(c2)
    a = answer(c1, t1, ["crossbody"])
    b = answer(c2, t2, ["crossbody"])
    assert a["question"] == b["question"] and a["match_count"] == b["match_count"]


def test_repeated_request_id_is_idempotent():
    c = client()
    t = start(c)
    rid = new_request_id()
    first = c.submit_answer(t["session_id"], "q_category", {"values": ["crossbody"]}, t["context"], rid)
    again = c.submit_answer(t["session_id"], "q_category", {"values": ["tote"]}, t["context"], rid)
    assert first == again


def test_answer_for_a_stale_question_is_refused():
    c = client()
    t = answer(c, start(c), ["crossbody"])
    resp = c.submit_answer(t["session_id"], "q_category", {"values": ["tote"]}, t["context"], new_request_id())
    assert not resp["ok"] and resp["error"]["code"] == "invalid_input"


def test_editing_an_early_answer_clears_its_dependants():
    c = client()
    final, _ = run_demo(c)
    ctx = final["context"]
    resp = c.update_preferences(final["session_id"], [{"question_id": "q_location", "values": ["both"]}], ctx,
                                new_request_id())
    t = resp["data"]["turn"]
    answers = t["context"]["answers"]
    assert answers["q_location"]["values"] == ["both"]
    assert "q_types" not in answers and "q_severity" not in answers   # depended on location
    assert answers["q_components"]["values"] == ["zipper"]            # did not
    assert t["question"]["question_id"] == "q_types"
    assert t["context"]["context_version"] == ctx["context_version"] + 1


def test_interpret_free_text_without_changing_context():
    c = client()
    t = answer(c, start(c), ["crossbody"])
    text = "A minor scratch on the back is fine. The front must be intact."
    resp = c.interpret_answer(t["session_id"], "q_location", text, t["context"], new_request_id())
    interpretation = resp["data"]["interpretation"]
    assert interpretation["values"] == ["back"] and interpretation["understood"]
    assert resp["data"]["context_version"] == t["context"]["context_version"]
    nonsense = c.interpret_answer(t["session_id"], "q_location", "purple elephant", t["context"], new_request_id())
    assert nonsense["data"]["interpretation"]["understood"] is False


def test_search_is_bound_to_the_context_version_and_ranks_verified_first():
    c = client()
    final, _ = run_demo(c)
    version = final["context"]["context_version"]
    resp = c.search_products(final["session_id"], final["context"]["preferences"], version, new_request_id())
    data = resp["data"]
    assert data["matched_against_context_version"] == version
    levels = [r["match"]["level"] for r in data["results"]]
    assert levels == sorted(levels, key=lambda lvl: lvl != "verified")
    units = {r["unit_id"]: r for r in data["results"]}
    assert set(units) == {"AM-006", "AM-007", "AM-008"}
    assert units["AM-007"]["match"]["level"] == "possible"
    assert units["AM-008"]["match"]["level"] == "verified" and units["AM-008"]["availability"]["purchasable"]
    assert units["AM-006"]["availability"]["purchasable"] is False  # published but reserved


def test_get_listing_returns_detail_bound_to_version():
    c = client()
    final, _ = run_demo(c)
    v = final["context"]["context_version"]
    resp = c.get_listing(final["session_id"], "LS-1007", final["context"]["preferences"], v, new_request_id())
    listing = resp["data"]["listing"]
    assert resp["data"]["matched_against_context_version"] == v
    assert listing["match"]["level"] == "possible" and listing["evidence"]
    assert c.get_listing(final["session_id"], "LS-1009", {}, v, new_request_id())["error"]["code"] == "not_found"


def test_empty_results_come_with_relaxation_suggestions():
    c = client()
    prefs = {"category": ["crossbody"], "tolerated_defects": [{"area": "back", "types": ["scuff"], "max_severity": "minor"}],
             "required_working": [], "colors": [], "styles": [], "price": {"min": None, "max": None, "currency": "CAD"},
             "notes": None}
    data = c.search_products("am-shop-" + "0" * 32, prefs, 3, new_request_id())["data"]
    assert data["total"] == 0
    labels = [s["label"] for s in data["suggestions"]]
    assert labels and all(s["adds"] > 0 for s in data["suggestions"])
    assert "Also accept minor scratches" in labels


def test_context_budget_is_enforced_deterministically():
    ctx = {"context_version": 3, "order": ["q_category"],
           "answers": {"q_category": {"values": ["crossbody"], "text": "x" * 5000}},
           "preferences": {"notes": "y" * 5000}}
    small = compact_context(ctx, max_bytes=2000)
    assert context_size(small) < 2000
    assert small == compact_context(ctx, max_bytes=2000)
    assert small["answers"]["q_category"]["values"] == ["crossbody"]
