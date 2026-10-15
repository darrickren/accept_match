"""AcceptMatch MVP 单页演示（合成数据）。

侧边栏：合成数据标签、编排模式、本轮演示信息、重置。
标签页：买家 / 检查员 / 控制台。

页面只调用现有服务：
- 写：tools.human_ops（人工操作）、agent.flow.continue_after（Agent 运行）、AppContext.reset（重置）
- 读：acceptmatch.ui.views
写操作只在按钮回调中执行；每次渲染都从数据库读取业务数据。
运行方式：.venv/bin/streamlit run app/streamlit_app.py（只监听 localhost，没有身份认证）
"""

from __future__ import annotations

import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st  # noqa: E402

from acceptmatch.agent.flow import continue_after  # noqa: E402
from acceptmatch.agent.orchestrator import RunOutcome  # noqa: E402
from acceptmatch.domain.enums import DefectType, Point, RequestStatus, Severity  # noqa: E402
from acceptmatch.tools import human_ops as ho  # noqa: E402
from acceptmatch.tools.envelope import ToolResult  # noqa: E402
from acceptmatch.ui import views  # noqa: E402
from acceptmatch.ui.context import AppContext, open_context  # noqa: E402

BUYER_ID = "B-DEMO-01"
DEFAULT_INSPECTOR = "SYN-INSPECTOR-02"
DEFAULT_DELIVERY_OPERATOR = "SYN-DELIVERY-01"
REQUEST_MODES = {"required": "要求完好", "accept": "可接受缺陷", "none": "不作要求"}
CONCLUSIONS = {"OK": "完好 / 正常", "DEFECT": "有缺陷", "INCONCLUSIVE": "无法确定"}
TOOL_LABELS = {
    ho.SUBMIT_REQUEST: "提交需求",
    ho.SUBMIT_INSPECTION: "提交检查结果",
    ho.CONFIRM: "确认预留",
    ho.DECLINE: "拒绝报价",
}
RUN_STATUS_LABELS = {
    "COMPLETED": "已完成本次可做的步骤",
    "INCOMPLETE": "提前结束，仍有可做的步骤",
    "FAILED_BEDROCK": "模型调用失败",
    "TIMEOUT": "超时",
    "GUARD_STOPPED": "被守卫停止",
    "REFUSED_HALTED": "被拒绝：上一次运行失败，需要恢复",
    "CONFIG_ERROR": "配置缺失",
}
FORM_KEY_PREFIXES = ("req_", "ack_", "scan_", "concl_", "dtype_", "dsev_", "eref_")

st.set_page_config(page_title="AcceptMatch MVP（合成数据）", layout="wide")
ss = st.session_state


# ================================================================ 环境与消息


def get_ctx() -> AppContext:
    if "ctx" not in ss:
        ss.ctx = open_context()
    return ss.ctx


def flash(kind: str, text: str) -> None:
    ss.setdefault("flash", []).append((kind, text))


def show_flash() -> None:
    for kind, text in ss.pop("flash", []):
        getattr(st, kind)(text)


def flash_result(res: ToolResult) -> None:
    name = TOOL_LABELS.get(res.tool, res.tool)
    if res.ok:
        flash("success", f"✅ {name}：完成。")
    else:
        codes = "、".join(res.reason_codes)
        extra = f"已记录：{'、'.join(res.recorded)}。" if res.recorded else ""
        flash("error", f"❌ {name}被拒绝（{codes}）：{res.message or ''} {extra}")


def flash_runs(runs: list[RunOutcome]) -> None:
    for run in runs:
        status = RUN_STATUS_LABELS.get(run.status, run.status)
        text = f"🤖 Agent 运行 {run.run_id or '（未启动）'}｜{run.mode}｜{status}：{run.summary}"
        if run.error_detail:
            text += f"｜错误：{run.error_detail}"
        kind = "info" if run.status == "COMPLETED" else "warning" if run.status == "INCOMPLETE" else "error"
        flash(kind, text)


