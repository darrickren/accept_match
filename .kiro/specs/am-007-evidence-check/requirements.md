# 需求文档：AcceptMatch MVP（AM-007 缺陷证据核验与匹配）

## 简介

AcceptMatch 把一件具体的已退商品匹配给明确接受其已核实缺陷的买家。匹配对象是单件实物，不是商品型号或 SKU。

核心原则：

- 每件商品用 Item_ID 和实物上的 Tag_Code 唯一标识，报价、预留、交付都绑定同一件商品。
- 只有已核实的缺陷才能被买家接受。未知状态既不能当作完好，也不能当作买家已接受。
- 证据未知时不做推测。是否安排人工检查由确定性的经济判断决定：检查成本必须在预算内，且检查的增量预期价值必须为正。
- 买家确认具体商品、缺陷清单和报价后才预留。
- 运行在 Amazon Bedrock 上的 Strands Agent 根据工具结果选择下一步。资格、经济判断、价格、预留和交付约束由确定性代码执行，Agent 无法绕过。

MVP 演示流程：买家接受背面轻微划痕，要求正面完好、拉链正常。AM-007 背面轻微划痕和正面完好均已核实，拉链状态未知。系统先做检查前的经济判断，并展示计算依据；只有检查在预算内且增量预期价值为正，才创建人工检查并等待结果。检查通过后，系统展示证据、按价格规则报价，买家确认后预留，交付时核验是同一件商品。检查失败时，系统重新匹配；没有合适商品时向买家说明原因。

MVP 只使用本文档定义的十件合成商品和合成经济参数，不使用任何真实数据。

不在范围内：真实数据接入（包括 `Hackathon/Dataset` 下的 CSV）、由 Agent 或模型估计概率与成本、执行不检查时的建议处置（如清仓）、支付、物流配送、图像自动识别缺陷、自然语言解析买家需求、预留过期、界面视觉设计。

## 术语表

- **AcceptMatch**：本 MVP 系统。
- **Item**：一件具体的已退商品，用 Item_ID（AM-001 至 AM-010）标识。
- **Tag_Code**：贴在实物上、可扫描的唯一标签码，用于核验实物身份。
- **Product**：商品型号，MVP 中为 `SYN-BAG-A` 和 `SYN-BAG-B`（合成手提包型号）。
- **Inspection_Point**：检查部位，MVP 固定为正面、背面、拉链三个。
- **Point_Status**：部位状态。`OK` 表示已核实无缺陷（拉链为能完整开合、无卡滞）；`DEFECT` 表示已核实存在一个或多个缺陷；`UNKNOWN` 表示未核实。
- **Verified_Defect**：已核实的缺陷，包含部位、缺陷类型和程度（轻微 < 中等 < 严重），并至少有一条 Evidence。
- **Evidence**：支撑某个部位状态的记录，包含来源（入库检测或人工检查）、检查员标识、时间和照片引用。
- **Condition_Record**：一件商品所有部位的状态和 Evidence，带版本号，每次更新版本号加一。
- **Item_Status**：商品状态。`INTAKE` 表示已退回但未完成入库检测，不参与匹配；另有 `AVAILABLE`、`RESERVED`、`DELIVERED`。
- **Buyer_Request**：买家的结构化需求，包含 Product、Accepted_Defects 和 Required_Conditions。
- **Accepted_Defects**：买家明确接受的缺陷，每条包含部位、缺陷类型和可接受的最高程度。
- **Required_Conditions**：买家要求必须为 `OK` 的部位。
- **Match_Result**：单件商品针对一个 Buyer_Request 的评估结果，取值为 `QUALIFIED`、`NEEDS_INSPECTION`、`EXCLUDED`，排除时附原因码。
- **Inspection_Economics**：一次检查决策使用的合成经济参数，包括检查成本 C、通过概率 p、交付处理成本 H、失败后净回收 R_fail、不检查时的可选处置，以及检查预算余额 B。
- **Pass_Probability（p）**：检查后该商品对当前 Buyer_Request 变为 `QUALIFIED` 的概率。结论为 `INCONCLUSIVE` 计为未通过。
- **Net_Recovery**：某种结果下扣除相应成本后的预计回收金额。R_pass 为检查通过后的净回收，R_fail 为检查失败后的净回收。
- **No_Inspection_Best（V_none）**：不检查时所有允许的处置选项中净回收最高的一项。
- **Incremental_EV（ΔEV）**：检查的预期价值减去 V_none。
- **Inspection_Budget（B）**：可用于人工检查的合成预算余额。
- **Inspection_Decision**：一次检查前经济判断的记录，包含输入参数、计算过程、判定（`INSPECT` 或 `SKIP`）和原因码。
- **Inspection_Task**：针对一件商品的 `UNKNOWN` 部位的人工检查任务。
- **Quote**：针对一件 `QUALIFIED` 商品的报价，绑定 Item_ID 和 Condition_Record 版本号。
- **Reservation**：买家确认后为该买家锁定一件商品的预留记录。
- **Delivery_Check**：交付时核验实物与预留商品是同一件的过程。
- **Orchestrator_Agent**：使用 Strands Agents 构建、调用 Amazon Bedrock 模型的编排 Agent，根据工具结果选择下一步。
- **Deterministic_Tool**：用确定性代码实现、供 Orchestrator_Agent 调用的工具。它负责执行所有资格、经济判断、价格、预留和交付约束，也是唯一可以变更状态的途径。

