"""替身模型（非 Bedrock）：在真实的 Strands Agent 循环里运行，但不访问网络。

它按系统提示的规则行事：先读取状态，再执行上一个工具结果中 allowed_next_actions 的第一项；
没有可做的动作时给出一句说明。用于没有 Bedrock 凭证时的演示（AGENT_MODE=stub）和测试。
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Callable

from strands.models.model import Model

Decision = tuple[str, dict[str, Any], str] | str  # (工具, 参数, 理由) 或最终文字


def _last_tool_result(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    for message in reversed(messages):
        for block in message.get("content", []):
            if isinstance(block, dict) and "toolResult" in block:
                for part in block["toolResult"].get("content", []):
                    if "text" in part:
                        try:
                            return json.loads(part["text"])
                        except json.JSONDecodeError:
                            return {"raw": part["text"]}
                return {}
    return None


def follow_allowed(messages: list[dict[str, Any]]) -> Decision:
    result = _last_tool_result(messages)
    if result is None:
        prompt = json.loads(messages[-1]["content"][0]["text"])
        return "get_request_status", {"request_id": prompt["request_id"]}, "先读取请求的当前状态"
    actions = result.get("allowed_next_actions") or []
    if not actions:
        return "已完成本次可做的步骤，现在等待人工操作。"
    action = actions[0]
    return action["tool"], action["args"], f"按允许的下一步执行 {action['tool']}"


class StubModel(Model):
    """decide 可替换决策逻辑；fail_on_call / error 用于模拟第 N 次模型调用失败。"""

    def __init__(self, decide: Callable[[list[dict[str, Any]], int], Decision] | None = None,
                 fail_on_call: int | None = None, error: BaseException | None = None):
        self.decide = decide or (lambda messages, n: follow_allowed(messages))
        self.fail_on_call = fail_on_call
        self.error = error
        self.calls = 0
        self.config: dict[str, Any] = {"model_id": "stub-model"}

    def update_config(self, **model_config: Any) -> None:
        self.config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return self.config

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs):  # pragma: no cover
        raise NotImplementedError("替身模型不支持结构化输出")
        yield  # noqa: B901

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs) -> AsyncIterator[dict]:
        self.calls += 1
        if self.fail_on_call is not None and self.calls >= self.fail_on_call:
            raise self.error or RuntimeError("模拟的模型调用失败")
        decision = self.decide(messages, self.calls)
        yield {"messageStart": {"role": "assistant"}}
        if isinstance(decision, str):
            yield {"contentBlockStart": {"start": {}}}
            yield {"contentBlockDelta": {"delta": {"text": decision}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        else:
            tool, args, rationale = decision
            yield {"contentBlockStart": {"start": {"toolUse": {"toolUseId": f"tool-{self.calls}", "name": tool}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps({**args, "rationale": rationale},
                                                                                  ensure_ascii=False)}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        yield {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                            "metrics": {"latencyMs": 1}}}
