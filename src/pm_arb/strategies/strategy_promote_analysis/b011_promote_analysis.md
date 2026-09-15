可以。基于前面所有结果，我建议 **下一阶段不要继续“调规则”**，而是把项目正式升级成一个 **High-Precision Selective Trading（高置信度选择性交易）** 实验。

核心目标只有一句话：

> **宁可只交易候选池中最好的 1%～10%，也不交易模型无法高置信判断的订单；先证明“少而精”在严格 OOS 上成立，再逐步扩大覆盖率。**

下面给你一套可以直接落地到当前项目的完整 B11 实施方案。

---

# 一、B11 的总体架构

最终系统变成：

```text
                    ┌────────────────────┐
                    │ Market Data        │
                    │ BTC / ETH / PM     │
                    └─────────┬──────────┘
                              ↓
                    ┌────────────────────┐
                    │ Feature Engine     │
                    │ 当前已有特征        │
                    └─────────┬──────────┘
                              ↓
                    ┌────────────────────┐
                    │ Candidate Generator│
                    │ 原有冷门候选策略     │
                    └─────────┬──────────┘
                              ↓
                    ┌────────────────────┐
                    │ Candidate Dataset  │
                    │ 一行 = 一个候选机会  │
                    └─────────┬──────────┘
                              ↓
                 ┌────────────┴────────────┐
                 ↓                         ↓
        ┌─────────────────┐       ┌─────────────────┐
        │ Rule Baseline   │       │ ML Model        │
        │ 原规则/简单规则  │       │ LightGBM        │
        └────────┬────────┘       └────────┬────────┘
                 └────────────┬────────────┘
                              ↓
                    ┌────────────────────┐
                    │ Confidence Score   │
                    │ P(high-quality)     │
                    └─────────┬──────────┘
                              ↓
             ┌────────────────┼────────────────┐
             ↓                ↓                ↓
        HIGH CONF.        UNCERTAIN        LOW CONF.
             ↓                ↓                ↓
           ENTER             SKIP             SKIP
             ↓
       Hold to Settlement
```

然后第二阶段再加入：

```text
Order Flow
    ↓
ML v2
    ↓
High Confidence
```

**不要一开始把所有东西一起做。**

---

# 二、第一阶段：先把数据集重新定义正确

这是整个 B11 最重要的一步。

现在不要再以：

```text
每秒 BTC observation
```

作为主要训练样本。

应该建立：

> **Candidate Order Dataset**

也就是：

**一行 = 原策略在某个时间点真正会考虑下单的一次机会。**

例如：

```text
candidate_id
timestamp
market
asset
side
entry_price
```

然后记录这个候选产生瞬间的全部信息。

---

## 需要保存的 Feature

第一版先使用当前已有特征：

```text
ud_delta_10
ud_delta_30
ud_delta_60

relative_obi
depth_ratio

momentum
momentum_decay

slope
return

volatility
max_vol

elapsed
price
spread
```

另外强烈建议保存：

```text
market_id
window_id
timestamp
```

因为后面做时间序列切分需要。

---

# 三、第二个关键：重新设计 Label

不要只保存：

```text
win = 0/1
```

建议一次性计算：

```text
future_return_30
future_return_60
future_return_120
future_return_300
```

同时计算：

```text
MFE_30
MFE_60
MFE_120
MFE_300

MAE_30
MAE_60
MAE_120
MAE_300
```

最后：

```text
settlement_win
```

---

# 四、为什么一定要同时记录 MFE / MAE？

因为我们现在已经发现：

> **120 秒慢反弹是真实存在的，但它不一定转化成最终 settlement win。**

所以必须把：

```text
路径
```

和：

```text
最终结果
```

分开。

例如：

```text
Entry = 0.25

30s → 0.24
60s → 0.25
120s → 0.32
Settlement → 0
```

这是：

```text
MFE 很高
settlement = LOSS
```

这种订单对于：

> settlement model

是坏单。

但对于：

> maker / exit model

可能是好单。

如果现在把它直接标成：

```text
LOSS
```

会损失大量信息。

---

# 五、建议建立三个 Label

### Label A：最终胜负

```text
settlement_win
```

这是第一阶段主要目标。

---

### Label B：反转是否发生

例如：

```text
reversal_120 =
future_return_120 > threshold
```

