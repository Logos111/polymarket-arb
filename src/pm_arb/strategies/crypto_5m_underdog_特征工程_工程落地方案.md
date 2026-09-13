# crypto_5m_underdog 策略优化方案 —— 工程落地计划

> 输入文档：`src/pm_arb/strategies/polymarket_crypto_5m_underdog_strategy_optimization.md`（以下简称"优化方案"）
> 对照现状：`polymarket-arb@master`（`crypto_5m_underdog.md` v2.0、`DEV_PLAN.md` v2.2）
> 定位：本方案是 **DEV_PLAN 阶段 5.3（特征工程）的详细工程设计**，交付后建议合并回 DEV_PLAN 作为该阶段的正式子文档。
> 日期：2026-09-13

---

## 0. 结论先行（TL;DR）

1. **方向认同**：优化方案的核心判断——"当前策略只回答了'冷门方够不够便宜'，没有回答'为什么便宜'"——与团队自己在 DEV_PLAN §5.3 已经列出的 F1–F8 特征候选、以及 `crypto_5m_underdog.md` §8 的下一步高度一致。这不是另起炉灶，是把已经定的方向做实、做细。
2. **不能直接照搬落地**：优化方案是一份**研究设想文档**，写作时假设"想要什么特征就能算什么特征"。但把它接到现有代码库时，有 **5 个必须先做工程判断的风险点**（见第 2 节），其中第一个（RTDS 心跳 vs 趋势特征）如果不先处理，后面整套 Trend/Momentum 特征在实盘可能是系统性失真的——这是本方案与优化方案原文相比新增的、最重要的一条工程判断。
3. **落地方式**：新增两个纯函数模块（`data/series_buffer.py`、`strategies/crypto_5m/features.py`）+ 一个仅回测可见的 `labels.py`，复用团队已经验证有效的架构纪律（单一决策来源、实盘/回测同源、默认关闭不影响现网）。**不建议**一次性实现优化方案第 25 节的全部 ~45 个特征，按第 4 节的三批次分阶段。
4. **验证纪律不变**：沿用团队已有标准——验证段（后 30%）胜率须**清晰超过**盈亏平衡线（当前 ~27%），"比基线好"不算通过；候选清单先冻结再看验证集结果。这条线本方案会在工程上做成"硬约束"而不只是口头纪律（见 §7）。
5. **工作量量级**：批次 0（零新增基建，直接可算）约 2–3 天；批次 1（引入 series_buffer 基建 + F1 心跳判定）约 1–2 周；批次 2 + 规则化 ReversalScore + walk-forward 验证 约 2–3 周；ML 阶段暂不建议启动（详见 §6 Phase 5.3.8 的门槛条件）。

---

## 1. 现状盘点：我们已经有什么、缺什么

### 1.1 现有架构一句话回顾

```
实盘：orchestrator.py ──WindowDataHub(context.py)──> decide_entry/decide_exit(decisions.py) ──> Broker
回测：engine.py(replay_ticks) ──HfDataset+SpotVol──────> decide_entry/decide_exit(decisions.py, 同一份代码)
```

关键架构纪律（读代码确认，非猜测）：

- **单一决策来源**：`decisions.py` 是纯函数（无 I/O、无时钟），实盘 orchestrator 与回测 engine 调同一套，`decisions.py` 文件头注释原话是"防两套逻辑漂移"——这是本方案设计新模块时必须延续的第一原则。
- **参数单一来源**：`Crypto5mParams`（pydantic frozen model），CLI `--param k=v` 覆盖，回测/实盘共用。
- **回测已具备的现货基建**（`backtest/spot_vol.py`）：用 Binance 1s K 线重建滚动 TWAP-60s，目前**只输出 `high-low` 一个标量**（`window_rng_seq`），喂给 `max_vol` 过滤。
- **实盘现货喂价**（`data/rtds.py`）：订阅 Polymarket RTDS 中继的 Chainlink TWAP-60s 流，**只维护 `last/high/low/base` 四个标量**，不保留时间序列。
- **盘口数据**：实盘 `LocalOrderBook` 是完整 L2（`top_levels(depth)`）；HF 回测数据集每窗口每秒仅有 **best bid/ask + 该档位份数**（`bu/au/bd/ad/su/sd/sau/sad`），无更深档位。
- **测试基线**：147 项全绿，`decide_entry` 有独立单测锚定当前行为（`test_decisions.py`、`test_hf_engine.py` 显式锚定参数，防止默认值漂移悄悄改变已验证行为）。

### 1.2 优化方案 vs 现状：逐项差距核查

