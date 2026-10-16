"""Return journey: checklist, photo handling, two-phase submission, decisions and status (mock)."""
import datetime as dt
import hashlib
import io

import pytest
from PIL import ExifTags, Image

from ui import images
from ui.client.contracts import new_request_id, new_session_id
from ui.client.mock_agent import MockAgentClient
from ui.client.mock_returns import requirements
from ui.client.mock_store import new_overlay
from ui.status import NEXT_UPDATE, customer_status

EXAMPLE = "The zipper doesn't close properly, and there's a small scratch on the back of the bag."


def jpeg(w=800, h=600, colour=(40, 40, 46), exif=None):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), colour).save(buf, "JPEG", exif=exif) if exif else \
        Image.new("RGB", (w, h), colour).save(buf, "JPEG")
    return buf.getvalue()


class Flow:
    """Drives the mock Agent the way the page does."""

    def __init__(self, description=EXAMPLE, product=None, reason="damaged"):
        self.c = MockAgentClient("return", new_overlay())
        self.sid = new_session_id("return")
        self.product = product or {"category": "crossbody", "name": None, "order_ref": None, "purchase_date": None}
        self.reason, self.description = reason, description
        self.reqs = self.c.get_return_requirements(self.sid, self.product["category"], reason, description,
                                                   new_request_id())["data"]
        self.photos = {}

    def upload(self, slot, raw=None, declared_sha=None):
        clean, info = images.prepare_upload(raw or jpeg(), 10, 2048)
        created = self.c.create_photo_upload(self.sid, self.sid, slot, "image/jpeg", info["bytes"],
                                             declared_sha or info["sha256"], new_request_id())
        assert created["ok"], created["error"]
        self.c.put_upload(created["data"]["upload_id"], clean)
        done = self.c.complete_photo_upload(self.sid, created["data"]["upload_id"], new_request_id())["data"]
        if done["validation_status"] == "accepted":
            self.photos[slot] = done["photo_id"]
        return done

    def upload_required(self, skip=()):
        for slot in self.reqs["slots"]:
            if slot["required"] and slot["slot"] not in skip:
                self.upload(slot["slot"])

    def submit(self, rid=None):
        resp = self.c.submit_return(self.sid, self.sid, self.product, self.reason, self.description,
                                    list(self.photos.values()), rid or new_request_id())
        assert resp["ok"], resp["error"]
        self.case = resp["data"]
        return self.case

    def assess(self):
        resp = self.c.assess_return(self.sid, self.case["case_id"], self.case["status_token"], new_request_id())
        assert resp["ok"], resp["error"]
        return resp["data"]


def test_checklist_adapts_to_the_description_and_category():
    reqs = requirements("crossbody", "damaged", EXAMPLE, 8)
    slots = {s["slot"]: s for s in reqs["slots"]}
    assert {"front", "back", "issue-zipper", "issue-back"} <= {k for k, s in slots.items() if s["required"]}
    assert slots["part-strap"]["required"] is False and slots["label"]["required"] is False
    assert reqs["min_photos"] == 4
    tote = requirements("tote", "damaged", "A handle is coming loose", 8)
    assert "issue-handles" in {s["slot"] for s in tote["slots"]}
    mind = requirements("tote", "changed_mind", "I just don't like it", 8)
    assert [s["slot"] for s in mind["slots"] if s["required"]] == ["front", "back"]


def test_upload_cleanup_removes_metadata_including_gps_and_fixes_orientation():
    exif = Image.Exif()
    exif[ExifTags.Base.Orientation] = 6
    exif[ExifTags.Base.Model] = "TestPhone"
    exif[ExifTags.IFD.GPSInfo] = {ExifTags.GPS.GPSLatitudeRef: "N", ExifTags.GPS.GPSLatitude: (45.0, 30.0, 1.0)}
    raw = jpeg(400, 200, exif=exif)
    assert Image.open(io.BytesIO(raw)).getexif().get_ifd(ExifTags.IFD.GPSInfo)
    clean, info = images.prepare_upload(raw, 10, 2048)
    out = Image.open(io.BytesIO(clean))
    assert dict(out.getexif()) == {} and "exif" not in out.info
    assert out.size == (200, 400)  # orientation applied before metadata was dropped
    assert info["sha256"] == hashlib.sha256(clean).hexdigest()


def test_upload_cleanup_rejects_bad_files(monkeypatch):
    with pytest.raises(images.PhotoRejected):
        images.prepare_upload(b"", 10, 2048)
    with pytest.raises(images.PhotoRejected):
        images.prepare_upload(b"%PDF-1.4 not an image", 10, 2048)
    gif = io.BytesIO()
    Image.new("RGB", (10, 10)).save(gif, "GIF")
    with pytest.raises(images.PhotoRejected, match="JPG, PNG or WebP"):
        images.prepare_upload(gif.getvalue(), 10, 2048)
    with pytest.raises(images.PhotoRejected, match="under"):
        images.prepare_upload(jpeg(), 0.001, 2048)
    monkeypatch.setattr(images, "MAX_DECODE_PIXELS", 1000)
    with pytest.raises(images.PhotoRejected, match="resolution"):
        images.prepare_upload(jpeg(100, 100), 10, 2048)


def test_large_photos_are_resized():
    clean, info = images.prepare_upload(jpeg(4000, 3000), 10, 2048)
    assert max(info["width"], info["height"]) == 2048


