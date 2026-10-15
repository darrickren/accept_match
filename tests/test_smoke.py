"""任务 1 冒烟测试：依赖可导入、配置默认值正确、种子目录限制生效。"""

from pathlib import Path

import pytest

from acceptmatch.config import (
    ALLOWED_SEED_ROOT,
    DEFAULT_DB_PATH,
    REPO_ROOT,
    AgentMode,
    ConfigError,
    load_settings,
)


def test_dependencies_importable():
    import botocore.config  # noqa: F401
    import pydantic  # noqa: F401
    import streamlit  # noqa: F401
    from strands import Agent, tool  # noqa: F401
    from strands.models import BedrockModel  # noqa: F401
    from strands.tools.executors.sequential import SequentialToolExecutor  # noqa: F401


def test_defaults():
    s = load_settings({})
    # 模型和区域没有内置默认值
    assert s.aws_region is None and s.bedrock_model_id is None
    with pytest.raises(ConfigError, match="BEDROCK_MODEL_ID"):
        s.require_bedrock()
    assert s.bedrock_connect_timeout_s == 5
    assert s.bedrock_read_timeout_s == 60
    assert s.agent_run_timeout_s == 120
    assert s.agent_max_tool_calls == 15
    assert s.agent_max_rejections == 3
    assert s.agent_mode is AgentMode.BEDROCK
    assert s.db_path == DEFAULT_DB_PATH
    assert s.seed_dir == ALLOWED_SEED_ROOT.resolve()


def test_bedrock_settings_come_from_env_only():
    with pytest.raises(ConfigError, match="AWS_REGION"):
        load_settings({"BEDROCK_MODEL_ID": "model-from-env"}).require_bedrock()
    s = load_settings({"BEDROCK_MODEL_ID": "model-from-env", "AWS_DEFAULT_REGION": "region-from-env"})
    assert s.require_bedrock() == ("model-from-env", "region-from-env")
    s = load_settings({"BEDROCK_MODEL_ID": " m ", "AWS_REGION": "r1", "AWS_DEFAULT_REGION": "r2"})
    assert s.require_bedrock() == ("m", "r1")  # AWS_REGION 优先


def test_overrides():
    s = load_settings(
        {
            "AWS_REGION": "ca-central-1",
            "AGENT_MODE": "SCRIPTED",
            "AGENT_MAX_TOOL_CALLS": "8",
            "DB_PATH": "var/test.db",
            "SEED_DIR": "data/seed/variant",
        }
    )
    assert s.aws_region == "ca-central-1"
    assert s.agent_mode is AgentMode.SCRIPTED
    assert s.agent_max_tool_calls == 8
    assert s.db_path == (REPO_ROOT / "var" / "test.db").resolve()
    assert s.seed_dir == (ALLOWED_SEED_ROOT / "variant").resolve()


@pytest.mark.parametrize(
    "seed_dir",
    [
        "/Users/zhouhengru/Desktop/Hackathon/Dataset",
        "/tmp",
        "data",
        "data/seed/../../Hackathon",
        "data/seed_other",
    ],
)
def test_seed_dir_outside_repo_seed_rejected(seed_dir):
    with pytest.raises(ConfigError):
        load_settings({"SEED_DIR": seed_dir})


@pytest.mark.parametrize(
    "key,value",
    [
        ("AGENT_RUN_TIMEOUT_S", "abc"),
        ("AGENT_RUN_TIMEOUT_S", "0"),
        ("BEDROCK_READ_TIMEOUT_S", "-5"),
        ("AGENT_MODE", "llm"),
    ],
)
def test_invalid_values_rejected(key, value):
    with pytest.raises(ConfigError):
        load_settings({key: value})


def test_repo_root_contains_package():
    assert (REPO_ROOT / "acceptmatch" / "config.py").is_file()
    assert isinstance(DEFAULT_DB_PATH, Path)
