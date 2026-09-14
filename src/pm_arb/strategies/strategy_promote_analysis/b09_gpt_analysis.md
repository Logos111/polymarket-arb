我看完了仓库 `master` 截至 **2026-09-14 13:37 UTC** 的最新提交 `52b495951bb3c940f25857e02be3031af9cdc1bb`。这次提交已经把 **reversal score → `min_reversal_score` → 回测网格** 这条链路真正接起来了。

我的判断先放在前面：

> **现在项目已经过了“工程可用性”阶段，进入真正的“策略有效性验证”阶段。**
>
> 但目前还不能直接认为新评分策略已经有效。最大的风险不是代码 bug，而是：**研究样本和最终 PnL 目标之间仍存在错位，而且当前评分规则是在同一批 BTC 数据上校准出来的，尚未经过真正独立的 walk-forward / OOS 验证。**

所以接下来我不建议继续大规模“盲调参数”，而应该严格按照：

**数据一致性验证 → baseline → score threshold OOS → 出场模型 → ETH/新增实盘数据验证 → paper → 小资金**

这个顺序走。

---

# 一、目前项目已经走到什么阶段

最新 `DEV_PLAN v2.4` 已经非常清楚：

* 阶段 0：结算规则、录制、成交解析等已经完成
* 阶段 1：决策函数、参数单一来源、Broker 抽象完成
* 阶段 2：SQLite、持仓恢复、结算回填完成主体
* 阶段 3：HF 回测引擎、网格、现货波动过滤都完成
* 阶段 4：风控最小集完成
* 阶段 5：正在做参数扫描 + 特征工程 + 一致性回归
* 当前测试基线已经到 **188 passed + 3 skipped**，ruff 也是干净的。

也就是说：

**工程底座现在已经基本够用了。**

真正没完成的是：

1. 评分规则有没有 OOS alpha
2. 这个 alpha 能不能转化成真实 PnL
3. 回测和实盘是否严格一致
4. ETH 是否也成立
5. 新数据是否还能成立

---

# 二、最新版本最重要的进展：评分门控已经真正接入回测

最新 commit 做的核心事情是：

```text
feature snapshot
      ↓
reversal_score
      ↓
min_reversal_score
      ↓
decide_entry_v2
      ↓
ENTER / OBSERVE
```

而且回测和实盘共用了：

* `WindowFeatureBufs`
* `compute_features`
* `reversal_score`
* `decide_entry_v2`

因此至少从架构上，已经不是“研究脚本和实盘两套策略”。

最新 engine 的逻辑也是：

> 门控关闭 → 原策略路径
> 门控打开 → 计算特征 → 算分 → score 不够就 OBSERVE

并且 `score=None` 时 fail-closed，不会因为特征异常误放行。

这是正确的。

---

# 三、但现在有一个非常重要的问题：评分规则本身还是“研究阶段”，不是策略阶段

最新评分规则 v2：

* `ud_ask_delta_30 <= -0.15` → +2
* `ud_ask_delta_30 <= -0.08` → +1
* `relative_obi >= 0.5` → +1
* `momentum_decay <= 0.0001` → +1
* `depth_ratio < 0.27` → -2
* 还有趋势猛烈、热门方增强等负分项。

从研究结果看，这个方向**非常有意思**。

尤其：

### 1. 冷门方自身价格下跌，是目前最强信号

`ud_mid/ask_delta` 几乎全部呈现负单调关系：

> 冷门方过去 30~60 秒跌得越多，未来 60 秒越容易反弹。

而且报告给出的：

* `ud_mid_delta_30`：Spearman ρ ≈ -1
* 深反转桶 future return ≈ +0.12
* `ud30 < -0.15 + momentum_decay < 0.0001`

  * `N=1562`
  * `ret60 = +0.1606`

这已经不是简单的随机噪声形态了。

### 2. 极薄深度是明显坏信号

`depth_ratio < 0.27`

对应：

* 胜率仅 13.6%
* N = 4548
* 而现行策略却在这个区域有 **34.5% 入场率**

这个我认为非常重要。

它甚至有可能比“找正向信号”更重要：

> **你现在策略亏损的一个主要原因，可能是大量买到了“已经基本确定输了”的极薄冷门盘口。**