| 优化方案要求的能力 | 现状 | 差距 |
|---|---|---|
| `ret_N` / `slope_N` / `acceleration`（第 3–5 节） | RTDS 只有 high/low 标量；SpotVol 只输出 range 标量 | **缺**：两侧都没有保留可查询的时间序列，无法算收益率/斜率 |
| `dist_high/low` / `new_high/low_count` / `time_since_extreme`（第 6–7 节） | 同上 | 同上，且 high/low 是"自窗口起点单调不减"的全局极值，不是"最近 N 秒"的滚动极值，语义不同，不能直接复用 |
| `ud_ask_delta_N` / `favorite_mid_delta_N`（第 8–9 节） | 实盘每次 poll（2s）拿到最新盘口即丢弃旧值；回测 tick 列表本身是历史，可直接切片 | **实盘缺**滚动缓冲；**回测**天然具备（`ticks[i-N:i]`），但需要写统一的取值函数，不能各写各的 |
| Order Book Imbalance（第 10 节） | 实盘/回测都有 bid_size/ask_size | **基本不缺**，是"批次 0"里工程量最小的一组特征 |
| Spot/Token Divergence、Lead-Lag（第 12–13 节） | 需要现货序列 + token 价格序列对齐 | 依赖上面两项先补齐 |
| Label（future_return/MFE/MAE，第 20 节） | 回测 tick 序列具备计算条件；但**任何形式的"未来窗口"计算绝对不能进入 `decide_entry` 调用路径** | 需要新建**仅回测可用**的模块，并且要有工程护栏防止被误接入实盘 |
| Feature Importance / ML（第 26 节） | `pyproject.toml` 无 pandas/sklearn/xgboost/shap 依赖 | 需要新增**可选依赖组**，且团队自己在 DEV_PLAN 明确"样本量不足前不上 ML"，与优化方案第 17 节"不要一开始就做黑盒"一致 |
| 四阶段入场流程 Detect→Wait→Confirm→Entry（第 24 节） | `decide_entry` 是一次性状态机（WAIT/MISSED/ABORT/OBSERVE/ENTER） | 概念上可以映射（OBSERVE≈Wait，新增 Confirm 逻辑），但要注意不能破坏现有状态机的**幂等性**（同一秒调用应给同一结果，回测按秒重放依赖这一点） |

---

## 2. 五个必须先做的工程判断（本方案在优化方案之上新增的核心见解）

这一节是我认为对项目**最有价值的部分**——不是"怎么把公式抄进代码"，而是"抄进代码之后，实盘会不会因为数据源的现实缺陷而系统性失真"。

### 2.1 【最高优先级】RTDS 心跳 vs 现货重建：趋势特征的地基问题

优化方案第 3–7 节的全部 Trend/Momentum/Extreme 特征，隐含假设"现货价格序列是连续、高频更新的"。但这个假设在**实盘**和**回测**两侧的成立程度完全不同：

- **回测**：`SpotVol` 用 Binance **1s K 线**重建，每秒都有干净的价格点，`slope_15`、`ret_60` 这些量在回测数据里永远算得出来、永远平滑。
- **实盘**：`RtdsTwapFeed` 订阅的是 Chainlink TWAP-60s **推送流**，不是固定频率采样——`crypto_5m_underdog.md` 自己在 §7 风险里承认"Chainlink 在窗口内可能长时间不更新（heartbeat），high-low 偏小不代表真实低波动"，并把这个列为**当前策略最脆弱的假设**，对应 DEV_PLAN 里优先级 P0 的特征 F1（Chainlink 更新频率甄别）。

**后果**：如果直接把优化方案的 `slope_15`、`momentum_decay` 接到实盘 RTDS 数据上，当喂价卡住（heartbeat）时，价格序列会呈现"完全走平"，这会被算法误判为"Exhaustion / 趋势衰竭"甚至"Reversal"——**卡死的喂价和真正走平的行情，在斜率计算上是无法区分的**。这不是优化方案的错，是优化方案没有涉及数据源可靠性这一层，而这恰好是这个项目自己识别出的头号风险。如果不处理，新特征上线后可能不是"提升胜率"，而是**在喂价卡死时制造虚假的反转买入信号**，比现在的静态阈值更危险。

**建议的工程判断（二选一，不建议跳过）**：

- **方案 A（推荐）**：实盘的 Trend/Momentum 类特征**不用 RTDS 作为数据源**，改为额外订阅一路 **Binance 1s K 线 / trade 流**作为"研究口径现货"，方法论与回测的 `SpotVol` 完全对齐（同样是 1s 采样重建滚动 TWAP-60s）。RTDS 继续保留，但只做两件事：① 结算价权威来源（不可替代，因为结算就是认它）；② 现有 `max_vol` 波动过滤的数据源（这条不动，因为已经用真实资金验证过）。这样"训练"和"上线"用的是同一套数据生成逻辑，不存在 train/serve skew。
- **方案 B（成本更低，但有效性打折）**：继续用 RTDS，但每个 Trend/Momentum 特征都必须搭配一个 `sample_count` / `feed_fresh` 伴随字段，在窗口内有效更新点数不足时，该特征强制置为 `None`（不可用），`decide_entry` 侧看到 `None` 一律不触发"确认反转"这一档，只能退回现有的价格/波动过滤。相当于把 F1（心跳甄别）做成第一批必须交付的基础设施，而不是"以后再说"的 P0 待办。

**无论选哪个，F1（Chainlink 更新频率甄别）都必须在 Trend 类特征之前或同批交付**，不能像 DEV_PLAN 现在这样只是列在清单里"待办"——优化方案的整个大厦是建在这块地基上的。

### 2.2 实盘 poll=2s vs 回测 1Hz tick 的粒度错位

