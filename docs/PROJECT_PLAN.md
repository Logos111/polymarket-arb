# Polymarket 量化套利交易系统 — 项目规划

> 版本：v1.0 ｜ 日期：2026-09-07
> 定位：以**无风险/低风险套利**为核心的自动化预测市场交易系统，先跑通确定性收益策略，再扩展到跨平台与做市。

---

## 1. 项目概述

### 1.1 目标

构建一套 7×24 小时运行的自动化交易系统，在 Polymarket（Polygon 链上预测市场）上捕获以下收益：

1. **确定性结构套利**（本金安全型，收益来自盘口定价偏离，不依赖预测方向）；
2. **跨平台价差套利**（Polymarket vs Kalshi 等）；
3. **流动性做市收益**（挂单赚点差 + 官方流动性奖励）。

### 1.2 Polymarket 核心机制（套利的基础）

- 市场为二元合约（YES/NO），**结算时 YES + NO = $1**（由 UMA 预言机裁决结果）。
- 底层为 Polygon 上的 **CTF（Conditional Tokens Framework）**：
  - `splitPosition`：存入 $1 USDC → 铸造 1 份 YES + 1 份 NO；
  - `mergePositions`：销毁 1 份 YES + 1 份 NO → 赎回 $1 USDC（**无需等待结算，即时锁利**）。
- **NegRisk 多结果市场**（如"谁将赢得大选"）：N 个互斥结果，一整套完整组合 = $1，通过 NegRiskAdapter 可在 negRisk 头寸与标准 CTF 头寸之间转换。
- 订单簿链下撮合（CLOB）、链上结算；下单为 EIP-712 签名订单，无需每笔上链，但 split/merge/redeem 需要链上交易。
- 当前大部分市场**交易手续费为 0**（以官方最新文档为准），存在官方 **Liquidity Rewards** 做市奖励计划。

> ⚠️ 关键参数会变化，所有合约地址、费率、tick size、最小下单额均以官方文档（docs.polymarket.com）与链上查询为准，配置化管理、不硬编码。

### 1.3 收益来源与策略矩阵

| 策略 | 原理 | 风险等级 | 容量 | 竞争烈度 | 优先级 |
|---|---|---|---|---|---|
| A. 互补盘口套利（binary merge/split） | ask(YES)+ask(NO)<$1 买入合并；或 bid(YES)+bid(NO)>$1 拆分卖出 | 极低（主要为腿风险） | 小 | 高 | P0 |
| B. NegRisk 完整组合套利 | N 个结果最优 ask 之和 < $1（或 bid 之和 > $1） | 低 | 中 | 中 | P1 |
| C. 关联/互斥市场套利 | 同一事件不同市场（如候选人各组市场）定价不一致 | 中（含裁决风险） | 中 | 中 | P2 |
| D. 跨平台套利 | Polymarket vs Kalshi / 体育博彩同事件价差 | 中（资金/合规/结算风险） | 大 | 中低 | P2 |
| E. 结算/裁决套利 | 高确定性事件在 0.99 附近的折价、争议事件错价 | 中高 | 中 | 低 | P3 |
| F. 做市 & 流动性奖励 | 双边挂单赚价差 + rewards 日均发放 | 中（库存风险） | 大 | — | P3 |

---

