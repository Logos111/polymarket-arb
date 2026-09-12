# 开发计划与项目进程（活文档）

> 版本：v2.2 ｜ 日期：2026-09-13
> **本文档是项目的方向与进展跟踪文档，实施过程中随进度持续更新。**
> v2.0 起每阶段采用统一结构：**目标 / 交付物（文件级）/ 完成判据 / 验证证据 / 遗留项**；
> 全部进度声明已经过对本地代码、测试与 pyproject 入口的逐条核查。
> 前置阅读：[PROJECT_PLAN.md](PROJECT_PLAN.md)（最初总体规划，阶段 3 之后路线以本文为准）。

---

## 〇、进展总面板

| 阶段 | 内容 | 完成度 | 状态 | 下一步 / 阻塞 |
|---|---|---|---|---|
| 0 | 结算规则实证 + bug 修复 + 录制脚本 | 4/4 | ✅ 完成 | — |
| 1 | 模块化重构（决策纯函数） | 5/5 | ✅ 完成 | — |
| 2 | SQLite 存储层 + 结算闭环 | 4/6 | ✅ 主体完成 | 遗留：proxy Safe 赎回（2.5）、trade5m 接 recorder（2.6） |
| 3 | 回测引擎（HF 数据集版） | 3/4 | 🔄 进行中 | 遗留：实盘日志回放对账（3.4，待自录数据积累） |
| 4 | 风控最小集 | 4/4 | ✅ 完成 | 放量前置条件已满足；阈值体验证后可调 |
| 5 | 参数扫描 + 特征工程 + 一致性回归 | 3/6 | 🔄 进行中 | 参数平面已扫完（无解）；5.4 基建完成，待积累样本；5.3 features.py 未开始 |
| 6 | 工程卫生与治理 | 4/6 | 🔄 进行中 | 完成：SecretStr/CI/USDC 精度/README；遗留：仓库可见性、trade5m 接 recorder |

状态图例：⬜ 未开始 ｜ 🔄 进行中 ｜ ✅ 完成 ｜ ⏸ 暂缓

**当前策略基线参数**（[params.py](../src/pm_arb/strategies/crypto_5m/params.py) 默认值，实盘/回测单一来源）：
名义 $2.00/窗；入场窗口 [70s, 135s]；入场价 [0.15, 0.30]；止盈 0.99；止损 0（关闭，已被 27 组扫描证伪）；max_vol $30；轮询 2s；ws_fresh 10s / feed_fresh 15s；结算前 60s 停止操作。

**测试基线**：pytest **147 项**全绿（144 passed + 3 skipped：duckdb 集成用例仅 <3.14 解释器可跑）；ruff 零警告。

---

## 一、背景与原始缺口（2026-09-11 勘察，均已闭环或转入跟踪）

| # | 问题（当时发现） | 解决于 | 现状 |
|---|---|---|---|
| 1 | 结算规则无权威依据，`rtds.py` base 语义是代码假设 | 阶段 0.1 | ✅ [settlement-rule.md](settlement-rule.md) 钉死（TWAP≥起点价→Up；578 窗口实证；taker fee=C×0.07×p×(1−p)） |
| 2 | 历史数据基本为空（TickRecorder 是孤儿模块） | 阶段 0.2 / 3.1 | ✅ pm-record 挂机 + HF 数据集（2026-03~05，BTC/ETH）双轨积累 |
| 3 | 580 行单文件，判定逻辑无法回测复用 | 阶段 1 | ✅ decisions.py 纯函数，实盘/回测共用 |
| 4 / 4a | place_market 成交解析 bug；order.size=0 致 PARTIAL 误判 FILLED | 阶段 0.3 | ✅ ref_price 预估份数 + 落库用 filled_size 双保险（test_orders 4 项回归） |
| 5 | 持仓到结算无自动 redeem；结算输赢不落盘 | 阶段 0.4 / 2 | ✅ 结算守护 + pm-backfill 回填；⏸ proxy（signature_type=3）赎回仍待 Safe 授权 |
| 6 | 进程重启丢持仓；无风控；策略/风控/组合层空壳 | 阶段 1/2/4 | 🟡 重启恢复 ✅；strategies/crypto_5m 已成完整包；**risk/、portfolio/ 仍为空壳**（仅 docstring）→ 阶段 4 |

