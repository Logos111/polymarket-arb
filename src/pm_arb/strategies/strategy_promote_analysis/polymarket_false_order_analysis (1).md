# Polymarket Crypto 5m 冷门方策略：真假订单识别与高精度过滤研究方案

## 1. 研究结论

当前策略已经不适合继续以“寻找一个更好的参数点”为主要优化方向。更合理的研究对象是：**基线策略产生的每一个入场机会，哪些是真正值得持有到结算的冷门方订单，哪些是假性冷门订单；同时哪些强规则又错误地过滤掉了真正的冷门反转订单。**

现有仓库的 b09 特征研究已经提供了足够强的证据支撑这一转向：HF BTC 数据集中 21,961 个特征观测行，对应约 4,618 个基线入场行；全体观测最终方向胜率约 31.2%。极薄深度 `depth_ratio_underdog < 0.27` 的胜率只有 13.6%，而该区域的基线入场率达到 34.5%，因此它是当前最明确的“假订单重灾区”。反转强度、动量和相对 OBI 又表现出明显的路径信号，但最终结算胜率仍未稳定跨过 50%。

一个需要修正的量化目标是“只过滤掉一半假单就能达到 50% 胜率”。在基线胜率 31.2%、且假设一个真单都不损失的理想情况下，必须移除约 54.7% 的错误订单才能将剩余样本提高到 50% 胜率。因此真正目标应定义为：**最大化 precision，同时把 recall（保留真订单的比例）控制在可接受范围内。**

这与典型的二元分类阈值优化完全一致：precision 衡量被保留下来的订单中有多少是真单，recall 衡量所有真单中有多少被保留下来；阈值越严，通常 precision 上升而 recall 下降。citehttps://scikit-learn.org/stable/auto_examples/model_selection/plot_precision_recall.html

---

## 2. 现有项目最重要的证据

仓库最新提交 `52b495951bb3c940f25857e02be3031af9cdc1bb` 已将 `reversal_score`、`min_reversal_score` 与回测 engine 真正接通；默认 `min_reversal_score=None` 时保持旧策略，开启门控后 score 不足或 score 不可用会降级为 OBSERVE。fileciteturn6file0

b09 的当前 BTC 数据集为 2026-03-24 至 2026-05-18。特征捕获包含 21,961 行、约 4,618 个当前策略入场机会。需要特别注意，HF 数据集的 `outcome` 是数据集作者根据窗口最后 tick 的 bid 推断的，不是 Gamma/on-chain 的权威结算值，因此当前结果适合作为**特征筛选和机制研究**，还不能作为最终真实胜率证明。fileciteturn6file0

现有结果中最值得保留的事实有四个：

1. `ud_ask/mid_delta_30` 对未来 60 秒价格路径的关系很强，深跌区的未来反弹显著高于基准；其桶序 Spearman 单调关系达到约 -1.00。fileciteturn8file0
2. `depth_ratio_underdog < 0.27` 胜率仅 13.6%，且基线在这里的入场率最高，说明它非常可能对应一类“结构性假订单”。fileciteturn6file0
3. `momentum_decay` 的低值区域和相对买压改善区域存在明显的路径改善，说明并非所有冷门下跌都是坏订单，确实存在潜在的优质反转形态。fileciteturn7file0turn8file0
4. `deep reversal + momentum` 的 `ret60=+0.1606`，但最终胜率只有 29.0%；因此路径型反转和最终结算胜利不是同一个 label。fileciteturn6file0

---

## 3. 研究问题应该重新定义为“Meta-Labeling”

当前原始策略已经是一个 primary signal：

> “当前价格/盘口已经足够便宜，买入冷门方。”

下一层不应该再重写 primary signal，而应该增加一个二级模型：

> **这个已经触发的冷门机会，究竟值不值得真正下单？**

这就是 meta-labeling 的典型形式：先由基础策略产生候选交易，再由第二层分类器筛选是否执行。对于本项目，它特别适合“策略本身方向正确，但假订单很多”的问题。

因此从现在开始应该把样本分为：

- **Candidate / Opportunity**：基础策略在某个时刻满足时间窗、价格区间、基础风险条件，可以考虑进场。
- **True Order / Positive**：候选冷门方最终赢得该 5m 市场结算。
- **False Order / Negative**：候选冷门方最终输掉结算。
- **Missed True Order / False Negative**：某个新增过滤规则将其拒绝，但最终该冷门方实际获胜。