例如根据 entry price / 波动率进行标准化，而不是固定一个绝对价格。

---

### Label C：是否存在可交易的反弹

例如：

```text
MFE_120 >= target
```

以后用于：

> maker / exit 策略。

---

# 六、第三步：严格划分 Train / Validation / Test

这一点必须比以前严格很多。

**绝对不要 random split。**

建议：

```text
Train
↓
Validation
↓
Test
```

按照时间划分。

例如假设目前数据覆盖：

```text
2026-08-01
      ↓
2026-09-15
```

可以：

```text
Train
08-01 ~ 08-31

Validation
09-01 ~ 09-07

Test
09-08 ~ 09-15
```

但是实际比例根据你的数据量调整。

---

# 七、进一步使用 Walk-Forward

最终不能只依赖一次 Test。

应该：

```text
Fold 1

Train ──────────→ Valid
08/01~08/15       08/16~08/20

Fold 2

Train ─────────────────→ Valid
08/01~08/20             08/21~08/25

Fold 3

Train ───────────────────────→ Valid
08/01~08/25                   08/26~08/30
```

最终汇总。

---

# 八、必须加入 Purge / Embargo

你的 forward label 会产生重叠。

例如：

```text
10:00 candidate
future 120s

10:01 candidate
future 120s
```

两个样本的 label 有重叠。

所以训练/验证边界附近必须：

```text
Purge
```

例如：

```text
Validation start
↓
至少留 120s embargo
```

更保守可以留一个完整 market window。

这是金融时间序列 ML 的标准做法。

---

# 九、第四步：先做一个“零 ML 基线”

不要直接 LightGBM。

首先测试：

```text
Baseline
```

也就是当前策略：

```text
所有 Candidate
→ settlement
```

得到：

```text
N
Win Rate
EV/trade
Total PnL
Max DD
```

然后做：

```text
Random 10%
Random 5%
Random 1%
```

这是非常重要的。

因为如果：

```text
Top 5%
Win Rate = 38%
```

但：

```text
Random 5%
Win Rate = 37%
```

那你的模型根本没价值。

---

# 十、第五步：建立第一个 ML 模型

第一版只使用：

```text
LightGBM
```

不要深度学习。

目标：

```text
settlement_win
```

输入：

```text
当前所有已有 feature
```

例如：

```text
ud_delta_10
ud_delta_30
ud_delta_60

depth_ratio
relative_obi

momentum
momentum_decay
slope

volatility
max_vol

elapsed
entry_price
spread
```

---

# 十一、不要疯狂调 LightGBM 参数

第一版直接用相对保守的模型。

例如控制：

```text
max_depth
num_leaves
min_child_samples
learning_rate
feature_fraction
```

核心目标不是：

> Train AUC 最大。

而是：

> **OOS Top-tail precision 最大。**

---

# 十二、真正要看的不是 AUC

这是 B11 的核心。

最终必须生成：

## Coverage Curve

例如：

| Coverage |    N | Win Rate | EV/trade |
| -------: | ---: | -------: | -------: |
|     100% | 4618 |      31% |        - |
|      50% | 2309 |        ? |        ? |
|      20% |  924 |        ? |        ? |
|      10% |  462 |        ? |        ? |
|       5% |  231 |        ? |        ? |
|       2% |   92 |        ? |        ? |
|       1% |   46 |        ? |        ? |

然后画：

```text
Coverage
    ↓
Win Rate
```

以及：

```text
Coverage
    ↓
EV/trade
```

---

# 十三、这里会出现三种结果

### 情况 A —— 最理想

```text
100% → 31%
20%  → 38%
10%  → 45%
5%   → 54%
2%   → 63%
1%   → 70%
```

说明：

> **存在明显的高置信订单。**

马上进入下一阶段。

---

### 情况 B —— 有一点预测能力

```text
100% → 31%
20%  → 34%
10%  → 37%
5%   → 40%
2%   → 42%
```

说明：

> ML 有一些信息，但不足以构建高 EV 策略。

然后引入 Order Flow。

---

### 情况 C —— 完全没有

```text
100% → 31%
20%  → 31%
10%  → 32%
5%   → 31%
1%   → 33%
```

那就直接得出：

> 当前信息集无法识别高质量订单。

不要继续调参。