## 合成演示数据

以下数据全部为合成数据，仅用于 MVP 演示，与任何真实客户、订单或商品无关。

### 商品（10 件）

| Item_ID | Product | Tag_Code | Item_Status | 正面 | 背面 | 拉链 | 对演示请求的预期结果 |
|---|---|---|---|---|---|---|---|
| AM-001 | SYN-BAG-A | SYN-T-K3P9 | AVAILABLE | DEFECT：划痕（轻微） | OK | OK | EXCLUDED：REQUIREMENT_NOT_MET（正面） |
| AM-002 | SYN-BAG-A | SYN-T-7XQ2 | AVAILABLE | OK | OK | DEFECT：卡滞（中等） | EXCLUDED：REQUIREMENT_NOT_MET（拉链） |
| AM-003 | SYN-BAG-A | SYN-T-M8D4 | AVAILABLE | OK | DEFECT：污渍（轻微） | OK | EXCLUDED：UNACCEPTED_DEFECT（缺陷类型未被接受） |
| AM-004 | SYN-BAG-A | SYN-T-2RVN | AVAILABLE | OK | DEFECT：划痕（中等） | OK | EXCLUDED：UNACCEPTED_DEFECT（程度超出接受范围） |
| AM-005 | SYN-BAG-B | SYN-T-W6HJ | AVAILABLE | OK | DEFECT：划痕（轻微） | OK | EXCLUDED：PRODUCT_MISMATCH |
| AM-006 | SYN-BAG-A | SYN-T-9CFL | RESERVED（B-DEMO-02） | OK | DEFECT：划痕（轻微） | OK | EXCLUDED：NOT_AVAILABLE |
| AM-007 | SYN-BAG-A | SYN-T-Q5ZT | AVAILABLE | OK | DEFECT：划痕（轻微） | UNKNOWN | NEEDS_INSPECTION（拉链） |
| AM-008 | SYN-BAG-A | SYN-T-H1BE | AVAILABLE | UNKNOWN | DEFECT：凹陷（中等） | OK | EXCLUDED：UNACCEPTED_DEFECT（不做经济判断，不创建检查） |
| AM-009 | SYN-BAG-A | SYN-T-D7YA | INTAKE | UNKNOWN | UNKNOWN | UNKNOWN | EXCLUDED：NOT_AVAILABLE |
| AM-010 | SYN-BAG-A | SYN-T-6NUG | AVAILABLE | OK | DEFECT：划痕（轻微）+ 污渍（轻微） | OK | EXCLUDED：UNACCEPTED_DEFECT（污渍未被接受） |

表中每个 `OK` 或 `DEFECT` 部位都附有一条合成的入库检测 Evidence，照片引用等具体字段在 design 阶段定义。

### 买家

| 买家 | 说明 |
|---|---|
| B-DEMO-01 | 演示买家，提交演示请求 |
| B-DEMO-02 | 背景买家，初始持有 AM-006 的 Reservation |

演示请求（B-DEMO-01）：

- Product：`SYN-BAG-A`
- Accepted_Defects：背面 / 划痕 / 最高轻微
- Required_Conditions：正面、拉链

### 价格规则（合成）

- 基础价格：`SYN-BAG-A` 120.00，`SYN-BAG-B` 150.00（合成金额，按 CAD 显示）。
- 每个 Verified_Defect 的折扣率按程度确定：轻微 10%，中等 25%，严重 40%。
- 报价 = 基础价格 ×（1 − 所有 Verified_Defect 折扣率之和），不低于基础价格的 50%，四舍五入到 0.01。
- 演示预期：AM-007 检查通过后报价为 120.00 × (1 − 10%) = 108.00。

### 检查经济参数（合成）