## 2. 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        监控告警层 (Observability)                 │
│   Prometheus + Grafana 大盘 / Telegram 告警 / 审计日志 / PnL 日报  │
├─────────────────────────────────────────────────────────────────┤
│  风控层 (Risk)                                                   │
│   盘前限额检查 · 腿风险监控 · 库存/敞口 · 自动止损 · Kill Switch  │
├─────────────────────────────────────────────────────────────────┤
│  策略层 (Strategies)                                             │
│   互补套利 │ NegRisk套利 │ 关联市场 │ 跨平台 │ 做市               │
│        信号生成 → 目标头寸 → 报价/下单意图                         │
├──────────────┬──────────────────────────┬───────────────────────┤
│  执行层       │  组合/账本层 (Portfolio)  │  回测层 (Backtest)     │
│  CLOB 下单/撤单│  链上头寸 · 挂单 · 成交   │  Tick 回放 · 成交模拟  │
│  智能订单路由  │  对账 · PnL · 资金管理    │  策略评估 · 参数优化   │
│  链上 split/  │                          │                       │
│  merge/redeem│                          │                       │
├──────────────┴──────────────────────────┴───────────────────────┤
│  数据层 (Market Data)                                            │
│   Gamma API(市场元数据) · CLOB REST(盘口/价格) ·                  │
│   WebSocket(book/delta/用户通道) · Polygon RPC/Subgraph(链上)     │
│   本地订单簿构建 · 数据录制(ClickHouse/TimescaleDB)               │
└─────────────────────────────────────────────────────────────────┘
```

### 2.1 数据层

| 数据源 | 用途 | 接入方式 |
|---|---|---|
| Gamma API (`gamma-api.polymarket.com`) | 市场/事件元数据、token ID、结算状态、奖励计划 | REST 轮询 + 增量同步 |
| CLOB REST (`clob.polymarket.com`) | 盘口快照、最优价、midpoint、sampled orderbooks | REST（注意限流） |
| CLOB WebSocket | 实时盘口（book 快照 + price_change 增量）、用户订单/成交通道 | `wss://ws-subscriptions-clob.polymarket.com/ws/market`、`/ws/user` |
| Polygon RPC + CTF Subgraph | 链上余额、approval、split/merge/redeem 回执、头寸核对 | web3.py + GraphQL |
| Kalshi API（后期） | 跨平台比价 | REST + WS |

**要点：**
- 本地维护 **L2 订单簿**：WS 先收 book 快照，再顺序应用 price_change delta，序列号乱序/缺口时自动重新快照。
- 全量 tick 数据落库（盘口快照 + 增量 + 成交），用于回测与策略复盘——**数据是量化团队的核心资产，第一天就要录**。
- 市场元数据每日/实时同步，自动发现新市场、标记 negRisk 市场、剔除已结算/流动性差市场。

### 2.2 执行层

- **认证**：L1（钱包私钥 EIP-712 签名派生 API Key）→ L2（key/secret/passphrase 签名请求头）。私钥仅存加密环境变量/HSM，绝不入库。
- **订单类型**：GTC（挂单）、FOK（全部成交或立即取消）、FAK/IOC（部分成交剩余取消）。套利吃单用 FOK/FAK 控制腿风险。
- **官方 SDK**：`py-clob-client`（Python）起步，关键路径可自研轻量客户端降低延迟。
- **链上模块**（web3.py）：USDC/USDC.e 授权（approval）、`splitPosition`、`mergePositions`、`redeemPositions`、NegRiskAdapter `convertPositions`；支持 multicall 批量 merge 省 gas；nonce/gas 管理、失败重试、回执确认。
- **智能订单路由**：双腿/多腿下单编排——先下确定性高的腿、第二腿限时 FOK 追价；失败腿按预案对冲或 unwind；成交后自动触发 merge 锁利。
- **订单状态机**：new → submitted → partially_filled → filled/canceled/rejected，超时未确认主动查询对账，杜绝"幽灵订单"。

### 2.3 策略层

统一抽象：`Strategy` 基类产出 `Signal`（目标腿、方向、价格、数量、紧急度），由执行层与风控层审批后执行。

- **策略 A（互补套利）逻辑**：
  - 买入合并：`ask_YES + ask_NO < 1 − 成本`（成本 = gas 摊销 + 滑点缓冲）→ 双腿 FOK 吃单 → 链上 merge 赎回 $1，单笔利润 `1 − (ask_YES+ask_NO) − 成本`；
  - 拆分卖出：`bid_YES + bid_NO > 1 + 成本` → 链上 split → 双边挂/吃 bid；
  - 过滤：盘口深度（最优档累计可成交量）、最小下单额（$5）、tick size（0.001/0.01）、市场结算时间（merge 不受时间影响，但要避开结算中市场）。
- **策略 B（NegRisk）**：订阅同一 event 下全部 outcome token，计算完整组合的最优 ask/bid 之和；偏离 $1 超过阈值即触发；注意 negRisk 头寸与普通 CTF 头寸的转换路径与费用模块。
- **策略 C（关联市场）**：构建市场关系图谱（互斥组、条件市场、同事件不同表述），用线性约束（概率和=1、包含关系）求解错价。
- **策略 D（跨平台）**：同事件映射（标题/规则/裁决日期模糊匹配 + 人工审核表），扣除提币/外汇/手续费后净价差 > 阈值才下单；注意两平台资金独立、无法即时对冲腿风险。
- **策略 F（做市）**：围绕 midpoint 双边报价，动态调整价差与库存偏斜（inventory skew），参与 rewards 计划（按有效挂单份额发奖），用策略 A/B 的 merge 通道兜底库存。

