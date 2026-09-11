# 开发计划与项目进程（活文档）

> 版本：v1.1 ｜ 日期：2026-09-11
> **本文档是项目的方向与进展跟踪文档，实施过程中随进度持续更新。**
> 每完成一项：勾选 `[x]` 并在文末变更记录登记；发现新问题/新决策：修订对应章节并升级版本号。
> 前置阅读：[PROJECT_PLAN.md](PROJECT_PLAN.md)（最初总体规划，本文修订其阶段 3 之后的路线）

---

## 进展面板（实时状态）

| 阶段 | 内容 | 状态 | 备注 |
|---|---|---|---|
| 0.1 | 结算规则实证 → docs/settlement-rule.md | 🔄 进行中 | Gamma slug 查询已结算市场返回空，正在排查查询方式 |
| 0.2 | pm-record 独立 24/7 录制脚本 | ⬜ 未开始 | **第一天上线，攒数据与开发并行** |
| 0.3 | 成交解析 bug（疑似已修）+ size=0 误判 FILLED | ⬜ 未开始 | 见下方修订说明 |
| 0.4 | PnL 口径 + pm-redeem 赎回脚本 | ⬜ 未开始 | |
| 1 | 模块化重构（决策纯函数） | ⬜ 未开始 | 提交拆分见 4.3 节 |
| 2 | SQLite 结构层 + 结算回填 + 自动赎回 | ⬜ 未开始 | |
| 3 | 回测引擎（17 份日志回放对账） | ⬜ 未开始 | |
| 4 | 风控最小集 | ⬜ 未开始 | 与 1–3 并行，放量前必须完成 |
| 5 | 参数扫描（walk-forward）+ 一致性回归 | ⬜ 未开始 | 持续 |

状态图例：⬜ 未开始 ｜ 🔄 进行中 ｜ ✅ 完成 ｜ ⏸ 暂缓

---

## 一、背景与问题

5m Up/Down 策略（[trade5m.py](../src/pm_arb/app/trade5m.py)）已实盘跑通，runtime/logs 有 4.5 小时真实成交记录。经两轮代码勘察 + 人工复核 17 份实盘日志，确认以下缺口。

### 地基缺口（回测能否"准"的前提）

| # | 问题 | 影响 |
|---|---|---|
| 1 | **结算规则无权威依据**：全仓库没有一处明确写出 Up/Down 判定规则。`rtds.py` 中 `base` 字段注释（"feed 生命周期内首个观测值"）是代码作者假设，无官方文档出处。官方文档 chainlink-twap.md 只描述 TWAP 数据流本身，且警告"Chainlink 不公布采样边界/权重/舍入，勿自行复现 TWAP" | 规则不钉死，回测胜率/PnL 全部不可信 |
| 2 | **历史数据基本为空**：TickRecorder 是孤儿模块（trade5m 从未导入），runtime/ 只有 17 份人类可读文本日志（≈4.5 小时，可用入场样本可能仅十几笔）。策略文档自认"预期胜率低于 50%"，调参需几百笔入场样本（p=0.5±0.05 需 ~384 个，已验算） | 数据积累受墙钟约束（1–2 周不同时段），必须最先启动 |

### 工程缺口（v1.1 修订）

| # | 问题 | 状态 |
|---|---|---|
| 3 | 580 行单文件，判定逻辑混在巨型协程 `try_window()`（~320 行）；回测无法复用同一套判定函数 | 待阶段 1 |
| 4 | ~~place_market 成交解析 bug（`FILLED 但成交 0.0000 份`）~~ | **疑似已修**：事故（01:56:03）8 分钟后的提交 `4d0fc18` 重写了 place_market（花费/份数比值算法）；其后同日三笔成交（02:36/02:46/02:51）全部正常。**待 10 分钟验证后从阶段 0 划掉** |
| 4a | **[新发现] `order.size` 硬编码 0 → PARTIAL 误判 FILLED**：`Order.apply_fill()` 用 `filled_size >= size` 判完成，而市价单 `size=0`，任何 1% 部分成交都会被判成终态 FILLED。当前 trade5m 把两种状态都当"够用"所以功能不出错，但阶段 2 落 SQLite、阶段 5 算胜率时会**系统性把部分成交标成完全成交，污染回测标签** | 待阶段 0.3 |
| 5 | 持仓到结算无自动 redeem（chain.py 已实现未接线）；结算输赢从未落盘，真实期望无法核算 | 待阶段 0.4 / 2 |
| 6 | 进程重启丢持仓；无风控；`strategies/`、`risk/`、`backtest/`、`portfolio/` 全是空壳 | 待阶段 1/2/4 |