以下参数全部为合成值，仅用于演示经济判断，不来自真实数据，也不由 Agent 或模型估计。

AM-007 拉链检查（默认参数组）：

| 参数 | 符号 | 合成值 | 说明 |
|---|---|---|---|
| 检查成本 | C | 8.00 | 一次拉链人工检查 |
| 通过概率 | p | 0.80 | 检查后 AM-007 对演示请求变为 `QUALIFIED` 的概率 |
| 交付处理成本 | H | 10.00 | 售出给买家时的处理成本 |
| 通过后净回收 | R_pass | 98.00 | 按价格规则算出的通过后报价 108.00 − H |
| 失败后净回收 | R_fail | 40.00 | 失败后的最佳处置：以"拉链卡滞"状态清仓 |
| 检查预算余额 | B | 20.00 | 演示初始预算 |

不检查时的可选处置（合成）：

| 选项 | 净回收 |
|---|---|
| 按现状清仓（拉链未核实） | 45.00 |
| 退回供应商 | 35.00 |

"向买家报价或预留"不在不检查选项中，因为拉链未核实（需求 3 第 9 条）。

计算依据（默认参数组）：

```
EV_inspect = p × R_pass + (1 − p) × R_fail − C
           = 0.80 × 98.00 + 0.20 × 40.00 − 8.00
           = 78.40 + 8.00 − 8.00
           = 78.40
V_none     = max(45.00, 35.00) = 45.00（按现状清仓）
ΔEV        = EV_inspect − V_none = 78.40 − 45.00 = 33.40 > 0
预算检查   C = 8.00 ≤ B = 20.00
判定       INSPECT；创建 Inspection_Task 后 B = 20.00 − 8.00 = 12.00
```

演示参数组（只列出与默认组不同的参数）：

| 参数组 | 变更 | EV_inspect | ΔEV | 预算检查 | 判定 |
|---|---|---|---|---|---|
| 默认 | — | 78.40 | 33.40 | 8.00 ≤ 20.00 | `INSPECT` |
| 预算不足 | B = 5.00 | 78.40 | 33.40 | 8.00 > 5.00 | `SKIP`：`INSPECTION_OVER_BUDGET` |
| 低通过率 | p = 0.10 | 0.10 × 98.00 + 0.90 × 40.00 − 8.00 = 37.80 | 37.80 − 45.00 = −7.20 | 8.00 ≤ 20.00 | `SKIP`：`INSPECTION_NOT_WORTHWHILE` |

## 需求

### 需求 1：合成演示数据集

**用户故事：** 作为演示负责人，我希望使用十件明确标注为合成数据的商品和合成经济参数，以便演示可以重复运行，并且不涉及真实数据。

#### 验收标准

1. THE AcceptMatch SHALL 使用"合成演示数据"一节定义的十件商品（AM-001 至 AM-010）作为 MVP 唯一的商品数据。
2. THE AcceptMatch SHALL 为每件商品、每个买家、每条 Evidence 和每个经济参数带上合成数据标记，并在所有面向买家、检查员、交付人员和演示负责人的输出中显示"合成数据"标签。
3. THE AcceptMatch SHALL 不读取 `Hackathon/Dataset` 下的任何文件。
4. IF 加载数据集时发现重复的 Item_ID 或 Tag_Code，THEN THE AcceptMatch SHALL 拒绝加载并报告重复值。
5. IF 某个部位状态为 `OK` 或 `DEFECT` 但没有对应的 Evidence，THEN THE AcceptMatch SHALL 拒绝加载并报告对应的 Item_ID 和部位。
6. WHEN 演示负责人执行重置时，THE AcceptMatch SHALL 将所有商品、Inspection_Task、Inspection_Decision、Quote、Reservation 和 Inspection_Budget 恢复为初始状态，并按演示负责人选择的演示参数组（默认、预算不足或低通过率）加载经济参数。

### 需求 2：买家需求

**用户故事：** 作为买家，我希望明确说明我接受哪些缺陷、要求哪些部位完好，以便只收到符合我条件的商品。

#### 验收标准

1. THE AcceptMatch SHALL 接收结构化的 Buyer_Request，内容包括 Product、Accepted_Defects 和 Required_Conditions。
2. IF 同一部位既出现在 Accepted_Defects 中又出现在 Required_Conditions 中，THEN THE AcceptMatch SHALL 拒绝该请求并指出冲突的部位。
3. THE AcceptMatch SHALL 只把 Accepted_Defects 中明确列出的缺陷视为买家已接受，未列出的缺陷一律视为不接受。
4. WHEN 收到有效的 Buyer_Request 时，THE AcceptMatch SHALL 记录该请求，并向买家回显其接受的缺陷和要求的部位状态。

