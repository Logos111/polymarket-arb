# 策略：BTC/ETH 5 分钟冷门方方向性交易（crypto_5m_underdog）

> 版本：v2.0 ｜ 日期：2026-09-13 ｜ 状态：**实验性小额实盘 + 回测研究阶段**
> 类型：**方向性投机，不是套利**。四轮回测（95+ 组参数）实证费后结构性负 EV（见 §6），
> 当前研究目标：通过过滤与新特征把验证段胜率推过盈亏平衡线（~27%）。
> 实现：`src/pm_arb/strategies/crypto_5m/`（params/decisions/context/orchestrator + backtest/），
> 入口 `pm-trade5m`（[app/trade5m.py](../app/trade5m.py) 瘦 CLI）。

---

## 1. 标的与结算规则

Polymarket 5 分钟加密 Up/Down 二元市场：

- slug 规则：`{sym}-updown-5m-{window_start_unix}`，窗口起点为 UTC 向下取整 300 秒；每窗口两个 token：**Up** / **Down**，结算时正确方向每份赎 $1，错误方向归 $0
- 支持：btc / eth（代码层发现 sol/xrp/doge/bnb/hype/zec，CLI 仅开放 btc/eth）
- **结算规则（2026-09-12 实证钉死，[docs/settlement-rule.md](../../../docs/settlement-rule.md)）**：
  - 结算价 = **Chainlink Data Streams RTDS TWAP-60s**（非链上聚合器 latestRoundData）；TWAP(窗口末) ≥ 起点价 → **Up** 赢，否则 **Down** 赢
  - 方法论：从 Gamma 市场 description 一手取规则原文；Binance 5m K 线 578 窗口交叉验证（总一致率 84.6%，分歧集中于平坦窗口 <0.02%→56.9%、≥0.5%→100%）
- **手续费（taker only）**：`fee = 份数 × 0.07 × p × (1−p)`（p=成交价），官方文档 + Gamma feeSchedule 双源确认；maker 挂单免费

## 2. 现行策略规则（参数单一来源：[params.py](crypto_5m/params.py) 默认值）

| 参数 | 现行值 | 含义 |
|---|---|---|
| `target_notional` | $2.00 | 单笔名义金额；份数 = ceil(2/ask) 且 ≥ 市场 orderMinSize |
| `entry_after` / `entry_until` | 70s / 135s | 开窗后第 70–135 秒（剩余 3:50–2:45）为入场判定窗 |
| `min_entry` / `max_entry` | 0.15 / 0.30 | 冷门方 best ask 必须落在 (0.15, 0.30)；过冷（≤0.15）近乎买彩票不入场 |
| `max_vol` | $30 | 本窗口 TWAP high−low 必须 < $30（波动过滤） |
| `take_profit_price` | 0.99 | 固定止盈价（best bid ≥ 0.99 即市价卖出）；≈持有到结算 |
| `stop_loss_price` | 0（关闭） | 固定止损价；**已被 27 组网格证伪**（见 §6），默认关闭 |
| `poll` | 2s | 监测轮询间隔 |
| `ws_fresh_sec` / `feed_fresh_sec` | 10s / 15s | 盘口 WS 簿 / RTDS 喂价新鲜度阈值，超龄回退/拒判 |
| `end_margin` | 60s | 结算前 60s 停止一切操作 |
| 下单方式 | **市价 FAK** | 能成交多少算多少、剩余取消；FOK 在快市连续被杀（09-11 实测，见 §5） |

**选边**（`pick_underdog`）：买双边中 best ask 更低的一方，即市场定价中概率较小的"冷门方"。

**入场判定状态机**（`decide_entry`，纯函数）：

| 结果 | 语义 | 可恢复性 |
|---|---|---|
| WAIT / MISSED | 未到 / 已错过入场时间窗 | 时间驱动 |
| ABORT_DATA | ask 或 rng 数据不全 | 不可恢复，本窗口放弃 |
| ABORT_VOL | rng ≥ max_vol（high-low 单调不减） | 不可恢复，本窗口永久拒绝 |
| OBSERVE | ask 越界（≥max_entry 或 ≤min_entry 过冷） | ask 回落区间内可重新判定 |
| ENTER | 信号成立 → 市价买入 ≈$2 | — |

**退出**：持仓后轮询 best bid——`decide_exit`（≥0.99 止盈，市价卖出，PnL 含出场 taker fee）→ `decide_stop`（默认关）→ 否则持有到结算（对赎 $1/份，错归 $0）。

## 3. 数据与执行架构（现行实现）

