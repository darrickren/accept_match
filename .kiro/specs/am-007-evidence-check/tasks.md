# 实施计划：AcceptMatch MVP（AM-007）

## 优先级与范围

- **P0**：能运行的 AM-007 完整流程。依次是：合成数据 → 买家请求 → 经济判断 → 人工检查 → 重新匹配 → 证据与报价 → 买家确认预留；同时跑通检查失败后的 `NO_MATCH`。P0 完成后启动本地演示。
- **P1**：交付核验、故障恢复（需求 12.9）、参数组场景、额外页面、文档同步。

P0 用最少的页面和数据表实现。以下关键约束在 P0 中必须保留：

- 逐件匹配资格
- 检查经济门槛和预算扣减
- 价格规则
- 报价绑定 Condition_Record 版本
- 一件商品只有一个有效预留
- 只有买家本人能确认
- 工具重新校验前置条件，被拒时不改变状态
- Agent 只能调用允许的下一步
- 合成数据标记
- 完整的操作记录

### P0 相对 design.md 的简化

| 项目 | P0 做法 | design.md 中的目标 |
|---|---|---|
| 数据表 | 6 张表，见下表 | §5.2 的 24 张表 |
| 页面 | 1 个 Streamlit 页面：侧边栏加 3 个标签页（买家 / 检查员 / 控制台） | §8 的 6 个页面 |
| 只读配置 | 型号、价格规则、买家、检查方法、经济参数在重置时从种子 JSON 校验后加载到内存，并在 `meta` 中记录种子哈希 | 存入数据库表 |
| 未完成任务唯一性 | 每件商品最多一个 `OPEN` 检查任务 | 按商品和部位分别唯一 |
| Agent 运行记录 | 用审计事件 `AGENT_RUN_STARTED` / `AGENT_RUN_ENDED` 记录 | `agent_runs` 表（P1 任务 10） |
| 参数组 | 只用默认参数组，但领域层完整实现 `SKIP` 逻辑并有测试 | 重置时可选三个参数组（P1 任务 11） |
| 编排模式 | `AGENT_MODE=bedrock`（默认）；另有 `scripted`，按 `allowed_next_actions` 的第一项执行，用于测试和离线演示，界面上明确标注"离线脚本编排，非 Bedrock" | 只有 Bedrock |

P0 的 6 张表：

| 表 | 内容 | 关键约束 |
|---|---|---|
| `meta` | 单行：`epoch`、`parameter_set`、`seed_hash`、`budget_initial_cents`、`budget_remaining_cents`、`budget_version`、`reset_at` | `CHECK(budget_remaining_cents >= 0)` |
| `items` | `item_id`、`product_id`、`tag_code`、`item_status`、`condition_version`、`synthetic` | `UNIQUE(tag_code)` |
| `condition_versions` | `item_id`、`version`、`points_json`（含缺陷和 Evidence）、`source_ref`、`created_at` | 主键 `(item_id, version)`，只追加 |
| `buyer_requests` | `request_id`、`buyer_id`、`product_id`、`accepted_defects_json`、`required_conditions_json`、`status`、`exclusions_json`、`no_match_json`、`created_at` | — |
| `artifacts` | `artifact_id`、`kind`（`EVALUATION` / `DECISION` / `INSPECTION_TASK` / `EVIDENCE_PACKAGE` / `QUOTE` / `RESERVATION`）、`request_id`、`item_id`、`condition_version`、`status`、`natural_key`、`content_json`、`created_at` | `UNIQUE(kind, natural_key)` 用于幂等；部分唯一索引保证每件商品最多一个 `ACTIVE` 的 `RESERVATION` 和一个 `OPEN` 的 `INSPECTION_TASK` |
| `audit_events` | `epoch`、`event_id`、`ts`、`actor_type`、`actor_id`、`event_type`、`item_id`、`request_id`、`related_ids_json`、`payload_json`、`outcome` | 只追加，重置时不删除；Agent 工具调用的工具名、参数、结果和理由都记在 `payload_json` |

