"""任务 7：Streamlit 单页（AppTest 驱动真实页面脚本，模拟点击）。

- 检查通过：报价 108.00 并预留
- 检查失败：NO_MATCH
- 界面明确显示编排模式（scripted / 替身模型 / Bedrock）
- 页面代码不直接写数据库
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app" / "streamlit_app.py"


def make_app(tmp_path, monkeypatch, mode="scripted", **env) -> AppTest:
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("AGENT_MODE", mode)
    for key in ("BEDROCK_MODEL_ID", "AWS_REGION", "AWS_DEFAULT_REGION"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    app = AppTest.from_file(str(APP), default_timeout=60)
    app.run()
    assert not app.exception, app.exception
    return app


def texts(app: AppTest) -> str:
    parts = []
    for kind in ("markdown", "caption", "info", "success", "warning", "error", "header", "subheader", "title"):
        parts += [str(e.value) for e in getattr(app, kind)]
    parts += [str(e.value) for e in app.sidebar.markdown] + [str(e.value) for e in app.sidebar.caption]
    return "\n".join(parts)


def click(app: AppTest, key: str) -> AppTest:
    app.button(key=key).click().run()
    assert not app.exception, app.exception
    return app


def submit_key(app: AppTest) -> str:
    keys = [b.key for b in app.button if b.key and b.key.startswith("submit_request_v")]
    assert len(keys) == 1, keys
    return keys[0]


def submit_demo_request(app: AppTest) -> None:
    before = submit_key(app)
    click(app, "prefill_demo")
    after = submit_key(app)
    assert after != before  # 一键填入生成新版本的表单
    version = after.removeprefix("submit_request_v")
    assert app.radio(key=f"req_mode_BACK_v{version}").value == "accept"  # 预设值作为新表单的默认值
    assert app.radio(key=f"req_mode_ZIPPER_v{version}").value == "required"
    click(app, after)


def inspect_zipper(app: AppTest, conclusion: str, tag: str = "SYN-T-Q5ZT") -> None:
    app.text_input(key="scan_TASK-0001").input(tag)
    app.radio(key="concl_TASK-0001_ZIPPER").set_value(conclusion)
    if conclusion == "DEFECT":
        app.selectbox(key="dtype_TASK-0001_ZIPPER").set_value("JAM")
        app.selectbox(key="dsev_TASK-0001_ZIPPER").set_value("MODERATE")
    click(app, "inspect_btn_TASK-0001")


def test_pass_path_quote_108_and_reserve(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    assert "离线脚本编排（非 Bedrock）" in texts(app)
    assert "合成数据" in texts(app)

    submit_demo_request(app)
    page = texts(app)
    assert "提交需求：完成" in page
    assert "您接受的缺陷：背面划痕（最高轻微）。要求完好的部位：正面、拉链。" in page
    assert "Agent 运行 RUN-0001｜离线脚本编排（非 Bedrock）" in page
    assert "正在等待人工检查：AM-007 的拉链" in page
    assert "检查预算余额 B：12.00 / 20.00" in page

    inspect_zipper(app, "OK")
    page = texts(app)
    assert "提交检查结果：完成" in page and "Agent 运行 RUN-0002" in page
    # 下拉框只显示不变的编号和型号；状态单独显示，不会过期
    assert app.selectbox(key="selected_request").options == ["REQ-0001（SYN-BAG-A）"]
    assert "需求 REQ-0001 的状态：已报价，等待买家确认" in page
    assert "请求当前状态：已报价，等待买家确认。" in page  # 运行说明不重复"等待买家"
    assert "报价金额：108.00 CAD（合成金额）" in page
    assert "人工检查（检查任务 TASK-0001）" in page  # 拉链证据来自人工检查

    # 未逐条确认缺陷时，服务拒绝预留
    click(app, "confirm_btn_QUO-0001")
    assert "确认预留被拒绝（ACKNOWLEDGEMENT_INCOMPLETE）" in texts(app)

    app.checkbox(key="ack_QUO-0001_DF-AM007-BACK-1").check()
    app.checkbox(key="ack_amount_QUO-0001").check()
    click(app, "confirm_btn_QUO-0001")
    page = texts(app)
    assert "确认预留：完成" in page
    assert "已预留：RSV-0001｜商品 AM-007｜标签 SYN-T-Q5ZT｜金额 108.00 CAD" in page


def reserve_am007(app: AppTest) -> None:
    submit_demo_request(app)
    inspect_zipper(app, "OK")
    app.checkbox(key="ack_QUO-0001_DF-AM007-BACK-1").check()
    app.checkbox(key="ack_amount_QUO-0001").check()
    click(app, "confirm_btn_QUO-0001")
    assert "确认预留：完成" in texts(app)


def scan_for_delivery(app: AppTest, tag: str) -> None:
    app.text_input(key="dscan_RSV-0001").input(tag)
    click(app, "dscan_btn_RSV-0001")


def test_delivery_wrong_tag_then_correct_tag(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    reserve_am007(app)
    assert app.selectbox(key="selected_reservation").value == "RSV-0001"  # 默认选最新的预留

    scan_for_delivery(app, "SYN-T-9CFL")  # AM-006 的标签
    page = texts(app)
    assert "交付被阻止（DELIVERY_MISMATCH）" in page and "这是 AM-006 的标签" in page
    assert "已预留：RSV-0001" in page and "已交付" not in page.split("交付核验记录")[0]

    scan_for_delivery(app, "SYN-T-Q5ZT")
    page = texts(app)
    assert "标签已核对：SYN-T-Q5ZT 属于预留商品 AM-007（核验 DLV-0002）" in page

    # 有部位没确认时，服务拒绝
    click(app, "dconfirm_btn_DLV-0002")
    assert "确认交付被拒绝（FINDING_INCOMPLETE）" in texts(app)

    for point in ("FRONT", "BACK", "ZIPPER"):
        app.radio(key=f"dmatch_DLV-0002_{point}").set_value("match")
    click(app, "dconfirm_btn_DLV-0002")
    page = texts(app)
    assert "确认交付：完成" in page and "已交付：AM-007（预留 RSV-0001，核验 DLV-0002）" in page
    assert "已交付：RSV-0001｜商品 AM-007｜标签 SYN-T-Q5ZT" in page  # 买家标签页
    assert "需求 REQ-0001 的状态：已交付" in page
    history = next(d.value for d in app.dataframe if "扫描到的标签" in d.value.columns)
    assert list(history["结果"]) == ["标签不一致，已阻止交付", "已交付"]


def test_delivery_condition_mismatch_goes_to_manual_review(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    reserve_am007(app)
    scan_for_delivery(app, "SYN-T-Q5ZT")
    app.radio(key="dmatch_DLV-0001_FRONT").set_value("match")
    app.radio(key="dmatch_DLV-0001_BACK").set_value("match")
    app.radio(key="dmatch_DLV-0001_ZIPPER").set_value("mismatch")
    click(app, "dconfirm_btn_DLV-0001")
    page = texts(app)
    assert "交付被阻止（CONDITION_MISMATCH）" in page and "拉链的实物与证据不一致" in page
    assert "已阻止交付并转人工处理，不能继续交付" in page
    assert "已预留：RSV-0001" in page


def test_fail_path_no_match_after_reset(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    submit_demo_request(app)
    inspect_zipper(app, "OK")  # 第一轮先走一遍通过路径，验证重置会清空

    click(app, "reset_demo")
    assert "请先勾选确认" in texts(app)
    app.sidebar.checkbox(key="confirm_reset").check()
    click(app, "reset_demo")
    page = texts(app)
    assert "已重置为第 2 轮演示" in page and "还没有提交需求" in page and "演示轮次：2" in page

    submit_demo_request(app)
    inspect_zipper(app, "DEFECT")
    page = texts(app)
    assert "目前没有符合您条件的商品" in page
    assert "**AM-007**：拉链不是完好状态，不符合您的要求；人工检查结论：拉链卡滞（中等）" in page
    assert "**AM-006**：该商品当前不可售" in page
    assert "报价金额" not in page  # 没有报价，也没有预留


def test_wrong_tag_is_rejected_in_ui(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    submit_demo_request(app)
    inspect_zipper(app, "OK", tag="SYN-T-9CFL")
    page = texts(app)
    assert "提交检查结果被拒绝（TAG_MISMATCH）" in page
    assert "正在等待人工检查：AM-007 的拉链" in page


def test_console_shows_decision_basis_and_agent_calls(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch)
    submit_demo_request(app)
    page = texts(app)
    assert any(e.label == "DEC-0001 · AM-007 · 请求 REQ-0001 · 判定 INSPECT" for e in app.expander)
    assert "C = 8.00 ≤ B = 20.00" in page
    tables = [t.value for t in app.table]
    steps = next(t for t in tables if "算式" in t.columns)
    assert list(steps["结果"]) == ["98.00", "78.40", "8.00", "78.40", "45.00", "33.40"]
    frames = [d.value for d in app.dataframe]
    calls = next(f for f in frames if "理由（经数值守卫）" in f.columns)
    assert list(calls["工具"]) == ["get_request_status", "evaluate_request", "decide_inspection",
                                   "create_inspection_task"]
    assert all(r for r in calls["理由（经数值守卫）"])


@pytest.mark.parametrize(
    "mode,env,expected",
    [
        ("stub", {}, "Strands + 替身模型（非 Bedrock）"),
        ("bedrock", {}, "bedrock 模式需要设置环境变量：BEDROCK_MODEL_ID"),
        ("bedrock", {"BEDROCK_MODEL_ID": "model-from-env", "AWS_REGION": "region-from-env"},
         "模型：model-from-env｜区域：region-from-env（来自环境变量）"),
    ],
)
def test_mode_is_clearly_shown(tmp_path, monkeypatch, mode, env, expected):
    app = make_app(tmp_path, monkeypatch, mode, **env)
    sidebar = "\n".join([str(e.value) for e in app.sidebar.markdown] + [str(e.value) for e in app.sidebar.caption]
                        + [str(e.value) for e in app.sidebar.error])
    assert expected in sidebar


def test_stub_mode_runs_through_strands_loop(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, "stub")
    submit_demo_request(app)
    assert "Agent 运行 RUN-0001｜Strands + 替身模型（非 Bedrock）" in texts(app)
    inspect_zipper(app, "OK")
    assert "报价金额：108.00 CAD（合成金额）" in texts(app)


def test_bedrock_without_env_reports_config_error(tmp_path, monkeypatch):
    app = make_app(tmp_path, monkeypatch, "bedrock")
    submit_demo_request(app)
    page = texts(app)
    assert "提交需求：完成" in page and "配置缺失" in page and "BEDROCK_MODEL_ID" in page


def test_page_code_never_writes_the_database():
    sources = [APP, *sorted((ROOT / "acceptmatch" / "ui").glob("*.py"))]
    forbidden = re.compile(r"\.execute\(|write_tx|executescript|\bINSERT\b|\bUPDATE\b|\bDELETE\b|sqlite3\.connect")
    for path in sources:
        code = path.read_text(encoding="utf-8")
        assert not forbidden.search(code), path