### 已确认的架构决策（仍然有效）

- 存储选型：**SQLite（WAL）结构化层 + JSONL 原始 tick 流 + 已封口整日文件压实为分区 Parquet（v2.1）**；网格结果暂落 CSV（见 3.2 遗留）。压实实测 25x（490MB→19.5MB/日），pm-compact 行数校验通过才删源。
- 判定/参数/执行三层各自可替换：decisions.py 唯一决策来源、params.py 唯一参数来源、Broker Protocol 唯一执行入口。
- 胜率优化路径：统计 + 参数扫描 + **特征工程**（v1.5 起新增，见阶段 5.3）；样本量不足前不上 ML。
- 方向选择：深耕 crypto_5m 冷门方策略；PROJECT_PLAN 的 NegRisk/跨平台/做市降级为远期可选。

---

## 二、阶段 0：结算规则实证 + bug 修复 + 录制脚本（✅ 4/4）

### 0.1 结算规则实证 ✅
- 交付物：[docs/settlement-rule.md](settlement-rule.md)（v1.0）
- 判据与证据：Gamma API 已结算市场查询须带 `closed=true`；Binance 5m K 线 578 窗口交叉验证（总一致率 84.6%，分歧集中于平坦窗口 <0.02%→56.9%、≥0.5%→100%）；taker fee 公式官方文档 + Gamma feeSchedule 双源一致。
- 遗留：pm-record 录制的 RTDS full_accuracy_value 与"起点价"口径直接复核（长期项，数据已在录）。

### 0.2 pm-record 独立 24/7 录制 ✅
- 交付物：[app/record_ticks.py](../src/pm_arb/app/record_ticks.py)（入口 `pm-record`）、[data/recorder.py](../src/pm_arb/data/recorder.py)（JsonlWriter/TickRecorder）
- 判据与证据：三路流按天分片（`{date}_market.jsonl` / `{date}_rtds_{sym}.jsonl` / `{date}_raw_ws.jsonl`）；原始帧 2000 帧样本证实无 price_change（快照-only 是服务端行为）；JsonlWriter 每 50 行强制 flush；RTDS 业务级看门狗。
- 遗留：挂服务器 24/7（当前挂本机；runtime/ticks 已有 09-08、09-12 数据）。

### 0.3 成交解析 + size=0 误判修复 ✅
- 交付物：[execution/clob_trader.py](../src/pm_arb/execution/clob_trader.py) place_market（ref_price 预估份数）、[execution/orders.py](../src/pm_arb/execution/orders.py)
- 验证证据：tests/test_orders.py 中 `test_place_market_buy_estimates_size_from_ref_price` 等 4 项回归；`test_market_order_zero_size_fill_fallback`。

### 0.4 PnL 口径 + pm-redeem ✅（含 1 项已知限制）
- 交付物：[app/redeem.py](../src/pm_arb/app/redeem.py)（入口 `pm-redeem`）
- 验证证据：--dry-run 实测 09-10/11 四笔实盘仓位与链上探针吻合。
- 已知限制：owner 为 funder proxy（signature_type=3）时 `--execute` 明确拒绝链上赎回（见 redeem.py docstring）；`balance` 换算硬编码 `10**6`（redeem.py:76，与 chain.py 动态 `usdc_decimals` 不一致）→ 转入阶段 6 卫生项 #4。

---

## 三、阶段 1：模块化重构（✅ 5/5）