### P0 完成标准

1. 默认参数组下提交演示请求，AM-007 的判定为 ΔEV = 33.40、`INSPECT`，创建拉链检查任务，B 从 20.00 变为 12.00。录入拉链 `OK` 后，展示证据、报价 108.00；买家确认后 AM-007 变为 `RESERVED`（需求 14.1–14.3）。
2. 重置后录入拉链 `DEFECT`（卡滞，中等），AM-007 以 `REQUIREMENT_NOT_MET` 被排除，请求变为 `NO_MATCH`，列出十件商品的排除原因，AM-007 保持 `AVAILABLE`（需求 14.5）。
3. 两条路径的每一步都有审计记录，Agent 的每次工具调用都带理由。
4. 测试全部通过，Streamlit 已在 `localhost` 上启动。

## 任务

### P0

- [x] 1. 项目骨架与运行环境
  - 安装 Python 3.12（本机目前是 3.9.6），在仓库根目录创建 `.venv`
    - 实际做法：Homebrew 的 python@3.12 在本机 macOS 26.2 上 `pyexpat` 无法加载，导致 pip 不可用，因此改用 `uv` 管理的 CPython 3.12.15；之后统一用 `uv pip install --python .venv/bin/python -r requirements.txt` 安装依赖
  - 创建 `requirements.txt`，精确锁定 `strands-agents==1.61.0`、`streamlit==1.66.0`，以及 pydantic、boto3、pytest、hypothesis 的具体版本
  - 创建目录 `acceptmatch/`（`domain`、`store`、`services`、`tools`、`agent`、`ui`）、`app/`、`data/seed/`、`tests/`
  - 在 `.gitignore` 中加入 `var/`、`.venv/`、`__pycache__/`、`.pytest_cache/`、`.hypothesis/`
  - 创建 `.streamlit/config.toml`，设置 `server.address = "localhost"`
  - 编写 `acceptmatch/config.py`，读取 design §7.1 的环境变量和 `AGENT_MODE`、`DB_PATH`、`SEED_DIR`；`SEED_DIR` 必须位于仓库的 `data/seed/` 下
  - 编写冒烟测试：依赖可以导入，配置默认值正确，`SEED_DIR` 指向仓库外时拒绝
  - _需求：1.3、12.1_

- [x] 2. 合成种子数据与校验
  - 实际做法：
    - 种子模型和加载器放在 `acceptmatch/seed/`（`models.py`、`loader.py`）
    - 枚举需要在这里用到，所以先写了 `acceptmatch/domain/enums.py`（任务 3.1 中的枚举部分），任务 3.1 只需要补领域模型
    - `dataset.json` 另外加入了 `demo_request`（演示请求，供页面一键填入和测试使用）；AM-006 的种子预留编号为 `RSV-SEED-01`，与运行时编号分开
    - `parse_seed` 只做纯校验，不访问文件，测试可以直接构造无效种子；`load_seed` 只读取 `data/seed/` 下的两个文件，会把符号链接解析成真实路径后再检查
  - [x] 2.1 编写 `data/seed/dataset.json`
    - 包含 2 个型号、价格规则、检查方法、2 个买家、10 件商品（各部位状态、缺陷和 Evidence），以及 AM-006 给 B-DEMO-02 的初始预留
    - 内容与 requirements.md 的"合成演示数据"一节一致，每条记录都带 `synthetic: true`
    - _需求：1.1、1.2_
  - [x] 2.2 编写 `data/seed/economics.json`
    - 包含 `base` 和 `default`、`budget_low`、`low_pass` 三个参数组，内容按 design §10
    - _需求：4.2_
  - [x] 2.3 编写种子模型和加载校验（Pydantic）
    - Item_ID 和 Tag_Code 不重复
    - 每个 `OK`/`DEFECT` 部位都有 Evidence
    - 所有记录 `synthetic: true`
    - 不检查选项的 `kind` 只能是 `LIQUIDATE_AS_IS` 或 `RETURN_TO_VENDOR`
    - p ∈ [0, 1]，金额不为负
    - 校验失败时返回 `SEED_INVALID` 和问题清单
    - _需求：1.4、1.5、4.4、4.10_
  - [x]* 2.4 种子校验测试：正常种子通过；分别构造重复 Tag_Code、缺少 Evidence、非法选项类型等情况，确认都被拒绝
    - _需求：1.4、1.5_