`orchestrator.py` 主循环轮询间隔 `poll=2.0s`（`params.py`），而 HF 回测数据集与 `SpotVol` 都是 **1Hz（每秒一条）**。一旦引入"最近 N 秒变化量"这类特征，两秒一采样 vs 逐秒采样在窗口边界上会有系统性偏差（例如 `ud_ask_delta_5` 在 2s 轮询下最多只能拿到 2–3 个采样点，回测却有 5 个）。

**建议**：在入场判定窗口内（`entry_after` 到 `entry_until` 之间，也就是本来就是策略最关心的 60–70 秒），把本地盘口/RTDS 的**读取频率**提到 1s；WS 本地簿本身是事件驱动、免费的，提高读取频率不增加下游调用量，只有 REST 兜底路径（WS 不新鲜时才触发）会增加调用次数，这条路径本来就是低频异常分支，可以接受。窗口其余时间维持 2s 不用改，没必要为不参与判定的时间段增加开销。

### 2.3 OBI 特征口径：live 全深度 vs HF 数据集仅 top-of-book

实盘 `LocalOrderBook.top_levels(depth)` 能拿多档深度，但 HF 回测数据集每个 token 每秒只有 **最优一档**的 bid/ask/size。如果研究阶段用实盘的多档深度算出一个"更强"的 OBI 定义，历史回测根本无法复现验证——这类特征会被迫只能"实盘调、无法批量验证"，违反团队自己的验证纪律。

**建议**：`features.py` 里的 OBI/spread/depth_ratio 一律**只用最优一档**（`best_bid.size` / `best_ask.size`），保证实盘输入和 HF 数据集输入是同一个信息集合。多档深度可以作为独立的、明确标注"仅实盘可用、无法历史回测"的探索性字段单独记录，不进入 `decide_entry` 的判定路径。

### 2.4 HF 数据集字段的一处文档/代码不一致（建议核查）

`hf_loader.py` 的 docstring 提到 tick 数据包含 `du/dd`（Up/Down 美元深度），但实际 `TICK_COLS` 常量里**没有**这两列，只加载了 `su/sd/sau/sad`（份数深度）。优化方案第 11 节的 `depth_ratio` 特征如果想用"美元深度"而不是"份数"，需要先确认这两列在 parquet 源文件里到底存不存在——如果存在但代码没读，是个一行修复；如果数据集本身就没有，第 11 节的深度质量特征只能退化为用份数 × 价格自行估算的"名义美元深度"，需要在特征文档里明确注明口径差异。这是一个很小但容易被忽略的数据核查项，建议放进批次 0 的检查清单。

### 2.5 组合条件的样本量陷阱（在优化方案的方法论之上再加一道纪律）

优化方案第 33/34 节给出的"最值得寻找的 Target Setup"是一个 **9 步复合结构**（强单边 → 极端冷门 → 现货减速 → 现货反向 → 不再创新低 → token 不再下跌 → token 反弹 → OBI 改善 → BUY）。这类多条件同时成立的复合信号，即使在 14,000+ 窗口的历史数据里，**触发次数可能只有几十到一两百次**——团队自己在 R4 轮回测里已经遇到过这个问题（"ETH 三档全不触发"，说明现有的单一 `max_vol` 阈值在 ETH 上触发率就已经偏低）。

**建议**：不要直接验证 9 步复合结构，按优化方案自己第 19 节建议的"二维矩阵"方法，**从 2 个特征的组合开始，每加一层复合条件都先看触发样本量**，样本量低于团队现有标准（`DEV_PLAN` 用过的 p=0.5±0.05 需要约 384 笔的算法）时，该组合的胜率数字只能作为"方向性参考"，不能作为"验证通过"的依据。这条建议应该写进第 7 节的验证纪律里作为硬性检查项，而不是研究人员凭经验判断。

---

## 3. 模块与代码改动方案

### 3.1 新增 `src/pm_arb/data/series_buffer.py`（通用滚动时间序列缓冲）

这是本方案里**唯一的新增基础设施**，目的是让 Trend/Momentum 类特征在实盘和回测之间共用同一套计算逻辑，而不是"实盘一套、回测重新算一套"（后者正是这个项目一直在防范的"两套逻辑漂移"）。

```python
"""通用有界时间序列缓冲（纯数据结构，无 I/O、无时钟）。

承载"现货 TWAP 序列"或"token best_ask/best_bid 序列"，为
features.py 的趋势/动量类特征提供统一输入源。调用方负责注入
timestamp（实盘传 wall clock 或注入 clock；回测传 tick["t"]）。
"""

class SeriesBuffer:
    def __init__(self, maxlen_sec: float = 180.0): ...

    def push(self, t: float, v: Decimal) -> None:
        """追加一个采样点；自动淘汰超出 maxlen_sec 的旧数据。"""

    def value_asof(self, t: float, max_age: float | None = None) -> Decimal | None:
        """不晚于 t 的最近一个值；不存在或超龄（数据太老）返回 None。"""

    def ret(self, t: float, seconds_ago: float, *, max_age: float = 5.0) -> Decimal | None:
        """(v(t) - v(t-seconds_ago)) / v(t-seconds_ago)；任一端缺失/超龄返回 None。"""

    def slope(self, t: float, window_sec: float, *, min_samples: int = 3) -> Decimal | None:
        """窗口内最小二乘 价格~时间 斜率；样本不足返回 None（对应优化方案 slope_N）。"""

    def new_extreme_count(self, t: float, window_sec: float, kind: Literal["high", "low"]) -> int | None:
        """窗口内创新高/新低次数（对应优化方案 new_high/low_count）。"""

    def time_since_extreme(self, t: float, kind: Literal["high", "low"]) -> float | None:
        """距离窗口内极值点已过去多久（对应优化方案 time_since_high/low）。"""

    def sample_count(self, t: float, window_sec: float) -> int:
        """窗口内有效采样点数——**喂给 §2.1 的新鲜度判定，不是可选项**。"""
```