def _blank_preset() -> dict[str, str]:
    preset = {"req_product": "SYN-BAG-A"}
    for p in Point:
        preset.update({f"req_mode_{p.value}": "none", f"req_type_{p.value}": DefectType.SCRATCH.value,
                       f"req_sev_{p.value}": Severity.MINOR.value})
    return preset


FORM_PRESETS = {
    "blank": _blank_preset(),
    "demo": {**_blank_preset(), "req_mode_FRONT": "required", "req_mode_BACK": "accept",
             "req_type_BACK": DefectType.SCRATCH.value, "req_sev_BACK": Severity.MINOR.value,
             "req_mode_ZIPPER": "required"},
}


def rk(name: str, version: int | None = None) -> str:
    """需求表单控件的键，带表单版本号。

    防止"一键填入"后过快点击提交旧值：
    - "一键填入"不改控件的值，而是生成新版本的表单（新键），预设值作为控件的默认值（index）
      随每次渲染一起下发，即使中间的页面更新被打断，前端也会显示预设值；
    - 旧表单的提交回调会因为版本号过期而拒绝；旧控件的值也写不到新版本的键上。
    - "一键填入"按钮渲染在提交按钮之前，两个点击被合并到同一次运行时，填入的回调先执行。
    """
    return f"{name}_v{ss.req_form_version if version is None else version}"


def preset_value(name: str) -> str:
    return FORM_PRESETS[ss.req_form_preset][name]


def init_defaults() -> None:
    ss.setdefault("inspector_id", DEFAULT_INSPECTOR)
    ss.setdefault("delivery_operator_id", DEFAULT_DELIVERY_OPERATOR)
    ss.setdefault("req_form_version", 0)
    ss.setdefault("req_form_preset", "blank")


# ================================================================ 回调（写操作）


def on_reset() -> None:
    if not ss.get("confirm_reset"):
        flash("error", "请先勾选确认，再重置演示。")
        return
    try:
        result = get_ctx().reset()
    except Exception as exc:  # noqa: BLE001 - 重置失败时旧状态保持不变
        flash("error", f"❌ 重置失败，旧状态保持不变：{exc}")
        return
    next_version = ss.get("req_form_version", 0) + 1  # 表单版本只增不减，避免重用旧控件的键
    for key in list(ss.keys()):
        if key.startswith(FORM_KEY_PREFIXES) or key in ("selected_request", "selected_task", "selected_reservation"):
            del ss[key]
    ss.req_form_version = next_version
    ss.req_form_preset = "blank"
    ss.confirm_reset = False
    flash("success", f"✅ 已重置为第 {result.epoch} 轮演示（参数组 {result.parameter_set}）。自检：" + "；".join(result.checks))


def on_prefill_demo() -> None:
    ss.req_form_preset = "demo"
    ss.req_form_version += 1  # 新版本的表单使用预设值作为默认值


def on_submit_request(version: int) -> None:
    if version != ss.req_form_version:
        flash("warning", "⚠️ 表单刚被“一键填入”更新，这次提交没有生效。请核对表单内容后再点“提交需求”。")
        return
    c = get_ctx()
    accepted, required = [], []
    for p in Point:
        mode = ss.get(rk(f"req_mode_{p.value}", version))
        if mode == "required":
            required.append(p.value)
        elif mode == "accept":
            accepted.append({"point": p.value, "defect_type": ss[rk(f"req_type_{p.value}", version)],
                             "max_severity": ss[rk(f"req_sev_{p.value}", version)]})
    res = ho.submit_buyer_request(c.rt, BUYER_ID, ss[rk("req_product", version)], accepted, required)
    flash_result(res)
    if res.ok:
        ss.selected_request = res.request_id
        flash("info", f"📝 系统回显：{res.data['echo']}")
        flash_runs(continue_after(c.orch, res))


def on_inspect(task_id: str, points: list[str], item_id: str) -> None:
    c = get_ctx()
    findings = []
    for point in points:
        conclusion = ss.get(f"concl_{task_id}_{point}")
        finding = {"point": point, "conclusion": conclusion, "evidence_refs": [ss.get(f"eref_{task_id}_{point}")]}
        if conclusion == "DEFECT":
            finding["defect"] = {"type": ss.get(f"dtype_{task_id}_{point}"), "severity": ss.get(f"dsev_{task_id}_{point}")}
        findings.append(finding)
    res = ho.submit_inspection_result(
        c.rt, task_id, (ss.get("inspector_id") or "").strip(), (ss.get(f"scan_{task_id}") or "").strip(),
        datetime.now().astimezone().isoformat(timespec="seconds"), findings)
    flash_result(res)
    if res.ok:
        flash_runs(continue_after(c.orch, res))