进入 Order Flow。

---

# 十四、第六步：加入“Reject Option”

这是你的核心需求。

模型不是：

```text
必须交易
```

而是：

```text
预测
 ↓
confidence
 ↓
高 → ENTER
低 → REJECT
```

例如：

```text
P(win) > 0.75
```

才交易。

但这里不要一开始拍脑袋设：

```text
0.75
```

应该让 validation 决定。

测试：

```text
Top 1%
Top 2%
Top 5%
Top 10%
```

然后选择：

> **OOS 最稳定的区域。**

---

# 十五、第七步：建立“明显真单”和“明显假单”三分区

最终不要二分类。

使用：

```text
HIGH
MEDIUM
LOW
```

例如：

```text
P(win)

≥ 0.75
    HIGH → TRADE

0.35 ~ 0.75
    MEDIUM → SKIP

< 0.35
    LOW → SKIP
```

注意：

**这个阈值只是示意。**

实际阈值由 validation 决定。

---

# 十六、第八步：做 False Negative Recovery

这是你提出的另一个非常重要的问题。

主模型：

```text
HIGH
→ ENTER
```

但：

```text
MEDIUM
→ SKIP
```

里面可能有真正的好订单。

所以再研究：

```text
MEDIUM
+
最终 WIN
```

它们有什么特殊特征。

这批就是：

> **False Negative Pool**

---

# 十七、建立 Rescue Model

最终：

```text
Candidate
     ↓
Main Model
     │
     ├── HIGH → ENTER
     │
     └── NOT HIGH
              ↓
         Rescue Model
              ↓
       Strong exception?
          ↓       ↓
        ENTER    SKIP
```

但是：

**Rescue Model 必须比 Main Model 更严格。**

否则整个系统会退化成：

```text
全部都交易
```

---

# 十八、第九步：专门研究 Hard Negative

与此同时，把：

```text
Main Model P(win) 很高
+
最后 LOSS
```

提取出来。

这批是：

> **Hard Negative**

然后做：

```text
Hard Negative
vs
High-confidence Winner
```

特征分析。

这比对全部 LOSS 做统计有价值很多。

---

# 十九、我建议把订单分成四类

最终数据分析直接建立：

```text
A:
High confidence + Win
```

```text
B:
High confidence + Loss
```

```text
C:
Low confidence + Win
```

```text
D:
Low confidence + Loss
```

其中：

### A

你真正想要的订单。

### B

最危险：

> 模型认为它是真单，但其实是假的。

### C

就是你之前说的：

> **被错误过滤掉的真单。**

### D

模型成功过滤掉的垃圾订单。

这个矩阵会非常有价值。

---

# 二十、第十步：Order Flow Recorder

只有在 B11 v1 完成后才进入。

目标：

```text
snapshot
```

升级：

```text
snapshot
+
trade
+
order book event
```

---

# 二十一、重点获取哪些数据？

### 1. Trades

```text
timestamp
price
size
side
```

---

### 2. Book changes

```text
timestamp
price
side
old_size
new_size
delta
```

---

### 3. Best Bid / Ask

```text
bid
ask
spread
```

---

### 4. Depth

```text
bid_depth_1
ask_depth_1

bid_depth_5
ask_depth_5
```

---

# 二十二、然后构造 Order Flow Features

第一批只做：

```text
trade_volume_1s
trade_volume_3s
trade_volume_5s
trade_volume_10s
trade_volume_30s
```

然后：

```text
buy_volume
sell_volume
```

以及：

```text
OFI
```

再做：

```text
depth_delta
```

最后：

```text
cancel/add
replenishment
trade_intensity
```

---

# 二十三、Order Flow ML v2

比较两个模型：

```text
Model A

Current Features
```

vs

```text
Model B

Current Features
+
Order Flow
```

必须使用：

> **完全相同的 OOS 时间段。**

最终看：

```text
AUC
PR-AUC
Top 1% precision
Top 2%
Top 5%
Top 10%
EV
```

---

# 二十四、最重要的不是 AUC，而是 Top Tail Lift

假设：

```text
Baseline
Win Rate = 31%
```

模型：

```text
Top 10% = 40%
Top 5% = 52%
Top 2% = 65%
Top 1% = 72%
```

这就是非常强的结果。

即使：

