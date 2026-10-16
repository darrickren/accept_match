"""api mode end to end through the real app (AppTest) against the fake FastAPI server (tests/ui/fake_api.py).

These prove the site's behaviour against the plan's contract. They are not
tests of the real backend or of a real AI model.
"""
import pytest
from conftest import api_app, page_text
from fake_api import jpeg

EXAMPLE = "Black ankle boots in US women's 7, under CA$100. Scratches are fine, but no stains."


def shop(at):
    return at.session_state["v3_shop"]


def ret(at):
    return at.session_state["v3_return"]


def turn(data):
    return len(data.get("history") or [])


def ok(at):
    assert not at.exception, [e.value for e in at.exception]
    return at


def ask(at, text):
    t = turn(shop(at))
    at.text_input(key=f"v3_s_api_text_{t}").input(text).run()
    ok(at.button(key=f"v3_s_api_send_{t}").click().run())


def start_shopping(at, text=EXAMPLE):
    ok(at.button(key="am_home_buy").click().run())
    ask(at, text)
    chat = shop(at)["chat"]
    ok(at.button(key=f"v3_s_api_see_{chat['request_id']}").click().run())


def open_item(at, item_id):
    ok(at.button(key=f"v3_s_view_{item_id}").click().run())
    return shop(at)["item"]["item"]


def accept_all(at, item):
    for defect in item["defects"]:
        at.checkbox(key=f"v3_s_acc_{item['item_id']}_v{item['listing_version']}_{defect['defect_id']}").check()
    at.run()
    ok(at.button(key=f"v3_s_accept_{item['item_id']}_v{item['listing_version']}").click().run())


# ----------------------------------------------------------------- home and modes

def test_home_works_without_any_backend_call(monkeypatch, fake_api):
    backend, url = fake_api
    at = api_app(monkeypatch, url)
    links = {lb.proto.label: lb.proto.url for lb in at.get("link_button")}
    assert links["Shop ALDO ↗"] == "https://www.aldoshoes.com/en-ca"
    assert links["Shop the ALDO Sale ↗"] == "https://www.aldoshoes.com/en-ca/collections/sale"
    assert backend.requests == [] and at.session_state["v3_clients"] == {}


def test_missing_base_url_is_a_visible_config_error_never_the_mock(monkeypatch, fake_api):
    _, url = fake_api
    at = api_app(monkeypatch, url, ACCEPTMATCH_API_BASE_URL=None)
    ok(at.button(key="am_home_buy").click().run())
    assert "isn't available right now" in page_text(at)
    assert "shop" not in at.session_state["v3_clients"]
    assert "Demo assistant · scripted" not in page_text(at)


def test_unreachable_backend_shows_an_error_and_a_retry(monkeypatch):
    at = api_app(monkeypatch, "http://127.0.0.1:9")  # nothing listens on the discard port
    ok(at.button(key="am_home_buy").click().run())
    assert "isn't reachable right now" in page_text(at)
    assert at.session_state["v3_clients"]["shop"].kind == "api"
    assert at.button(key="v3_s_retry_start")


def test_journeys_can_mix_api_and_mock(monkeypatch, fake_api):
    backend, url = fake_api
    at = api_app(monkeypatch, url, ACCEPTMATCH_AGENT=None, ACCEPTMATCH_AGENT_SHOP="api",
                 ACCEPTMATCH_AGENT_RETURN="mock")
    ok(at.button(key="am_home_return").click().run())
    assert "Demo assistant · scripted" in page_text(at)
    assert at.button(key="v3_r_next_1")  # the mock's bag form
    at.session_state["v3_route"] = "home"
    at.run()
    start_shopping(at)
    assert at.session_state["v3_clients"]["shop"].kind == "api"
    assert "return" not in at.session_state["v3_clients"]  # the mock client is created on its first call
    assert backend.calls("POST", "/api/sessions")


# ----------------------------------------------------------------- Buy Defective