- [x] 3. 确定性领域层（纯函数）
  - 实际做法：
    - 新增 `domain/money.py`（ROUND_HALF_UP 舍入和显示）、`seed/convert.py`（把种子记录转换成领域模型）
    - 新增内部原因码 `UNVERIFIED_POINTS`：有 UNKNOWN 部位时 `price_item` 拒绝报价，只有经济判断可以用 `assume_unknown_ok`
    - 部位缺失一律视为 UNKNOWN
    - 已用变异测试确认：把"UNKNOWN 当作通过"或"不检查缺陷是否被接受"写进代码后，测试会失败
  - [x] 3.1 编写枚举和领域模型（design §3.1），金额统一用 `Decimal`
  - [x] 3.2 编写 `matching`：计算所有适用的原因码，叠加请求排除项，给出三类结果和排名（design §4.1）
    - _需求：3.1–3.11_
  - [x] 3.3 编写 `pricing`：折扣率求和、50% 价格下限、ROUND_HALF_UP，并支持 `assume_unknown_ok`；缺少规则时返回 `PRICE_RULE_MISSING`（design §4.2）
    - _需求：9.2、9.4、4.3_
  - [x] 3.4 编写 `economics`：按步骤计算并四舍五入，同时完成预算和 ΔEV 检查，参数无效时返回 `ECONOMICS_INPUT_INVALID`，输出 `steps` 和建议处置（design §4.3）
    - _需求：4.3–4.10、4.12、4.16_
  - [x]* 3.5 领域测试
    - 十件商品对演示请求的结果与商品表一致；AM-007 通过后报价 108.00
    - 三个参数组的数值：78.40 / 33.40 / `INSPECT`；预算 5.00 时 `INSPECTION_OVER_BUDGET`；p = 0.10 时 37.80 / −7.20 / `INSPECTION_NOT_WORTHWHILE`
    - 用 Hypothesis 验证 design §11.1 的属性 1、2、3、5、6
    - _需求：3、4、9、14.1_

- [x] 4. 精简存储、重置与操作记录
  - 实际做法：
    - 文件：`store/schema.sql`、`store/db.py`（连接、`write_tx`、规范化 JSON、`state_hash`）、`store/ids.py`、`store/audit.py`、`store/repo.py`（只读查询）、`services/reset.py`
    - 约束全部在数据库层：
      - `meta` 只有一行，预算 `0 ≤ 余额 ≤ 初始值`
      - 有效预留、未完成检查任务都用部分唯一索引限制为每件商品一个
      - `artifacts` 的 kind 与 status 组合受 CHECK 约束
      - 所有业务表要求 `synthetic = 1`，JSON 列必须是合法 JSON
      - `audit_events` 用触发器禁止 UPDATE/DELETE；`condition_versions` 禁止 UPDATE
    - 审计事件编号按轮次从 `EVT-0001` 开始，主键为 `(epoch, seq)`；首次重置之前被拒的操作记在第 0 轮
    - 新一轮编号取 `meta` 与审计中最大轮次再加一，所以即使 `meta` 丢失，编号也不会和历史审计冲突
    - 重置被拒（种子无效、未知参数组、自检失败、中途出错）时，在单独的事务中写 `DEMO_RESET_REJECTED`，旧状态保持不变
    - `ensure_initialized` 在事务内再次确认尚未初始化，多个会话同时首次启动时只会重置一次；以后启动会核对种子哈希
  - [x] 4.1 编写 `store/schema.sql`，包含上述 6 张表和约束；连接辅助函数设置 WAL、`foreign_keys`、`busy_timeout`，写操作使用 `BEGIN IMMEDIATE`
  - [x] 4.2 编写审计写入（只追加）和按 `epoch` 与类型前缀生成的序号 ID（例如 `REQ-0001`）
    - _需求：13.1、13.2_
  - [x] 4.3 编写 `reset_demo`
    - 先校验种子，再在一个事务内：`epoch` 加一，清空 `audit_events` 以外的表，写入种子和 AM-006 的预留，设置预算
    - 写审计 `DEMO_RESET`
    - 首次启动且数据库不存在时自动重置
    - 重置后自检：10 件商品、AM-006 为 `RESERVED`、AM-007 拉链为 `UNKNOWN`
    - _需求：1.1、1.6_
  - [x]* 4.4 存储测试：连续两次重置后状态哈希相同；同一商品的第二个 `ACTIVE` 预留被唯一索引拒绝；预算不能写成负数
    - _需求：1.6、10.6_