```text
AUC = 0.63
```

也可能已经非常有价值。

反过来：

```text
AUC = 0.70
```

但是：

```text
Top 5%
只有 38%
```

也未必适合你的策略。

---

# 二十五、回测必须把交易成本完整纳入

最终 PnL 不能只看：

```text
win/loss
```

而要：

```text
Entry
 ↓
MFE
 ↓
Exit
 ↓
Fee
 ↓
Slippage
 ↓
Realized PnL
```

也就是我们之前讨论的：

> **mfe → exit → fee → slippage → realized PnL**

对于当前第一阶段：

```text
Entry
→ Hold to Settlement
→ Fee
→ Realized PnL
```

就够了。

等 maker 策略进入再增加：

```text
maker fill probability
queue position
partial fill
cancel
replace
```

---

# 二十六、回测结果必须输出这些指标

每次实验统一输出：

```text
N candidates

N trades

Coverage

Win Rate

Average Win
Average Loss

EV / Trade

Total PnL

Profit Factor

Max Drawdown

Sharpe

Median PnL

P5 PnL
P95 PnL
```

以及：

```text
BTC
ETH
```

分别统计。

---

# 二十七、一定要统计“样本数量”

虽然你说：

> 不考虑数量。

策略层面我同意。

但是：

> **统计验证层面绝对不能不考虑数量。**

例如：

```text
Top 1%

N = 8
Win = 7
```

胜率：

```text
87.5%
```

这个没有意义。

但：

```text
N = 500
Win = 300
```

就完全不同。

所以我们可以：

> **不优化 coverage，但必须报告 coverage。**

---

# 二十八、最终设置一个最小统计门槛

例如：

```text
OOS N >= 100
```

或者更严格：

```text
每个 rolling fold
N >= 20
```

具体数字根据你的市场频率再定。

如果：

```text
Top 1%
```

只有 10 个样本：

> 不允许宣布成功。

---

# 二十九、最重要的“成功标准”

我建议 B11 事先写死：

### Gate 1

```text
OOS Win Rate > Baseline
```

---

### Gate 2

```text
OOS EV > 0
```

---

### Gate 3

```text
至少 3 个 rolling folds 成立
```

---

### Gate 4

```text
Top-tail precision 单调改善
```

也就是：

```text
Top 1%
>
Top 2%
>
Top 5%
>
Top 10%
```

---

### Gate 5

换时间段仍然成立。

---

# 三十、绝对禁止这几件事

### ❌ 不允许

```text
看 Test
→ 调参数
→ 再 Test
```

---

### ❌ 不允许

```text
随机拆分 tick
```

---

### ❌ 不允许

```text
只报告胜率
```

---

### ❌ 不允许

```text
只看 profitable windows
```

---

### ❌ 不允许

```text
因为 Top 1% 胜率 80%
→ 宣布策略成功
```

必须同时看：

```text
confidence
N
EV
DD
rolling OOS
```

---

# 三十一、代码层面建议这样组织

当前项目建议增加：

```text
research/
```

下面：

```text
research/
├── datasets/
│   ├── build_candidate_dataset.py
│   └── build_labels.py
│
├── analysis/
│   ├── candidate_profile.py
│   ├── hard_negative.py
│   ├── false_negative.py
│   └── feature_analysis.py
│
├── models/
│   ├── train_lgbm.py
│   ├── predict.py
│   └── calibration.py
│
├── validation/
│   ├── walk_forward.py
│   ├── purge_split.py
│   └── coverage_curve.py
│
└── reports/
    └── b11_report.py
```

然后：

```text
data/
```

增加：

```text
candidate_dataset/
order_flow/
```

---

# 三十二、建议数据库/Parquet 结构

Candidate Dataset：

```text
candidate_id
timestamp
market_id
asset
side

entry_price

ud_delta_10
ud_delta_30
ud_delta_60

depth_ratio
relative_obi

momentum
momentum_decay
slope
return

volatility
max_vol

elapsed
spread

future_return_30
future_return_60
future_return_120

mfe_30
mfe_60
mfe_120

mae_30
mae_60
mae_120

settlement_win
```

第二阶段增加：

```text
ofi_1s
ofi_3s
ofi_5s
ofi_10s

buy_volume_1s
sell_volume_1s

trade_intensity

bid_depth_delta
ask_depth_delta

replenishment_rate
cancel_add_ratio
```