关键设计点：

- **`max_age` 是必填的防御性参数，不是可有可无的细节**——直接对应 §2.1 的心跳风险：任何一个 `ret`/`slope` 调用，如果所需的历史点距今太久（喂价卡住），一律返回 `None`，调用方（`features.py`）看到 `None` 必须视为"不可用"，不能当作 0 处理（0 意味着"确认走平"，`None` 意味着"不知道"，这是两个完全不同的语义，混淆会直接导致 §2.1 描述的虚假信号）。
- 数据源塞入方式：实盘由 `WindowDataHub`（或新增的轻量 `BookHistory`，见 3.6）在每次拿到新数据时 `push`；回测由 `engine.py` 在 `replay_ticks` 循环里逐 tick `push` 后再取值——**同一个类，两处调用**，杜绝逻辑分叉。

### 3.2 新增/扩展 `src/pm_arb/strategies/crypto_5m/features.py`（特征纯函数，与 `decisions.py` 平级）

与 DEV_PLAN §5.3 原定计划完全一致（"新增 features.py，无 I/O、无时钟，实盘/回测共用"），本方案把它具体化为：

```python
@dataclass(frozen=True)
class FeatureSnapshot:
    # --- Trend（批次1）---
    ret_15: Decimal | None
    ret_30: Decimal | None
    ret_60: Decimal | None
    slope_15: Decimal | None
    slope_30: Decimal | None
    slope_60: Decimal | None
    acceleration: Decimal | None          # slope_15 - slope_60
    momentum_decay: Decimal | None        # abs(ret_60) - abs(ret_15)
    # --- Extreme（批次1）---
    dist_high: Decimal | None
    dist_low: Decimal | None
    new_low_count_30: int | None
    new_high_count_30: int | None
    time_since_low: float | None
    time_since_high: float | None
    # --- Token / Favorite momentum（批次2，依赖 BookHistory）---
    ud_ask_delta_15: Decimal | None
    ud_ask_delta_30: Decimal | None
    fav_mid_delta_30: Decimal | None
    # --- Book（批次0，无新增基建即可算）---
    obi_up: Decimal | None
    obi_down: Decimal | None
    relative_obi: Decimal | None
    spread_underdog: Decimal | None
    depth_ratio_underdog: Decimal | None
    # --- Cross（批次2）---
    spot_token_divergence_30: Decimal | None
    # --- 元数据：必须携带，供 §2.1 的可靠性判定使用 ---
    spot_sample_count_60: int             # 现货序列在过去 60s 的有效采样点数
    feed_fresh: bool                      # 是否满足最小新鲜度要求

def compute_features(
    spot: SeriesBuffer,
    ud_book_hist: SeriesBuffer,     # underdog 一侧 ask/mid 历史
    fav_book_hist: SeriesBuffer,    # favorite 一侧 mid 历史
    book_now: dict,                 # 当前双边最优价/量视图（context.py 的 _book_view 格式）
    t: float,
) -> FeatureSnapshot:
    """在时刻 t 计算一份特征快照。纯函数：只读传入的缓冲区，不做 I/O、不读时钟。"""
```

以及一个独立的评分函数（对应优化方案第 28 节，**先规则、不先上 ML**，与优化方案自己第 17 节的建议一致）：

```python
def reversal_score(f: FeatureSnapshot, weights: ScoreWeights) -> Decimal | None:
    """规则打分：feed 不新鲜（f.feed_fresh=False）时返回 None，不给出误导性分数。"""
```

`ScoreWeights` 建议做成独立的小型配置（JSON/TOML 均可，放在 `strategies/crypto_5m/score_weights.json`），而不是硬编码常量——这样研究阶段调整权重不需要改代码、不需要过 CI 里的 ruff/pytest，只是改一份数据文件，同时也方便把"当前生效的权重版本"和历史版本做 diff 留痕。

### 3.3 新增 `src/pm_arb/strategies/crypto_5m/labels.py`（**仅回测可用**，明确的泄漏护栏）

优化方案第 20 节的 Label（`future_return_30/60/120`、`MFE_60`、`MAE_60`）本质上是"未来信息"，**绝对不能出现在 `decide_entry` 的调用路径里**。这个项目对"前视偏差"的重视程度很高（`engine.py` 文件头明确写着"严防前视偏差：每个 tick 只用截至当前秒的盘口做判定"），所以本方案建议不只是"约定不要用"，而是加一道**可执行的护栏**：

```python
"""仅供离线研究使用的 Label 构造（未来信息）。

护栏：本模块不得被 orchestrator.py / context.py / decisions.py 导入，
tests/test_no_lookahead_leak.py 用静态 import 检查强制这一点。
"""
```