def test_backend_questions_are_rendered_and_answers_are_sent_as_messages(monkeypatch, fake_api):
    backend, url = fake_api
    at = api_app(monkeypatch, url)
    ok(at.button(key="am_home_buy").click().run())
    ask(at, "Something black for work")
    text = page_text(at)
    assert "Which size do you wear?" in text and "What's your budget?" in text
    assert "Live service · simulated AI" in text
    t = turn(shop(at))
    ok(at.button(key=f"v3_s_api_opt_size_0_{t}").click().run())  # "US women's 7" (budget is optional)
    assert "Which imperfections are fine with you?" in page_text(at)
    t = turn(shop(at))
    assert at.button(key=f"v3_s_api_continue_defects_{t}").disabled
    ok(at.button(key=f"v3_s_api_opt_defects_0_{t}").click().run())  # toggle "Scratches"
    ok(at.button(key=f"v3_s_api_continue_defects_{t}").click().run())
    sent = [r for r in backend.requests if r["path"].endswith("/messages")]
    assert len(sent) == 3
    assert [i["item_id"] for i in shop(at)["results"]["items"]] == ["ITM-001", "ITM-005"]
    assert [row["text"] for row in shop(at)["history"] if row["who"] == "you"] == [
        "Something black for work", "US women's 7", "Scratches"]


def test_items_come_from_the_api_with_footwear_details_in_cad(monkeypatch, fake_api):
    _, url = fake_api
    at = api_app(monkeypatch, url)
    start_shopping(at)
    text = page_text(at)
    assert "2 pieces fit what you told us" in text
    assert "Matches" in text and "Needs your confirmation" in text
    assert "CA$69.00" in text and "US women's 7" in text and "Scratch · back of the left heel (grade A)" in text
    assert "ITM-003" not in text  # SOLD pairs are never offered
    assert at.session_state["v3_route"] == "results"


def test_earlier_matches_are_dropped_when_the_conversation_moves_on(monkeypatch, fake_api):
    backend, url = fake_api
    at = api_app(monkeypatch, url)
    start_shopping(at)
    assert shop(at)["results"]["items"]
    session = next(iter(backend.sessions.values()))
    session["prefs"].pop("size")  # the fake now asks for the size again, with no items in that turn
    ok(at.button(key="v3_s_back_shop").click().run())
    ask(at, "Actually, what about other sizes?")
    assert "results" not in shop(at)
    assert not [b for b in at.button if str(b.key).startswith("v3_s_api_see_")]
    assert "Which size do you wear?" in page_text(at)


def test_no_match_is_honest_and_shows_what_blocks_it(monkeypatch, fake_api):
    _, url = fake_api
    at = api_app(monkeypatch, url)
    start_shopping(at, "US men's 9 under CA$50, no scuffs")
    text = page_text(at)
    assert "No exact matches yet" in text and "Under CA$50" in text and "No scuff" in text
    assert not [b for b in at.button if str(b.key).startswith("v3_s_view_")]


def test_every_imperfection_must_be_ticked_and_nothing_is_preaccepted(monkeypatch, fake_api):
    backend, url = fake_api
    at = api_app(monkeypatch, url)
    start_shopping(at)
    item = open_item(at, "ITM-005")  # scuff + discoloration: preferences never covered these
    keys = [f"v3_s_acc_ITM-005_v1_{d['defect_id']}" for d in item["defects"]]
    assert [at.checkbox(key=k).value for k in keys] == [False, False]
    assert at.button(key="v3_s_buy_ITM-005_v1").disabled
    assert at.button(key="v3_s_accept_ITM-005_v1").disabled
    at.checkbox(key=keys[0]).check().run()
    assert at.button(key="v3_s_accept_ITM-005_v1").disabled  # one of two isn't enough
    assert backend.calls("POST", "/api/items/") == []