报告也已经明确指出这一点。

---

# 四、但这里有一个核心矛盾：ret60 很漂亮，不代表最终交易赚钱

这是我目前对这个项目最关注的问题。

你现在发现：

```text
深反转 + 动能
ret60 +0.1606
```

甚至：

```text
现货下行侧 +0.2980
```

看起来非常漂亮。

但同一条件下：

```text
最终胜率只有 29.0%
```

甚至：

```text
现货下行侧胜率 28.5%
```

也就是说：

**中价路径很好看，但最终结算胜率仍然没有突破盈亏平衡。**

这个问题项目文档自己已经意识到：

> `divergence` 等特征存在“赢路径、不赢结算”的现象。

这说明你现在实际上同时存在两个不同的 alpha：

### Alpha A：短时间价格均值回归

```text
future_return_30/60
mfe_60
```

### Alpha B：最终二元事件预测

```text
final_outcome
win_rate
```

这两个不一定一致。

---

# 五、因此，下一步最重要的不是继续调 score，而是先解决“出场模型”

这是整个项目当前最核心的战略转折点。

现在的策略：

```text
入场
 ↓
如果 bid >= 0.99
    → 卖
否则
    → 一直持有到结算
```

所以其实你的研究目标一直被：

> “最终这个冷门方能不能赢”

绑住。

但是最新数据告诉你：

> **有些形态非常适合短线反弹，却不适合拿到 5 分钟结算。**

例如：

```text
deep reversal
        ↓
未来60秒中价明显反弹
        ↓
但是最终没有翻盘
```

这完全解释得通。

---

# 六、现在最值得做的，不是继续扩大 feature 数量，而是做“出场实验”

我建议你接下来马上进入这个实验：

## 实验 A：保持原入场逻辑不变，只改变 exit

至少测试：

```text
TP = 0.35
TP = 0.40
TP = 0.45
TP = 0.50
TP = 0.55
TP = 0.60
TP = 0.70
TP = 0.80
0.99 / settlement
```

但不要简单扫一个 `take_profit_price`。

因为你的入场价格本身是：

```text
0.20 ~ 0.30
```

所以：

```text
entry = 0.22
TP = 0.50
```

和：

```text
entry = 0.29
TP = 0.50
```

经济意义完全不同。

因此更合理的是测：

### 相对收益率

例如：

```text
TP multiple = 1.5x
TP multiple = 1.75x
TP multiple = 2.0x
TP multiple = 2.25x
TP multiple = 2.5x
TP multiple = 3.0x
```

项目文档其实已经把这个方向列进下一步了：**mfe 口径出场实验（2x 止盈）**。

我认为这个实验的优先级甚至应该高于继续增加 feature。

---

# 七、第二个非常重要的问题：当前数据集仍然不够“真实”

这里要特别小心。

HF 数据集目前是：

> **2026-03-24 → 2026-05-18**

而且：

> `outcome` 是数据集作者根据最后 tick 的 bid 推断出来的，并不是链上/Gamma 最终结算结果。

这意味着现在你得到的：

```text
31.2% 胜率
29.0% 胜率
37.5% 胜率
```

都不能直接理解成：

> “真实 Polymarket 结算胜率就是这个。”

尤其你这个策略最终是二元结算，所以：

**Gamma/on-chain 真实 settlement 才是最终 ground truth。**

---

# 八、我反而认为项目当前最大短板是“真实 tick + 真实 settlement”的回测集还没真正形成

这点从 `DEV_PLAN` 也很明显：

阶段 3.4：

> 实盘日志回放对账仍然未完成

阶段 2.6：

> trade5m 当前还没有直接接 recorder。

这个事情其实非常重要。

你现在已经有：

```text
pm-record
    ↓
JSONL
    ↓
Parquet
```

但是交易行为本身还没有形成：

```text
真实决策时刻
+
真实盘口
+
真实 Chainlink/TWAP
+
真实策略输出
+
真实下单价格
+
真实成交
+
最终 settlement
```

的完整逐事件数据集。

---

# 九、所以我会把接下来的工作分成 4 层

## 第一层：先建立“黄金基线”

永远固定一个：

```text
BASELINE
```

当前 baseline 应该是：