配套一个非常轻量的测试（`tests/test_no_lookahead_leak.py`）：静态解析 `orchestrator.py`、`decisions.py`、`context.py` 的 import 语句，断言其中不出现 `labels`。这种测试成本极低（几行代码），但能把"人为纪律"变成"CI 挂了就发现"，值得加。

### 3.4 `backtest/spot_vol.py` 扩展：暴露完整 TWAP 序列，而不只是 range 标量

现有 `SpotVol.window_rng_seq` 只返回累计 `high-low`。建议新增一个方法，把中间算出来的 `twap60(t)` 序列本身暴露出去，供 `SeriesBuffer` 消费：

```python
def window_twap_seq(self, ws: int, tick_ts: list[int]) -> list[Decimal | None]:
    """各 tick 秒的 twap60(t) 本身（与 window_rng_seq 共享同一段计算，
    只是不做 high/low 折叠）。覆盖率不足时对应位置返回 None（同 ABORT_DATA 口径）。"""
```

这样 `window_rng_seq` 可以重写成 `window_twap_seq` 的一个后处理（避免重复计算 TWAP），同时 `engine.py` 在回测里可以把这份序列逐点 `push` 进 `SeriesBuffer`，喂给 `features.py`——**回测侧的特征计算和实盘侧用的是同一个 `SeriesBuffer` + `features.py`，只是数据源不同**，这正是本方案反复强调的"同源"原则在这里的具体体现。

### 3.5 `backtest/engine.py` 扩展：新增"特征采集模式"，不改变默认回测行为

新增一个可选参数 `capture: FeatureCapture | None = None`，默认 `None` 时 `replay_ticks` 行为与现在完全一致（**零回归风险**，147 项现有测试不受影响）。开启后，在入场判定窗口前后一段时间（建议 `[entry_after-30, entry_until+30]`，比策略实际关心的窗口略宽，为特征提供"上下文"、也方便算 Label 需要的未来数据）逐秒计算 `FeatureSnapshot` + 对应的 Label，写入采集器：

```python
def replay_ticks(
    ticks: list[dict], mkt: HfMarket, p: Crypto5mParams, *,
    min_size: int = 5, rng_seq: list[Decimal | None] | None = None,
    twap_seq: list[Decimal | None] | None = None,   # 新增：3.4 的产出
    capture: FeatureCapture | None = None,           # 新增：特征+label 采集钩子
) -> WindowResult:
    ...
```

`FeatureCapture` 是一个简单的累加器，`run_backtest` 结束后统一 `flush` 成 Parquet（复用项目已经在用的 pyarrow），不新增依赖。

### 3.6 `context.py` 扩展：给实盘补一个轻量 `BookHistory`

`WindowDataHub` 目前只提供"当前"盘口视图，不留历史。建议新增一个与之组合使用的小类（不改动 `WindowDataHub` 本身的已测行为）：

```python
class BookHistory:
    """双边 best_ask/best_bid 的滚动缓冲（各一个 SeriesBuffer），
    由 orchestrator 主循环每次拿到新 book 后调用 .update(t, book) 追加。
    与 RtdsTwapFeed 的现货 SeriesBuffer 是同一套基础设施（3.1），
    只是数据源换成 token 价格。"""
```

orchestrator.py 的改动量很小：主循环里 `got = await hub.get_books()` 之后加一行 `book_hist.update(elapsed, b)`，不影响现有判定逻辑。

### 3.7 `params.py` 扩展：新增可选阈值，默认关闭，向后兼容

```python
class Crypto5mParams(BaseModel):
    ...
    min_reversal_score: Decimal | None = None   # None=关闭（默认，与现行完全一致）
    score_weights_path: str | None = None       # None=用内置默认权重
```

`None` 默认值是关键——这保证在没有显式传 `--param min_reversal_score=X` 之前，**现有 147 项测试、现有实盘行为字节级不变**，新功能是纯增量、可随时回滚的开关，不是替换。

### 3.8 `decisions.py` 的改法：不动现有纯函数

不建议直接修改 `decide_entry` 的函数体（它已经被 `test_decisions.py` 逐条锚定，且文案要求与实盘日志"逐字对齐"，改动风险高、收益低）。建议新增一层薄的组合函数：

```python
def decide_entry_v2(
    elapsed: float, cand: str, ask: Decimal | None, ask_size: int | None,
    rng: Decimal | None, features: FeatureSnapshot | None, p: Crypto5mParams,
    *, min_size: int = 5,
) -> EntryDecision:
    """decide_entry 的超集：价格/波动过滤逻辑完全复用 decide_entry；
    仅当 p.min_reversal_score 非 None 时，额外要求
    features is not None and features.feed_fresh and reversal_score(features, weights) >= p.min_reversal_score，
    否则把 ENTER 降级为 OBSERVE 并在 log 里说明"评分不足"。
    p.min_reversal_score is None 时与 decide_entry 完全等价（含返回值/文案）。
    """
    base = decide_entry(elapsed, cand, ask, ask_size, rng, p, min_size=min_size)
    if p.min_reversal_score is None or base.action is not EntryAction.ENTER:
        return base
    ...
```

