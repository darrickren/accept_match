"""Return journey through the real app (AppTest), including session isolation."""
import io

from conftest import ROOT, page_text
from PIL import Image

EXAMPLE = "The zipper doesn't close properly, and there's a small scratch on the back of the bag."


def jpeg(colour=(40, 40, 46)):
    buf = io.BytesIO()
    Image.new("RGB", (900, 700), colour).save(buf, "JPEG")
    return buf.getvalue()


def shared(at):
    return at.session_state["v3_return"]


def fill_item(at, category="crossbody", reason="damaged", order=""):
    at.button(key="am_home_return").click().run()
    at.segmented_control(key="v3_r_category").set_value(category).run()
    if order:
        at.text_input(key="v3_r_order").input(order).run()
    at.selectbox(key="v3_r_reason").set_value(reason).run()
    at.button(key="v3_r_next_1").click().run()


def upload(at, slot):
    nonce = shared(at).get("nonce", {}).get(slot, 0)
    at.file_uploader(key=f"v3_r_up_{slot}_{nonce}").upload(f"{slot}.jpg", jpeg(), "image/jpeg").run()
    assert not at.exception, [e.value for e in at.exception]


def submit_example(at, skip=()):
    fill_item(at)
    assert shared(at)["step"] == 2
    at.text_area(key="v3_r_desc").input(EXAMPLE).run()
    at.button(key="v3_r_next_2").click().run()
    assert shared(at)["step"] == 3
    for slot in shared(at)["requirements"]["slots"]:
        if slot["required"] and slot["slot"] not in skip:
            upload(at, slot["slot"])
    if skip:
        shared(at)["step"] = 4  # the page blocks this; tests force it to exercise the server-side rule
        at.run()
    else:
        at.button(key="v3_r_next_3").click().run()
    at.button(key="v3_r_submit").click().run()
    assert not at.exception, [e.value for e in at.exception]
    return shared(at)


def test_item_step_validates_and_refuses_real_looking_order_numbers(app):
    fill_item(app, order="4500012345")
    assert shared(app)["step"] == 1
    assert "demo order IDs" in page_text(app)


def test_photo_checklist_adapts_and_continue_waits_for_required_photos(app):
    fill_item(app)
    app.text_area(key="v3_r_desc").input(EXAMPLE).run()
    app.button(key="v3_r_next_2").click().run()
    slots = [s["slot"] for s in shared(app)["requirements"]["slots"]]
    assert "issue-zipper" in slots and "issue-back" in slots
    assert app.button(key="v3_r_next_3").disabled
    upload(app, "front")
    assert shared(app)["photos"]["front"]["status"] == "accepted"
    app.button(key="v3_r_remove_front").click().run()
    assert "front" not in shared(app)["photos"]


def test_approved_return_end_to_end_then_status_lookup(app):
    data = submit_example(app)
    assert data["step"] == "outcome" and data["view"]["assessment_status"] == "approved"
    text = page_text(app)
    assert "Your return is approved." in text and data["case"]["case_id"] in text
    assert "no shipping label or refund is created" in text
    app.button(key="v3_r_to_status").click().run()
    app.button(key="v3_r_status_go").click().run()
    assert "Approved · ship your item" in page_text(app)


def test_double_submit_creates_one_case(app):
    data = submit_example(app)
    case_id = data["case"]["case_id"]
    shared(app)["pending"] = "submit"
    app.run()
    assert shared(app)["case"]["case_id"] == case_id
    assert len(app.session_state["v3_overlay"]["returns"]) == 1


def test_missing_photo_leads_to_action_needed_and_recovers(app):
    data = submit_example(app, skip=("issue-zipper",))
    assert data["view"]["assessment_status"] == "needs_more_info"
    assert "We need a little more to decide." in page_text(app)
    upload(app, "issue-zipper")
    app.button(key="v3_r_send_more").click().run()
    assert shared(app)["view"]["assessment_status"] == "approved"


def test_two_browser_sessions_never_share_returns_or_photos(app):
    from streamlit.testing.v1 import AppTest

    data = submit_example(app)
    other = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60)
    other.run()
    other.session_state["v3_route"] = "return_status"
    other.run()
    other.text_input(key="v3_r_status_case").input(data["case"]["case_id"]).run()
    other.text_input(key="v3_r_status_code").input(data["case"]["status_token"]).run()
    other.button(key="v3_r_status_go").click().run()
    text = page_text(other)
    assert "couldn't find" in text and "only kept in the browser session" in text
    assert other.session_state["v3_overlay"]["photos"] == {}
    assert other.session_state["v3_overlay"]["returns"] == {}