```text
target_notional = 2
entry_after = 70
entry_until = 135
min_entry = 0.15
max_entry = 0.30
max_vol = 30
take_profit = 0.99
stop_loss = 0
min_reversal_score = None
```

而目前 grid 的固定配置已经换成：

```text
max_vol = 20
entry_until = 100
min_entry = 0.20
max_entry = 0.30
TP = 0.99
```

评分网格再扫：

```text
score ∈ {None,1,2,3,4,5,6,7}
```

这个有一个问题：

> **你在测试 score 的时候，同时把 max_vol 和 entry_until 从 baseline 改成了上一轮“最优值”。**

这会使 score 的增量效果没那么干净。

更好的实验应该是：

### Experiment 1

```text
所有参数固定为 production baseline
只变化 score
```

这样得到：

```text
score 对 PnL 的纯增量
```

然后：

### Experiment 2

```text
确定 score 最优区域以后
再联合优化 entry_until / max_vol
```

不要把多个优化混在一起。

---

# 十、score threshold 怎么扫，我建议不要只看 `pnl_per`

当前 grid 的排名逻辑是：

```python
btc_va_per + eth_va_per
```

也就是验证段每笔平均 PnL。

这个指标太单薄。

至少应该同时输出：

```text
N
win rate
pnl
pnl / trade
pnl / entered window
max drawdown
profit factor
std(pnl)
Sharpe-like
95% CI
```

尤其：

## `pnl / entered window`

和：

## `pnl / all eligible windows`

一定要分开。

因为 score 越高，最容易出现：

```text
交易笔数暴跌
平均每笔很好
总收益却很低
```

例如：

```text
score 1
N = 5000
pnl/trade = +0.01

score 6
N = 150
pnl/trade = +0.08
```

不能直接说 score=6 更好。

你真正应该优化的是：

> **单位时间的期望收益，而不是单笔收益。**

---

# 十一、score 最值得看的不是“哪个分数最高”，而是整个 score-response curve

例如最终要画：

```text
score     N       win%      pnl/trade      pnl/hour
---------------------------------------------------
0         5000    28%       -0.03         -0.30
1         2200    29%       -0.01         -0.10
2         1200    31%       +0.005        +0.03
3          700    34%       +0.02         +0.07
4          400    38%       +0.03         +0.08
5          180    41%       +0.05         +0.04
6           70    45%       +0.08         +0.02
7           15    60%       +0.10         +0.00
```

真正值得上线的可能不是：

```text
score >= 7
```

而是：

```text
score >= 4
```

因为交易频率仍然够。

---

# 十二、还有一个我非常建议马上检查的地方：score 与特征之间可能存在强相关/重复计分

你现在 score：

```text
ud_delta
momentum_decay
slope
fav_delta
relative_obi
depth
extreme
```

看起来是多因子。

但实际上：

```text
ud_delta
fav_delta
spot_token_divergence
```

之间可能高度相关。

报告自己已经发现：

> `fav` 基本是 `ud` 的镜像，并不提供真正独立的信息。

因此当前 scoring：

```text
ud -2
fav + ...
divergence + ...
```

很可能存在：

**同一个 underlying move 被重复计分。**

这会使 score 看起来很有预测力，但实际上只是把一个因子“加了三遍”。

---

# 十三、这意味着现在不应该急着上 ML，但应该做 feature correlation

目前项目“不直接上 ML”的方向我赞成。

先做：

```text
corr matrix
Spearman
mutual information
conditional lift
```

重点分析：

```text
ud_delta
fav_delta
relative_obi
momentum_decay
slope
divergence
depth_ratio
```

尤其：

```text
P(win | A)
P(win | B)
P(win | A,B)
```

以及：

```text
lift(A+B)
vs
lift(A)+lift(B)
```

这比现在简单的：

```text
+1
+1
+2
```

更重要。

---

# 十四、目前我认为真正有希望的策略形态，其实已经隐约出现了

从已有结果，我会重点关注这种形态：

```text
入场价 0.20~0.30
+
冷门方 30s 明显超跌
+
动能尚未完全衰竭
+
盘口相对买压改善
+
不是极薄深度
+
不是现货单边猛烈上涨
```

也就是：

> **“被快速砸下去，但盘口还没有失去流动性，同时现货趋势没有继续猛烈朝坏方向走”的冷门反弹。**