`orchestrator.py` 和 `engine.py` 都从调用 `decide_entry` 切换成调用 `decide_entry_v2`，但由于默认参数下两者行为完全相同，这个替换本身不需要新的回归测试就能保证安全——**新增测试只需要覆盖 `min_reversal_score` 非 None 的分支**。

### 3.9 CLI 与研究脚本

- `pm-bt5m --capture-features`：复用现有 `pm-bt5m` 入口加一个 flag，跑一遍 HF 数据集，落盘 `runtime/features/{symbol}_features.parquet`（复用 3.5 的采集器）。不新增二进制入口，减少 `pyproject.toml [project.scripts]` 的膨胀。
- `scripts/feature_bucket_analysis.py`：单变量分桶 + 单调性检验（优化方案第 18 节 / DEV_PLAN 已用过的方法论），风格对齐现有 `scripts/settlement_study.py`、`scripts/hf_sweep.py`。
- `scripts/feature_matrix_2d.py`：二维矩阵（优化方案第 19 节），输出 win_rate / N 的交叉表。
- `scripts/feature_importance.py`（**批次 2 之后、样本量达标才启用**）：Feature Importance / SHAP，见 3.10 的依赖隔离。

### 3.10 依赖管理：新增可选 `research` 依赖组

```toml
[dependency-groups]
dev = [...]
research = [
    "pandas>=2.2",
    "scikit-learn>=1.5",
    "lightgbm>=4.5",
    "shap>=0.46",
]
```

不进入 `[project.dependencies]`（生产运行时依赖），保持 `pm-trade5m` 实盘运行环境的最小化——这与团队现有的谨慎风格一致（`duckdb` 已经因为环境兼容性问题被专门标注版本约束）。`uv sync --group research` 按需安装，只在跑分析脚本的机器上装。

---

## 4. 特征落地优先级：三批次 + 与现有 F1–F8 的合并

优化方案第 25 节列了约 45 个特征，不建议一次性实现。按"零基建成本 → 需要 series_buffer → 需要更复杂的联动"分三批，同时和 DEV_PLAN 已有的 F1–F8 候选做合并去重（避免团队内部出现两份特征清单各说各话）。

### 批次 0（零新增基建，1–2 天可出单变量分桶结果）

| 特征 | 对应优化方案 | 对应 DEV_PLAN | 数据来源 |
|---|---|---|---|
| `obi_up` / `obi_down` / `relative_obi` | 第 10 节 | F2 | 现有 bid/ask size，live/HF 都有 |
| `spread_underdog` / `spread_favorite` | 第 11 节 | F5 | 现有 ask-bid，live/HF 都有 |
| `depth_ratio_underdog` | 第 11 节 | F5 | 需先核查 §2.4 的 du/dd 字段问题 |

**这批应该最先做**，因为不依赖任何新基建，可以立刻跑通"计算特征 → 落盘 → 单变量分桶"的完整流水线，用来验证 3.5/3.9 的工程管线本身是否正确，再往上叠更复杂的特征。

### 批次 1（需要 `series_buffer.py` + F1 心跳判定，1–2 周）

对应优化方案自己第 32 节标的 **S 级（第一优先级）**：

| 特征 | 对应优化方案 | 对应 DEV_PLAN | 前置条件 |
|---|---|---|---|
| `slope_15/30/60`、`ret_15/30/60` | 第 3–4 节 | F3 | §2.1 心跳新鲜度判定必须先到位 |
| `acceleration` / `momentum_decay` | 第 5 节 | F4 | 同上 |
| `dist_high/low`、`new_high/low_count` | 第 6–7 节 | （新增，DEV_PLAN 未列） | 同上 |
| Chainlink 更新频率本身（`sample_count`/`feed_fresh`） | 未直接提及 | **F1（P0）** | 无前置，本身就是前置条件 |

**F1 必须和这批一起做，不能延后**——见 §2.1，这是本方案相对优化方案原文最重要的一处修正。

### 批次 2（依赖 `BookHistory`，2–3 周）

| 特征 | 对应优化方案 | 对应 DEV_PLAN |
|---|---|---|
| `ud_ask_delta_N` / `ud_mid_delta_N` | 第 8 节 | （新增） |
| `favorite_mid_delta_N` | 第 9 节 | （新增） |
| `spot_token_divergence_N`、Lead-Lag | 第 12–13 节 | （新增，团队认为"最值得深入研究"） |

F6（跨币种联动）、F7（时段效应）、F8（连续窗口自相关）是 DEV_PLAN 已有但优化方案未覆盖的方向，**不冲突，可以并行**，不在本方案范围内展开（沿用 DEV_PLAN 原有优先级 P2/P3）。

---

## 5. 数据 Schema

### 5.1 `FeatureSnapshot`（落盘时展平成 parquet 列）

见 3.2 的 dataclass 定义；落盘时额外附加：`symbol`、`condition_id`、`window_start`、`elapsed`、`entered`（该 tick 是否满足当时 `decide_entry` 的价格/波动条件，方便区分"全量分桶"和"仅入场窗口内"两种分析视角）。

### 5.2 Label（仅回测落盘，`labels.py` 产出）

| 字段 | 说明 |
|---|---|
| `future_return_30/60/120` | 相对当前 tick 价格的未来收益率（优化方案第 20 节 Label A/B/C） |
| `mfe_60` / `mae_60` | 未来 60 秒最大有利/不利移动（Label D/E） |
| `final_outcome` | 窗口最终结算方向（Label F） |

