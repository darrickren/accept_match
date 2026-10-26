"""The V3 customer site, unchanged, against the real FastAPI backend (APP_MODE=mock) and DynamoDB Local.

One module-scoped backend, so the steps share state like a real demo:
return (cust-01) -> approval -> admin receive/inspect -> another customer (cust-02) finds and buys it.
"""

import json
import urllib.error
import urllib.request

from conftest import page_text, site

VIEWS = ["pair_top", "pair_outer", "pair_inner", "pair_soles", "pair_heels", "defect_closeup", "size_label"]
EXAMPLE = "Black ankle boots in US women's 7, under CA$100. Scratches are fine, but no stains."


def ok(at):
    assert not at.exception, [e.value for e in at.exception]
    return at


def photo(view, blurry=False):
    from backend.seed.images import render  # pure Pillow, importable from the frontend venv

    return render(view, "black", "scratch", seed=VIEWS.index(view) + 3, blurry=blurry)


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=30) as response:
        return json.loads(response.read())


def ask(at, text):
    turn = len(at.session_state["v3_shop"].get("history") or [])
    at.text_input(key=f"v3_s_api_text_{turn}").input(text).run()
    ok(at.button(key=f"v3_s_api_send_{turn}").click().run())


def buy(at, item_id):
    ok(at.button(key=f"v3_s_view_{item_id}").click().run())
    item = at.session_state["v3_shop"]["item"]["item"]
    for defect in item["defects"]:
        at.checkbox(key=f"v3_s_acc_{item_id}_v{item['listing_version']}_{defect['defect_id']}").check()
    ok(at.run())
    ok(at.button(key=f"v3_s_accept_{item_id}_v{item['listing_version']}").click().run())
    ok(at.button(key=f"v3_s_buy_{item_id}_v{item['listing_version']}").click().run())
    return item


def test_1_home_and_health(monkeypatch, stack):
    base, _, _ = stack
    at = site(monkeypatch, stack)
    assert get(base, "/health")["mode"] == "mock"
    assert {lb.proto.label for lb in at.get("link_button")} >= {"Shop ALDO ↗", "Shop the ALDO Sale ↗"}


def test_2_shopping_questions_and_items_come_from_the_backend(monkeypatch, stack):
    at = site(monkeypatch, stack)
    ok(at.button(key="am_home_buy").click().run())
    text = page_text(at)
    assert "What kind of shoes are you looking for?" in text and "Live service · simulated AI" in text
    ask(at, EXAMPLE)
    data = at.session_state["v3_shop"]
    assert [i["item_id"] for i in data["results"]["items"]] == ["ITM-001"]
    text = page_text(at)  # the backend's own understanding, with an Edit per answer
    assert "Your preferences" in text and "Under CA$100" in text and "1 piece fits so far" in text
    ok(at.button(key="v3_s_api_edit_size_1").click().run())
    turn = len(at.session_state["v3_shop"]["history"])
    assert at.text_input(key=f"v3_s_api_text_{turn}").value == "Change my size to "
    ok(at.button(key=f"v3_s_api_see_{data['chat']['request_id']}").click().run())
    text = page_text(at)
    assert "CA$69.00" in text and "US women's 7" in text and "Scratch · back of the left heel (grade A)" in text