def on_confirm(request_id: str, quote: dict) -> None:
    c = get_ctx()
    qid = quote["quote_id"]
    acked = [d["defect_id"] for d in quote["discounts"] if ss.get(f"ack_{qid}_{d['defect_id']}")]
    amount = quote["amount"] if ss.get(f"ack_amount_{qid}") else ""
    res = ho.confirm_reservation(c.rt, request_id, BUYER_ID, quote["item_id"], qid, acked, amount)
    flash_result(res)
    flash_runs(continue_after(c.orch, res))  # 只有需要重新评估时才会启动运行


def on_decline(request_id: str, quote: dict) -> None:
    res = ho.decline_quote(get_ctx().rt, request_id, BUYER_ID, quote["quote_id"])
    flash_result(res)


# ================================================================ 侧边栏


def sidebar(c: AppContext) -> None:
    sb = st.sidebar
    sb.title("AcceptMatch MVP")
    sb.markdown("**🧪 合成数据**：所有商品、买家、价格和经济参数都是合成值，与真实客户或订单无关。")

    mode = views.mode_info(c.settings)
    sb.subheader("编排模式")
    sb.markdown(f"**{mode.label}**")
    sb.caption(mode.description)
    if not mode.ready:
        sb.error(f"⚠️ {mode.problem}")

    meta = views.meta(c.conn)
    sb.subheader("本轮演示")
    sb.markdown(
        f"- 演示轮次：{meta.epoch}\n"
        f"- 参数组：{meta.parameter_set}\n"
        f"- 检查预算余额 B：{meta.budget_remaining} / {meta.budget_initial}（合成）"
    )
    if meta.seed_hash != c.rt.bundle.seed_hash:
        sb.error("⚠️ 种子文件在上次重置后被修改，请重置演示。")

    sb.subheader("重置演示")
    sb.checkbox("我确认清空本轮的请求、检查、报价和预留（操作记录会保留）", key="confirm_reset")
    sb.button("重置演示", key="reset_demo", on_click=on_reset)
    sb.caption("仅在本机运行，没有身份认证；角色通过标签页区分。")


# ================================================================ 买家


def evidence_section(evidence: dict) -> None:
    st.markdown("#### 证据（报价前请先查看）")
    st.caption(f"商品 {evidence['item_id']}｜标签 {evidence['tag_code']}｜状态版本 {evidence['condition_version']}｜合成数据")
    for point in evidence["points"]:
        st.markdown(f"**{point['label']}**：{views.status_label(point['status'])}")
        lines = []
        for d in point["defects"]:
            lines.append(f"- 已核实缺陷：{views.defect_text(d['type'], d['severity'])}；"
                         f"对应您接受的：{views.accepted_text(d['accepted_defect'])}")
        for e in point["evidence"]:
            task = f"（检查任务 {e['inspection_task_id']}）" if e.get("inspection_task_id") else ""
            lines.append(f"- 证据 {e['evidence_id']}：{views.source_label(e['source'])}{task}｜{e['inspector_id']}｜"
                         f"{e['recorded_at']}｜照片占位 `{e['photo_ref']}`（合成，无真实图片）")
        if lines:
            st.markdown("\n".join(lines))
    proof = "；".join(f"{views.point_label(p['point'])}：{'、'.join(p['evidence_ids'])}"
                      for p in evidence["required_conditions_proof"])
    st.caption(f"要求完好的部位及其证据：{proof or '无'}")