def test_accept_then_buy_once_even_when_the_purchase_is_repeated(monkeypatch, fake_api):
    backend, url = fake_api
    at = api_app(monkeypatch, url)
    start_shopping(at)
    item = open_item(at, "ITM-001")
    assert "Every imperfection on this pair" in page_text(at) and "listing version 1" in page_text(at)
    accept_all(at, item)
    assert backend.acceptances[("cust-02", "ITM-001", 1)] == {"D-001"}
    ok(at.button(key="v3_s_buy_ITM-001_v1").click().run())
    text = page_text(at)
    assert "Demo purchase confirmed." in text and "no payment was taken" in text
    assert shop(at)["item"]["item"]["status"] == "SOLD"  # reloaded from the backend, not set by the page
    assert not [b for b in at.button if str(b.key).startswith("v3_s_buy_")]  # no second Buy
    ok(at.run())
    assert len(backend.purchases) == 1 and len(backend.calls("POST", "/api/demo-checkout")) == 1


def test_stale_listing_version_requires_a_fresh_confirmation(monkeypatch, fake_api):
    backend, url = fake_api
    at = api_app(monkeypatch, url)
    start_shopping(at)
    accept_all(at, open_item(at, "ITM-001"))
    backend.bump_listing("ITM-001")
    ok(at.button(key="v3_s_buy_ITM-001_v1").click().run())
    text = page_text(at)
    assert "details changed since you looked" in text and "listing version 2" in text
    assert at.checkbox(key="v3_s_acc_ITM-001_v2_D-001").value is False
    assert at.button(key="v3_s_buy_ITM-001_v2").disabled and backend.purchases == {}


def test_a_pair_sold_elsewhere_cannot_be_bought(monkeypatch, fake_api):
    backend, url = fake_api
    at = api_app(monkeypatch, url)
    start_shopping(at)
    accept_all(at, open_item(at, "ITM-001"))
    backend.sell("ITM-001")
    ok(at.button(key="v3_s_buy_ITM-001_v1").click().run())
    text = page_text(at)
    assert "no longer available" in text and "can't be bought right now (sold)" in text
    assert not [b for b in at.button if str(b.key).startswith("v3_s_buy_ITM-001")]
    assert backend.purchases == {}


def test_checkout_timeout_then_retry_confirms_the_same_single_purchase(monkeypatch, fake_api):
    backend, url = fake_api
    at = api_app(monkeypatch, url, ACCEPTMATCH_API_TIMEOUT="1")
    start_shopping(at)
    accept_all(at, open_item(at, "ITM-001"))
    backend.fault("POST", "/api/demo-checkout", delay=1.6)  # the backend finishes after the site gives up
    ok(at.button(key="v3_s_buy_ITM-001_v1").click().run())
    text = page_text(at)
    assert "took too long" in text and "can't buy twice" in text
    import time

    time.sleep(0.8)
    ok(at.button(key="v3_s_buy_retry_ITM-001").click().run())
    assert "Demo purchase confirmed." in page_text(at)
    assert len(backend.purchases) == 1
    assert len({r["idempotency_key"] for r in backend.calls("POST", "/api/demo-checkout")}) == 1


def test_missing_checkout_endpoint_is_labelled_pending_integration(monkeypatch, fake_api):
    backend, url = fake_api
    backend.remove_route("POST", "/api/demo-checkout")
    at = api_app(monkeypatch, url)
    start_shopping(at)
    open_item(at, "ITM-001")
    assert at.button(key="v3_s_buy_pending").disabled
    assert "pending integration" in at.button(key="v3_s_buy_pending").label


def test_browse_lists_the_same_inventory_with_paging(monkeypatch, fake_api):
    backend, url = fake_api
    at = api_app(monkeypatch, url)
    ok(at.button(key="am_home_buy").click().run())
    ok(at.button(key="v3_s_api_browse").click().run())
    ok(at.button(key="v3_s_b_go").click().run())
    first = [i["item_id"] for i in shop(at)["results"]["items"]]
    assert first == ["ITM-001", "ITM-002"]
    ok(at.button(key="v3_s_api_more_2").click().run())
    assert [i["item_id"] for i in shop(at)["results"]["items"]] == ["ITM-001", "ITM-002", "ITM-005"]