- [x] 5. 工具层与服务
  - 实际做法：
    - 文件：`tools/envelope.py`（返回结构、`ToolCode`、`Actor`、`Rejection`）、`tools/state.py`（请求状态读取和 `plan()`）、`tools/runner.py`（一次调用一个事务）、`tools/runtime.py`、`tools/agent_tools.py`、`tools/human_ops.py`、`ui/messages.py`
    - `plan()` 是唯一的下一步规则：`allowed_next_actions` 由它生成，Agent 工具在事务内用 `require_allowed()` 校验调用是否与 `plan()` 完全一致
    - 被拒时回滚全部业务写入，单独追加一条 `OPERATION_REJECTED` 审计；未预期异常返回 `INTERNAL_ERROR`，工具不向调用方抛异常
    - 新增工具层原因码：`EVALUATION_STALE`、`DECISION_NOT_FOUND`、`REQUEST_NOT_FOUND`、`TASK_NOT_FOUND`、`TASK_NOT_OPEN`、`UNKNOWN_BUYER`、`ACTOR_NOT_ALLOWED`、`CONFIRMATION_NOT_FROM_BUYER`、`QUOTE_NOT_FOUND`、`QUOTE_NOT_ACTIVE`、`SEED_CHANGED`
    - 人工操作校验操作者类型：只有 buyer 能提交需求和确认、拒绝报价；只有 inspector 能提交检查结果。确认预留不在 Agent 工具表中
    - 经济判断的决定键包含预算版本，预算变化后旧判定自动失效（`DECISION_STALE`）
    - 证据整理后商品如果发生变化，下一步改为重新评估，按旧证据报价会被拒（`EVALUATION_STALE`）
    - 重新评估时，旧的 `ACTIVE` 报价一律失效
    - 运行时新增的 Evidence 和缺陷编号为 `EV-0001`、`DF-0001` 起；`INCONCLUSIVE` 的证据引用只记在任务结果中，部位保持 UNKNOWN
    - 审计按请求查询时，同时匹配 `related_ids`（一条检查结果可以关联多个请求）
    - 领域层补充 `EconomicsInputs.input_problems`：参数中的待检查部位与商品的未核实部位不一致时，判定为 `ECONOMICS_INPUT_INVALID`
  - [x] 5.1 编写统一返回结构、前置条件函数和 `allowed_next_actions`（design §6.1、§6.4），工具执行和下一步计算共用同一组前置条件
    - _需求：12.2、12.5_
  - [x] 5.2 编写 7 个 Agent 工具：`get_request_status`、`evaluate_request`、`decide_inspection`、`create_inspection_task`、`build_evidence_package`、`generate_quote`、`finalize_no_match`
    - 每个工具在事务内重新校验前置条件
    - 被拒时业务状态不变，单独写一条拒绝审计
    - 按自然键幂等重放
    - `create_inspection_task` 在事务内重新检查 `C ≤ B`，然后扣预算；关联已有任务时不扣
    - _需求：3、4.1、4.11、4.13、5.1–5.6、7.1–7.5、8.1–8.5、9.1–9.3、12.3、12.5_
  - [x] 5.3 编写人工操作：`submit_buyer_request`、`submit_inspection_result`、`confirm_reservation`、`decline_quote`
    - `submit_buyer_request` 检查部位冲突并回显请求
    - `submit_inspection_result` 校验 Tag_Code、证据引用，处理 `INCONCLUSIVE`，写入新版本，商品保持 `AVAILABLE`
    - `confirm_reservation` 依次校验报价归属、商品与报价对应、缺陷清单和金额是否逐项确认、版本、可售状态；因版本变化或不可售被拒时，请求改为 `NEEDS_REEVALUATION`
    - _需求：2.1–2.4、6.1–6.3、6.7、6.8、10.1–10.7_
  - [x] 5.4 编写面向买家的原因码消息模板，模板中不包含经济参数
    - _需求：4.15、7.4、12.7_
  - [x]* 5.5 工具测试
    - 每个前置条件对应的拒绝原因码
    - 被拒的调用前后业务表状态哈希相同
    - 重放不会重复扣预算，也不会重复建任务
    - 确认时的各类拒绝
    - 对应 design §11.1 的属性 4、7、8、9
    - _需求：10、12.3_

