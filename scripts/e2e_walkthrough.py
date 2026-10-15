"""在真实浏览器中点击走通两条演示路径（tasks.md 任务 8）。

前提：应用已在本机启动，例如
    AGENT_MODE=scripted .venv/bin/streamlit run app/streamlit_app.py
运行：
    .venv/bin/python scripts/e2e_walkthrough.py --url http://localhost:8501
截图保存在 var/e2e/（已被 .gitignore 忽略）。

每条路径开始前都会在页面上点击"重置演示"，所以可以重复运行。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from playwright.sync_api import Page, expect, sync_playwright

TIMEOUT_MS = 60_000


def wait_idle(page: Page) -> None:
    """等 Streamlit 这次重新运行脚本结束，避免在页面还没更新时就点下一步。"""
    page.wait_for_timeout(300)
    page.locator('[data-testid="stApp"][data-test-script-state="notRunning"]').wait_for(timeout=TIMEOUT_MS)


def wait_text(page: Page, text: str) -> None:
    expect(page.get_by_text(text, exact=False).first).to_be_visible(timeout=TIMEOUT_MS)
    print(f"    ✓ 看到：{text}")


def click_button(page: Page, name: str) -> None:
    page.get_by_role("button", name=name, exact=True).first.click()
    print(f"  点击按钮：{name}")
    wait_idle(page)


def open_tab(page: Page, name: str) -> None:
    page.get_by_role("tab", name=name, exact=True).click()
    print(f"  切换到标签页：{name}")


def tick(page: Page, label: str) -> None:
    page.get_by_text(label, exact=True).first.click()
    print(f"  勾选：{label}")
    wait_idle(page)


def choose(page: Page, select_label: str, option: str) -> None:
    page.get_by_label(select_label, exact=True).first.click()
    page.get_by_role("option", name=option, exact=True).click()
    print(f"  选择：{select_label} → {option}")


def expect_radio(page: Page, group: str, option: str) -> None:
    radio = page.get_by_role("radiogroup", name=group).get_by_role("radio", name=option, exact=True)
    expect(radio).to_be_checked(timeout=TIMEOUT_MS)
    print(f"    ✓ {group}：{option}")


def shot(page: Page, out: Path, name: str) -> None:
    path = out / f"{name}.png"
    page.screenshot(path=str(path), full_page=True)
    print(f"    截图：{path}")


def reset(page: Page) -> None:
    tick(page, "我确认清空本轮的请求、检查、报价和预留（操作记录会保留）")
    click_button(page, "重置演示")
    wait_text(page, "已重置为第")
    wait_text(page, "还没有提交需求")


def submit_demo_request(page: Page) -> None:
    open_tab(page, "买家")
    if not page.get_by_role("button", name="提交需求", exact=True).first.is_visible():
        page.get_by_text("提交新需求", exact=True).first.click()  # 展开表单
    click_button(page, "一键填入演示请求")
    expect_radio(page, "正面的要求", "要求完好")
    expect_radio(page, "背面的要求", "可接受缺陷")
    expect_radio(page, "拉链的要求", "要求完好")
    click_button(page, "提交需求")
    wait_text(page, "提交需求：完成")
    wait_text(page, "您接受的缺陷：背面划痕（最高轻微）。要求完好的部位：正面、拉链。")
    wait_text(page, "正在等待人工检查：AM-007 的拉链")


def fill_visible(page: Page, label: str, value: str) -> None:
    """只填当前标签页里可见的输入框（检查员和交付标签页都有"扫描到的标签码"）。"""
    page.locator(f'input[aria-label="{label}"]:visible').fill(value)
    print(f"  输入{label}：{value}")


def inspect_zipper(page: Page, defect: bool) -> None:
    open_tab(page, "检查员")
    fill_visible(page, "扫描到的标签码", "SYN-T-Q5ZT")
    page.get_by_text("有缺陷" if defect else "完好 / 正常", exact=True).first.click()
    print(f"  拉链结论：{'有缺陷' if defect else '完好 / 正常'}")
    if defect:
        choose(page, "拉链缺陷类型（结论为有缺陷时填写）", "卡滞")
        choose(page, "拉链缺陷程度（结论为有缺陷时填写）", "中等")
    click_button(page, "提交检查结果")
    wait_text(page, "提交检查结果：完成")


def fast_prefill_then_submit(page: Page, out: Path) -> None:
    """点"一键填入"后不等页面更新就立刻点"提交需求"：不能用旧的表单值创建需求。"""
    print("\n== 快速连点：一键填入后立刻提交 ==")
    reset(page)
    open_tab(page, "买家")
    if not page.get_by_role("button", name="提交需求", exact=True).first.is_visible():
        page.get_by_text("提交新需求", exact=True).first.click()
    page.get_by_role("button", name="一键填入演示请求", exact=True).first.click()
    page.get_by_role("button", name="提交需求", exact=True).first.click()  # 故意不等待
    print("  连点：一键填入演示请求 → 提交需求（中间不等待）")
    wait_idle(page)
    page.wait_for_timeout(1000)
    wait_idle(page)
    shot(page, out, "00_fast_click")
    stale = page.get_by_text("您不接受任何缺陷", exact=False)
    if stale.count() > 0:
        raise AssertionError("快速连点提交了旧的表单值（需求中没有任何可接受缺陷和要求）")
    if page.get_by_text("还没有提交需求", exact=False).count() > 0:
        print("    ✓ 旧表单的提交没有生效，没有创建需求")
        expect_radio(page, "背面的要求", "可接受缺陷")  # 表单显示填入后的值，可以正常提交
    else:
        # 两次点击被合并进同一次运行时，填入先执行，提交的就是新值
        wait_text(page, "您接受的缺陷：背面划痕（最高轻微）。要求完好的部位：正面、拉链。")
        print("    ✓ 提交的是一键填入后的新值")


def pass_path(page: Page, out: Path) -> None:
    print("\n== 路径一：拉链检查通过 → 报价 108.00 → 预留 ==")
    reset(page)
    submit_demo_request(page)
    shot(page, out, "01_waiting_for_inspection")
    inspect_zipper(page, defect=False)
    open_tab(page, "买家")
    wait_text(page, "报价金额：108.00 CAD（合成金额）")
    shot(page, out, "02_offer_108")
    tick(page, "我已查看并接受：背面划痕（轻微）")
    tick(page, "我确认报价金额 108.00 CAD（合成金额）")
    click_button(page, "确认预留")
    wait_text(page, "确认预留：完成")
    wait_text(page, "已预留：RSV-0001｜商品 AM-007｜标签 SYN-T-Q5ZT｜金额 108.00 CAD")
    shot(page, out, "03_reserved")
    open_tab(page, "控制台")
    wait_text(page, "C = 8.00 ≤ B = 20.00")
    shot(page, out, "04_console_pass")


def confirm_point(page: Page, label: str, choice: str) -> None:
    group = page.get_by_role("radiogroup", name=f"{label}：实物与证据是否一致")
    group.get_by_text(choice, exact=True).click()
    print(f"  {label}：{choice}")


def delivery_paths(page: Page, out: Path) -> None:
    """接在通过路径之后：AM-007 已预留（RSV-0001）。"""
    print("\n== 交付：错误标签 → 阻止交付 ==")
    open_tab(page, "交付")
    expect(page.get_by_role("combobox", name="选择要交付的预留")).to_have_value(
        "RSV-0001（商品 AM-007｜买家 B-DEMO-01）", timeout=TIMEOUT_MS)
    print("    ✓ 默认选中新的预留：RSV-0001（商品 AM-007）")
    fill_visible(page, "扫描到的标签码", "SYN-T-9CFL")  # AM-006 的标签
    click_button(page, "核对标签")
    wait_text(page, "交付被阻止（DELIVERY_MISMATCH）")
    wait_text(page, "SYN-T-9CFL（这是 AM-006 的标签）不是预留商品 AM-007 的标签")
    shot(page, out, "07_delivery_wrong_tag")
    open_tab(page, "买家")
    wait_text(page, "已预留：RSV-0001｜商品 AM-007")  # 预留保持不变
    print("    ✓ 预留保持不变，商品仍为已预留")

    print("\n== 交付：正确标签 → 逐项核对 → 已交付 ==")
    open_tab(page, "交付")
    fill_visible(page, "扫描到的标签码", "SYN-T-Q5ZT")
    click_button(page, "核对标签")
    wait_text(page, "标签已核对：SYN-T-Q5ZT 属于预留商品 AM-007（核验 DLV-0002）")
    for label in ("正面", "背面", "拉链"):
        confirm_point(page, label, "一致")
    shot(page, out, "08_delivery_compare")
    click_button(page, "确认交付")
    wait_text(page, "确认交付：完成")
    wait_text(page, "已交付：AM-007（预留 RSV-0001，核验 DLV-0002）")
    shot(page, out, "09_delivery_done")
    open_tab(page, "买家")
    wait_text(page, "已交付：RSV-0001｜商品 AM-007｜标签 SYN-T-Q5ZT")
    wait_text(page, "需求 REQ-0001 的状态：已交付")
    open_tab(page, "控制台")
    wait_text(page, "已交付")
    shot(page, out, "10_console_delivered")


def fail_path(page: Page, out: Path) -> None:
    print("\n== 路径二：拉链检查失败 → NO_MATCH ==")
    reset(page)
    submit_demo_request(page)
    inspect_zipper(page, defect=True)
    open_tab(page, "买家")
    wait_text(page, "目前没有符合您条件的商品")
    wait_text(page, "拉链不是完好状态，不符合您的要求；人工检查结论：拉链卡滞（中等）")
    shot(page, out, "05_no_match")
    open_tab(page, "控制台")
    wait_text(page, "finalize_no_match")
    shot(page, out, "06_console_fail")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8501")
    parser.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "var" / "e2e"))
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--only", choices=["all", "fast", "flows"], default="all")
    parser.add_argument("--fast-repeats", type=int, default=3)
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=not args.headed)
        page = browser.new_page(viewport={"width": 1400, "height": 1000}, locale="zh-CN")
        page.goto(args.url)
        wait_text(page, "AcceptMatch MVP")
        mode = page.get_by_text("编排模式：", exact=False).first.inner_text()
        print(f"页面显示：{mode}")
        if args.only in ("all", "fast"):
            for _ in range(args.fast_repeats):
                fast_prefill_then_submit(page, out)
        if args.only in ("all", "flows"):
            pass_path(page, out)
            delivery_paths(page, out)
            fail_path(page, out)
        browser.close()
    print("\n两条路径都已在浏览器中走通。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