主标签必须以最终真实结算为准；`ret60`、`MFE`、`MAE` 应作为辅助标签和解释变量，而不能与最终胜负混成一个目标。

---

## 4. 第一项必须做的实验：逐规则“误杀审计”

不要直接把几个条件拼起来。第一步要建立一张**过滤路径审计表**：每加一层规则，分别统计保留了多少真单、杀掉了多少假单、误杀了多少真单。

推荐固定如下指标：

| Gate | 总候选 | 保留候选 | 胜率 | True Recall | False-positive reduction | EV | 备注 |
|---|---:|---:|---:|---:|---:|---:|---|
| Baseline | 100% | 100% | 31.2% | 100% | 0% | 基准 | 起点 |
| + 非薄深度 | … | … | … | … | … | … | 骨干过滤 |
| + ud 30s 深跌 | … | … | … | … | … | … | 反转确认 |
| + momentum | … | … | … | … | … | … | 动能确认 |
| + rel OBI | … | … | … | … | … | … | 盘口确认 |
| + trend veto | … | … | … | … | … | … | 防接刀 |

这里最重要的不是最终胜率，而是：

> **每条规则究竟杀掉了多少假单，又杀掉了多少真单。**

例如一条规则如果：

- 杀掉 1000 个假单；
- 同时杀掉 600 个真单；

那么它并不一定是好规则，即使总体胜率暂时上升。

真正优秀的过滤器应该表现为：

> **False Positive 大幅下降，而 True Positive Recall 的损失很小。**

---

## 5. 第二项实验：直接研究“真单 vs 假单”的特征分布

当前项目的分桶分析主要是在研究 `feature → future_return`。下一步要切换到：

> `feature | baseline candidate | final outcome`

即只看基线真正会入场的 4,600 左右候选，而不是全部 21,961 行。

对于每一个 feature，分别统计：

- 真单分布：`P(X | win)`
- 假单分布：`P(X | loss)`
- 真单均值 / 中位数
- 假单均值 / 中位数
- 分位数差异
- KS statistic / Wasserstein distance
- 单特征 ROC-AUC
- 单调性
- 条件胜率 `P(win | X bucket)`

重点不是“p-value 最小”，而是**效应大小 + 稳定性 + 是否有单调结构**。

优先检查这几组变量：

### A. 冷门方自身状态

`ud_ask_delta_5/10/15/30/60`

`ud_mid_delta_10/30/60`

### B. 深度与流动性

`depth_ratio_underdog`

`spread_underdog`

`spread_favorite`

`obi_up/down`

`relative_obi`

### C. 现货趋势状态

`ret_15/30/60`

`slope_15/30/60`

`acceleration`

`momentum_decay`

### D. 极端位置

`dist_high/low`

`new_high/low_count`

`time_since_high/low`

### E. 时间维度

`elapsed`

以及候选发生时距离窗口结束还有多少秒。

仓库现有分析已经明确显示 `depth_ratio`、`ud_delta`、`momentum_decay`、`relative_obi`、`slope` 是最应该优先分析的变量。fileciteturn6file0turn7file0turn8file0

---

## 6. 第三项实验：研究“假订单内部”到底有几种类型

不要把全部亏损单当作一个类。

我建议把 false orders 做聚类或规则分层，寻找不同失败机制。

### 类型 F1：已经接近确定性输盘

典型特征可能是：

- `depth_ratio` 极低
- 冷门价格已经非常低
- 双边盘口表现出明显的一边倒

现有数据显示 `dep<0.27` 的胜率只有 13.6%，这已经非常接近一个明确的 failure regime。fileciteturn6file0

### 类型 F2：逆现货趋势接刀

例如现货在快速上涨，但冷门方仍然不断被买入/价格走弱。

现有 `slope_30` / `ret_15` 分桶已经看到明显的负向结构。fileciteturn7file0

### 类型 F3：冷门真的跌了，但只是“价值塌陷”

这是最危险的一类。

它可能满足：

- `ud_delta_30` 很差
- 但是 depth 很薄
- 盘口相对买压没有恢复
- 热门方继续加强