def quote_section(quote: dict) -> None:
    st.markdown("#### 报价（按价格规则计算）")
    rows = [{"项目": "基础价格", "金额（CAD，合成）": quote["base_price"]}]
    for d in quote["discounts"]:
        rate = f"{Decimal(d['rate']) * 100:.0f}%"
        rows.append({"项目": f"{views.point_label(d['point'])}{views.defect_text(d['type'], d['severity'])}，折扣 {rate}",
                     "金额（CAD，合成）": f"−{d['amount']}"})
    rows.append({"项目": "报价", "金额（CAD，合成）": quote["amount"]})
    st.table(rows)
    if quote["floor_applied"]:
        st.caption("已按价格下限（基础价格的 50%）计算。")
    st.markdown(f"**报价金额：{quote['amount']} CAD（合成金额）**｜报价编号 {quote['quote_id']}｜"
                f"绑定商品 {quote['item_id']} 的状态版本 {quote['condition_version']}")


def confirm_form(request_id: str, quote: dict) -> None:
    qid = quote["quote_id"]
    with st.form(f"confirm_{qid}"):
        st.markdown("请逐条确认这件商品的已核实缺陷和报价金额，然后再预留：")
        for d in quote["discounts"]:
            st.checkbox(f"我已查看并接受：{views.point_label(d['point'])}{views.defect_text(d['type'], d['severity'])}",
                        key=f"ack_{qid}_{d['defect_id']}")
        st.checkbox(f"我确认报价金额 {quote['amount']} CAD（合成金额）", key=f"ack_amount_{qid}")
        left, right = st.columns(2)
        left.form_submit_button("确认预留", key=f"confirm_btn_{qid}", type="primary", on_click=on_confirm,
                                args=(request_id, quote))
        right.form_submit_button("拒绝报价", key=f"decline_btn_{qid}", on_click=on_decline, args=(request_id, quote))


def request_panel(c: AppContext, request_id: str) -> None:
    v = views.request_view(c.conn, request_id)
    if v is None:
        st.warning("找不到这个需求。")
        return
    st.markdown(f"**需求 {v['request_id']} 的状态：{v['status_label']}**")
    st.caption(v["echo"])
    status = v["status"]
    if v["waiting"]:
        st.info(f"⏳ {v['waiting']}")
    if v["quote"] and v["evidence"]:
        evidence_section(v["evidence"])
        quote_section(v["quote"])
    if status == RequestStatus.OFFERED:
        confirm_form(request_id, v["quote"])
    elif status == RequestStatus.RESERVED and v["reservation"]:
        r = v["reservation"]
        st.success(f"✅ 已预留：{r['reservation_id']}｜商品 {v['quote']['item_id']}｜标签 {v['quote']['tag_code']}｜"
                   f"金额 {r['amount']} CAD（合成金额）")
    elif status == RequestStatus.NO_MATCH and v["no_match"]:
        st.warning(f"🚫 {v['no_match']['summary']}")
        st.markdown("\n".join(f"- **{e['item_id']}**：{'；'.join(e['buyer_messages'])}" for e in v["no_match"]["items"]))
    elif status == RequestStatus.DECLINED:
        st.info("您已拒绝这个报价，商品保持可售。")
    elif status not in (RequestStatus.WAITING_FOR_INSPECTION,):
        st.caption("系统处理尚未完成；如果长时间停在这里，请查看控制台中的 Agent 运行记录。")