### 需求 3：逐件匹配

**用户故事：** 作为买家，我希望系统逐件判断商品是否符合我的条件，并说明不符合的原因。

#### 验收标准

1. WHEN 收到有效的 Buyer_Request 时，THE AcceptMatch SHALL 对每件商品单独评估，并给出 `QUALIFIED`、`NEEDS_INSPECTION` 或 `EXCLUDED` 之一。
2. IF 商品的 Product 与请求不同，THEN THE AcceptMatch SHALL 将其标记为 `EXCLUDED`，原因码为 `PRODUCT_MISMATCH`。
3. IF 商品的 Item_Status 不是 `AVAILABLE`，THEN THE AcceptMatch SHALL 将其标记为 `EXCLUDED`，原因码为 `NOT_AVAILABLE`。
4. IF Required_Conditions 中的某个部位已核实为 `DEFECT`，THEN THE AcceptMatch SHALL 将该商品标记为 `EXCLUDED`，原因码为 `REQUIREMENT_NOT_MET`，并记录该部位。
5. IF 不属于 Required_Conditions 的部位上存在未被 Accepted_Defects 覆盖的 Verified_Defect，THEN THE AcceptMatch SHALL 将该商品标记为 `EXCLUDED`，原因码为 `UNACCEPTED_DEFECT`，并记录该缺陷。覆盖的条件是部位相同、缺陷类型相同，且程度不高于可接受的最高程度。
6. IF 商品未被排除且至少一个部位为 `UNKNOWN`，THEN THE AcceptMatch SHALL 将其标记为 `NEEDS_INSPECTION`，并列出所有 `UNKNOWN` 部位。
7. WHEN 商品未被排除且所有部位均已核实时，THE AcceptMatch SHALL 将其标记为 `QUALIFIED`。
8. THE AcceptMatch SHALL 为 `EXCLUDED` 商品记录全部适用的原因码。
9. THE AcceptMatch SHALL 不把 `UNKNOWN` 部位视为 `OK`，也不视为买家已接受。
10. THE AcceptMatch SHALL 先排 `QUALIFIED` 候选，再排 `NEEDS_INSPECTION` 候选，同类候选按 Item_ID 升序排列。
11. THE AcceptMatch SHALL 对相同的数据和相同的 Buyer_Request 产生相同的 Match_Result。

### 需求 4：检查前的经济判断

**用户故事：** 作为运营负责人，我希望只在检查成本在预算内、且检查预计比不检查回收更多时才安排人工检查，并且能看到计算依据，以便检查成本花在值得的地方。

#### 验收标准

1. WHEN 不存在 `QUALIFIED` 候选但存在 `NEEDS_INSPECTION` 候选时，THE AcceptMatch SHALL 在创建任何 Inspection_Task 之前，按排名顺序对 `NEEDS_INSPECTION` 候选逐件生成 Inspection_Decision。
2. THE AcceptMatch SHALL 只从"检查经济参数（合成）"一节和当前演示参数组读取 C、p、H、R_fail、不检查时的可选处置和 B。
3. THE AcceptMatch SHALL 用需求 9 的价格规则计算该商品检查通过后的报价，并以"通过后报价 − H"作为 R_pass。这一步计算不生成 Quote，也不向买家展示。
4. THE AcceptMatch SHALL 取不检查时可选处置中净回收最高的一项作为 V_none。可选处置中不得包含把 `UNKNOWN` 部位当作 `OK` 来报价或预留的选项。
5. THE AcceptMatch SHALL 按 EV_inspect = p × R_pass + (1 − p) × R_fail − C 和 ΔEV = EV_inspect − V_none 计算，每一步结果四舍五入到 0.01。
6. WHEN C ≤ B 且 ΔEV > 0 时，THE AcceptMatch SHALL 将 Inspection_Decision 判定为 `INSPECT`，并按需求 5 创建 Inspection_Task。
7. IF C > B，THEN THE AcceptMatch SHALL 将 Inspection_Decision 判定为 `SKIP`，原因码为 `INSPECTION_OVER_BUDGET`。
8. IF ΔEV ≤ 0，THEN THE AcceptMatch SHALL 将 Inspection_Decision 判定为 `SKIP`，原因码为 `INSPECTION_NOT_WORTHWHILE`。
9. THE AcceptMatch SHALL 始终同时完成预算检查和 ΔEV 计算；两项都不满足时，记录两个原因码。
10. IF 任一参数缺失或无效（p 不在 0 到 1 之间，或任一金额为负），THEN THE AcceptMatch SHALL 将 Inspection_Decision 判定为 `SKIP`，原因码为 `ECONOMICS_INPUT_INVALID`，报告缺失或无效的参数，并且不使用任何默认值代替。
11. WHEN 判定为 `SKIP` 时，THE AcceptMatch SHALL 以对应原因码将该商品从本次请求中排除，并继续判断下一个 `NEEDS_INSPECTION` 候选；没有更多候选时，按需求 7 第 4 条将请求设为 `NO_MATCH`。
12. WHEN 判定为 `SKIP` 时，THE AcceptMatch SHALL 把 V_none 对应的处置记录为建议处置，但不执行该处置。
13. WHEN 判定为 `INSPECT` 且 Inspection_Task 创建成功时，THE AcceptMatch SHALL 从 B 中扣除 C。
14. THE AcceptMatch SHALL 向演示负责人展示每个 Inspection_Decision 的计算依据，包括：每个输入参数及其合成数据标记、R_pass 的来源（通过后报价和 H）、公式、代入数值后每一步的结果、每个不检查选项及其净回收、被选为 V_none 的选项、C 与 B 的比较、判定和原因码。
15. THE AcceptMatch SHALL 向买家只说明该商品有哪些部位未核实，以及是否安排检查，不展示检查成本、概率等内部经济参数。
16. THE AcceptMatch SHALL 对相同的参数和相同的 Condition_Record 产生相同的 Inspection_Decision。