这类订单看起来“已经跌很多了”，但实际上不是 oversold，而是**市场在有效地重新定价它**。

### 类型 F4：看起来反转，但持续时间不够

这类可能拥有很高 `MFE`，但最后仍输掉结算。

这就是当前 `ret60` 很漂亮、最终 win rate 不漂亮的根源之一。fileciteturn6file0

因此 F4 是最值得和 F1/F2 区分开的。

---

## 7. 一个非常关键的研究：Hard Negative Analysis

机器学习里一个非常有价值的方法是专门研究“最难分类的负样本”。

在这里就是：

> **那些看起来几乎和真订单一样，但最终还是输了的订单。**

例如：

```text
ud_delta_30 很深
momentum_decay 很好
relative_obi 很好
depth_ratio 也正常
```

但最终还是输。

这种订单才真正告诉我们：

> **当前特征体系缺少了什么。**

相比之下，那些 `depth_ratio=0.05` 的亏损单太容易识别，它们已经不是研究重点。

所以建议把 false orders 按当前 score 排序：

```text
score 最高 → 但最终输
```

优先分析这些样本。

这可以直接告诉我们：

> “score 很高却输掉的订单有什么共同特征？”

这往往比继续看总体分桶更容易找到下一代特征。

---

## 8. 第四项实验：研究“被过滤掉的真单”，防止过拟合成极端保守

你提出的第二个问题非常关键：

> **有没有真正的冷门反转订单被我们的过滤器错误杀掉？**

答案应该通过“False Negative Audit”明确回答，而不是凭直觉。

做法是把每一条候选规则当成一个“门”，然后专门统计：

```text
被规则拒绝
AND
最终冷门方获胜
```

形成：

| 过滤原因 | 被拒真单 | 被拒假单 | 误杀率 | 召回损失 |
|---|---:|---:|---:|---:|
| 薄深度 | … | … | … | … |
| score 不足 | … | … | … | … |
| 趋势 veto | … | … | … | … |
| price veto | … | … | … | … |

真正值得“吸收回来”的情况是：

> 某类真单被一个单独条件错误阻挡，但这个条件对假单的过滤能力并不强。

这种时候不应该简单删除规则，而应该改成**条件例外**。

---

## 9. 最推荐的“吸收回来”方法：Hard Veto + Soft Probability

不要把所有条件写成：

```text
A AND B AND C AND D
```

这样很容易把真正的优质订单一起杀掉。

推荐采用三级结构：

```text
               Base Strategy
                     ↓
          ┌──────────┴──────────┐
          ↓                     ↓
     Hard Veto              Candidate
          ↓                     ↓
   明确必死区域           meta probability
                                ↓
                     ┌──────────┴──────────┐
                     ↓                     ↓
                 Execute               Observe
```

### Hard Veto

只保留极少数非常确定的坏区域，例如当前已经高度怀疑的：

```text
作为候选实验：depth_ratio < 0.27
```

### Soft probability

其他因素不要“一票否决”，而是转化成：

```text
P(final_win | current features)
```

最终只需要：

```text
P(win) >= threshold
```

这种设计天然允许“被某一个 feature 判坏，但整体证据足够强”的真订单重新回来。

这也是阈值优化的标准做法：分类器和最终执行阈值应该分开，而阈值由实际业务/交易效用决定，而不是机械固定为 0.5。citeturn663160search4

---

## 10. 建议第二阶段引入一个非常小的 Meta Model

我不建议现在直接上大模型，也不建议深度学习。

先做两个模型：

### Model A：正则化 Logistic Regression

用途：

- 看方向
- 看系数
- 看是否真的有独立信息
- 很容易解释

### Model B：浅层 LightGBM

用途：

- 自动捕捉 `depth × ud_delta`
- `momentum × slope`
- `price × depth`
- `ud_delta × relative_obi`
- 非线性阈值

模型不负责直接下单，只负责：

```text
P(win)
```

然后由策略层负责：

```text
P(win) >= T → ENTER
```

这样以后调整 `T` 不需要重新训练模型。

---

## 11. 一定不要随机切 train/test

这个项目的数据具有非常强的时间和窗口相关性。