### 已确认的决策

- 存储选型：**SQLite**（结构化层）+ JSONL（原始 tick 流）
- 胜率优化路径：**统计 + 参数扫描**（样本量到之前不上 ML）
- 缺口修复（结算赎回闭环、成交 bug、真实 PnL、风控最小集）**全部纳入本轮**

---

## 二、实施路线（按依赖 + 墙钟约束排序）

```
阶段0 结算规则实证 + bug修复 + 录制脚本（1–2 天）
  │   ├─ 0.1 结算规则实证 → docs/settlement-rule.md
  │   ├─ 0.2 pm-record 独立 24/7 录制脚本（第一天上线，攒数据与开发并行）
  │   ├─ 0.3 成交解析验证 + size=0 误判修复
  │   └─ 0.4 PnL 口径 + pm-redeem 赎回脚本
  │         ────── 阶段 0 完成后提交 git，录制开始挂机 ──────
  ├────────> 阶段2 数据管道（SQLite + 结算回填 + 自动赎回 + 持仓持久化，2–4 天）
  │                        │
阶段1 模块化重构（决策纯函数，2–3 天）──> 阶段3 回测引擎（1–1.5 周）
  │
  └────────> 阶段4 风控最小集（1–2 天，与 1–3 并行，放量前必须完成）
                                  │
                          阶段5 参数扫描 + 一致性回归（持续）
```

**排序理由**：录制脚本独立于模块化——数据积累要 1–2 周墙钟时间，必须第一天上线；模块化（决策纯函数抽取）是回测复用生产代码的前提；17 份日志不够调参但足以做**回测引擎正确性校验**。

---

## 三、阶段 0：结算规则实证 + bug 修复 + 录制脚本

### 0.1 钉死结算规则（一切的地基）

- [ ] 用 Gamma API 拉取一批**已结算的 5min 历史市场**（slug `{sym}-updown-5m-{ws}`），取 `outcome` / `outcomePrices` 拿每个窗口的真实结算方向，并读取市场 `description` 的规则原文。**已发现问题：按 slug 查已结算市场返回 `[]`，需先排查 Gamma 对 closed 市场的查询参数（closed=true 等）或归档机制**；
- [ ] 用 Binance 5m K 线（data-api.binance.vision 公共端点）作窗口价格路径代理，交叉验证候选规则（如"窗口终点 TWAP > 窗口起点 TWAP"），统计一致率；
- [ ] （pm-record 上线后）用录制的 RTDS full_accuracy_value 直接复核；
- [ ] **[v1.1 新增] 确认 taker fee**：Polymarket CLOB 当前对 5m 市场是否收 taker fee（直接影响止盈 0.65 与结算 $1 的真实到手，否则回测 PnL 系统性偏乐观）。Gamma 市场字段或官方文档确认，写入 settlement-rule.md；
- [ ] 产出 **docs/settlement-rule.md**：规则定义 + 验证方法 + N 个市场的实证一致率。规则钉死前，回测结算模拟一律标注"未验证"。

### 0.2 独立 24/7 纯录制脚本（第一天上线）

- [ ] 新增 `app/record_ticks.py`，注册 `pm-record`：复用现成 `RtdsTwapFeed` + `MarketDataFeed`（含 ws 空闲看门狗 / is_fresh 兜底），**不下单、不占资金**；
- [ ] 落盘（JSONL 按天分片，append-only）：
  - `runtime/ticks/{YYYY-MM-DD}_market.jsonl`：盘口 book 快照 + price_change 增量 + last_trade + **REST 回退快照**（新增 type:"RestBook"，补 REST 段数据洞）；
  - `runtime/ticks/{YYYY-MM-DD}_rtds_{sym}.jsonl`：TWAP 逐笔（含 full_accuracy_value、seq）；
  - **[v1.1 新增] WS 连接事件流**：`{"type":"WsReconnect","attempt":N,"delay":X}`、`{"type":"WsIdleTimeout",...}`——回测数据里要能回答"当时断流是网络/代理/订阅失效哪类问题"，成本几乎为零；