### 5.3 落盘路径

```
runtime/features/{symbol}_features_{dataset_tag}.parquet
```

`dataset_tag` 建议写死数据集时间范围（如 `hf_20260324_20260518`），因为团队已经在 `crypto_5m_underdog.md` §6 反复强调"历史数据集微观结构可能与当前不同"，特征分析结论必须能追溯到具体数据集版本，避免几个月后混用不同期数据的分析结果却看不出差异来源。`runtime/features/` 加入 `.gitignore`（与现有 `runtime/hf`、`runtime/logs` 处理方式一致）。

---

## 6. 分阶段路线图（建议合并进 DEV_PLAN 阶段 5.3 子项）

| 子阶段 | 目标 | 交付物 | 验收判据 |
|---|---|---|---|
| 5.3.1 | F1 心跳/新鲜度基建 | `series_buffer.py`（含 `sample_count`/新鲜度）+ RTDS 或 Binance-live 数据源判断（§2.1 二选一的工程决策落地） | 有一份实盘 `pm-record` 数据上的 RTDS 更新间隔分布统计，明确回答"心跳 vs 真实更新"的比例 |
| 5.3.2 | 批次 0 特征 + 采集管线 | `features.py`（OBI/spread/depth_ratio）、`engine.py` capture 模式、`pm-bt5m --capture-features` | 能在 HF 数据集上产出 parquet，`scripts/feature_bucket_analysis.py` 跑出单变量分桶表 |
| 5.3.3 | 批次 1 特征（Trend/Momentum/Extreme） | `spot_vol.py` 扩展、`features.py` 补全 S 级特征 | 单变量分桶胜率曲线单调性检验（复用团队 `take_profit` 分桶时用过的方法） |
| 5.3.4 | `labels.py` + 二维矩阵分析 | `labels.py`（含泄漏护栏测试）、`scripts/feature_matrix_2d.py` | Spot Trend × Token Momentum / Spot Trend × OBI 两张矩阵有可读的样本量与胜率分布 |
| 5.3.5 | 批次 2 特征（Token/Favorite momentum、Divergence） | `BookHistory`（context.py） | 与批次 1 结果联合过一遍单调性检验 |
| 5.3.6 | 规则化 `reversal_score` + walk-forward | `decide_entry_v2`、`score_weights.json`、`grid.py` 扩展支持评分阈值轴 | 验证段（后 30%）名义胜率**清晰超过**盈亏平衡线（~27%），且触发样本量 ≥ 384（见 §2.5） |
| 5.3.7 | Paper Trading 对照 | `pm-trade5m --dry-run` 双开：baseline vs 评分门控，同窗口对照 | 至少覆盖团队现有统计口径的样本量后，评分门控组的实际胜率与回测预期方向一致 |
| 5.3.8 | （可选，仅在 5.3.6 规则模型验证有效后启动）Feature Importance / ML | `research` 依赖组、`scripts/feature_importance.py` | **门槛条件**：规则打分模型已在验证段跑出正 EV 信号，且样本量足以支撑训练/验证/测试三段切分；否则不启动，避免在负 EV 基线上做模型选美 |

每个子阶段建议按项目现有习惯，在 `DEV_PLAN.md` 里用"目标/交付物/完成判据/验证证据/遗留"的统一格式登记，保持文档风格一致，方便后续核查。

---

## 7. 验证纪律（在团队既有标准上做工程强化）

团队已有的纪律（沿用，不改）：

- 候选特征清单先冻结，再看验证集结果，防止 14,000+ 窗口上的多重检验过拟合；
- 验证段（后 30%，从未参与筛选）名义胜率须**清晰超过**入场均价对应的盈亏平衡胜率（~27%），"比基线好"不算通过；
- HF `outcome` 抽样核对要从 30 窗口扩到数百窗口；
- 历史数据集结论"方向可迁移、数值需实盘标定"，任何阈值最终要过 `pm-record` 实盘数据校准。

本方案新增的工程化强化项：

1. **冻结清单要留痕**：每次进入新一轮特征验证前，把当时的候选特征清单存成带日期的文件（如 `docs/feature_candidates_frozen_20260920.md`），commit 时间戳早于看验证集结果的时间——不是靠自觉，是靠 git 历史可查。
2. **复合条件必须报告样本量**：任何多特征组合（尤其是优化方案第 33/34 节的复合结构）汇报胜率时，**必须同时报告触发次数 N**，N < 384 的结论标注"样本不足，方向性参考"，不能进入"验证通过"的结论。
3. **心跳新鲜度是一等公民，不是事后补丁**：任何 Trend/Momentum 类特征的验证报告，必须同时报告"因喂价不新鲜被置 None 的比例"——如果这个比例很高（比如超过 20%），说明 §2.1 的风险是真实存在的，需要重新评估方案 A/B 的取舍，而不是硬着头皮上线。
4. **实盘/回测一致性回归**（对应 DEV_PLAN 5.6 已有条目，这里补充特征相关的具体内容）：`pm-record` 积累一定量实盘数据后，用同一套 `features.py` 分别跑实盘录制数据和 HF 历史数据，比较同名特征的分布是否接近（尤其是 §2.1/2.2/2.3 提到的三处口径差异），差异明显则先修正口径,再谈阈值标定。