一个 5 分钟窗口里，你有大量连续的 1 秒观测，而未来 60 秒 label 又彼此重叠。如果随机把这些 observation 拆到 train/test，训练集和测试集会共享大量近乎相同的信息，会产生明显的乐观偏差。

金融时间序列应考虑 purging 和 embargo：把测试区间相关 label 在训练侧删掉，并对测试之后设置 embargo，从而减少 forward label overlap 和 serial correlation 带来的泄漏。citeturn163586search9turn163586search1

对这个项目，我建议更进一步：

> **拆分单位以“5 分钟窗口”为最小 group，而不是以行。**

即：

```text
window_001 → train
window_002 → train
...
window_120 → validation
```

不能：

```text
window_001 的 60 行 → train
window_001 的另外 60 行 → test
```

否则结果会非常漂亮，但没有实际意义。

---

## 12. 推荐采用“滚动 Walk-Forward + Purge”

当前代码已经支持一次 70/30 window-level split，但下一阶段建议升级。

例如：

```text
Fold 1
Train: W001-W080
Validate: W081-W105

Fold 2
Train: W001-W105
Validate: W106-W130

Fold 3
Train: W001-W130
Validate: W131-W155

Fold 4
Train: W001-W155
Validate: W156-W175
```

在每个 fold 内：

1. 只用 train 估计阈值/模型。
2. validation 完全锁死。
3. 下一 fold 允许重新训练。
4. 最终只汇总 validation 表现。

如果采用逐 observation 的 meta model，则还要做 purge/embargo；不要仅做普通 K-fold。citeturn163586search1turn163586search9

---

## 13. 不要把“50% 胜率”作为唯一优化目标

你的思路总体正确，但最终优化指标应该比“win rate”更严格。

对于持有到结算的二元市场，在不考虑费用时，单份买入价格为 `p`、最终胜率为 `q`，其期望收益核心上取决于：

`EV_per_share = q - p`

因此：

- `q=50%`、`p=0.20`：非常有吸引力
- `q=50%`、`p=0.29`：仍然有正 EV
- `q=40%`、`p=0.20`：依然可能非常赚钱
- `q=55%`、`p=0.30`：虽然胜率更高，但不能脱离价格和费用判断

所以最终模型应该同时报告：

```text
win rate
entry price
EV / trade
EV / eligible window
EV / hour
trade frequency
max drawdown
```

用户当前策略的核心优势恰恰是：**低价买入 + 二元结算的非对称 payoff**，因此不应该为了追求一个漂亮的 50% 胜率，把价格优势和交易频率一起牺牲掉。

---

## 14. 一个更专业的最终优化目标：Precision @ Minimum Recall

结合项目“宁可少做，不要大量做错”的实际偏好，我推荐正式定义：

> **在 recall 不低于某个下限的条件下，最大化 precision / EV。**

例如分别测：

```text
Recall ≥ 80%
Recall ≥ 70%
Recall ≥ 60%
Recall ≥ 50%
```

每个 recall 水平寻找最高 precision 的过滤阈值。

最终你可能得到：

| Recall floor | 实际胜率 | N | EV/trade | EV/window |
|---:|---:|---:|---:|---:|
| 80% | 38% | 高 | … | … |
| 70% | 43% | 中高 | … | … |
| 60% | 49% | 中 | … | … |
| 50% | 55% | 低 | … | … |

然后选择真正适合资金容量的区域。

Precision/Recall 曲线正是用来研究这种“少做但更准”权衡的标准工具。citeturn663160search1

---

## 15. 一个特别值得做的分析：“当前 score 错在哪里”

当前 `reversal_score` 是离散规则：+2/+1/−2/−3 等。它的优点是可解释，但缺点是：

> **不同特征的真实预测能力被粗暴压缩成同一个整数尺度。**

例如：

```text
A: +2
B: +1
C: -2
```

实际上可能：

```text
A 对胜率提升 +2pp
B 对胜率提升 +8pp
C 对胜率下降 -15pp
```

却被编码成：

```text
+2 +1 -2
```

因此下一版 score 最好从：

```text
rule score
```

逐步升级成：

```text
estimated P(win)
```

再由策略选择阈值。

概率模型如果经过良好 calibration，输出概率可以直接解释为条件发生概率；可靠性图和 Brier/log loss 可用于检查这种校准是否可信。citeturn663160search0turn663160search2

