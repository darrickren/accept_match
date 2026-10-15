"""运行配置（design §7.1）。

所有配置都来自环境变量，并在加载时校验。种子目录必须位于仓库的 data/seed/ 下，
这样 MVP 不会读取 Hackathon/Dataset 等外部数据（需求 1.3）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping

REPO_ROOT = Path(__file__).resolve().parent.parent
ALLOWED_SEED_ROOT = REPO_ROOT / "data" / "seed"
DEFAULT_DB_PATH = REPO_ROOT / "var" / "acceptmatch.db"


class ConfigError(ValueError):
    """配置缺失或无效。"""


class AgentMode(str, Enum):
    BEDROCK = "bedrock"  # Strands Agent + Amazon Bedrock（默认）
    SCRIPTED = "scripted"  # 离线脚本编排，按 allowed_next_actions 第一项执行，非 Bedrock


@dataclass(frozen=True)
class Settings:
    # 模型和区域只从环境变量读取，没有内置默认值；未设置时为 None，只有 bedrock 模式才需要
    aws_region: str | None
    bedrock_model_id: str | None
    bedrock_connect_timeout_s: int
    bedrock_read_timeout_s: int
    agent_run_timeout_s: int
    agent_max_tool_calls: int
    agent_max_rejections: int
    agent_mode: AgentMode
    db_path: Path
    seed_dir: Path

    def require_bedrock(self) -> tuple[str, str]:
        """返回 (model_id, region)；缺少任何一项时抛出 ConfigError，列出需要设置的环境变量。"""
        missing = []
        if not self.bedrock_model_id:
            missing.append("BEDROCK_MODEL_ID")
        if not self.aws_region:
            missing.append("AWS_REGION（或 AWS_DEFAULT_REGION）")
        if missing:
            raise ConfigError("bedrock 模式需要设置环境变量：" + "、".join(missing))
        assert self.bedrock_model_id and self.aws_region
        return self.bedrock_model_id, self.aws_region


_DEFAULTS: dict[str, str] = {
    "BEDROCK_CONNECT_TIMEOUT_S": "5",
    "BEDROCK_READ_TIMEOUT_S": "60",
    "AGENT_RUN_TIMEOUT_S": "120",
    "AGENT_MAX_TOOL_CALLS": "15",
    "AGENT_MAX_REJECTIONS": "3",
    "AGENT_MODE": AgentMode.BEDROCK.value,
}


def _get(env: Mapping[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    return value if value else _DEFAULTS[key]


def _positive_int(env: Mapping[str, str], key: str) -> int:
    raw = _get(env, key)
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} 必须是整数，当前为 {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{key} 必须大于 0，当前为 {value}")
    return value


def _resolve(path_str: str) -> Path:
    path = Path(path_str).expanduser()
    if not path.is_absolute():
        path = REPO_ROOT / path
    return path.resolve()


def validate_seed_dir(path: Path | str) -> Path:
    """解析路径并确认它位于仓库的 data/seed/ 下（含子目录），否则抛出 ConfigError。

    只做路径解析，不读取目录内容。
    """
    seed_dir = _resolve(str(path))
    allowed = ALLOWED_SEED_ROOT.resolve()
    if seed_dir != allowed and allowed not in seed_dir.parents:
        raise ConfigError(f"SEED_DIR 必须位于 {allowed} 下，当前为 {seed_dir}")
    return seed_dir


def _seed_dir(env: Mapping[str, str]) -> Path:
    raw = env.get("SEED_DIR", "").strip()
    return validate_seed_dir(raw if raw else ALLOWED_SEED_ROOT)


def _agent_mode(env: Mapping[str, str]) -> AgentMode:
    raw = _get(env, "AGENT_MODE").lower()
    try:
        return AgentMode(raw)
    except ValueError as exc:
        allowed = ", ".join(m.value for m in AgentMode)
        raise ConfigError(f"AGENT_MODE 只能是 {allowed}，当前为 {raw!r}") from exc


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """从环境变量加载并校验配置。env 为 None 时读取 os.environ。"""
    env = os.environ if env is None else env
    db_raw = env.get("DB_PATH", "").strip()
    region = env.get("AWS_REGION", "").strip() or env.get("AWS_DEFAULT_REGION", "").strip() or None
    return Settings(
        aws_region=region,
        bedrock_model_id=env.get("BEDROCK_MODEL_ID", "").strip() or None,
        bedrock_connect_timeout_s=_positive_int(env, "BEDROCK_CONNECT_TIMEOUT_S"),
        bedrock_read_timeout_s=_positive_int(env, "BEDROCK_READ_TIMEOUT_S"),
        agent_run_timeout_s=_positive_int(env, "AGENT_RUN_TIMEOUT_S"),
        agent_max_tool_calls=_positive_int(env, "AGENT_MAX_TOOL_CALLS"),
        agent_max_rejections=_positive_int(env, "AGENT_MAX_REJECTIONS"),
        agent_mode=_agent_mode(env),
        db_path=_resolve(db_raw) if db_raw else DEFAULT_DB_PATH,
        seed_dir=_seed_dir(env),
    )