- [ ] **[v1.1 新增] 原始 WS 消息抽样落盘**（上线第一天，至少留 1 小时）：把未解析的原始 WS 帧另存 `runtime/ticks/{date}_raw_ws.jsonl`，人工翻几条确认 `event_type` 字段本身是 `"book"` 而不是我们把 `"price_change"` 误分类——验证"增量恒为 0"是服务端行为而非解析 bug。若假设错了，阶段 3 撮合模型需重新设计，越早发现成本越低；
- [ ] 窗口发现：循环 `get_window_market`，每窗口起止自动换订阅；窗口结束追加一条窗口元数据（slug/tokens/base）；
- [ ] 挂服务器 24/7，覆盖不同时段（"亚洲凌晨/周末低波动"假设须用数据验证）。

### 0.3 成交解析验证 + size=0 误判修复

- [ ] **验证 `4d0fc18` 修复**：dry-run/小额造一个部分成交场景，确认"花费/份数"算法正常（预期通过 → 划掉原 bug 项）；
- [ ] **修复 [问题 4a]**：`place_market` 下单前用 `calc_size(ask, min_size)` 估算目标份数赋给 `order.size`，让 FILLED/PARTIAL 区分有意义；同时约定：**阶段 2 SQLite 落库一律用 `filled_size` 对比目标名义金额算实际成交比例，不依赖 status 字段**（双保险）；
- [ ] 顺带修成交价 28 位小数打印（统一 `_fmt`）。

### 0.4 PnL 口径 + 赎回脚本

- [ ] realized_pnl 分"已止盈 / 待结算"两栏统计；
- [ ] 新增 `app/redeem.py`（`pm-redeem`）：扫描待赎回仓位调 `ChainClient.redeem_positions`（阶段 2 前用 `--window-start` 手动指定 + Gamma 查 condition_id）。

**阶段 0 验证**：settlement-rule.md 实证 N≥20 + taker fee 结论；pm-record 挂机 24h 双流数据非零且增长 + 原始帧确认 event_type；dry-run 部分成交场景验证通过；`pm-redeem --dry-run`；pytest 全绿；git 提交。

---

## 四、阶段 1：模块化重构——决策纯函数抽取

### 目标布局

```
src/pm_arb/
├── strategies/crypto_5m/
│   ├── params.py        # Crypto5mParams(pydantic)：11 个常量 → 可配置（CLI --param k=v）
│   ├── decisions.py     # 纯决策：decide_entry / decide_exit / pick_underdog / calc_size
│   ├── context.py       # WindowDataHub：盘口(WS/REST)+RTDS+新鲜度+时钟注入
│   └── orchestrator.py  # 窗口生命周期：对齐/引导/守卫/循环/清理
├── execution/
│   ├── broker.py        # Broker Protocol + on_order 钩子
│   ├── clob_broker.py   # live（包装 ClobTrader）
│   └── paper_runner.py  # dry-run（复用 PaperBroker）
└── app/
    ├── tui5m.py         # SessionLog + _render_tui（纯搬运）
    └── trade5m.py       # 瘦 CLI（~90 行），注册 pm-trade5m
```

### 关键设计决策

1. **回测与实盘同一套判定函数**：decisions.py 是唯一决策来源，实盘 orchestrator 与回测引擎都调它——防两套逻辑漂移；
2. **时间注入**：context.py 提供 `clock: Callable[[], float]`；策略/编排层禁止直接调 `time.time()`；`LocalOrderBook.age()` 接受 `now` 覆盖；
3. **dry-run 换 PaperBroker**（唯一行为改进点）：扩展 paper.py 的 `submit_market_buy`（按美元逐档吃 ask）/ `submit_market_sell` / `settle`（$1/$0 兑付）。