---

## 16. 最关键的“真假订单雷达图”应该是什么样

最终建议每个候选订单输出一张结构化 snapshot：

```text
window
elapsed
cand
entry_price

--- token ---
ud_ask_delta_5
ud_ask_delta_15
ud_ask_delta_30
ud_mid_delta_30

--- depth ---
depth_ratio
relative_obi
spread

--- spot ---
ret_15
ret_30
ret_60
slope_15
slope_30
slope_60
momentum_decay

--- extreme ---
dist_high
dist_low
new_extreme_count

time_since_extreme

--- decision ---
base_entry = 1
score = 5
meta_prob = 0.63
final_decision = ENTER

--- outcome ---
settlement = WIN
```

随后直接按：

```text
WIN vs LOSS
```

做二维、三维和模型残差分析。

---

## 17. 对当前项目，我认为最值得测试的规则组合

下面不是“直接上线参数”，而是**下一轮实验的优先级**：

### Regime A：结构性淘汰

```text
depth_ratio < 0.27
```

作为第一候选 hard veto。

原因：当前证据最干净，胜率仅 13.6%，同时覆盖了大量现有入场。fileciteturn6file0

### Regime B：超跌但仍有流动性

```text
ud_delta_30 <= -0.08
AND
(depth_ratio >= 0.27)
```

验证其真实 precision 增量。

### Regime C：深反转 + 动能

```text
ud_delta_30 <= -0.15
AND
momentum_decay <= 0.0001
```

这是当前最强的路径型形态，必须重点验证最终结算胜率。fileciteturn6file0

### Regime D：反转 + 深度 + 相对盘口

```text
ud_delta_30 <= -0.08
AND
relative_obi > 0.53
AND
depth_ratio > 0.45
```

当前研究里这一组合已经达到 37.5% 胜率，但仍没有证明能达到目标，因此应该把它作为一个“骨架条件”而非最终条件。fileciteturn6file0

### Regime E：危险趋势区

验证：

```text
slope_30 极端上升
```

是否应该成为 hard veto，还是只降低 meta probability。

---

## 18. 最重要的工程改造建议

仓库当前已经有 `FeatureCapture` 和 `parquet` 管线，因此下一轮不需要重新造数据系统。fileciteturn9file0

建议增加一个新的研究数据表/Parquet：

```text
candidate_orders.parquet
```

一行 = 一个“基础策略可入场决策时刻”。

字段至少包括：

```text
condition_id
window_start
elapsed
candidate
entry_price

all feature columns

baseline_enter
baseline_reason

final_outcome
win_label

filter_1_pass
filter_2_pass
filter_3_pass
...

score
meta_probability
final_decision
```

这样之后所有实验都可以做到：

```text
filter A
filter A+B
filter A+B+C
model threshold 0.45
model threshold 0.50
model threshold 0.55
```

而不需要每次重新解析原始 tick。

---

## 19. 推荐的实验矩阵

### Experiment 1：错误订单画像

目标：找出 LOSS 的结构。

输出：

- WIN/LOSS 每特征分布
- effect size
- AUC
- bucket lift
- top failure regimes

### Experiment 2：单规则 precision/recall

目标：知道每条规则的真实价值。

输出：

- precision
- recall
- false-positive reduction
- false-negative loss

### Experiment 3：二维/三维交互

重点：

```text
ud_delta × depth
ud_delta × momentum
ud_delta × relative_obi
depth × slope
momentum × slope
price × depth
```

### Experiment 4：Hard Negative

目标：分析：

```text
score ≥ high_threshold
AND
最终输
```

找到隐藏失败机制。

### Experiment 5：False Negative Recovery

目标：分析：

```text
score/filter failed
AND
最终赢
```

寻找 rescue conditions。

### Experiment 6：Meta Model

先 Logistic，再浅层 LightGBM。

### Experiment 7：阈值优化

研究：

```text
P(win) threshold
```

对应的 precision / recall / EV 曲线。

### Experiment 8：真正 OOS

BTC rolling walk-forward + purge/embargo + ETH 外部验证。

---

## 20. 最终策略结构建议

我认为项目最终最可能演化成：