---

## 8. 测试计划

| 测试文件 | 覆盖内容 | 风格参照 |
|---|---|---|
| `tests/test_series_buffer.py` | push/value_asof/ret/slope/new_extreme_count 的边界情况（空缓冲、单点、超龄、样本不足） | `test_decisions.py` 的纯函数单测风格 |
| `tests/test_features.py` | `compute_features` 在合成数据上的正确性；`feed_fresh=False` 时所有 Trend 字段确为 `None`（直接验证 §2.1 的护栏生效） | `test_hf_engine.py` 的合成 tick 流风格 |
| `tests/test_labels.py` | Label 计算正确性 | 同上 |
| `tests/test_no_lookahead_leak.py` | 静态 import 检查：`orchestrator.py`/`decisions.py`/`context.py` 不得 import `labels` | 新增，成本低、价值高 |
| `tests/test_decisions.py`（追加用例） | `decide_entry_v2` 在 `min_reversal_score=None` 时与 `decide_entry` 输出逐字段相等（含 log 文案） | 保证零回归 |
| `tests/test_hf_engine.py`（追加用例） | `replay_ticks(capture=None)` 与现有调用结果完全一致 | 保证零回归 |

CI（`.github/workflows/ci.yml`）不需要改动，新文件自动纳入现有 `pytest` + `ruff` 流程；`research` 依赖组不装进 CI 环境（研究脚本不需要跑在 CI 里，保持 CI 轻量快速）。

---

## 9. 建议同步更新 `crypto_5m_underdog.md` §7（风险）的条目

| 新增风险 | 触发条件 |
|---|---|
| **趋势类特征在喂价卡死时产生虚假反转信号** | 若采用 §2.1 方案 B（继续用 RTDS），必须监控 `feed_fresh=False` 比例 |
| **回测/实盘的 OBI 与深度特征口径不一致** | 若未来有人在实盘侧用了多档深度而忘记这条约束 |
| **复合条件小样本被误当验证通过** | 上线前检查是否有团队成员绕开了 §7 第 2 条纪律 |

---

## 10. Definition of Done（本方案的验收清单）

- [ ] `series_buffer.py` + 单测合入，覆盖率含边界情况
- [ ] §2.1 的方案 A/B 已经过团队讨论并写入代码/文档（不是含糊带过）
- [ ] 批次 0 特征在 HF 数据集上跑出单变量分桶报告，管线（capture → parquet → 分桶脚本）打通
- [ ] `decide_entry_v2` 的零回归测试通过，`min_reversal_score=None` 时与现网行为逐字段一致
- [ ] `labels.py` 的泄漏护栏测试通过
- [ ] 批次 1 特征的单变量分桶结果 + 心跳新鲜度比例统计已出具报告
- [ ] 至少一版 `reversal_score` 规则模型在验证段跑出**清晰超过 27%** 的名义胜率，且触发样本量 ≥ 384
- [ ] Paper trading 对照结果与回测预期方向一致，方可考虑小额实盘

---

## 11. 额外建议（不局限于本次优化方案本身）

1. **止损结论可能需要在有了反转特征之后重新审视**：`crypto_5m_underdog.md` §6 R3 轮"止损假设证伪"的结论，是在**没有反转确认信号**的前提下得出的（止损买不回被砍的结算赢家）。一旦引入 `reversal_score`，止损的逻辑角色可能变化——例如"评分很高但 ask 继续恶化"这种情形下止损可能重新变得有意义。建议 §6 的止损扫描在批次 1/2 特征上线后**重新做一轮**，而不是把 R3 的结论当作与新特征无关的定论继续沿用。
2. **ETH 零费口径接近打平这条线索值得单独立项**：`crypto_5m_underdog.md` §6 提到 ETH 零费口径 `-$0.035` 接近打平，maker 挂单方向是"转正的硬候选路径之一"。这条路径和本方案（找更好的入场时机）是**互补而非替代**的关系——即使反转特征验证有效，扣掉 taker fee 后可能仍然不够，maker 方向的探索建议与特征工程并行推进，而不是等特征工程有结论了再启动。
3. **不要只用"是否超过 27%"这一个指标做上线决策**：验证段胜率超过盈亏平衡线是**必要条件**，不是充分条件。建议同时看 PnL 的分布方差、最大回撤路径、以及触发频率（每天/每周能有多少笔满足条件的入场机会）——一个胜率刚好卡在 28% 但方差很大、且一个月只触发 5 次的规则，在工程上的价值可能不如"胜率 27.5% 但触发稳定、方差小"的规则。这些指标现有 `report.py` 已经有 PnL 分位数输出，建议在评分门控上线评估时复用，不需要新增太多东西。
4. **建议保留一条并行的 A/B 观察路径**：即使 `reversal_score` 验证通过并小额上线，建议保留一个**不加评分门控的 baseline 影子仓**（同样 `$2` 名义、dry-run 或极小仓位）继续跑，持续对照两者的实际表现——因为历史数据集是 2026-03~05 的静态切片，市场微观结构会漂移，"新规则一直优于旧规则"这件事本身也需要持续验证，而不是验证一次就默认永远成立。