---

# 三十三、实际执行顺序

我建议你**严格按照这个顺序实施**：

```text
B11.1
重新构建 Candidate Dataset
        ↓
B11.2
重新计算 Label
        ↓
B11.3
建立严格时间切分
        ↓
B11.4
Baseline 回测
        ↓
B11.5
Rule Extreme Tail
        ↓
B11.6
LightGBM v1
        ↓
B11.7
Coverage Curve
        ↓
B11.8
Hard Negative
        ↓
B11.9
False Negative
        ↓
B11.10
Rescue Model
        ↓
──────────────
如果存在明显高置信区
        ↓
Paper Trading
        ↓
真实数据验证
```

只有如果：

```text
B11 v1
```

证明：

> 当前 feature 不足以产生明显高置信区域

才进入：

```text
B11.11
Order Flow Recorder
        ↓
B11.12
Order Flow Features
        ↓
B11.13
LightGBM v2
```

---

# 三十四、最终生产策略应该长这样

如果实验成功：

```text
                Candidate
                    │
                    ↓
             Quality Gate
                    │
                    ↓
              LightGBM
                    │
             ┌──────┴──────┐
             ↓             ↓
        High Confidence   Other
             │             │
             ↓             ↓
          ENTER           SKIP
             │
             ↓
       Hold Settlement
             │
             ↓
          Realized PnL
```

如果以后 maker 也验证成功：

```text
             High Confidence
                    │
                    ↓
             Reversal Model
                    │
          ┌─────────┴─────────┐
          ↓                   ↓
     Settlement           Maker Exit
```

模型最终甚至可以比较：

```text
EV_settlement
vs
EV_maker
```

动态选择。

---

# 三十五、我对 B11 的预期

我不会把目标定成：

> “LightGBM 最终把胜率做到 50%。”

这个目标太早。

我建议分成三个阶段：

### 第一阶段

回答：

> **现有特征有没有隐藏的高置信订单？**

如果：

```text
Top 5% OOS > 50%
```

这是非常好的结果。

---

### 第二阶段

回答：

> **Order Flow 能不能把 31% 的候选进一步切成明显的赢家和输家？**

如果：

```text
AUC 0.55
→
0.65~0.70
```

就是重大进展。

---

### 第三阶段

回答：

> **这些高置信订单能否转化为真实正 EV？**

也就是最终：

```text
OOS
+
Fee
+
Slippage
+
实际 fill
```

仍然：

```text
EV > 0
```

这才是最终成功标准。

---

## 最后，我认为现在最重要的一点

**不要先改实盘策略。**

先做一个完全独立的：

```text
B11 Research Pipeline
```

让它自动生成一张最终表：

| Strategy         | Coverage |  N | Win Rate | EV/trade | Max DD | OOS folds |
| ---------------- | -------: | -: | -------: | -------: | -----: | --------: |
| Baseline         |     100% |  — |      31% |        — |      — |         — |
| Rule Top 10%     |      10% |  — |        — |        — |      — |         — |
| ML Top 10%       |      10% |  — |        — |        — |      — |         — |
| ML Top 5%        |       5% |  — |        — |        — |      — |         — |
| ML Top 2%        |       2% |  — |        — |        — |      — |         — |
| ML Top 1%        |       1% |  — |        — |        — |      — |         — |
| ML + Flow Top 5% |       5% |  — |        — |        — |      — |         — |

**这张表才是 B11 的核心产物。**

如果它最终出现：

```text
Baseline       31%
ML Top 10%     43%
ML Top 5%      55%
ML Top 2%      65%
```

并且多个 rolling OOS 都重复出现，那么我会认为我们真的找到了突破口。

如果出现：

```text
31%
32%
33%
34%
35%
```

那就不要再继续“调参”，直接转向 **Order Flow**。

而如果加入 Order Flow 后仍然没有形成明显的 Top-tail lift，那么我们就应该非常认真地考虑：**这个冷门反转现象虽然存在，但可能没有足够的可交易可预测性。**

这套路线最大的优点是：**无论最终成功还是失败，B11 都能给出一个非常明确、统计上可信的答案，而不是再得到一个“看起来很漂亮”的回测数字。**