def test_3_return_with_real_uploads_reaches_awaiting_receipt(monkeypatch, stack):
    base, _, _ = stack
    at = site(monkeypatch, stack)
    ok(at.button(key="am_home_return").click().run())
    at.text_input(key="v3_r_api_order").input("ORD-001")
    at.radio(key="v3_r_api_claim").set_value("DEFECT_CLAIM")
    at.selectbox(key="v3_r_api_reason").set_value("defect")
    ok(at.run())
    ok(at.button(key="v3_r_api_next_1").click().run())
    ok(at.button(key="v3_r_api_example").click().run())
    ok(at.button(key="v3_r_api_next_2").click().run())
    data = at.session_state["v3_return"]
    assert data["step"] == 3 and [s["view"] for s in data["required"]] == VIEWS
    for view in VIEWS:
        nonce = data.get("nonce", {}).get(view, 0)
        at.file_uploader(key=f"v3_r_api_up_{view}_{nonce}").upload(f"{view}.jpg", photo(view, view == "pair_soles"),
                                                                    "image/jpeg")
        ok(at.run())
    assert all(p["photo_id"].startswith("P-") for p in data["photos"].values())  # the backend's photo ids
    ok(at.button(key="v3_r_api_next_3").click().run())
    ok(at.button(key="v3_r_api_submit").click().run())
    text = page_text(at)
    assert "We need a few more photos." in text and "too blurry" in text
    nonce = data.get("nonce", {}).get("pair_soles", 0)
    at.file_uploader(key=f"v3_r_api_up_pair_soles_{nonce}").upload("soles.jpg", photo("pair_soles"), "image/jpeg")
    ok(at.run())
    ok(at.button(key="v3_r_api_send_more").click().run())
    text = page_text(at)
    assert "Approved in the demo review." in text and "not an actual ALDO refund approval" in text
    assert "FICTIONAL DEMO ADDRESS — DO NOT SHIP" in text and "Waiting for the item to arrive" in text
    assert "Photo analysis in this environment is simulated" in text
    view = data["return"]
    assert view["return_id"] == "RET-101" and view["item_id"] == "ITM-RET-101"
    ret_id = view["return_id"]
    ret_state = dict(data)
    ret_state["pending"] = "submit"  # a repeated Submit must not create a second case
    at.session_state["v3_return"] = ret_state
    ok(at.run())
    assert at.session_state["v3_return"]["return"]["return_id"] == ret_id
    try:  # approved but not inspected: not for sale yet
        get(base, "/api/items/ITM-RET-101")
        raise AssertionError("an uninspected unit was public")
    except urllib.error.HTTPError as exc:
        assert exc.code == 404


def test_4_inspection_publishes_and_another_customer_buys_exactly_once(monkeypatch, stack):
    base, _, admin_call = stack
    assert admin_call("POST", "/api/admin/items/ITM-RET-101/receive", {"simulated": True})["status"] == \
        "INSPECTION_PENDING"
    assert admin_call("POST", "/api/admin/items/ITM-RET-101/inspect",
                      {"outcome": "PASS", "notes": "demo", "simulated": True})["status"] == "AVAILABLE"
    at = site(monkeypatch, stack, shop_persona="cust-03")  # a different customer than the one who returned it
    ok(at.button(key="am_home_buy").click().run())
    ask(at, "Black ankle boots, US women's 7, under CA$100, scratches are fine")
    found = [i["item_id"] for i in at.session_state["v3_shop"]["results"]["items"]]
    assert "ITM-RET-101" in found
    ok(at.button(key=f"v3_s_api_see_{at.session_state['v3_shop']['chat']['request_id']}").click().run())
    item = buy(at, "ITM-RET-101")
    assert "Demo purchase confirmed." in page_text(at) and item["price"] == 70.95
    assert get(base, "/api/items/ITM-RET-101")["status"] == "SOLD"
    ok(at.run())  # reruns never repeat the purchase
    sold = admin_call("GET", "/api/admin/items?status=SOLD")
    assert [r["item_id"] for r in sold].count("ITM-RET-101") == 1


def test_5_status_page_and_sold_pair_cannot_be_bought_again(monkeypatch, stack):
    at = site(monkeypatch, stack)
    ok(at.button(key="am_home_return").click().run())
    at.session_state["v3_route"] = "return_status"
    ok(at.run())
    at.text_input(key="v3_r_api_status_id").input("RET-101")
    ok(at.button(key="v3_r_status_go").click().run())
    assert "Inspection complete" in page_text(at)
    at.session_state["v3_route"] = "home"
    ok(at.run())
    ok(at.button(key="am_home_buy").click().run())
    ok(at.button(key="v3_s_api_browse").click().run())
    ok(at.button(key="v3_s_b_go").click().run())
    listed = [i["item_id"] for i in at.session_state["v3_shop"]["results"]["items"]]
    assert "ITM-RET-101" not in listed and "ITM-001" in listed