```text
                   5m Market
                       │
                       ▼
              Base Underdog Signal
                       │
                       ▼
                Data Quality Gate
                       │
                       ▼
                 Hard Risk Veto
               （少数必死区域）
                       │
                       ▼
                  Meta Model
                       │
               P(win | features)
                       │
          ┌────────────┴────────────┐
          │                         │
      high probability          ambiguous
          │                         │
        ENTER                    OBSERVE
          │                         │
          └────────────┬────────────┘
                       ▼
                 Hold to Settlement
```

这比目前纯粹的：

```text
score >= N
```

更有成长空间。

---

## 21. 当前阶段的验收标准

我建议不要再使用“验证段某一个参数排名第一”作为策略上线条件。

下一阶段应该满足：

### 数据层

- 每个候选订单有完整 feature snapshot。
- outcome 使用真实 Gamma/on-chain settlement。
- 一次交易一条 trade-level 记录。

### 统计层

- 每个 hard filter 都有 TP/FP/FN/TN 统计。
- 每个规则都有 precision/recall。
- 每个重要特征有 OOS effect size。

### 模型层

- rolling walk-forward 稳定。
- 高概率桶胜率明显单调。
- 概率 calibration 合理。citeturn663160search0turn663160search2

### 策略层

- fee/slippage 后 EV > 0。
- precision 高于目标阈值。
- trade frequency 仍然足够。
- max drawdown 可接受。

---

## 22. 对当前思路的最终判断

**你的大方向是正确的，而且比“继续扫参数”更接近真正的量化研究。** 当前证据已经说明：策略并不是简单地“买冷门就亏”，而是混合了两种截然不同的订单：一类是价格已经被有效重新定价、继续下坠的假反转；另一类确实存在超跌后的反转机会。`depth_ratio`、`ud_delta`、`momentum_decay`、`relative_obi` 与趋势状态已经给出了足够明确的研究入口。fileciteturn6file0turn7file0turn8file0

但目前不能直接把“持有到结算排名领先”解释为策略已经证明正确，更准确的结论是：**在当前测试空间中，退出到 0.99/结算这一类持有逻辑尚未被参数扫描证伪，而新的研究重点已经从“什么时候卖”转向“哪些基础候选根本不该买”。**

下一阶段真正重要的目标不是让 `score` 从 4 调到 5、6，而是建立：

**Candidate → True/False Order → Failure Regime → Filter → Rescue → OOS Precision/Recall → Realized EV**

一旦这条链跑通，才有可能回答你最关心的两个问题：

1. **假订单到底有哪些稳定、可交易的共同特征？**
2. **哪些过滤条件会误杀真正的冷门反转订单，以及如何通过“rescue branch”把它们重新吸收回来？**

这会比继续做普通分桶分析高一个层级。

---

## Sources

1. `Logos111/polymarket-arb`, latest commit `52b495951bb3c940f25857e02be3031af9cdc1bb` — reversal-score gate integrated into backtest and live decision path.
2. `Logos111/polymarket-arb`, `src/pm_arb/strategies/crypto_5m/backtest/runtime/feature_cond_btc.md` — BTC composite conditions, win rates, entry rates, ret60/MFE/MAE.
3. `Logos111/polymarket-arb`, `src/pm_arb/strategies/crypto_5m/backtest/runtime/feature_buckets_btc_batch1.md` — trend/momentum feature buckets.
4. `Logos111/polymarket-arb`, `src/pm_arb/strategies/crypto_5m/backtest/runtime/feature_buckets_btc_batch2.md` — underdog/favorite momentum feature buckets.
5. `Logos111/polymarket-arb`, `docs/b09_reversal_feature_engine_plan.md` — feature-engineering and validation design.
6. scikit-learn, “Precision-Recall” — precision/recall definitions and threshold trade-offs. https://scikit-learn.org/stable/auto_examples/model_selection/plot_precision_recall.html
7. scikit-learn, “Tuning the decision threshold for class prediction” — decision threshold should be chosen for the actual utility objective. https://scikit-learn.org/stable/modules/classification_threshold.html
8. scikit-learn, “Probability Calibration” — calibration curves, Brier/log loss, and probability interpretability. https://scikit-learn.org/stable/modules/calibration.html
9. López de Prado, *Advances in Financial Machine Learning*, Chapter 7 — purging and embargo for overlapping financial labels.