- [x] 6. Agent 编排
  - 实际做法：
    - 文件：`agent/orchestrator.py`（运行管理、守卫、`ScriptedDriver`）、`agent/strands_driver.py`（`build_bedrock_model`、7 个 Strands 工具、`StrandsDriver`）、`agent/guards.py`（参数模型、数值守卫）、`agent/prompts.py`、`agent/flow.py`（人工操作后继续）；测试用模型替身 `tests/fakes.py`
    - 模型编号和区域只从环境变量读取：`BEDROCK_MODEL_ID`、`AWS_REGION`（没有时用 `AWS_DEFAULT_REGION`），不再有内置默认值；缺少时 bedrock 模式返回 `CONFIG_ERROR`，不写入任何数据（design §7.1 中的默认值待任务 13 同步删除）
    - 一次运行在没有 Agent 可做的动作时结束（等待检查员 / 等待买家 / 终态）；人工操作成功后由 `flow.continue_after()` 启动新运行
    - 没有可做的动作或运行已停止时，工具设置 Strands 的 `stop_event_loop`，不会再多发一次模型调用（通过路径共 8 次模型调用，对应 8 次工具调用）
    - 守卫在调用工具前先用与工具相同的前置条件检查是否在 `allowed_next_actions` 中；工具在事务内仍会再查一次。新增守卫原因码 `ACTION_NOT_ALLOWED`（请求编号不属于本次运行）、`RUN_HALTED`
    - 每次调用写一条 `AGENT_TOOL_CALL` 审计，包含参数、原始理由、经过数值守卫的理由和完整结果；运行另有 `AGENT_RUN_STARTED` / `AGENT_RUN_ENDED` / `AGENT_RUN_REFUSED`
    - P0 先实现轻量暂停：运行以 `FAILED_BEDROCK`、`TIMEOUT` 或 `GUARD_STOPPED` 结束后，同一请求的新运行会被拒（`RUN_HALTED`），直到以 `resume=True` 恢复。暂停期间阻止人工操作、同一请求只允许一个运行中的 Agent，留到任务 10
    - 运行记录如实标注驱动：脚本模式标为"离线脚本编排（非 Bedrock）"，替身模型标为"Strands + FakeModel（非 Bedrock）"，只有 BedrockModel 标为"Strands + Amazon Bedrock"
    - SQLite 连接改为允许跨线程使用（Strands 在工作线程中执行工具；一次运行内工具按顺序执行）
    - 尚未用真实 Bedrock 验证：本机没有设置 `BEDROCK_MODEL_ID`、`AWS_REGION`，也没有 AWS 配置文件。已有带 `bedrock` 标记的测试，设置 `RUN_BEDROCK_TESTS=1` 和上述环境变量后运行
  - [x] 6.1 编写 Orchestrator
    - 按 design §7.2 构建 `BedrockModel` 和 `Agent`（顺序执行器、`retry_strategy=None`、超时配置）
    - 每次运行用工厂函数生成绑定 `run_id` 的工具
    - 系统提示按 design §7.3
    - 写审计 `AGENT_RUN_STARTED` / `AGENT_RUN_ENDED`
    - _需求：12.1、12.6、12.8、13.3_
  - [x] 6.2 编写工具守卫
    - 记录 `rationale`
    - 用 Pydantic 校验参数
    - 同一运行内不允许以相同参数重复被拒绝的调用
    - 限制工具调用次数和运行时长
    - 对 Agent 文本做数值守卫（design §7.7）
    - _需求：12.4、12.7、13.3_
  - [x] 6.3 编写 `ScriptedAgent`，总是执行 `allowed_next_actions` 的第一项；通过 `AGENT_MODE=scripted` 启用，界面上标注
  - [x] 6.4 Bedrock 异常处理（P0 版本）：捕获异常，记录 `AGENT_RUN_ENDED`（失败原因），界面提示错误；已提交的操作保留，不自动重试
    - _需求：12.9（部分，完整实现见任务 10）_
  - [x]* 6.5 场景测试（`ScriptedAgent`，不调用 Bedrock）
    - 通过路径：一直到 `RESERVED`，B 从 20.00 变为 12.00，报价 108.00
    - 失败路径：`NO_MATCH`，十件商品的原因都列出，AM-007 保持 `AVAILABLE`
    - 两条路径中，每个工具调用都有带理由的审计记录
    - _需求：14.1–14.3、14.5、13_