### 需求 5：创建人工检查

**用户故事：** 作为买家，我希望在关键部位状态未知、且经济判断认为值得检查时，系统先安排人工检查，而不是猜测商品状态。

#### 验收标准

1. WHEN 存在 `QUALIFIED` 候选时，THE AcceptMatch SHALL 直接对排名第一的候选执行需求 8 和需求 9，不做经济判断，也不创建 Inspection_Task。
2. WHEN 需求 4 对某个 `NEEDS_INSPECTION` 商品判定为 `INSPECT` 时，THE AcceptMatch SHALL 为该商品创建 Inspection_Task，并将 Buyer_Request 的状态设为 `WAITING_FOR_INSPECTION`。
3. IF 某个商品没有判定为 `INSPECT` 的当前 Inspection_Decision，THEN THE AcceptMatch SHALL 拒绝为该商品创建 Inspection_Task。
4. THE AcceptMatch SHALL 在 Inspection_Task 中写明 Item_ID、Tag_Code、待检查部位、每个部位的检查方法（例如拉链：完整开合一次，并检查是否卡滞）、对应的 Inspection_Decision 编号和合成数据标记。
5. IF 该商品的相同部位已有未完成的 Inspection_Task，THEN THE AcceptMatch SHALL 关联已有任务，不再创建新任务，也不再扣减 B。
6. WHILE Buyer_Request 处于 `WAITING_FOR_INSPECTION` 状态，THE AcceptMatch SHALL 不为该商品生成 Quote，也不接受针对该商品的 Reservation。
7. WHILE Buyer_Request 处于 `WAITING_FOR_INSPECTION` 状态，THE AcceptMatch SHALL 向买家显示正在等待检查的 Item_ID 和部位。

### 需求 6：处理检查结果

**用户故事：** 作为检查员，我希望提交的检查结果可以追溯到我检查的那件实物，以便系统据此更新商品状态。

#### 验收标准

1. WHEN 检查员提交 Inspection_Task 结果时，THE AcceptMatch SHALL 要求结果包含现场扫描的 Tag_Code、每个待检查部位的结论（`OK`、带类型和程度的 `DEFECT`，或 `INCONCLUSIVE`）、至少一条 Evidence 引用、检查员标识和检查时间。
2. IF 扫描到的 Tag_Code 与任务商品的 Tag_Code 不一致，THEN THE AcceptMatch SHALL 拒绝该结果，任务保持未完成状态。
3. WHEN 结果被接受时，THE AcceptMatch SHALL 更新该商品的 Condition_Record，版本号加一，并保留原有 Evidence。
4. WHEN Condition_Record 更新后，THE AcceptMatch SHALL 按需求 3 重新评估处于等待状态的 Buyer_Request。
5. WHEN 重新评估结果为 `QUALIFIED` 时，THE AcceptMatch SHALL 判定检查通过，并执行需求 8 和需求 9。
6. IF 重新评估结果为 `EXCLUDED`，THEN THE AcceptMatch SHALL 判定检查未通过，并执行需求 7。
7. IF 任一待检查部位的结论为 `INCONCLUSIVE`，THEN THE AcceptMatch SHALL 保持该部位为 `UNKNOWN`，以原因码 `INSPECTION_INCONCLUSIVE` 将该商品从本次请求中排除，并执行需求 7。
8. WHEN 检查发现缺陷时，THE AcceptMatch SHALL 将其作为 Verified_Defect 保留在 Condition_Record 中，商品保持 `AVAILABLE`，以便与接受该缺陷的其他买家匹配。