### 2.4 风控层

| 风险 | 应对 |
|---|---|
| **腿风险**（一条腿成交另一条未成交） | FOK/FAK 下单、第二腿限时追价、失败自动 unwind、单信号最大腿风险敞口限额、极端情况下用关联市场对冲 |
| 库存/方向性敞口 | 单市场、单事件、全局敞口上限；超时未 merge 的头寸告警；净敞口实时计算（挂单+链上） |
| 订单/API 风险 | 下单幂等、客户端单号去重、限流排队、WS 断连自动重订+订单全量对账 |
| 链上风险 | gas 上限、nonce 冲突处理、tx 卡死重试（ Polygon ~2s 区块）、merge 前校验头寸数量 |
| 裁决/预言机风险 | 不参与争议市场（UMA dispute）、结算中市场禁止新开仓 |
| 智能合约/平台风险 | 合约升级监控、单日最大亏损熔断、Kill Switch（一键撤所有单 + 停止开仓 + 只允许平仓/merge） |
| 密钥安全 | 专用交易钱包、小额起步、多签资金钱包、IP 白名单、API key 定期轮换 |

### 2.5 回测与研究层

- 基于录制的 tick 数据回放：订单簿状态机逐 tick 重建，模拟 FOK/GTC 成交（考虑排队位置、盘口深度、自身订单对簿子的影响）。
- 回测必须计入：真实手续费、gas、滑点、下单延迟、腿失败概率（用实盘 paper 数据校准）。
- 输出指标：年化收益、Sharpe、最大回撤、单笔收益分布、日均信号数、捕获率、容量上限、资金利用率。
- **Paper trading 先行**：所有策略先在仿真环境（实时行情 + 模拟成交）跑至少 1–2 周，对照回测验证。

### 2.6 监控告警

- 大盘：实时 PnL、敞口、挂单数、成交率、延迟（WS→决策→下单）、API 错误率、gas 消耗、链上余额。
- 告警（Telegram/钉钉）：腿失败、merge 失败、余额不足、WS 断连、回撤超阈值、每日开盘/收盘日报。
- 全量审计日志：每个信号、每张订单、每笔链上交易可追溯。

---

## 3. 技术栈

| 层 | 选型 |
|---|---|
| 语言 | Python 3.12+（策略/数据/回测，asyncio 全异步）；热路径（下单/盘口解析）后期可用 Rust/Go 重写 |
| 包管理 | uv + pyproject.toml |
| 链上 | web3.py、eth-account、官方 py-clob-client |
| 数据 | Pydantic v2（模型）、Polars/Pandas（研究）、ClickHouse 或 TimescaleDB（tick 存储）、Redis（实时状态/缓存） |
| 部署 | Docker Compose（单机起步）→ 云服务器（低延迟区域，靠近 Polygon RPC / CLOB 节点） |
| 可观测 | structlog、Prometheus、Grafana、Telegram Bot |
| 测试 | pytest、pytest-asyncio、合约交互用 fork 测试（anvil/hemera） |

---

## 4. 目录结构（建议）

```
polymarket/
├── pyproject.toml
├── .env.example                 # 私钥/API key/RPC 模板（真实 .env 不入库）
├── config/                      # 策略参数、限额、合约地址（按环境区分）
├── src/pm_arb/
│   ├── data/                    # gamma 同步、clob rest、ws 客户端、orderbook 构建、录制
│   ├── execution/               # clob 封装、订单状态机、智能路由、链上 ops(split/merge/redeem)
│   ├── portfolio/               # 头寸、对账、PnL、资金管理
│   ├── strategies/              # complement / negrisk / related / cross_venue / market_making
│   ├── risk/                    # 限额、腿风险、kill switch
│   ├── backtest/                # tick 回放、成交模拟、指标
│   ├── infra/                   # config、logging、metrics、alerts、secrets
│   └── app/                     # 各入口：trader(实盘) / paper(仿真) / recorder(录数) / backtest
├── tests/
├── notebooks/                   # 研究与策略分析
└── docs/                        # 本文档、API 笔记、策略复盘
```