- **喂价**：`data/rtds.py` RtdsTwapFeed——Polymarket RTDS 中继的 Chainlink TWAP-60s 流，**与结算同源**；每窗口新建（base/high/low 与窗口起点对齐）；带业务级看门狗（服务端空帧心跳会喂饱链路层看门狗，订阅静默失效时按业务时钟重连）
- **盘口**：`data/ws.py` + `data/orderbook.py` 本地 L2 簿（WS 快照；实测服务端 price_change 增量恒为 0，2000 帧原始样本确认）；WS 静默/超龄（10s）自动回退 REST `/book` 快照
- **编排**：`orchestrator.py` WindowOrchestrator——窗口对齐 → 引导（Gamma 取市场，6 次重试，endDate 守卫防边界竞态取错市场）→ 监测循环 → 清理；时钟注入（禁直接 time.time()）；默认最多 20 窗口 / 累计 3 笔成交停止
- **执行抽象**：`execution/broker.py` Broker Protocol——live=ClobBroker（包装 ClobTrader），dry-run/回测=PaperRunner（真实本地簿逐档 VWAP 撮合，深度不足保守 FAILED）
- **持久化**：`infra/store.py` SQLite（WAL）三表 orders/windows/positions；on_order 钩子统一落单；结算守护每 60s 扫描、窗口结束 +360s 宽限期后查 Gamma 回填输赢与 PnL；重启自动恢复遗留持仓
- **周边 CLI**：`pm-record`（24/7 三路流录制）、`pm-backfill`（批量回填历史窗口结算，回测样本放大器）、`pm-redeem`（赎回核对/执行；proxy 钱包 signature_type=3 场景待 Safe 授权）
- **回测**：`crypto_5m/backtest/`（HF 数据集引擎 + Binance 1s K 线现货波动模块），入口 `pm-bt5m` / `pm-grid5m`；回测与实盘调**同一套** decisions.py / params.py，杜绝逻辑漂移

## 4. 参数演进史

| 日期 | 参数组合 | 动因 |
|---|---|---|
| 初版 | 入场 90–120s / 波动 <$20 / 止盈 +50% | — |
| 2026-09-09 | 105–135s / <$25 / +100% | 首跑 3 窗口全被 $20 过滤拦截（实测 range $32–43） |
| 2026-09-11 | 60–135s / <$33 / 止盈 0.65 | 实盘调参，首笔真实成交 |
| 2026-09-12 起（现行） | **70–135s / <$30 / 价带 (0.15,0.30) / 止盈 0.99 / 止损关** | 60 组网格 walk-forward：tp=0.99（持有到结算）方向最优；min_entry=0.15 过滤"近乎定局"窗口；止损 27 组扫描证伪 |

## 5. 实盘验证记录

### 2026-09-09 首跑（3 窗口，未成交，$0 盈亏）

三窗口 Chainlink range $32–43 全部超当时 $20 阈值，未发出任何订单。要点：过滤实际很严（heartbeat 单次跳动常 $30–40）；窗口前段方向明确时冷门方常只有 bid 没有 ask（流动性假象）。工程链路（L2 凭证派生、盘口轮询、引导重试、末段 404 容错）验证通过。

### 2026-09-11 首笔真实成交（亏 -$2.10）

- t+60s 信号成立（Up ask=0.280、档深 461 份、波动 $24.61<$33）→ 市价买单 FILLED，7.142858 份 @ 0.28 ≈ $2.00
- 持有到结算未止盈（bid 最高 0.28 远低于 0.65）；BTC 从 77,373 跌至 77,314，结算 Down → 亏 $2.00 + 费 $0.10
- **下单方式修正**：FOK 限价单在快市连续 2 窗被杀（往返 ~1s 内价格上移/钓鱼档消失），改市价 FAK 后一次成交
- 顺带修复回执解析 bug（CLOB 市价单回执为十进制字符串，曾多除 1e6）与 PARTIAL 误判 FILLED（order.size=0 → ref_price 预估份数 + 落库用 filled_size 双保险）

### 2026-09-10/11 仓位核对

`pm-redeem --dry-run` 实测 4 笔实盘仓位全部在 funder（proxy）名下、**全部为输方**（应赎 $0.00），与链上探针吻合——小样本真实成交方向与回测"实际胜率低于盈亏平衡线"结论一致。

### 运行规模

runtime/logs 现有 20 份实盘日志；pm-record 持续录制（09-08 起，三路流 + 原始帧验证）。

## 6. 回测研究（HF 数据集 2026-03-24 → 05-18，约 8 周）

**数据底座**：HF 公开数据集秒级盘口（BTC 14,226 窗口 / ETH 11,245 窗口，outcome 为推断标签，30 窗口抽样核对 30/30 一致）；b07 起接入 Binance 1s K 线（各 492 万行）重建滚动 TWAP-60s 现货波动（Chainlink 历史数据需商务审批不可得）。撮合保守：仅最优档深度，超深按部分成交/放弃。

### 四轮实验汇总（费后口径）

| 轮次 | 设计 | 结论 |
|---|---|---|
| R1 基线 | 现行参数全量 | BTC 每笔 -$0.47 / ETH -$0.28；名义胜率 31~34% > 隐含胜率 28~29%，但止盈截获上限 + 手续费吃光毛优势 |
| R2 参数网格 | 60 组 × walk-forward 70/30 | **验证段无一组转正**；tp=0.99（持有到结算）方向最优，仍是"亏得最少" |
| R3 止损扫描 | 27 组（sl=0.05/0.10/0.15） | 触发率 78~88% 形同虚设；最优组仍差于无止损——**止损假设证伪**，左尾封顶买不回被砍的结算赢家 |
| R4 b07 现货波动过滤 | max_vol {20,25,30} × until {100,135,180}，--spot | **四轮以来第一个方向正确的杠杆**：BTC 验证段随阈值收紧单调改善；最优 mv20/until=100 全量 BTC -$720.02（vs 无过滤 -$2,479.86，减亏 54%）；被滤 3,954 笔平均 -$0.445（留下的 1.67 倍），删的确实是更差窗口；**ETH 三档全不触发**（TWAP60 波幅极少超 $20） |