- [x] 7. Streamlit 单页演示（`app/streamlit_app.py`）
  - 实际做法：
    - 文件：`app/streamlit_app.py`（页面）、`acceptmatch/ui/context.py`（配置、连接、运行时、编排器、重置）、`acceptmatch/ui/views.py`（只读视图）
    - 页面只调用现有服务：写操作只有 `human_ops`、`flow.continue_after`、`AppContext.reset`；读操作走 `ui/views.py`，它只用 `repo`、`audit`、`tools.state` 的只读查询（新增只读的 `state.list_requests`）。测试会静态检查页面和 `ui/` 代码中不含 SQL 或写事务
    - 新增编排模式 `AGENT_MODE=stub`：Strands Agent 循环 + 本地替身模型（`agent/stub_model.py`），不访问网络。页面标题、侧边栏、控制台和每条运行记录都显示当前模式：
      - "离线脚本编排（非 Bedrock）"
      - "Strands + 替身模型（非 Bedrock）"
      - "Strands + Amazon Bedrock｜模型：…｜区域：…（来自环境变量）"
      - bedrock 模式缺少环境变量时，侧边栏显示需要设置哪些变量；提交需求后的运行返回"配置缺失"
    - 确认表单把买家勾选的缺陷和金额原样交给 `confirm_reservation`，由服务判断是否逐条确认，页面不替买家补全
    - 检查结论默认不选，需要检查员主动选择；检查员页面不显示正确的标签码
    - 修复"一键填入"后过快点击"提交需求"会提交旧表单值的问题：
      - "一键填入"不再直接改控件的值，而是生成新版本的表单（控件键带版本号），预设值作为控件的默认值下发
      - 旧版本表单的提交回调发现版本过期会拒绝，并提示核对后再提交
      - 浏览器中连续 10 次"填入后立即提交"，都没有用旧值创建需求，表单随后都显示预设值（`scripts/e2e_walkthrough.py --only fast`）
    - 浏览器走查中发现并修复两个问题：
      - "选择需求"下拉框的选项文字原来包含状态，状态变化后会显示过期的文字；现在只显示编号和型号，状态单独显示在下方
      - 运行说明原来会重复"等待买家"之类的提示
    - 自动化测试 `tests/test_app.py`（Streamlit AppTest）覆盖通过路径、失败路径、标签不一致、控制台计算依据、三种模式显示
    - 恢复按钮属于 P1 任务 10，控制台目前只提示运行失败，并说明可以重置
  - [x] 7.1 侧边栏：合成数据标签、当前参数组、预算 B、`epoch`、编排模式和模型 ID；重置按钮（需要先勾选确认）
    - _需求：1.2、1.6_
  - [x] 7.2 买家标签页
    - 结构化需求表单，可一键填入演示请求，提交后回显
    - 等待检查时显示商品和部位
    - 先显示证据包，再显示报价明细
    - 逐条勾选确认缺陷并显示金额，然后可以确认或拒绝
    - `NO_MATCH` 时按模板逐件说明原因
    - _需求：2.4、5.7、7.4、8.1–8.5、9.3、10.1_
  - [x] 7.3 检查员标签页：未完成任务、检查方法、标签扫描输入、逐部位结论（`OK` / `DEFECT` / `INCONCLUSIVE`）、合成证据引用；提交后触发 Agent 运行
    - _需求：5.3、6.1、6.2_
  - [x] 7.4 控制台标签页
    - Inspection_Decision 的完整计算依据（输入参数带"合成"标签，显示步骤表、不检查选项、预算比较、判定）
    - 十件商品的状态和版本
    - 可以按 Item_ID 或请求筛选的审计时间线，包含 Agent 工具调用和经过守卫的理由
    - _需求：4.14、13.1–13.4_
  - 写操作只在按钮回调中执行；业务数据每次渲染都从数据库读取；状态同时用文字表达，不只靠颜色