### 需求 7：重新匹配或说明无合适商品

**用户故事：** 作为买家，我希望在检查未通过后，系统继续寻找其他商品；如果找不到，就清楚告诉我原因。

#### 验收标准

1. WHEN 检查未通过时，THE AcceptMatch SHALL 对同一 Buyer_Request 重新执行需求 3 的匹配，并排除检查未通过的商品。
2. WHEN 重新匹配得到 `QUALIFIED` 候选时，THE AcceptMatch SHALL 对排名第一的候选执行需求 8 和需求 9。
3. WHEN 重新匹配只得到 `NEEDS_INSPECTION` 候选时，THE AcceptMatch SHALL 按需求 4 做经济判断，并只在判定为 `INSPECT` 时按需求 5 创建 Inspection_Task。
4. IF 没有任何候选，或所有 `NEEDS_INSPECTION` 候选都被需求 4 判定为 `SKIP`，THEN THE AcceptMatch SHALL 将 Buyer_Request 的状态设为 `NO_MATCH`，告知买家没有合适商品，并列出每件商品的排除原因。检查未通过的商品附上检查结论；因经济判断未检查的商品附上原因码，并说明"该部位未核实，本次不安排检查"。
5. WHILE Buyer_Request 处于 `NO_MATCH` 状态，THE AcceptMatch SHALL 不生成 Quote，不创建 Reservation，也不推荐不满足条件的商品。

### 需求 8：展示证据

**用户故事：** 作为买家，我希望在报价前看到这件商品的具体证据，以便确认我接受的正是它已核实的缺陷。

#### 验收标准

1. WHEN 一件商品对 Buyer_Request 为 `QUALIFIED` 时，THE AcceptMatch SHALL 在报价前向买家展示该商品的 Item_ID、Tag_Code、Condition_Record 版本号，以及每个部位的状态和 Evidence（来源、检查员、时间、照片引用）。
2. THE AcceptMatch SHALL 逐条列出该商品的 Verified_Defect，并标明每条对应买家 Accepted_Defects 中的哪一项。
3. THE AcceptMatch SHALL 对每个 Required_Conditions 部位展示证明其为 `OK` 的 Evidence。
4. THE AcceptMatch SHALL 对经人工检查核实的部位注明来源为 Inspection_Task，并显示任务编号。
5. THE AcceptMatch SHALL 展示该件实物的证据，而不是商品型号的通用描述。

### 需求 9：按价格规则报价

**用户故事：** 作为买家，我希望报价按明确的规则从缺陷计算出来，以便理解价格的来由。

#### 验收标准

1. THE AcceptMatch SHALL 只为 `QUALIFIED` 商品生成 Quote。
2. WHEN 向买家展示 `QUALIFIED` 商品的证据后，THE AcceptMatch SHALL 按"价格规则"一节计算报价。
3. THE AcceptMatch SHALL 在 Quote 中包含 Quote_ID、Item_ID、Condition_Record 版本号、基础价格、逐项折扣、最终金额和合成数据标记。
4. IF 某个 Verified_Defect 的程度在价格规则中没有对应折扣率，THEN THE AcceptMatch SHALL 不生成 Quote，并报告缺失的规则。
5. WHEN AM-007 的拉链检查通过时，THE AcceptMatch SHALL 为 B-DEMO-01 生成金额为 108.00 的 Quote。

### 需求 10：买家确认与预留

**用户故事：** 作为买家，我希望只有在我确认具体商品、缺陷清单和报价之后才预留，并且预留的就是我确认的那一件。

#### 验收标准