| 子项 | 交付物 | 验证证据 |
|---|---|---|
| 1.1 参数单一来源 ✅ | [params.py](../src/pm_arb/strategies/crypto_5m/params.py)（Crypto5mParams frozen + parse_overrides） | `pm-trade5m --param k=v`；test_decisions::test_params_defaults |
| 1.2 决策纯函数 ✅ | [decisions.py](../src/pm_arb/strategies/crypto_5m/decisions.py)（pick_underdog/calc_size/decide_entry/decide_exit/decide_stop，无 I/O 无时钟） | test_decisions.py 15 项 |
| 1.3 上下文 + 时钟注入 ✅ | [context.py](../src/pm_arb/strategies/crypto_5m/context.py)（WindowDataHub，clock 注入，禁直接 time.time()） | test_feed_freshness.py 5 项 |
| 1.4 编排器 + 瘦 CLI ✅ | [orchestrator.py](../src/pm_arb/strategies/crypto_5m/orchestrator.py)（WindowOrchestrator）、[trade5m.py](../src/pm_arb/app/trade5m.py)（瘦 CLI，入口 `pm-trade5m`）、[tui5m.py](../src/pm_arb/app/tui5m.py) | 提交拆分 A `504e6b2` / B `6f9198e` / C `d432e04`，可精确 bisect |
| 1.5 Broker Protocol ✅ | [execution/broker.py](../src/pm_arb/execution/broker.py)（Protocol + on_order 钩子）、[clob_broker.py](../src/pm_arb/execution/clob_broker.py)（live）、[paper_runner.py](../src/pm_arb/execution/paper_runner.py)（dry-run/回测，真实档深 VWAP 撮合） | test_broker.py 9 项 |

---

## 四、阶段 2：SQLite 存储层 + 结算闭环（✅ 4/6，2 项遗留转入跟踪）

| 子项 | 交付物 | 状态 | 验证证据 |
|---|---|---|---|
| 2.1 SQLite 三表 | [infra/store.py](../src/pm_arb/infra/store.py)：`orders` / `windows` / `positions`（WAL，Decimal 存 TEXT，fill_ratio 不依赖 status） | ✅ | test_store.py 8 项 |
| 2.2 on_order 钩子统一写入 | orchestrator._on_order → Store.upsert_order（live/paper 共用） | ✅ | test_broker::test_clob_broker_delegates_and_emits_hook 等 |
| 2.3 结算守护 + 批量回填 | [data/settlement.py](../src/pm_arb/data/settlement.py)（market_winner/settle_result/settle_key）+ orchestrator._settlement_loop（60s 扫描，360s 宽限期）+ [app/backfill.py](../src/pm_arb/app/backfill.py)（`pm-backfill --windows/--since`，无交易窗口也回填 market_winner 作样本放大器） | ✅ | 真实 Gamma 回填 29 窗口 + 预置持仓 lose 结算 -$2.10 与手算一致；平仓 PnL 含出场 taker fee |
| 2.4 重启恢复 | run() 启动 open_positions 打印遗留持仓 + sweeper 接管 | ✅ | kill 重启实测 |
| 2.5 赢单自动赎回 | settlement.py：仅 EOA（signature_type≠3）尝试链上赎回；proxy 只记待赎 | ⏸ 遗留 | **待办**：proxy Safe execTransaction + EIP-1271 签名授权路径（实盘钱包现状，未解除前赢单资金滞留 proxy） |
| 2.6 trade5m 实盘接 recorder | — | ⬜ 遗留 | **待办**：trade5m.py 当前未 import recorder（grep 核实）；复盘真实交易盘口上下文需手动对齐日志与 pm-record 数据流；接入格式与 pm-record 一致 |

> **v2.0 更正**：原规划表中列有 `backtest_runs` 表（阶段 5 留痕用）——**实际未建**，网格结果当前落 [runtime/grid_runs.csv](../runtime/grid_runs.csv)。若阶段 5.3 特征分析需要结构化留痕再补建。

---

## 五、阶段 3：回测引擎（🔄 3/4）

### 实际交付布局（v2.0 按代码核实修正，替代旧版设想布局）

```
src/pm_arb/strategies/crypto_5m/backtest/
├── hf_loader.py   # HF 数据集加载（btc/eth markets+ticks parquet，outcome 为推断标签）
├── engine.py      # 事件驱动回放：replay_ticks / replay_window / run_backtest / run_grid，
│                  #   taker_fee 与 ExitKind；复用 decisions.py + params.py
├── spot_vol.py    # SpotVol：Binance 1s K 线重建滚动 TWAP-60s，输出窗口 high-low range 序列
├── grid.py        # 参数网格 + walk-forward（pm-grid5m，分片并行）
├── run.py         # 单组参数回测 CLI（pm-bt5m，--param k=v，--spot 接现货过滤）
└── report.py      # 终端报表
```

### 子项状态