这个形态比最初的：

> “低价买冷门等待结算翻盘”

要精确得多。

---

# 十五、所以我建议你接下来不要把它定义成“套利策略”

至少从现在的数据来看，它更像：

> **5 分钟 crypto prediction market 的短周期 mean-reversion strategy**

而不是严格意义上的无风险 arb。

因为现在：

```text
结构性负 EV
↓
feature filter
↓
寻找局部正 EV
```

实际上是在做：

**microstructure + short-term mean reversion**

这会影响你后面的所有优化方向。

---

# 十六、一个更合理的回测体系

我建议直接把回测拆成 4 层。

### Layer 1：Signal backtest

只回答：

> 哪些形态未来 30/60 秒会上涨？

目标：

```text
future_return
mfe
mae
```

这就是你现在 feature research 正在做的东西。

---

### Layer 2：Execution backtest

把真实交易成本加入：

```text
entry ask
available depth
fill size
slippage
taker fee
exit bid
```

而不是只看中价 `ret60`。

---

### Layer 3：Strategy backtest

完整模拟：

```text
signal
→ entry
→ position
→ exit
→ fee
→ PnL
```

---

### Layer 4：Reality replay

用真实：

```text
pm-record
+
真实 Chainlink
+
真实 CLOB
+
真实 settlement
```

重新执行一次：

```text
strategy decision
```

然后比较：

```text
replay decision
vs
当时 live decision
```

这就是现在 3.4 最缺的东西。

---

# 十七、还有一个我建议你马上解决的小问题：真实 recorder 要直接进入 trade5m

现在：

> `trade5m.py` 尚未直接接 recorder。

这会导致以后你遇到：

```text
为什么线上这一单进了？
```

只能：

```text
trade5m log
+
pm-record
```

再靠时间人工拼。

这是非常痛苦的。

理想状态应该让每个 ENTRY 同时记录：

```json
{
  "window": "...",
  "t": 83,
  "cand": "Down",
  "ask": 0.247,
  "bid": 0.239,

  "spot": 77276.4,
  "spot_ret_30": -0.00042,

  "ud_delta_30": -0.112,
  "relative_obi": 0.71,
  "depth_ratio": 0.58,
  "momentum_decay": 0.00003,

  "score": 4,

  "entry_price": 0.247,
  "size": 9
}
```

这样以后每天都可以直接做：

```text
live vs backtest
live signal distribution
score bucket
realized PnL
```

---

# 十八、你现在最应该做的回测顺序

我给你一个明确的执行顺序：

## 第 1 步：锁死 baseline

不要修改：

```text
baseline_v1
```

输出：

```text
all eligible
entered
win%
PnL
PnL/trade
PnL/window
maxDD
```

---

## 第 2 步：只扫 score

固定其它参数。

扫：

```text
None
1
2
3
4
5
6
7
```

但一定做：

```text
train
validation
```

而且记录：

```text
N
win%
EV
PnL
maxDD
```

---

## 第 3 步：不要选单点，选“平台”

比如：

```text
score >= 3
score >= 4
score >= 5
```

如果：

```text
3 / 4 / 5
```

都表现不错，而不是只有 `4` 突然爆炸，说明规则更稳健。

---

# 十九、然后做真正的 walk-forward，而不是目前简单的一次 70/30

当前 grid 的确已经是：

```text
前70% train
后30% validation
```

而且明确只按 validation 排名。

这个作为第一轮是对的。

但下一阶段应该升级成：

```text
Fold1:
Train Mar → Apr
Val Apr → Apr

Fold2:
Train Mar → Apr
Val Apr → May

Fold3:
Train Mar → May
Val May → ...
```

也就是：

**rolling walk-forward**

最终看：

```text
score=3
score=4
score=5
```

在哪些时期都有效。

真正值得上线的不是：

> “某个验证集很好”

而是：

> **多个时间段方向一致。**

---

# 二十、ETH 现在不是“顺手跑一下”，而是非常重要的 sanity check

当前 BTC 的结果已经出现很多非常漂亮的：

```text
ρ=-1.00
ρ=-0.90
ret60 +0.16
```

这种结果。

这当然令人兴奋。

但也要警惕：