- [x] 8. 检查点：P0 验证并启动本地演示
  - 实际结果：
    - 全部测试：232 通过，1 跳过（真实 Bedrock 测试，需要凭证）
    - 应用以 `AGENT_MODE=scripted` 在后台运行，只监听 `127.0.0.1:8501`
    - 用 Playwright 驱动本机 Chrome 在真实浏览器中点击（`scripts/e2e_walkthrough.py`，依赖写在 `requirements-e2e.txt`，截图保存在被忽略的 `var/e2e/`）：
      - 通过路径：重置 → 一键填入演示请求 → 提交 → 等待检查 → 检查员扫描 `SYN-T-Q5ZT`、拉链完好 → 报价 108.00 → 逐条确认 → 预留 `RSV-0001`
      - 失败路径：重置 → 同样提交 → 拉链卡滞（中等）→ `NO_MATCH`，十件商品逐件说明原因
      - scripted 和 stub 两种模式都走通；stub 模式的运行记录标注为"Strands + 替身模型（非 Bedrock）"
    - 未用 Bedrock：本机没有 `BEDROCK_MODEL_ID`、`AWS_REGION` 和 AWS 凭证
    - 走查脚本在每次点击后会等 Streamlit 重新运行结束（`data-test-script-state="notRunning"`）；否则在页面更新前就点"提交需求"，会提交旧的表单值
  - 运行全部测试
  - 用 `streamlit run app/streamlit_app.py` 在 `localhost` 后台启动
  - 手动走一遍通过路径，重置后再走一遍失败路径，对照 P0 完成标准逐项核对
  - 如果 Bedrock 凭证可用，用 `AGENT_MODE=bedrock` 跑一遍；不可用时说明情况，并用 `scripted` 模式演示
  - 确认无误后再进入 P1

### P1