def buyer_tab(c: AppContext) -> None:
    st.header("买家")
    st.caption(f"当前买家：{BUYER_ID}（演示买家，合成数据）。这里不显示检查成本、概率或预算等内部参数。")

    with st.expander("提交新需求", expanded=not views.buyer_requests(c.conn, BUYER_ID)):
        st.button("一键填入演示请求", key="prefill_demo", on_click=on_prefill_demo)
        version = ss.req_form_version
        modes = list(REQUEST_MODES)
        types = [t.value for t in DefectType]
        sevs = [s.value for s in Severity]
        with st.form(f"request_form_v{version}"):
            product_names = {p.product_id: p.name for p in c.rt.bundle.dataset.products}
            products = list(product_names)
            st.selectbox("型号", products, key=rk("req_product"), index=products.index(preset_value("req_product")),
                         format_func=lambda pid: f"{pid}（{product_names[pid]}）")
            for p in Point:
                st.radio(f"{p.label}的要求", modes, key=rk(f"req_mode_{p.value}"), horizontal=True,
                         index=modes.index(preset_value(f"req_mode_{p.value}")), format_func=REQUEST_MODES.get)
                left, right = st.columns(2)
                left.selectbox(f"{p.label}可接受的缺陷类型", types, key=rk(f"req_type_{p.value}"),
                               index=types.index(preset_value(f"req_type_{p.value}")),
                               format_func=lambda v: DefectType(v).label)
                right.selectbox(f"{p.label}可接受的最高程度", sevs, key=rk(f"req_sev_{p.value}"),
                                index=sevs.index(preset_value(f"req_sev_{p.value}")),
                                format_func=lambda v: Severity(v).label)
            st.caption("缺陷类型和程度只在选择“可接受缺陷”时使用；未列出的缺陷一律视为不接受。")
            st.form_submit_button("提交需求", key=f"submit_request_v{version}", type="primary",
                                  on_click=on_submit_request, args=(version,))

    requests = views.buyer_requests(c.conn, BUYER_ID)
    st.subheader("我的需求")
    if not requests:
        st.info("还没有提交需求。")
        return
    request_products = {r.request_id: r.product_id for r in requests}
    if ss.get("selected_request") not in request_products:
        ss.selected_request = requests[-1].request_id
    # 选项文字保持不变（只含编号和型号）；状态会变化，单独显示在下方，避免下拉框显示过期状态
    request_id = st.selectbox("选择需求", list(request_products), key="selected_request",
                              format_func=lambda rid: f"{rid}（{request_products[rid]}）")
    request_panel(c, request_id)


# ================================================================ 检查员


def inspector_tab(c: AppContext) -> None:
    st.header("检查员")
    st.text_input("检查员编号（合成）", key="inspector_id")
    tasks = views.open_tasks(c.conn)
    if not tasks:
        st.info("当前没有未完成的检查任务。")
        return
    by_id = {t.artifact_id: t for t in tasks}
    if ss.get("selected_task") not in by_id:
        ss.selected_task = tasks[0].artifact_id
    task_id = st.selectbox("选择检查任务", list(by_id), key="selected_task",
                           format_func=lambda t: f"{t}（商品 {by_id[t].item_id}）")
    task = by_id[task_id]
    item_id = task.item_id
    points = [p["point"] for p in task.content["points"]]
    st.markdown(f"**商品：** {item_id}｜**关联需求：** {'、'.join(task.content['request_ids'])}")
    st.markdown("\n".join(f"- 待检查部位：**{p['label']}**｜检查方法：{p['method']}" for p in task.content["points"]))
    st.caption("请在实物前扫描标签码，系统会核对标签是否属于这件商品。证据引用为合成占位，没有真实图片。")

    with st.form(f"inspect_{task_id}"):
        st.text_input("扫描到的标签码", key=f"scan_{task_id}", placeholder="扫描或输入标签码，例如 SYN-T-XXXX")
        for point in points:
            label = views.point_label(point)
            st.radio(f"{label}的结论", list(CONCLUSIONS), key=f"concl_{task_id}_{point}", index=None, horizontal=True,
                     format_func=CONCLUSIONS.get)
            a, b, e = st.columns(3)
            a.selectbox(f"{label}缺陷类型（结论为有缺陷时填写）", [t.value for t in DefectType],
                        key=f"dtype_{task_id}_{point}", format_func=lambda v: DefectType(v).label)
            b.selectbox(f"{label}缺陷程度（结论为有缺陷时填写）", [s.value for s in Severity],
                        key=f"dsev_{task_id}_{point}", format_func=lambda v: Severity(v).label)
            e.selectbox(f"{label}证据引用（合成占位）", views.evidence_ref_options(item_id, point),
                        key=f"eref_{task_id}_{point}")
        st.form_submit_button("提交检查结果", key=f"inspect_btn_{task_id}", type="primary", on_click=on_inspect,
                              args=(task_id, points, item_id))


# ================================================================ 控制台