### b07 最优参数全量（mv=20 / until=100 / tp=0.99 / 价带 0.20–0.30 / 止损关）

| 指标 | BTC | ETH |
|---|---|---|
| 入场笔数 | 2,708（过滤 59%） | 5,348 |
| 名义胜率（含止盈） | 25.0% | 25.9% |
| 总 PnL | -$720.02 | -$777.10 |
| 其中手续费 | $295.73 | $591.10 |
| 每笔均值（费后 / 零费） | -$0.266 / -$0.157 | -$0.145 / -$0.035 |

### 根因与限定

- **根因**：冷门方入场均价 ~0.27，盈亏平衡胜率需 ~27%，实际 23.5~25.1%——**定价劣势 2~4 个点，不是执行/调参问题**；同一参数平面继续微调无解，须引入新信息维度（见 §8）
- **置信度**：BTC 的 max_vol 单调性仅验证段成立（train 段非单调），如实记录打折；Binance 单所代理 ≠ Chainlink 多所聚合，**方向可迁移、阈值数值需实盘标定**
- ETH 零费口径 -$0.035 接近打平 → maker 方向（吃返佣/省 taker 费）是转正的硬候选路径之一

详细数据：[backtest/runtime/](crypto_5m/backtest/runtime/) 下各轮专项报告。

## 7. 风险（现行认知）

- **结构性负 EV（实测非推测）**：四轮 95+ 组参数费后全负；当前所有持仓口径均为小额学费（$2/笔），不构成可规模化策略
- **全损**：冷门方结算错误时整笔名义归零
- **喂价卡死 / 假低波动**：Chainlink 在窗口内可能长时间不更新（heartbeat），high-low 偏小不代表真实低波动——本策略**最脆弱的假设**，已列为特征工程 F1（Chainlink 更新频率甄别）最优先项
- **冷门方流动性假象**：方向明确时冷门方常无 ask 或仅极端价位有量；档深不足时 PaperBroker 保守拒单，live 按 FAK 部分成交
- **部分成交**：市价 FAK 可能部分成交（FILLED/PARTIAL 语义已修，落库按 filled_size 计）
- **proxy 赎回滞留**：实盘钱包 signature_type=3，赢单自动赎回待 Safe execTransaction + EIP-1271 授权路径（当前只记待赎）
- **窗口间隙/边界竞态**：市场创建/关闭瞬间 Gamma 查不到自动跳过；endDate 守卫防取错市场（历史事故教训）

## 8. 下一步（与 DEV_PLAN v2.0 对齐）

1. **风控最小集先行**（risk/gates.py：限额/熔断/余额检查/gas 告警/Kill Switch）——任何放量之前的硬前提
2. **特征工程 F1–F8**（新增 features.py 纯函数模块）：F1 Chainlink 更新频率（P0，直击最脆弱假设）、F2 盘口失衡、F3 TWAP 短窗斜率、F4 二阶导、F5 价差/深度质量、F6 BTC/ETH 跨币种联动、F7 时段效应、F8 连续窗口自相关
3. **验证纪律**：候选清单先定再看验证集；新过滤器及格线 = 验证段名义胜率**清晰超过**盈亏平衡线 ~27%（非"比基线好"）；HF outcome 抽样 30→数百窗口；阈值经 pm-record 实盘数据校准
4. **参数遗留项**：BTC max_vol 阈值未探底（补 10/15）；ETH 需独立阈值刻度（5/10/15 或 bps 相对阈值）；maker 挂单方向评估
5. **人工判断信号系统化**：事前结构化标签记录人工进/不进依据，攒 ~384 笔后验证，有效标签转特征

## 9. 使用

```bash
# 仿真（PaperBroker 真实档深撮合，观察一个窗口）
uv run pm-trade5m --symbol btc --dry-run --now --windows 1
# 真实 $2 交易（参数覆盖走 --param k=v，与回测同一来源）
uv run pm-trade5m --symbol btc --windows 3
# 回测 / 网格
uv run pm-bt5m --spot --param max_vol=20 --param entry_until=100
uv run pm-grid5m --workers 8 --spot
# 数据与结算周边
uv run pm-record --symbols btc,eth   # 24/7 录制
uv run pm-backfill --windows 30      # 批量回填结算
uv run pm-redeem --windows 5         # 赎回核对（--execute 执行，proxy 场景拒绝）
```

需要 `.env`：`PM_PRIVATE_KEY`（小额专用钱包；proxy 场景配 `PM_FUNDER_ADDRESS` + `PM_SIGNATURE_TYPE=3`）、网络受限时 `PM_PROXY_URL`；L2 API key 留空时由私钥自动派生。诊断：`uv run pm-doctor`。