**BTC-specific microstructure artifact。**

所以 ETH 必须作为：

```text
out-of-domain validation
```

而不是普通 test。

如果：

```text
BTC +0.16
ETH +0.11
```

很好。

如果：

```text
BTC +0.16
ETH -0.02
```

那就说明：

> score 可能在学习 BTC 的结构，而不是 crypto 5m 的通用反转结构。

---

# 二十一、然后才是实盘 paper trading

我建议：

```text
real trading
     ↓
不要马上真下单
     ↓
paper broker
     ↓
完全实时运行
```

但 paper 必须使用**真实盘口和真实 Chainlink**。

也就是说：

> 代码是真实 production path，只是 `Broker` 不真正发单。

这样才能验证：

```text
signal latency
feed freshness
score availability
actual ask
actual depth
actual entry frequency
```

---

# 二十二、最终上线条件，我建议直接这样定义

我不会因为：

```text
win rate > 27%
```

就上线。

应该同时满足：

### 条件 1

至少：

```text
N >= 384
```

这个与你现在项目里的样本纪律一致。

### 条件 2

多个 walk-forward fold：

```text
EV > 0
```

### 条件 3

ETH 或新增真实 BTC 数据也同向

### 条件 4

score-response 曲线平滑，不是单点峰值

### 条件 5

实际 fee + slippage 后仍然 > 0

### 条件 6

paper trading 与 backtest 的：

```text
entry rate
score distribution
fill rate
```

没有明显漂移

满足这些后才：

```text
$2
→
$5
→
$10
```

逐级放量。

---

# 二十三、我对当前项目的总体评分

如果站在“一个真正准备进实盘的量化项目”角度：

| 项目                  | 当前状态       |
| ------------------- | ---------- |
| 工程架构                | **8.5/10** |
| 风控                  | **8/10**   |
| 回测框架                | **8/10**   |
| 数据工程                | **7.5/10** |
| 特征工程                | **8/10**   |
| 防 look-ahead        | **8.5/10** |
| 真实 execution replay | **5/10**   |
| OOS 策略验证            | **4.5/10** |
| 当前策略 alpha 可信度      | **5~6/10** |
| 直接放量实盘              | **不建议**    |

核心不是项目写得不好。

恰恰相反：

> **现在代码工程已经明显领先于策略证据。**

你现在最应该把精力从：

```text
继续写功能
继续加 feature
继续扫参数
```

转移到：

```text
OOS
真实交易成本
真实 replay
真实 settlement
exit optimization
```

---

# 二十四、我认为当前最有价值的下一轮实验

按照优先级，我会这样排：

**P0**

```text
Score threshold walk-forward
+ baseline 对照
+ 完整 PnL metrics
```

**P0**

```text
2x / 2.5x / 3x TP
+ time-based exit
+ settlement exit
```

**P0**

```text
BTC OOS
+
ETH OOS
```

**P1**

```text
trade5m 接 recorder
```

**P1**

```text
真实 live log replay
```

**P1**

```text
feature correlation / redundancy
```

**P2**

```text
SHAP / LightGBM
```

现在**不要急着上 LightGBM**。因为如果底层 label / exit / execution model 还没统一，ML 只会把数据集里的偏差拟合得更漂亮。

---

## 最后一个关键判断

我认为这套项目现在已经找到了一个值得继续深挖的东西：

> **“冷门方短期超跌 + 动能形态 + 盘口结构改善”可能存在真实的短周期均值回归 edge。**

但目前还没有证明：

> **它可以稳定地转化成 Polymarket 最终可实现的正 PnL。**

而这个问题，**靠继续调整 `min_reversal_score = 3/4/5/6` 本身解决不了**。

真正的突破点应该是：

**把 `mfe → exit → fee → slippage → realized PnL` 这条链闭起来。**

这会直接决定你这套策略究竟是“统计上很好看的特征”，还是“可以赚钱的交易系统”。目前仓库的研究报告也已经明确把这一步列为评分门控之后的核心任务。

如果按现在这个代码状态继续推进，我最建议下一轮就做 **“score × exit multiple 的二维 walk-forward 实验”**，而不是继续扩 feature；这一步很可能是整个项目从“研究项目”进入“有实际 alpha 的策略”的分水岭。
