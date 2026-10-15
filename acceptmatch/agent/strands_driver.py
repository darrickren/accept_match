"""Strands + Amazon Bedrock 驱动（design §7.2）。

- 模型编号和区域只从环境变量读取（BEDROCK_MODEL_ID、AWS_REGION / AWS_DEFAULT_REGION）。
- 非流式调用、temperature 0、botocore 超时，最多重试一次；关闭 Strands 自带的模型重试。
- 工具按顺序执行；每个工具都只是 Orchestrator.invoke() 的薄封装，暴露给模型的只有 7 个 Agent 工具。
- 当没有可做的动作或运行已停止时，工具设置 stop_event_loop，Strands 不会再发起模型调用。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from botocore.config import Config as BotocoreConfig
from strands import Agent, tool
from strands.models import BedrockModel
from strands.models.model import Model
from strands.tools.executors.sequential import SequentialToolExecutor
from strands.types.tools import ToolContext

from acceptmatch.agent.prompts import SYSTEM_PROMPT, run_prompt
from acceptmatch.config import Settings
from acceptmatch.tools import state as st

if TYPE_CHECKING:
    from acceptmatch.agent.orchestrator import Orchestrator, RunContext


def build_bedrock_model(settings: Settings) -> BedrockModel:
    """按环境变量构建 BedrockModel；缺少模型编号或区域时抛出 ConfigError。不发起网络请求。"""
    model_id, region = settings.require_bedrock()
    return BedrockModel(
        model_id=model_id,
        region_name=region,
        temperature=0.0,
        max_tokens=1024,
        streaming=False,
        boto_client_config=BotocoreConfig(
            connect_timeout=settings.bedrock_connect_timeout_s,
            read_timeout=settings.bedrock_read_timeout_s,
            retries={"total_max_attempts": 2, "mode": "standard"},
        ),
    )


def make_tools(orch: Orchestrator, ctx: RunContext) -> list[Any]:
    """为一次运行生成 7 个绑定该运行的 Strands 工具。"""

    def call(tool_name: str, args: dict[str, Any], rationale: str, tool_context: ToolContext) -> dict[str, Any]:
        result = orch.invoke(ctx, tool_name, args, rationale)
        if ctx.stop_loop:  # 没有可做的动作或运行已停止：本批工具执行完后结束循环
            tool_context.invocation_state.setdefault("request_state", {})["stop_event_loop"] = True
        return result

    @tool(name=st.GET_STATUS, context="tool_context")
    def get_request_status(request_id: str, rationale: str, tool_context: ToolContext) -> dict:
        """读取请求的当前状态和 allowed_next_actions（只读，随时可以调用）。

        Args:
            request_id: 买家请求编号，例如 REQ-0001
            rationale: 一句话说明为什么调用；不要写工具结果中没有的数字
        """
        return call(st.GET_STATUS, {"request_id": request_id}, rationale, tool_context)

    @tool(name=st.EVALUATE, context="tool_context")
    def evaluate_request(request_id: str, rationale: str, tool_context: ToolContext) -> dict:
        """对全部商品逐件匹配。只有 allowed_next_actions 中出现 evaluate_request 时才能调用。

        Args:
            request_id: 买家请求编号
            rationale: 一句话说明为什么选择这一步；不要写工具结果中没有的数字
        """
        return call(st.EVALUATE, {"request_id": request_id}, rationale, tool_context)

    @tool(name=st.DECIDE, context="tool_context")
    def decide_inspection(request_id: str, item_id: str, rationale: str, tool_context: ToolContext) -> dict:
        """对待检查商品做检查前的经济判断。只有 allowed_next_actions 中出现时才能调用，item_id 必须一致。

        Args:
            request_id: 买家请求编号
            item_id: 商品编号，例如 AM-007
            rationale: 一句话说明为什么选择这一步；不要写工具结果中没有的数字
        """
        return call(st.DECIDE, {"request_id": request_id, "item_id": item_id}, rationale, tool_context)

    @tool(name=st.CREATE_TASK, context="tool_context")
    def create_inspection_task(request_id: str, item_id: str, decision_id: str, rationale: str,
                               tool_context: ToolContext) -> dict:
        """为判定 INSPECT 的商品创建人工检查任务。只有 allowed_next_actions 中出现时才能调用。

        Args:
            request_id: 买家请求编号
            item_id: 商品编号
            decision_id: 经济判断编号，例如 DEC-0001
            rationale: 一句话说明为什么选择这一步；不要写工具结果中没有的数字
        """
        return call(st.CREATE_TASK, {"request_id": request_id, "item_id": item_id, "decision_id": decision_id},
                    rationale, tool_context)

    @tool(name=st.BUILD_EVIDENCE, context="tool_context")
    def build_evidence_package(request_id: str, item_id: str, rationale: str, tool_context: ToolContext) -> dict:
        """为排名第一的可成交商品整理证据。只有 allowed_next_actions 中出现时才能调用。

        Args:
            request_id: 买家请求编号
            item_id: 商品编号
            rationale: 一句话说明为什么选择这一步；不要写工具结果中没有的数字
        """
        return call(st.BUILD_EVIDENCE, {"request_id": request_id, "item_id": item_id}, rationale, tool_context)

    @tool(name=st.GENERATE_QUOTE, context="tool_context")
    def generate_quote(request_id: str, item_id: str, evidence_package_id: str, rationale: str,
                       tool_context: ToolContext) -> dict:
        """按价格规则生成报价。只有 allowed_next_actions 中出现时才能调用。

        Args:
            request_id: 买家请求编号
            item_id: 商品编号
            evidence_package_id: 证据包编号，例如 EVP-0001
            rationale: 一句话说明为什么选择这一步；不要写工具结果中没有的数字
        """
        return call(st.GENERATE_QUOTE,
                    {"request_id": request_id, "item_id": item_id, "evidence_package_id": evidence_package_id},
                    rationale, tool_context)

    @tool(name=st.FINALIZE_NO_MATCH, context="tool_context")
    def finalize_no_match(request_id: str, rationale: str, tool_context: ToolContext) -> dict:
        """没有任何候选时，告知买家没有合适商品。只有 allowed_next_actions 中出现时才能调用。

        Args:
            request_id: 买家请求编号
            rationale: 一句话说明为什么选择这一步；不要写工具结果中没有的数字
        """
        return call(st.FINALIZE_NO_MATCH, {"request_id": request_id}, rationale, tool_context)

    return [get_request_status, evaluate_request, decide_inspection, create_inspection_task,
            build_evidence_package, generate_quote, finalize_no_match]


def message_text(message: Any) -> str | None:
    """取出 Agent 最后一条消息中的文字部分。"""
    if not isinstance(message, dict):
        return None
    parts = [block["text"] for block in message.get("content", []) if isinstance(block, dict) and "text" in block]
    text = "\n".join(p.strip() for p in parts if p and p.strip())
    return text or None


class StrandsDriver:
    def __init__(self, orch: Orchestrator, model: Model):
        self.orch = orch
        self.model = model

    @property
    def mode_label(self) -> str:
        """如实标注实际使用的模型：只有 BedrockModel 才标为 Amazon Bedrock。"""
        if isinstance(self.model, BedrockModel):
            return "Strands + Amazon Bedrock"
        from acceptmatch.agent.stub_model import StubModel  # noqa: PLC0415

        if isinstance(self.model, StubModel):
            return "Strands + 替身模型（非 Bedrock）"
        return f"Strands + {type(self.model).__name__}（非 Bedrock）"

    def run(self, ctx: RunContext) -> str | None:
        agent = Agent(
            model=self.model,
            system_prompt=SYSTEM_PROMPT,
            tools=make_tools(self.orch, ctx),
            tool_executor=SequentialToolExecutor(),
            retry_strategy=None,  # 不重试模型调用；失败立即结束运行
            callback_handler=None,
        )
        result = agent(run_prompt(ctx.run_id, ctx.trigger, ctx.request_id))
        return message_text(result.message)