- [x] 9. 交付核验
  - 实际做法：
    - 文件：`tools/delivery_ops.py`（人工操作，不暴露给 Agent）；页面在现有单页上增加"交付"标签页（买家 / 检查员 / 交付 / 控制台），没有拆分页面
    - 不改表结构：每次核验（`DLV-0001` 起）记录在预留记录的 `content_json["delivery_checks"]` 中，同时写审计 `DELIVERY_CHECK_STARTED` / `DELIVERY_MISMATCH` / `CONDITION_MISMATCH` / `DELIVERY_COMPLETED`
    - 交付时对照的是买家确认时那个状态版本的证据（预留记录上的 `condition_version`），不是当前版本
    - 标签不一致时：记录预留的商品、扫描到的标签，以及这个标签属于哪件商品；预留和商品状态都不变，换成正确的商品后可以继续交付
    - 有部位不一致时：阻止交付，标记 `needs_manual_review`，之后该预留不能再发起核验（`MANUAL_REVIEW_REQUIRED`）。人工处理流程不在本任务范围内
    - 三个部位都必须明确确认（`FINDING_INCOMPLETE`）；同一预留同一时间只能有一次核验等待确认（`DELIVERY_CHECK_PENDING`）；只有 delivery 角色、合成编号 `SYN-DELIVERY-xx` 能操作
    - 新增原因码：`RESERVATION_NOT_ACTIVE`、`DELIVERY_MISMATCH`、`CONDITION_MISMATCH`、`DELIVERY_CHECK_PENDING`、`DELIVERY_CHECK_NOT_PENDING`、`MANUAL_REVIEW_REQUIRED`
    - 种子预留 `RSV-SEED-01`（AM-006）也可以按同样流程交付
    - 浏览器走查（`scripts/e2e_walkthrough.py`）：
      - 预留 AM-007 后先扫描 `SYN-T-9CFL`：交付被阻止，提示这是 AM-006 的标签，买家页面仍显示已预留
      - 再扫描 `SYN-T-Q5ZT`：三个部位都选"一致"后确认，商品、预留和需求都变为已交付
  - [x] 9.1 编写 `start_delivery_check` 和 `complete_delivery_check`：标签不一致时记录 `DELIVERY_MISMATCH`；逐部位比对证据，有不一致时记录 `CONDITION_MISMATCH` 并转人工；全部通过后商品改为 `DELIVERED`
    - _需求：11.1–11.6_
  - [x] 9.2 增加交付标签页（或独立页面）
  - [x]* 9.3 场景测试：扫描 `SYN-T-Q5ZT` 后交付完成；扫描 `SYN-T-9CFL` 时被阻止，AM-007 保持 `RESERVED`；design §11.1 的属性 11
    - _需求：14.4、14.6_

- [ ] 10. 故障恢复（需求 12.9）
  - [ ] 10.1 增加 `agent_runs` 表，并用部分唯一索引保证同一请求只有一个 `RUNNING` 运行；在 `buyer_requests` 中增加 `halted`、`halt_reason`、`halted_run_id`
  - [ ] 10.2 按 design §7.4 区分运行状态；遇到 `FAILED_BEDROCK`、`TIMEOUT`、`GUARD_STOPPED`、`INTERRUPTED` 时暂停请求，所有写操作检查 `FLOW_HALTED`
  - [ ] 10.3 编写 `resume_flow`、启动时把遗留的 `RUNNING` 标为 `INTERRUPTED`，并在控制台增加"恢复"和"继续"按钮
  - [ ]* 10.4 故障测试：`ScriptedAgent` 在指定步骤后抛出模拟超时，验证已提交的操作保留、暂停期间写操作被拒、恢复后能完成流程；design §11.1 的属性 10
    - _需求：12.9_

- [ ] 11. 参数组场景
  - [ ] 11.1 重置时可选择 `default`、`budget_low`、`low_pass` 三个参数组
    - _需求：1.6_
  - [ ]* 11.2 场景测试：预算不足时判定 `SKIP`（`INSPECTION_OVER_BUDGET`，ΔEV 33.40），B 保持 5.00，结果 `NO_MATCH`；低通过率时为 37.80 / −7.20，B 保持 20.00，结果 `NO_MATCH`；两种情况都记录建议处置
    - _需求：14.7、14.8_

- [ ] 12. 额外页面
  - 用 `st.navigation` 拆成 design §8 的 6 个页面：演示控制台、买家、检查员、交付、检查决策、商品与记录（含 Condition_Record 版本历史）
  - _需求：13.4、4.14_

- [ ] 13. 收尾
  - 把 P0 的简化和实际实现同步到 design.md
  - 增加带 `bedrock` 标记、默认跳过的 Bedrock 冒烟测试
  - 编写 README，说明运行步骤、AWS 凭证配置，以及"只在本机运行、没有身份认证"的限制