def test_ai_unavailable_during_chat_is_clear_and_retry_reuses_the_request(monkeypatch, fake_api):
    backend, url = fake_api
    at = api_app(monkeypatch, url)
    ok(at.button(key="am_home_buy").click().run())
    body = {"request_id": "req-x", "code": "AI_UNAVAILABLE", "message": "Bedrock throttled", "retryable": True}
    backend.fault("POST", r"/api/sessions/[^/]+/messages", status=503, body=body, times=2)
    ask(at, "hello")
    assert "unavailable right now" in page_text(at) and "Bedrock" not in page_text(at)
    pending = shop(at)["pending_send"]
    ok(at.button(key="v3_s_api_retry").click().run())
    sent = [r["x_request_id"] for r in backend.requests if r["path"].endswith("/messages")]
    assert sent[-1] == pending["rid"] and "Which size do you wear?" in page_text(at)


def test_real_ai_label_needs_a_model_id_from_the_backend(monkeypatch, fake_api):
    backend, url = fake_api
    backend.analysis_source, backend.model_id = "BEDROCK", None
    at = api_app(monkeypatch, url)
    ok(at.button(key="am_home_buy").click().run())
    ask(at, "hello")
    assert "Live service · AI not confirmed" in page_text(at)
    backend.model_id = "us.amazon.nova-2-lite-v1:0"
    ask(at, "US women's 7")
    assert "AI-assisted · us.amazon.nova-2-lite-v1" in page_text(at)


# ----------------------------------------------------------------- Return

def fill_order(at, order="ORD-001", claim="DEFECT_CLAIM"):
    ok(at.button(key="am_home_return").click().run())
    at.text_input(key="v3_r_api_order").input(order)
    at.radio(key="v3_r_api_claim").set_value(claim)
    at.selectbox(key="v3_r_api_reason").set_value("defect")
    ok(at.run())
    ok(at.button(key="v3_r_api_next_1").click().run())


def describe_and_answer(at):
    ok(at.button(key="v3_r_api_example").click().run())
    ok(at.button(key="v3_r_api_next_2").click().run())
    assert "Have they been worn outdoors?" in page_text(at)
    ok(at.button(key=f"v3_r_api_opt_worn_0_{turn(ret(at))}").click().run())  # "Only indoors"


def upload(at, view, size=(900, 700)):
    nonce = ret(at).get("nonce", {}).get(view, 0)
    data = jpeg((70, 70, 76), size)
    ok(at.file_uploader(key=f"v3_r_api_up_{view}_{nonce}").upload(f"{view}.jpg", data, "image/jpeg").run())


def submit_return(at, blurry=()):
    for slot in ret(at)["required"]:
        upload(at, slot["view"], (200, 150) if slot["view"] in blurry else (900, 700))
    ok(at.button(key="v3_r_api_next_3").click().run())
    ok(at.button(key="v3_r_api_submit").click().run())


def test_return_questions_photos_more_evidence_then_demo_approval(monkeypatch, fake_api):
    backend, url = fake_api
    at = api_app(monkeypatch, url)
    fill_order(at)
    describe_and_answer(at)
    assert ret(at)["step"] == 3
    views = [s["view"] for s in ret(at)["required"]]
    assert views == ["pair_top", "pair_soles", "pair_heels", "defect_closeup", "size_label"]
    assert "Soles" in page_text(at) and "We can't see both soles." in page_text(at)
    assert at.button(key="v3_r_api_next_3").disabled
    submit_return(at, blurry=("pair_soles",))
    photo_ids = {p["photo_id"] for p in ret(at)["photos"].values() if p.get("photo_id")}
    assert photo_ids and photo_ids <= set(backend.photos)  # the backend's ids, from real uploads
    text = page_text(at)
    assert "We need a few more photos." in text and "too blurry" in text
    assert at.button(key="v3_r_api_send_more").disabled
    upload(at, "pair_soles")
    ok(at.button(key="v3_r_api_send_more").click().run())
    text = page_text(at)
    assert "Approved in the demo review." in text and "not an actual ALDO refund approval" in text
    assert "FICTIONAL DEMO ADDRESS — DO NOT SHIP" in text and "Waiting for the item to arrive" in text
    assert "Photo analysis in this environment is simulated" in text
    assert len(backend.returns) == 1
    return_id = ret(at)["return"]["return_id"]
    ok(at.button(key="v3_r_to_status").click().run())
    ok(at.button(key="v3_r_status_go").click().run())
    assert return_id in page_text(at) and "Approved in the demo review · send your item" in page_text(at)


