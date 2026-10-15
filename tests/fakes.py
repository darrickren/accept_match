"""测试用模型替身：直接复用包内的 StubModel（不访问网络）。"""

from acceptmatch.agent.stub_model import StubModel as FakeModel
from acceptmatch.agent.stub_model import follow_allowed

__all__ = ["FakeModel", "follow_allowed"]