### 4.3 [v1.1 新增] 提交拆分纪律（真实资金在跑，必须可精确 bisect）

| 批次 | 内容 | 性质 | 验证 |
|---|---|---|---|
| commit A | 纯函数抽取（常量→params.py、判定→decisions.py、TUI/数据访问搬运） | **行为不变** | dry-run 基线 diff 一致 + 特征化测试 |
| commit B | Broker 协议 + paper_runner（dry-run 换 PaperBroker） | 引入新行为 | PaperBroker 单测 |
| commit C | orchestrator + 瘦 CLI | 装配 | 全量测试 |

重构前先跑 `pm-trade5m --dry-run --now --windows 1` 留基线日志。

---

## 五、阶段 2：数据管道结构层 + 结算闭环

### SQLite 结构层（infra/store.py，WAL 模式，Decimal 存 TEXT）

| 表 | 用途 |
|---|---|
| `orders` | 所有 live/paper 订单（Broker on_order 钩子统一写入；**成交比例用 filled_size/目标名义计算，不依赖 status**——见问题 4a） |
| `windows` | 每窗口一行：entered / entry_price / tp_hit / **settlement_outcome** / redeemed / realized_pnl |
| `positions` | 持仓持久化（启动读未平仓记录，修复重启丢持仓） |
| `backtest_runs` | 阶段 5 留痕 |

### 结算结果记录 + 自动赎回

- [ ] 窗口结束后 5–10 分钟按 slug 查 Gamma（closed 市场带 outcomePrices）回填 `windows.settlement_outcome` 与 `realized_pnl`（赢：filled_size×1−成本；输：−成本；**含 taker fee 口径**）；
- [ ] **历史回填器** `app/backfill.py`（`pm-backfill`）：任意历史窗口批量查 Gamma 拿结算——没交易没录制的窗口也能拿到，回测样本的关键放大器；
- [ ] 结算回填成功且 tp_hit=0 的仓位自动触发 `ChainClient.redeem`，更新 redeemed=1；
- [ ] trade5m 实盘接 recorder（与 pm-record 相同落盘格式）。

**验证**：sqlite 查询窗口结算；开仓后 kill 进程重启能继续监测该仓位。

---

## 六、阶段 3：回测引擎

```
backtest/
├── logparse.py   # 17 份实盘 .log → 结构化表（elapsed/TWAP/双边bid-ask-size/来源）
├── loader.py     # JSONL tick → (recv_ts, event) 合并流，按时间排序
├── engine.py     # 虚拟时钟事件驱动回放（复用 LocalOrderBook + decisions.py）
├── settlement.py # 结算模拟（依据 settlement-rule.md 钉死的规则）
├── metrics.py    # 胜率/PnL分布/过滤漏斗/参数敏感性
└── report.py     # 终端报表
```

### 四条铁律

1. **判定复用生产代码**：engine 调 decisions.py 同一套函数，数据源从实时流换成历史迭代器；
2. **严防前视偏差**：TWAP high/low 按截止当前模拟时刻的数据滚动计算（与实盘 rtds.high/low 行为一致），绝不用窗口最终 high/low 做判定；
3. **撮合保守**：录制数据只有最优一档 bid/ask/size。份数超过 ask_size 时按部分成交或放弃处理，报告明确标注假设，避免虚高收益；**（前提：0.2 的原始帧验证已确认快照-only 是服务端行为）**；
4. **回放对账校验引擎本身**：拿 17 份日志的实盘片段重放（logparse → engine），算出的入场/止盈判定必须与当时实盘决策一致，否则引擎有 bug。

### 结算模拟三级源

windows 表实盘结果 > Gamma 回填 > 按 settlement-rule.md 钉死的规则从录制 TWAP 推导（推导启用前先对 ~20 个已回填窗口验证一致）。

---

## 七、阶段 4：风控最小集（与 1–3 并行，放量前必须完成）

`risk/gates.py`，所有 live 下单过闸：