1. THE AcceptMatch SHALL 只在买家明确确认后创建 Reservation。确认内容必须指明 Item_ID 和 Quote_ID，并表明买家已查看该商品的 Verified_Defect 清单和报价金额。
2. WHEN 确认有效时，THE AcceptMatch SHALL 创建关联买家、Item_ID 和 Quote_ID 的 Reservation，并将 Item_Status 从 `AVAILABLE` 改为 `RESERVED`。
3. IF Item_ID 与 Quote_ID 不对应，或该 Quote 不属于该买家，THEN THE AcceptMatch SHALL 拒绝确认。
4. IF 当前 Condition_Record 版本号与 Quote 中记录的版本号不同，THEN THE AcceptMatch SHALL 拒绝确认，并按需求 3、需求 8 和需求 9 重新评估和报价。
5. IF 商品已不是 `AVAILABLE`，THEN THE AcceptMatch SHALL 拒绝确认、通知买家，并按需求 7 重新匹配。
6. THE AcceptMatch SHALL 保证一件商品同一时间最多只有一个有效的 Reservation。
7. IF 买家拒绝报价，THEN THE AcceptMatch SHALL 不创建 Reservation，商品保持 `AVAILABLE`。

### 需求 11：交付时核验同一件商品

**用户故事：** 作为买家，我希望收到的就是我确认并预留的那一件商品。

#### 验收标准

1. WHEN 交付人员发起交付时，THE AcceptMatch SHALL 要求输入 Reservation 编号和现场扫描的 Tag_Code。
2. IF 该 Reservation 不存在或已失效，THEN THE AcceptMatch SHALL 拒绝交付。
3. IF 扫描到的 Tag_Code 与预留商品的 Tag_Code 不一致，THEN THE AcceptMatch SHALL 阻止交付，记录 `DELIVERY_MISMATCH`（包含预留的 Item_ID 和扫描到的 Tag_Code），Reservation 保持不变。
4. WHEN Tag_Code 一致时，THE AcceptMatch SHALL 向交付人员展示该商品的 Evidence（包括 Verified_Defect 的照片引用），并要求交付人员逐个部位确认实物与 Evidence 一致。
5. IF 交付人员报告实物与 Evidence 不一致，THEN THE AcceptMatch SHALL 阻止交付，记录 `CONDITION_MISMATCH`，并转人工处理。
6. WHEN Tag_Code 一致且所有部位都确认一致时，THE AcceptMatch SHALL 将 Item_Status 设为 `DELIVERED`，并记录 Delivery_Check 结果。

### 需求 12：Agent 编排与确定性约束

**用户故事：** 作为系统负责人，我希望由 Agent 根据工具结果决定下一步，同时资格、价格、检查门槛和预留规则始终由确定性代码执行，这样 Agent 的选择不会突破业务约束。

#### 验收标准

1. THE AcceptMatch SHALL 由 Orchestrator_Agent（Strands Agents + Amazon Bedrock）编排流程：根据上一个 Deterministic_Tool 的结果，选择下一步调用哪个 Deterministic_Tool。
2. THE AcceptMatch SHALL 由 Deterministic_Tool 执行以下判断，Orchestrator_Agent 不得自行判断或覆盖：数据集校验（需求 1）、匹配资格（需求 3）、检查经济判断与预算（需求 4）、检查任务创建条件（需求 5）、检查结果校验（需求 6）、报价计算（需求 9）、确认与预留约束（需求 10）、交付核验（需求 11）。
3. IF Orchestrator_Agent 请求的操作不满足确定性约束，THEN THE Deterministic_Tool SHALL 拒绝该操作，不改变任何状态，并返回原因码。
4. WHEN Deterministic_Tool 拒绝某个操作时，THE Orchestrator_Agent SHALL 根据原因码选择其他允许的下一步，不以相同参数重复提交被拒绝的操作。
5. THE Deterministic_Tool SHALL 在执行状态变更前自行重新校验所有前置条件（例如创建 Inspection_Task 前重新确认 Inspection_Decision 为 `INSPECT`），不依赖 Orchestrator_Agent 的判断。
6. THE Orchestrator_Agent SHALL 不直接修改 Condition_Record、Item_Status、Inspection_Budget、Inspection_Decision、Quote 或 Reservation。这些状态只能通过 Deterministic_Tool 变更。
7. THE AcceptMatch SHALL 保证向买家和演示负责人展示的所有金额、概率、状态和原因码都来自 Deterministic_Tool 的结果，Orchestrator_Agent 不生成或修改这些值。
8. THE Orchestrator_Agent SHALL 不代替买家确认报价；只有买家本人的确认输入才能触发需求 10 的预留。
9. IF 调用 Amazon Bedrock 失败或超时，THEN THE AcceptMatch SHALL 不再执行新的状态变更，保留并记录已完成的 Deterministic_Tool 操作，并在恢复时从持久化状态继续。

### 需求 13：操作记录

**用户故事：** 作为演示负责人，我希望能查看每件商品从匹配到交付的完整记录，以便说明每一步的依据。

#### 验收标准