---

## 5. 开发路线图

### 阶段 0：准备与脚手架（第 1 周）
- [ ] 注册 Polymarket 账户、准备交易钱包（小额 USDC on Polygon）、申请 RPC（商用节点，如 Alchemy/QuickNode）
- [ ] 通读官方文档：CLOB API、NegRisk、CTF 合约、Liquidity Rewards 规则
- [ ] 仓库脚手架、配置/日志/密钥管理、Docker
- [ ] 跑通官方 py-clob-client 只读示例

### 阶段 1：数据层（第 1–2 周）
- [ ] Gamma 市场元数据同步（市场发现、negRisk 标记、状态过滤）
- [ ] CLOB REST + WS 接入，本地 L2 订单簿构建（快照+delta，序列号校验）
- [ ] 用户通道（orders/trades）订阅
- [ ] Tick 数据录制入库（**从第一天开始积累**）
- [ ] 链上余额/头寸读取（RPC + subgraph）

### 阶段 2：交易接口 + 仿真（第 3–4 周）
- [ ] L1/L2 认证、下单/撤单/改单全链路（**先用最小额 $5 真实下单测试**）
- [ ] 订单状态机 + 成交回报 + 异常对账
- [ ] 链上：approval、split、merge、redeem 实操验证（一笔完整 merge 锁利闭环）
- [ ] Paper trading 框架：实时行情 + 模拟撮合

### 阶段 3：策略 A 互补套利上线（第 5–6 周）
- [ ] 信号检测（含深度、成本模型、过滤规则）
- [ ] 双腿执行编排 + 腿风险处理 + 自动 merge
- [ ] 回测框架跑通，用已录制数据验证收益分布
- [ ] Paper 跑 1–2 周 → 小额实盘（如 $50–200/单）逐步放量

### 阶段 4：风控与运维硬化（第 7 周）
- [ ] 全套限额、敞口监控、Kill Switch
- [ ] Grafana 大盘 + Telegram 告警 + 日报
- [ ] 断网/断连/API 故障演练、重启恢复与对账
- [ ] 资金管理：单笔/单市场/总仓位上限、自动赎回结算资金

### 阶段 5：策略 B/C 扩展（第 8–10 周）
- [ ] NegRisk 完整组合套利（含 convertPositions 路径）
- [ ] 关联市场图谱 + 互斥组套利
- [ ] 多策略资金分配与信号优先级

### 阶段 6：跨平台与做市（第 11 周起）
- [ ] Kalshi 行情接入与事件映射，策略 D 上线（注意合规与资金通道）
- [ ] 做市引擎 + rewards 优化（策略 F）
- [ ] 延迟优化（服务器部署、热路径语言重写）、容量评估与放量

---

## 6. 关键成功指标（KPI）

| 指标 | 目标（起步阶段） |
|---|---|
| 系统可用性 | > 99.5%（故障 5 分钟内告警） |
| 信号→下单延迟 | < 200ms（后期优化到 < 50ms） |
| 腿失败率 | < 1%，失败腿 100% 在 60s 内 unwind/对冲 |
| 对账差异 | 每日 0 笔未解释差异 |
| 策略 A 单笔净利 | 覆盖 gas+滑点后 ≥ 0.3%（阈值可配） |
| 最大回撤 | < 总资金 5%（触发熔断） |

---

## 7. 重要注意事项

1. **合规**：Polymarket 服务条款限制部分地区（含美国）用户访问；Kalshi 对美国用户开放但需 KYC。请自行确认所在地法律与平台 ToS，本项目仅作技术研究用途。
2. **资金安全**：交易钱包与资金钱包分离；私钥只存本地加密环境变量；任何外部服务不接触私钥。
3. **参数时效**：合约地址、费率、tick size、奖励规则、API 限流均可能变化——全部配置化并定期核对官方文档。
4. **先数据后策略**：套利机会稍纵即逝且竞争激烈（大量专业 bot），录制数据、回测、paper 三步不可省略。
5. **不要预测方向**：套利系统的纪律是只做锁定利润的交易；任何"腿失败后赌方向"都是失控的开始。
