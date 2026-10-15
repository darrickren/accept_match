"""共享测试夹具：从合成种子构建领域对象。"""

from __future__ import annotations

import os

import pytest


def pytest_collection_modifyitems(config, items):
    """带 bedrock 标记的测试需要真实凭证，只有设置 RUN_BEDROCK_TESTS=1 时才运行。"""
    if os.environ.get("RUN_BEDROCK_TESTS") == "1":
        return
    skip = pytest.mark.skip(reason="需要真实 Bedrock 凭证；设置 RUN_BEDROCK_TESTS=1 以及 BEDROCK_MODEL_ID、AWS_REGION 后运行")
    for item in items:
        if "bedrock" in item.keywords:
            item.add_marker(skip)

from acceptmatch.domain.models import BuyerRequestSpec, ItemSnapshot
from acceptmatch.domain.pricing import PriceRules
from acceptmatch.seed import SeedBundle, load_seed
from acceptmatch.seed import convert


@pytest.fixture(scope="session")
def seed() -> SeedBundle:
    return load_seed()


@pytest.fixture(scope="session")
def snapshots(seed: SeedBundle) -> dict[str, ItemSnapshot]:
    return {i.item_id: convert.item_snapshot(i) for i in seed.dataset.items}


@pytest.fixture(scope="session")
def demo_spec(seed: SeedBundle) -> BuyerRequestSpec:
    return convert.buyer_request_spec(seed.dataset.demo_request)


@pytest.fixture(scope="session")
def rules(seed: SeedBundle) -> PriceRules:
    return convert.price_rules(seed.dataset.price_rules)