1. THE AcceptMatch SHALL 记录以下事件：匹配评估、Inspection_Decision（含完整计算依据）、Inspection_Task 创建、预算扣减、检查结果、Quote 生成、买家确认或拒绝、Reservation 创建、Delivery_Check 结果，以及被 Deterministic_Tool 拒绝的操作。
2. THE AcceptMatch SHALL 在每条记录中包含时间、操作者、Item_ID、相关编号和结果。
3. THE AcceptMatch SHALL 记录 Orchestrator_Agent 的每次工具调用，包括工具名、输入、输出和 Agent 给出的选择理由。
4. WHEN 按 Item_ID 查询时，THE AcceptMatch SHALL 按时间顺序列出该商品的全部记录。

### 需求 14：端到端演示场景

**用户故事：** 作为演示负责人，我希望用固定的合成数据完整演示检查通过、检查失败、交付不一致，以及经济判断不安排检查的路径。

#### 验收标准

1. WHEN 在默认参数组下从初始状态提交 B-DEMO-01 的演示请求时，THE AcceptMatch SHALL 给出与"合成演示数据"商品表一致的 Match_Result，只对 AM-007 生成 Inspection_Decision（EV_inspect = 78.40，V_none = 45.00，ΔEV = 33.40，判定 `INSPECT`），只为 AM-007 的拉链创建一个 Inspection_Task，并将 B 从 20.00 减为 12.00。
2. WHEN AM-007 的检查结果为拉链 `OK` 时，THE AcceptMatch SHALL 将 AM-007 判为 `QUALIFIED`，展示其证据，并生成 108.00 的 Quote。
3. WHEN B-DEMO-01 确认 AM-007 和对应 Quote 时，THE AcceptMatch SHALL 创建 Reservation，并将 AM-007 设为 `RESERVED`。
4. WHEN 交付时扫描 `SYN-T-Q5ZT` 且所有部位确认一致时，THE AcceptMatch SHALL 将 AM-007 设为 `DELIVERED`。
5. WHEN AM-007 的检查结果为拉链 `DEFECT`（卡滞，中等）时，THE AcceptMatch SHALL 以 `REQUIREMENT_NOT_MET` 排除 AM-007，重新匹配后将请求设为 `NO_MATCH`，列出全部十件商品的排除原因，并保持 AM-007 为 `AVAILABLE`。
6. WHEN 在 AM-007 已预留的情况下扫描到 `SYN-T-9CFL`（AM-006 的标签）时，THE AcceptMatch SHALL 阻止交付并记录 `DELIVERY_MISMATCH`，AM-007 保持 `RESERVED`。
7. WHEN 在"预算不足"参数组下提交演示请求时，THE AcceptMatch SHALL 将 AM-007 的 Inspection_Decision 判定为 `SKIP`（`INSPECTION_OVER_BUDGET`，ΔEV 仍显示为 33.40），不创建 Inspection_Task，B 保持 5.00，将请求设为 `NO_MATCH`，并记录建议处置"按现状清仓 45.00"。
8. WHEN 在"低通过率"参数组下提交演示请求时，THE AcceptMatch SHALL 将 AM-007 的 Inspection_Decision 判定为 `SKIP`（`INSPECTION_NOT_WORTHWHILE`，EV_inspect = 37.80，ΔEV = −7.20），不创建 Inspection_Task，B 保持 20.00，将请求设为 `NO_MATCH`，并记录建议处置"按现状清仓 45.00"。
9. THE AcceptMatch SHALL 支持通过重置（需求 1 第 6 条）从初始状态、按所选参数组重复运行每个场景。

## 假设与待确认

1. MVP 固定为正面、背面、拉链三个检查部位，十件商品都是合成手提包型号。
2. 基础价格、折扣率、50% 价格下限，以及全部经济参数（C、p、H、R_fail、不检查选项、B）都是合成值，可以调整。
3. 按当前数据，检查失败或经济判断为 `SKIP` 后的结果都是 `NO_MATCH`。如果希望演示重新匹配到另一件商品，需要修改或新增一件商品。
4. 有多个 `NEEDS_INSPECTION` 候选时，按排名逐件判断，为第一个判定为 `INSPECT` 的商品创建任务。MVP 不在候选之间按 ΔEV 择优。
5. 检查结果和交付扫描在演示中由操作人员手动录入。
6. MVP 不处理预留过期，也不演示多个买家同时争抢同一件商品，但需求 10 第 6 条的唯一性约束仍然生效。
7. 具体使用的 Bedrock 模型和 Strands 工具清单在 design 阶段确定。
