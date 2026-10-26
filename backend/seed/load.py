"""Load the deterministic `demo-v1` dataset into DynamoDB and the image store.

Idempotent: every record is a conditional put on a fixed key, so running it
twice never increases counts or overwrites changes made through the API (for
example a seeded pair that was bought). `reset` deletes only records tagged
with a dataset id, and only against a localhost endpoint.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from typing import Any, Dict, List

from ..repository import (ConditionFailed, Repository, counter_key, inv_key, line_key, order_key, return_key,
                          return_line_key, review_key, seed_key)
from . import dataset
from .images import render


def _put(repo: Repository, item: Dict[str, Any]) -> bool:
    try:
        repo.put(item, if_absent=True)
        return True
    except ConditionFailed:
        return False


def checksum() -> str:
    blob = json.dumps({"items": dataset.items(), "orders": dataset.orders(), "returns": dataset.returns(),
                       "reviews": dataset.reviews(), "version": dataset.SEED_VERSION}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def seed(repo: Repository, store: Any, images: bool = True) -> Dict[str, int]:
    tag = {"dataset_id": dataset.DATASET}
    created = {"items": 0, "orders": 0, "returns": 0, "reviews": 0, "images": 0}
    manifest: List[Dict[str, str]] = []
    for row in dataset.items():
        colour = row.pop("image_colour")
        pk, sk = inv_key(row["item_id"])
        created["items"] += _put(repo, {"PK": pk, "SK": sk, "entity_type": "inventory_item", **tag,
                                        "created_at": "2026-10-14T12:00:00Z", "updated_at": "2026-10-14T12:00:00Z",
                                        **row})
        for n, photo in enumerate(row["photos"]):
            kind = row["defects"][0]["type"] if row["defects"] else None
            label = f"{row['size_system'].replace('_', ' ')} {row['size_value']}"
            if images and not store.exists(photo["storage_key"]):
                store.put(photo["storage_key"], render(photo["view"], colour, kind, seed=int(row["item_id"][-3:]) * 10 + n,
                                                       label=label))
                created["images"] += 1
            data = store.get(photo["storage_key"]) if images else b""
            manifest.append({"photo_id": photo["photo_id"], "file_name": photo["storage_key"], "view": photo["view"],
                             "source_type": "SYNTHETIC", "origin_url_or_generation_id": f"backend.seed.images:{dataset.SEED_VERSION}",
                             "rights_note": "Generated for this demo; no third-party content",
                             "associated_item_or_case": row["item_id"],
                             "checksum": hashlib.sha256(data or b"").hexdigest()[:16]})
    for order in dataset.orders():
        lines = order.pop("lines")
        pk, sk = order_key(order["order_id"])
        created["orders"] += _put(repo, {"PK": pk, "SK": sk, "entity_type": "demo_order", **tag, **order})
        for line in lines:
            lpk, lsk = line_key(order["order_id"], line["line"])
            _put(repo, {"PK": lpk, "SK": lsk, "entity_type": "order_item", **tag, "order_id": order["order_id"], **line})
    for record in dataset.returns():
        pk, sk = return_key(record["return_id"])
        created["returns"] += _put(repo, {"PK": pk, "SK": sk, "entity_type": "return_request", **tag,
                                          "policy_version": "aldo-ca-2026-10-15-v1", "evaluations": 1, **record})
        gpk, gsk = return_line_key(record["order_id"], record["order_line"])
        _put(repo, {"PK": gpk, "SK": gsk, "entity_type": "return_guard", **tag, "return_id": record["return_id"]})
    for review in dataset.reviews():
        pk, sk = review_key(review["review_id"])
        created["reviews"] += _put(repo, {"PK": pk, "SK": sk, "entity_type": "human_review", **tag, **review})
    for name, value in dataset.COUNTERS.items():
        pk, sk = counter_key(name)
        _put(repo, {"PK": pk, "SK": sk, "entity_type": "counter", **tag, "value": value})
    pk, sk = seed_key(dataset.DATASET)
    repo.put({"PK": pk, "SK": sk, "entity_type": "seed", **tag, "seed_version": dataset.SEED_VERSION,
              "checksum": checksum()})
    if images:
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(manifest[0]))
        writer.writeheader()
        writer.writerows(manifest)
        store.put("image_manifest.csv", out.getvalue().encode())
    return created


def counts(repo: Repository) -> Dict[str, int]:
    inventory = repo.query("INV", "ITEM#")
    return {"inventory": len(inventory), "available": sum(1 for r in inventory if r.get("status") == "AVAILABLE"),
            "reviews": len(repo.query("REVIEWS", "REVIEW#")),
            "orders": sum(1 for o in dataset.orders() if repo.get(order_key(o["order_id"])))}


def reset(repo: Repository, include_runtime: bool) -> int:
    """Delete demo-v1 records (and, if asked, records the API created). Never touches other datasets."""
    removed = 0
    for scope in (dataset.DATASET,) + (("runtime",) if include_runtime else ()):
        for key in repo.scan_keys(filter_dataset=scope):
            repo.delete(key)
            removed += 1
    return removed


if __name__ == "__main__":  # python -m backend.seed.load  (settings from the environment only)
    from ..config import load_settings
    from ..storage import LocalImageStore

    settings = load_settings()
    repo = Repository.from_settings(settings)
    repo.ensure_table()
    print({"created": seed(repo, LocalImageStore(settings.image_root)), "now": counts(repo)})