def test_server_side_validation_rejects_tampered_or_unexpected_uploads():
    flow = Flow()
    assert flow.upload("front", declared_sha="0" * 64)["validation_status"] == "rejected"   # checksum mismatch
    assert flow.upload("not-a-slot")["validation_status"] == "rejected"                     # unknown slot
    created = flow.c.create_photo_upload(flow.sid, flow.sid, "front", "image/png", 100, "x", new_request_id())
    assert not created["ok"] and created["error"]["code"] == "photo_rejected"               # only clean JPEGs
    flow.c._settings = type("S", (), {"session_photo_budget_mb": 0.0001, "photo_max_px": 2048, "photo_max_mb": 10,
                                      "return_photos_max": 8})()
    assert "remove one" in flow.upload("back")["reason"]                                     # session budget


def test_approved_return_gets_id_code_and_simulated_shipping():
    flow = Flow()
    flow.upload_required()
    case = flow.submit()
    assert case["assessment_status"] == "pending" and case["case_id"].startswith("RET-")
    view = flow.assess()
    assert view["assessment_status"] == "approved" and view["logistics_status"] == "awaiting_receipt"
    assert view["shipping"]["simulated"] is True
    assert customer_status(view) == "Approved · ship your item"


def test_missing_required_photo_asks_for_more_then_approves_after_it_is_added():
    flow = Flow()
    flow.upload_required(skip=("issue-zipper",))
    flow.submit()
    view = flow.assess()
    assert view["assessment_status"] == "needs_more_info" and view["shipping"] is None
    assert [r["slot"] for r in view["assessment"]["requested_info"]] == ["issue-zipper"]
    stale = flow.c.add_return_info(flow.sid, flow.case["case_id"], flow.case["status_token"], "", [], 99,
                                   new_request_id())
    assert stale["error"]["code"] == "state_conflict"
    flow.upload("issue-zipper")
    added = flow.c.add_return_info(flow.sid, flow.case["case_id"], flow.case["status_token"], "Zip sticks halfway",
                                   [flow.photos["issue-zipper"]], view["state_version"], new_request_id())["data"]
    assert added["assessment_status"] == "pending" and added["state_version"] == view["state_version"] + 1
    assert flow.assess()["assessment_status"] == "approved"


def test_specialist_review_keeps_photos():
    flow = Flow(description="It got wet in the rain and the lining smells. Scratch on the back.")
    flow.upload_required()
    flow.submit()
    view = flow.assess()
    assert view["assessment_status"] == "manual_review" and customer_status(view) == "Under review"
    assert all(p["data"] for p in flow.c._overlay["photos"].values())


@pytest.mark.parametrize("product", [
    {"category": "crossbody", "name": None, "order_ref": None,
     "purchase_date": (dt.date.today() - dt.timedelta(days=45)).isoformat()},
    {"category": "crossbody", "name": None, "order_ref": "DEMO-90001", "purchase_date": None},
])
def test_rejected_returns_have_no_shipping_and_photo_content_is_deleted(product):
    flow = Flow(product=product)
    flow.upload_required()
    flow.submit()
    view = flow.assess()
    assert view["assessment_status"] == "rejected"
    assert view["shipping"] is None and view["logistics_status"] == "not_started"
    assert customer_status(view) == "Not accepted"
    photos = flow.c._overlay["photos"].values()
    assert all(p["data"] is None for p in photos)
    audit = [a for a in flow.c._overlay["audit"] if a["event"] == "submitted"]
    assert audit and all(len(h) == 64 for h in audit[0]["photos"])  # checksums only


def test_submit_is_idempotent_and_assessment_runs_once_per_version():
    flow = Flow()
    flow.upload_required()
    rid = new_request_id()
    first = flow.submit(rid)
    again = flow.submit(rid)
    assert first == again and len(flow.c._overlay["returns"]) == 1
    a = flow.assess()["assessment"]["assessment_id"]
    b = flow.assess()["assessment"]["assessment_id"]
    assert a == b and len(flow.c._overlay["returns"][first["case_id"]]["assessments"]) == 1


def test_status_lookup_requires_the_matching_code():
    flow = Flow()
    flow.upload_required()
    case = flow.submit()
    bad = flow.c.get_return_case(flow.sid, case["case_id"], "WRONG-CODE", new_request_id())
    assert bad["error"]["code"] == "not_found"
    good = flow.c.get_return_case(flow.sid, case["case_id"], case["status_token"], new_request_id())
    assert good["ok"] and customer_status(good["data"]) == "Reviewing your request"


def test_order_numbers_must_be_demo_ids():
    flow = Flow(product={"category": "tote", "name": None, "order_ref": "123456789", "purchase_date": None})
    flow.upload_required()
    resp = flow.c.submit_return(flow.sid, flow.sid, flow.product, flow.reason, flow.description,
                                list(flow.photos.values()), new_request_id())
    assert resp["error"]["code"] == "invalid_input"


@pytest.mark.parametrize("case,expected", [
    ({"assessment_status": "pending", "logistics_status": "not_started"}, "Reviewing your request"),
    ({"assessment_status": "approved", "logistics_status": "received", "verification_result": "verified"},
     "Inspection complete"),
    ({"assessment_status": "approved", "logistics_status": "received", "verification_result": "mismatch"},
     NEXT_UPDATE),
    ({"assessment_status": "approved", "logistics_status": "expired"}, "Return window closed"),
    ({"assessment_status": "rejected", "logistics_status": "not_started", "shipping": {"x": 1}}, None),
    ({"assessment_status": "rejected", "logistics_status": "awaiting_receipt"}, None),
    ({"assessment_status": "pending", "logistics_status": "received"}, None),
    ({"assessment_status": "approved", "logistics_status": "awaiting_receipt", "shipping": None}, None),
])
def test_customer_status_table_and_untrusted_combinations(case, expected):
    assert customer_status(case) == expected
    assert "contact" not in (expected or "").lower()