def test_resubmitting_never_creates_a_second_case(monkeypatch, fake_api):
    backend, url = fake_api
    at = api_app(monkeypatch, url)
    fill_order(at)
    describe_and_answer(at)
    submit_return(at)
    calls = len(backend.requests)
    ret(at)["pending"] = "submit"  # a second Submit (double click or rerun)
    ok(at.run())
    assert len(backend.returns) == 1 and len(backend.requests) == calls  # no new case, no re-evaluation
    creates = [r for r in backend.calls("POST", "/api/returns") if r["path"] == "/api/returns"]
    assert len(creates) == 1 and creates[0]["idempotency_key"]


@pytest.mark.parametrize("order,headline,note,label", [
    ("ORD-002", "A specialist will review your request.", "simulated review queue",
     "With a specialist (simulated review)"),
    ("ORD-009", "Your request wasn't accepted after review.", "not an ALDO decision",
     "Not accepted after review (demo)"),
])
def test_human_review_and_rejection_are_shown_as_demo_outcomes(monkeypatch, fake_api, order, headline, note, label):
    _, url = fake_api
    at = api_app(monkeypatch, url)
    fill_order(at, order=order)
    describe_and_answer(at)
    submit_return(at)
    text = page_text(at)
    assert headline in text and note in text and "FICTIONAL DEMO ADDRESS" not in text
    ok(at.button(key="v3_r_to_status").click().run())
    ok(at.button(key="v3_r_status_go").click().run())
    assert label in page_text(at)


def test_ai_unavailable_and_timeouts_during_review_are_clear_and_retryable(monkeypatch, fake_api):
    backend, url = fake_api
    at = api_app(monkeypatch, url, ACCEPTMATCH_API_VISION_TIMEOUT="1", ACCEPTMATCH_API_MAX_RETRIES="0")
    fill_order(at)
    describe_and_answer(at)
    backend.fault("POST", r"/api/returns/[^/]+/evaluate", status=503,
                  body={"request_id": "r", "code": "AI_UNAVAILABLE", "message": "x", "retryable": True})
    submit_return(at)
    text = page_text(at)
    assert "Your request is saved. The review didn't finish." in text and "unavailable right now" in text
    backend.fault("POST", r"/api/returns/[^/]+/evaluate", delay=1.5)
    ok(at.button(key="v3_r_api_retry_eval").click().run())
    assert "took too long" in page_text(at)
    import time

    time.sleep(0.7)
    ok(at.button(key="v3_r_api_retry_eval").click().run())
    assert "Approved in the demo review." in page_text(at) and len(backend.returns) == 1


def test_unknown_order_shows_the_backend_message_and_keeps_the_form(monkeypatch, fake_api):
    _, url = fake_api
    at = api_app(monkeypatch, url)
    fill_order(at, order="ORD-404")
    describe_and_answer(at)
    submit_return(at)
    assert "Order ORD-404 was not found." in page_text(at)
    assert ret(at).get("return") is None


# ----------------------------------------------------------------- diagnostics and secrets

def test_diagnostics_show_both_mode_layers_and_never_secrets(monkeypatch, fake_api):
    backend, url = fake_api
    at = api_app(monkeypatch, url, ACCEPTMATCH_DIAGNOSTICS="1")
    start_shopping(at)
    accept_all(at, open_item(at, "ITM-001"))
    ok(at.button(key="v3_s_buy_ITM-001_v1").click().run())
    text = page_text(at)
    assert "Frontend client: api" in text and "Backend APP_MODE: mock" in text and "analysis_source: SIMULATED" in text
    tokens = list(backend.tokens)
    dumped = text + repr(at.session_state["v3_calls"]) + repr(at.session_state["v3_violations"]) + \
        repr(at.session_state["v3_trace"]) + repr(at.session_state["v3_clients"]["shop"].http.recent_calls())
    for secret in tokens + ["pw-cust-02", "pw-cust-01"]:
        assert secret not in dumped