def console_tab(c: AppContext) -> None:
    st.header("控制台（演示负责人）")
    mode = views.mode_info(c.settings)
    st.markdown(f"**编排模式：{mode.label}**｜{mode.description}")
    meta = views.meta(c.conn)

    st.subheader("商品（合成数据）")
    st.dataframe(views.items_table(c.conn), hide_index=True, width="stretch")

    st.subheader("买家需求")
    rows = views.requests_table(c.conn)
    if rows:
        st.dataframe(rows, hide_index=True, width="stretch")
    else:
        st.caption("暂无需求。")

    st.subheader("检查前的经济判断（计算依据）")
    decisions = views.decisions(c.conn)
    if not decisions:
        st.caption("暂无经济判断。")
    for d in decisions:
        with st.expander(d["title"], expanded=True):
            st.caption(d["meta"])
            st.markdown("**输入参数**（全部为合成值，不由 Agent 或模型估计）")
            st.table(d["inputs"])
            if d["steps"]:
                st.markdown("**计算步骤**（每步四舍五入到 0.01）")
                st.table(d["steps"])
            st.markdown("**不检查时的可选处置**")
            st.table(d["options"])
            st.markdown(f"**预算检查：** {d['budget']}")
            codes = "、".join(d["reason_codes"]) or "无"
            st.markdown(f"**判定：{d['verdict']}**｜原因码：{codes}")
            if d["recommended"]:
                st.markdown(f"**建议处置（只记录，不执行）：** {d['recommended']['label']}，净回收 {d['recommended']['net']}")
            if d["invalid_inputs"]:
                st.warning("参数问题：" + "；".join(d["invalid_inputs"]))

    st.subheader("Agent 运行")
    runs = views.agent_runs(c.conn, meta.epoch)
    if not runs:
        st.caption("本轮还没有 Agent 运行。")
    for run in runs:
        end = run["end"] or {}
        status = end.get("status") or "RUNNING"
        title = (f"{run['run_id']}｜{run['mode_label']}｜{RUN_STATUS_LABELS.get(status, status)}｜"
                 f"请求 {run['request_id']}｜触发：{run['trigger']}")
        with st.expander(title, expanded=run is runs[0]):
            if run["resume_of"]:
                st.caption(f"恢复自 {run['resume_of']}")
            if end.get("summary"):
                st.markdown(f"**说明：** {end['summary']}")
            if end.get("error_detail"):
                st.error(f"错误：{end['error_detail']}。已提交的操作保留；该请求的新运行会被拒绝，"
                         "恢复功能在 P1（任务 10）中提供，目前可以重置演示。")
            if run["calls"]:
                st.dataframe(run["calls"], hide_index=True, width="stretch")
    refused = views.refused_runs(c.conn, meta.epoch)
    if refused:
        st.markdown("**被拒绝启动的运行**")
        st.dataframe(refused, hide_index=True, width="stretch")

    st.subheader("操作记录")
    items = ["全部"] + [r["商品"] for r in views.items_table(c.conn)]
    request_ids = ["全部"] + [r["请求"] for r in rows]
    a, b, e = st.columns(3)
    item = a.selectbox("按商品筛选", items, key="audit_item")
    req = b.selectbox("按需求筛选", request_ids, key="audit_request")
    history = e.checkbox("包含历史演示轮次", key="audit_history")
    audit_rows = views.audit_rows(c.conn, epoch=None if history else meta.epoch,
                                  item_id=None if item == "全部" else item,
                                  request_id=None if req == "全部" else req)
    st.dataframe(audit_rows, hide_index=True, width="stretch")


# ================================================================ 页面


def main() -> None:
    try:
        c = get_ctx()
    except Exception as exc:  # noqa: BLE001 - 启动失败时给出明确提示
        st.error(f"启动失败：{exc}")
        st.stop()
    init_defaults()
    sidebar(c)
    st.title("AcceptMatch MVP")
    mode = views.mode_info(c.settings)
    st.caption(f"🧪 合成数据演示｜编排模式：{mode.label}")
    show_flash()
    buyer, inspector, console = st.tabs(["买家", "检查员", "控制台"])
    with buyer:
        buyer_tab(c)
    with inspector:
        inspector_tab(c)
    with console:
        console_tab(c)


main()