- [ ] 单窗口名义上限 / 单日累计投入上限 / 单日已实现亏损熔断；
- [ ] 下单前余额检查；每窗口频率限制（防回执异常重复下单）；
- [ ] **[v1.1 新增] POL（gas）余额低于阈值告警**——否则 redeem 会某天因没 gas 静默失败，"已实现 PnL"与"待赎回仓位"对不上账；
- [ ] Kill switch：`runtime/KILL` 文件存在 → 拒新仓。

Prometheus / Grafana / Telegram 后移（现阶段 structlog + 文件日志够用）。

---

## 八、阶段 5：参数扫描 + 持续一致性回归

1. **样本量优先**：pm-record 24/7 积累 + pm-backfill 放大历史结算样本。先产出每日胜率/EV 报表与置信区间（p=0.5±0.05 需 ~384 入场样本，已验算），让"是否正 EV"有统计答案；
2. **参数网格 + walk-forward**：ENTRY_AFTER/UNTIL、MAX_VOL、MIN/MAX_ENTRY、TAKE_PROFIT_PRICE；前 70% 调参、后 30% 验证（或滚动窗口）；报告敏感性曲线而非单点最优；结果落 backtest_runs 表。**清醒认知**：策略结构性负期望，小样本网格搜索极易拟合噪声——回测好看 ≠ 放大仓位；
3. **持续一致性回归**：每次实盘跑都留录制数据，定期用新鲜实盘数据重放回测引擎，核对决策与线上日志一致，防长期漂移。

---

## 九、对原规划（PROJECT_PLAN.md）的修订

| 原规划 | 修订 |
|---|---|
| ClickHouse/TimescaleDB 存 tick | JSONL + SQLite（单日 ~100MB 量级） |
| "WS 快照+增量、序列号校验"架构假设 | 实测 price_change 增量恒为 0（待原始帧验证确认），按快照节奏回放，撮合取保守口径 |
| 阶段 4 风控整体后置 | 拆最小集提前（熔断/限额/余额/gas 告警），观测栈后移 |
| "不要预测方向"套利纪律 | 已有意转向方向性投机——小仓位（$2/窗）+ 单日限额 + 熔断补偿，README 显式声明 |
| 阶段 5/6（NegRisk/跨平台/做市） | 降级远期可选，Strategy 抽象保持轻量 |
| 新增 | 结算规则实证文档为回测前置条件；数据积累与开发并行 |

---

## 十、关键文件清单

| 文件 | 角色 |
|---|---|
| src/pm_arb/app/trade5m.py | 拆分对象（580 行） |
| src/pm_arb/execution/clob_trader.py | place_market（4d0fc18 重写版）+ size=0 误判修复点 |
| src/pm_arb/execution/orders.py | apply_fill 的 FILLED/PARTIAL 判定（问题 4a） |
| src/pm_arb/data/recorder.py | 孤儿录制器，pm-record 复活它 + 补 REST 快照/RTDS/连接事件路径 |
| src/pm_arb/data/rtds.py | TWAP 流（on_update 钩子挂录制；base 语义待结算规则实证后修正） |
| src/pm_arb/data/ws.py | 补 WsReconnect/WsIdleTimeout 事件回调（供录制） |
| src/pm_arb/execution/paper.py | dry-run/回测共用成交模型（扩展市价/结算/保守单档撮合） |
| src/pm_arb/execution/chain.py | redeem 已实现，待接线 |

---

## 变更记录

| 日期 | 版本 | 变更 |
|---|---|---|
| 2026-09-11 | v1.0 | 初版：两轮勘察 + 三项用户决策（SQLite / 统计+参数扫描 / 缺口全纳入） |
| 2026-09-11 | v1.1 | 人工复核代码后的五点修订：①0.3 成交 bug 疑似已被 4d0fc18 修复（待验证后划掉）；②[新发现] order.size=0 导致 PARTIAL 误判 FILLED，纳入 0.3 + 阶段 2 落库纪律；③pm-record 增加原始 WS 帧抽样落盘（验证增量恒 0 是服务端行为）+ WS 连接事件流；④0.1 增加 taker fee 确认；⑤风控增加 POL gas 余额告警、阶段 1 提交拆分纪律（A 纯搬运/B 新行为/C 装配） |