| 子项 | 状态 | 交付物 / 证据 |
|---|---|---|
| 3.1 HF 数据集回测引擎 | ✅ | engine.py + hf_loader.py + run.py（`fd1496d`）；test_hf_engine.py 13 项 |
| 3.2 参数网格 + walk-forward | ✅ | grid.py：60 组网格（前 70% 调参 / 后 30% 验证）；结果落 runtime/grid_runs.csv（**未落 SQLite，见 2.1 更正**）；报告 [backtest/runtime/backtest-report-b07.md](../src/pm_arb/strategies/crypto_5m/backtest/runtime/backtest-report-b07.md) 及同目录历史报告 |
| 3.3 现货波动过滤（b07） | ✅ | spot_vol.py + [scripts/fetch_spot_1s.py](../scripts/fetch_spot_1s.py)（Binance 1s K 线 → runtime/hf/*_spot_1s.parquet，各约 492 万行）；test_spot_vol.py 5 项 |
| 3.4 实盘日志回放对账 | ⬜ | **待办**：17 份历史日志（runtime/logs 现已有 20 份 trade5m 日志）重放核对引擎决策与实盘一致；依赖 pm-record 自录数据积累 + trade5m 接 recorder（2.6） |

### 回测结论档案（四轮实验，费后口径）

| 实验 | 结论 | 证据 |
|---|---|---|
| 基线全量 | BTC 每笔 -$0.47 / ETH -$0.28；名义胜率 31~34% > 隐含胜率 28~29%，止盈 0.65 截获上限 + 手续费吃光毛优势 | backtest-report（早期） |
| 60 组网格 + walk-forward | 验证段无一组转正；tp=0.99（持有到结算）方向最优，仍是"亏得最少" | grid_runs.csv |
| 27 组止损扫描 | 83% 持仓触发止损且全部更差 → 止损假设证伪，stop_loss_price 默认 0 | grid_runs.csv |
| b07 max_vol 现货过滤 | BTC 减亏 54%（基线 -$2,479.86 → mv20/until=100：-$720.02，过滤 59% 窗口）；ETH 未触发（-$777.10 不变，TWAP60 波幅占比 0.5% 极少超 $20）；合计 -$1,497.12 | backtest-report-b07.md |
| **根因** | 冷门方入场均价 ~0.27，盈亏平衡胜率需 27%，实际 23.5~25.1%；**定价劣势 2~4 个点，非执行/调参问题** | 各报告汇总 |

---

## 六、阶段 4：风控最小集（✅ 4/4，v2.2 完成）

`risk/gates.py`（186 行）：检查逻辑纯函数（`check_order`，与 decisions.py 同纪律），
I/O 聚合集中在 `RiskGate` 装配层（依赖全部可注入，回测传 None 即跳过）；
`trade5m.py` 装配（live 挂链上余额/gas 查询），orchestrator ENTER 分支下单前过闸。

| 子项 | 内容 | 状态 |
|---|---|---|
| 4.1 限额与熔断 | 单笔名义 / 单日累计投入 / 单日已实现亏损熔断（UTC 日界自动解除） | ✅ |
| 4.2 余额检查 | USDC ≥ 名义+buffer；**查询失败也拒绝（fail-closed）**；同窗口重复入场拒绝 | ✅ |
| 4.3 POL gas 告警 | 每窗口一次（缓存），低于阈值仅告警不阻断 | ✅ |
| 4.4 Kill Switch | `runtime/KILL` 存在即拒新仓并中止进程（return 2），删文件即解除 | ✅ |

验证：`tests/test_risk_gates.py` 17 项（拒绝优先级/边界/fail-closed/UTC 日界/Store 聚合）；
测试抓到两个真实 bug（balance 异常穿透、day_stats 缺上界把未来行计入）已修复；
拒绝路径集成在 orchestrator（KILL/熔断中止全部窗口，其余放弃本窗口）。

口径：Prometheus / Grafana / Telegram 后移（现阶段 structlog + 文件日志够用）；
`RiskLimits` 阈值当前为代码内默认（$5/$20/$10），放量前经实盘体验后可提为 Settings 字段。

---

## 七、阶段 5：参数扫描 + 特征工程 + 一致性回归（🔄 2/6，持续）

| 子项 | 状态 | 内容 |
|---|---|---|
| 5.1 样本量与每日报表 | 🔄 持续 | pm-record 24/7 积累 + pm-backfill 放大结算样本；每日胜率/EV 报表与置信区间（p=0.5±0.05 需 ~384 入场样本） |
| 5.2 参数网格 + walk-forward | ✅ 首轮完成 | 见 3.2；**结论：价格/波动/止盈/止损参数平面内无解**，同平面继续微调不再投入 |
| 5.3 特征工程（新信息维度） | ⬜ 未开始 | 见 5.3 详表 |
| 5.4 人工判断信号系统化 | ⬜ 未开始 | 见 5.4 |
| 5.5 验证标准与数据口径 | ⬜ 待执行纪律 | 见 5.5 |
| 5.6 一致性回归 | 🔄 持续 | 每次实盘留录制数据，定期重放核对引擎与线上决策一致（依赖 2.6、3.4） |

### 5.3 特征工程：从参数平面转向新信息维度（v1.5 新增）

候选特征（作为 `decide_entry` 的额外过滤条件，类似 max_vol 现有角色）：

| # | 特征 | 说明 | 假设 | 优先级 |
|---|---|---|---|---|
| F1 | Chainlink 更新频率 | 心跳 vs 真实价格更新 | 喂价卡死造成假低波动读数——crypto_5m_underdog.md 自认最脆弱假设 | **P0 先做** |
| F2 | 盘口失衡 | 冷门方/热门方 `(bid_size−ask_size)/(bid_size+ask_size)` | 短期买卖压力；LocalOrderBook 已有档深，改造成本低 | P1 |
| F3 | TWAP 短窗斜率 | 最近 10–30s 价格变化率（一阶导） | 比 high-low range 更反映"现在还在往哪边冲" | P1 |
| F4 | TWAP 二阶导 | 斜率是否放缓/反转 | 趋势衰竭常是均值回归前置信号 | P2 |
| F5 | 价差/流动性质量 | 冷门方 bid-ask 宽度 + 深度 | 宽价差可能是流动性噪声造成的假信号 | P2 |
| F6 | 跨币种联动 | 同窗口 BTC/ETH 结果相关性 | 同一宏观驱动互为先验；现成 HF 数据即可检验 | P2 |
| F7 | 时段效应 | 亚洲/欧美时段流动性与基线波动差异 | 分桶 + 单调性方法论直接复用 | P3 |
| F8 | 连续窗口自相关 | 上一窗口方向/幅度是否预示下一窗口 | 检验"动量延续 vs 均值回归" | P3 |

**工程落地**：新增 `strategies/crypto_5m/features.py`（与 decisions.py 平级的纯函数模块，无 I/O、无时钟，实盘/回测共用）；回测引擎落盘"入场时刻特征向量 + 结算结果"（CSV/SQLite，必要时补建 backtest_runs 表）；方法论沿用单变量分桶 + 单调性检验，单变量不够用时才考虑逻辑回归级打分模型，仍走 70/30 train/val 纪律。

### 5.4 人工判断信号系统化记录（v1.5 新增；✅ 基建完成 v2.2）

人工判断胜率高于机械规则说明 decisions.py 未编码全部可用信息。行动：① 每次人工进/不进判断**之前**用固定格式记录依据信号（结构化标签，事前记录而非事后回忆）；② 积累到统计口径一致样本量（~384 笔）再下结论，警惕小样本与记忆偏差；③ 有效标签逐条转为可计算特征，拿 HF 历史 + pm-record 实盘数据回测验证后再写进 decisions.py。

**基建（v2.2）**：`data/judgment_log.py`（`Judgment` dataclass + 固定词表 `SIGNAL_TAGS` 11 个标签 + append-only JSONL 读写，未登记标签直接拒绝防口径漂移）+ `pm-judgment` CLI（`enter/skip --window --signal --note`，`--list/--stats/--tags`）。落地纪律：判断当时记录，样本到 ~384 笔后做标签-胜率分析。

### 5.5 验证标准与数据口径（v1.5 新增）

- **采纳及格线**：新过滤器在验证段（后 30%，从未参与筛选）名义胜率须**清晰超过**入场均价对应的盈亏平衡胜率（当前口径 ~27%），"比基线好"不算通过；
- **先定清单再看结果**：候选特征清单固定后再看验证集，防 14,000+ 窗口上的多重检验过拟合；
- **HF outcome 抽样扩量**：目前仅 30 窗口抽样核对（30/30 一致），任何新特征结论被当作定论前，抽样扩至数百窗口；
- **实盘校准**：HF 数据集为 2026-03~05 历史微观结构，新特征阈值须经 pm-record 实盘数据交叉校准（方向可迁移、数值需实盘标定）。

---

## 八、阶段 6：工程卫生与治理（🔄 4/6，v1.5 新增，v2.2 更新）

| # | 事项 | 代码核查结论（2026-09-13） | 优先级 | 状态 |
|---|---|---|---|---|
| 1 | 私钥改 `pydantic.SecretStr` | **属实**：config.py docstring 承诺 SecretStr，实际 `private_key: str = Field(repr=False)`（config.py:40） | 高 | ✅ v2.2（私钥+L2 凭证四处调用方同步改） |
| 2 | 仓库可见性确认 | **属实**：docs/conversations/README.md 称私有；若实际公开，重审会话记录与回测报告敏感内容 | 高 | ⬜ |
| 3 | 最小 CI（push 跑 pytest + ruff） | **属实**：无 `.github/workflows/`；防重构悄悄破坏 decisions.py 单一真相来源 | 中 | ✅ v2.2（.github/workflows/ci.yml：3.12 + uv sync --frozen + ruff + pytest） |
| 4 | USDC 精度统一 | **属实**：redeem.py:76 硬编码 `10**6`；chain.py 动态查询 `usdc_decimals`（chain.py:128-134） | 中 | ✅ v2.2（redeem 动态读 decimals） |
| 5 | trade5m 接 TickRecorder | **属实**：trade5m.py 未 import recorder（同阶段 2.6 遗留，两处跟踪一处） | 中 | ⬜ |
| 6 | README 目录图同步 | **属实**：README.md L44-47 仍写顶层 `backtest/`，实际已迁至 `strategies/crypto_5m/backtest/` | 低 | ✅ v2.2（目录图+路线图重写） |

---

## 九、对原规划（PROJECT_PLAN.md）的修订（汇总，含历史项）

| 原规划 | 修订 | 状态 |
|---|---|---|
| ClickHouse/TimescaleDB 存 tick | JSONL + SQLite（单日 ~100MB 量级） | 已落地 |
| "WS 快照+增量、序列号校验"架构假设 | 实测 price_change 增量恒为 0（原始帧验证），按快照节奏回放，撮合取保守口径 | 已落地 |
| 阶段 4 风控整体后置 | 拆最小集提前（熔断/限额/余额/gas 告警），观测栈后移 | **仍未开始，放量硬前提** |
| "不要预测方向"套利纪律 | 已有意转向方向性投机——小仓位（$2/窗）+ 单日限额 + 熔断补偿 | 已落地（README 声明） |
| 阶段 5/6（NegRisk/跨平台/做市） | 降级远期可选，Strategy 抽象保持轻量 | 有效 |
| backtest/ 顶层目录 | 实际收进策略包 `strategies/crypto_5m/backtest/`（`446a128`） | 已落地（README 未同步，见阶段 6 #6） |
| backtest_runs 表留痕 | 未建；网格结果落 runtime/grid_runs.csv | v2.0 更正 |

---

## 十、关键文件清单（v2.0 按代码核实更新）

| 文件 | 角色 | 状态 |
|---|---|---|
| strategies/crypto_5m/params.py | 参数单一来源（Crypto5mParams，--param k=v） | ✅ 现行 |
| strategies/crypto_5m/decisions.py | 决策唯一来源（纯函数） | ✅ 现行 |
| strategies/crypto_5m/context.py | WindowDataHub + 时钟注入 | ✅ 现行 |
| strategies/crypto_5m/orchestrator.py | 窗口生命周期 + store 接线 + 结算守护 | ✅ 现行 |
| strategies/crypto_5m/backtest/{engine,grid,hf_loader,spot_vol,run,report}.py | HF 回测全套 | ✅ 现行 |
| execution/broker.py + clob_broker.py + paper_runner.py | Broker Protocol + live/paper 实现 | ✅ 现行 |
| execution/orders.py | 订单状态机（含 size=0 修复） | ✅ 现行 |
| execution/chain.py | 链上 ops（redeem 已实现，proxy 场景待 Safe 授权） | 🟡 部分 |
| execution/clob_trader.py | CLOB 下单（place_market ref_price 版） | ✅ 现行 |
| infra/store.py | SQLite 三表（orders/windows/positions） | ✅ 现行 |
| infra/config.py | 集中配置（私钥 SecretStr 化待办） | 🟡 卫生项 |
| data/recorder.py | JsonlWriter / TickRecorder | ✅ 现行 |
| data/ticks_compact.py + app/compact.py | ticks 压实管线（JSONL→分区 Parquet，pm-compact，须 3.12 跑） | ✅ 现行 |
| data/rtds.py / ws.py / feed.py | RTDS TWAP 流 / WS + 看门狗 / 数据订阅 | ✅ 现行 |
| data/settlement.py | 结算纯函数 + 赎回守卫 | ✅ 现行 |
| app/{trade5m,tui5m,record_ticks,redeem,backfill,doctor,setup,watch,crypto5m}.py | 9 个 CLI 入口（pm-trade5m / pm-record / pm-redeem / pm-backfill / pm-bt5m / pm-grid5m 等，见 pyproject） | ✅ 现行 |
| risk/ 、 portfolio/ | risk/gates.py 风控闸门（阶段 4）；portfolio/ 仍空壳 | ✅ 阶段 4（portfolio 待） |
| strategies/crypto_5m/features.py | 特征纯函数模块 | ⬜ 阶段 5.3 |

---

## 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-09-11 | v1.0 | 初版：两轮勘察 + 三项用户决策（SQLite / 统计+参数扫描 / 缺口全纳入） |
| 2026-09-11 | v1.1 | 人工复核代码后的五点修订：①0.3 成交 bug 疑似已被 4d0fc18 修复（待验证后划掉）；②[新发现] order.size=0 导致 PARTIAL 误判 FILLED，纳入 0.3 + 阶段 2 落库纪律；③pm-record 增加原始 WS 帧抽样落盘（验证增量恒 0 是服务端行为）+ WS 连接事件流；④0.1 增加 taker fee 确认；⑤风控增加 POL gas 余额告警、阶段 1 提交拆分纪律（A 纯搬运/B 新行为/C 装配） |
| 2026-09-12 | v1.2 | 阶段 0.1/0.2 完成：①结算规则钉死（Gamma 须 closed=true；578 窗口实证；taker fee 公式 fee=C×0.07×p×(1−p)，Crypto 仅 taker）→ docs/settlement-rule.md；②pm-record 上线挂机（三路流 + RestBook + 连接事件 + WindowMeta；原始帧证实无 price_change）；③附带修复：JsonlWriter 周期 flush、RTDS 业务级看门狗（空帧心跳/宽限期）；④环境：regex 包 DLL 损坏重装（镜像 403 走官方源） |
| 2026-09-12 | v1.3 | 阶段 1 完成（A/B/C 三提交可精确 bisect）：A `504e6b2` 纯函数抽取；B `6f9198e` Broker Protocol + PaperBroker 市价撮合/结算（dry-run 换真实档深撮合——唯一行为改进点，按计划声明）；C orchestrator（WindowOrchestrator：对齐/引导/守卫/循环/清理，时钟注入 clock，SessionLog try/finally 关闭修句柄泄漏）+ trade5m 瘦 CLI（75 行，--param k=v 接 params 单一来源）+ 注册 pm-trade5m。另：回测收进策略包（pm-bt5m/pm-grid5m）、HF 数据集 60 组网格全负 EV（验证段），止盈 0.99 方向被 25k 样本证实但结构性负 EV 不变 |
| 2026-09-12 | v1.4 | 阶段 2 完成：①infra/store.py（SQLite WAL，orders/windows/positions 三表，Decimal 存 TEXT，fill_ratio 不依赖 status——问题 4a 双保险纪律）；②Broker on_order 钩子接 orders 表（orchestrator._on_order，live/paper 共用）；③orchestrator 窗口/持仓行写入 + 平仓含出场 taker fee + 重启恢复（遗留持仓打印 + 结算守护接管）；④data/settlement.py（market_winner/settle_result/settle_key）+ orchestrator 后台结算守护（宽限期 360s）+ app/backfill.py（pm-backfill，--windows/--since 批量回填，无交易窗口也回填 market_winner 作回测样本放大器）；⑤赢单自动赎回仅 EOA 尝试，proxy 只记待赎（0.4 遗留不变）。验证：真实 Gamma 回填 29 窗口 + 预置持仓 lose 结算与手算一致；pytest 118/118。遗留：trade5m 接 recorder、proxy Safe 授权赎回 |
| 2026-09-13 | v1.5 | 吸收外部分析报告（master@c53375b 评审）：①进展面板订正——报告称阶段 2 未开始系基于旧提交，实际 v1.4 已完成；②阶段 5 扩展：特征工程候选清单（8 项新信息维度，features.py 纯函数模块）、人工判断信号系统化记录、验证标准（验证段胜率须超盈亏平衡线 ~27%；HF outcome 抽样 30→数百；阈值实盘校准）；③新增工程卫生待办（SecretStr、仓库可见性、最小 CI、USDC 精度统一、recorder 接入、README 同步）；④重申风控最小集为放量硬前提 |
| 2026-09-13 | v2.0 | 全文重构 + 逐条代码核查：①统一阶段结构（目标/交付物/完成判据/验证证据/遗留），子项颗粒度细化到 4–6 项/阶段并标完成度（0:4/4、1:5/5、2:4/6、3:3/4、4:0/4、5:2/6、6:0/6）；②事实更正：store.py 实际仅三表（backtest_runs 未建，网格结果落 runtime/grid_runs.csv）；回测包实际布局为 engine/grid/hf_loader/spot_vol/run/report（替代旧设想布局）；README L44-47 目录脱节属实；config.py:40 私钥为 str 属实；redeem.py:76 硬编码 10**6 属实；trade5m 未接 recorder 属实；无 .github/workflows 属实；③基线快照：params.py 现行默认值全量登记；pytest 118 项实测；runtime/logs 已 20 份；④回测结论档案化（四轮实验汇总表，含根因：盈亏平衡 27% vs 实际 23.5~25.1%）；⑤特征候选 F1–F8 排优先级（F1 Chainlink 更新频率 P0） |
| 2026-09-13 | v2.1 | ticks 压实管线（b08）：①data/ticks_compact.py + app/compact.py（pm-compact）：已封口 market JSONL → date 分区 Parquet（book 行 = 全档展开 1480 万行/日，meta 行 = 小事件原样保 JSON），双重校验（parquet 行数 = 写出行数；事件数守恒）通过才删源；②实测压缩 25.1x（490MB→19.5MB/日，b08 实验先验证 28.1x）；③duckdb 依赖带 `python_version < '3.14'` marker（cp314 wheel DLL 损坏实测），pm-compact 须 3.12 运行；④pm-record 开机自启 + tkinter 监视器（启动文件夹 VBS，绿/橙/灰状态窗）；⑤测试基线 118→127 |
| 2026-09-13 | v2.2 | **阶段 4 风控最小集完成 + 工程卫生四项（b09）**：①risk/gates.py（check_order 纯函数 + RiskGate 装配层，KILL/单笔/单日投入/单日亏损熔断/重复入场/USDC fail-closed/gas 告警，拒绝优先级固定）；②orchestrator ENTER 分支下单前过闸（KILL/熔断中止全部窗口 return 2，其余放弃本窗口），trade5m 装配 live 链上余额/gas 注入；③store.py 增 day_stats（[ds, ds+86400) 半开区间）/has_open_position/mark_redeemed 补 windows 表；④config.py 私钥+L2 凭证改 SecretStr（clob_trader/chain/doctor/setup 四调用方同步）；⑤redeem.py USDC 精度改动态查询；⑥.github/workflows/ci.yml（3.12 + ruff + pytest）+ README 目录图/路线图同步；⑦5.4 基建：data/judgment_log.py（SIGNAL_TAGS 词表 11 标签）+ pm-judgment CLI（append-only JSONL）；测试抓到两真实 bug（balance 异常穿透、day_stats 未来行计入）已修；测试基线 127→147 |
