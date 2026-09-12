# Polymarket Crypto 5m Underdog 策略优化方案

> 目标：在不丢失现有策略颗粒度的前提下，把当前"冷门方 +
> 低波动"的静态入场逻辑，升级为"市场状态识别 + 趋势衰竭 + 反转确认 +
> 冷门价格"的动态策略。

------------------------------------------------------------------------

## 1. 当前策略与核心问题

当前策略可以抽象为：

``` text
t ∈ [70s, 135s]
AND max_vol 条件通过
AND 0.15 < underdog_ask < 0.30
→ 买入冷门方
```

当前逻辑主要回答两个问题：

1.  冷门方够不够便宜？
2.  最近市场够不够平静？

但没有回答最关键的问题：

> **这个冷门方为什么这么便宜？**

同样的 `underdog ask = 0.20`，可能对应完全不同的市场状态。

### 状态 A：真正单边行情

例如 BTC：

``` text
77300
↓
77270
↓
77240
↓
77190
↓
77150
```

盘口：

``` text
Up   0.25 → 0.20 → 0.15 → 0.08
Down 0.75 → 0.80 → 0.85 → 0.92
```

此时 `Up=0.20` 是因为市场持续向 Down 定价。

这属于：

> **Continuation / 单边延续**

买 Up 相当于接飞刀。

### 状态 B：冲击后的反转

例如：

``` text
BTC：

77300
↓
77270
↓
77220
↓
77210
↑
77230
↑
77260
```

盘口：

``` text
Up：
0.70 → 0.55 → 0.28 → 0.22
                    ↓
0.22 → 0.30 → 0.38 → 0.45
```

这里的 `0.22` 是短期冲击压出来的低价，随后开始反转。

这属于：

> **Reversal / 冷门反转**

真正值得买的是第二种，而不是单纯寻找低价冷门。

------------------------------------------------------------------------

# 2. 优化的总体方向

不要再把策略简单定义成：

``` text
单边 / 反转
```

而应该定义成三个市场状态：

``` text
1. Continuation
   单边延续

2. Exhaustion
   趋势衰竭

3. Reversal
   反转
```

最终交易流程：

``` text
                 当前 5m 市场
                      │
             ┌────────┼────────┐
             ▼        ▼        ▼
       Continuation Exhaustion Reversal
             │        │        │
            放弃     继续观察    候选
                                │
                      冷门价格满足条件
                                │
                                ▼
                              BUY
```

核心变化：

> 从"寻找便宜的冷门方"，变成"寻找被单边行情压低、但市场状态已经发生切换的冷门方"。

------------------------------------------------------------------------

# 3. 第一优先级：现货价格趋势特征

当前策略使用 `max_vol`，本质上主要看：

``` text
high - low
```

它只能说明市场波动了多少，却不能告诉我们：

> 波动是单边的，还是来回震荡的？

例如：

``` text
100 → 80 → 100
```

与：

``` text
100 → 110 → 120
```

可能有类似的 Range，但对冷门反转策略而言意义完全不同。

因此必须增加方向性特征。

------------------------------------------------------------------------

## 3.1 Return

计算：

``` text
ret_10
ret_15
ret_30
ret_60
ret_90
ret_120
```

定义：

``` text
ret_N = (TWAP_t - TWAP_{t-N}) / TWAP_{t-N}
```

例如：

``` text
ret_60 = -0.20%
ret_30 = -0.08%
ret_15 = +0.03%
```

这种结构已经暗示：

> 大周期仍然向下，但短周期开始向上。

------------------------------------------------------------------------

# 4. 第二优先级：Trend Slope

单纯 Return 还不够，需要衡量价格趋势的斜率。

计算：

``` text
slope_15
slope_30
slope_60
```

建议对不同时间窗口执行线性回归：

``` text
TWAP ~ time
```

得到价格斜率。

------------------------------------------------------------------------

## 4.1 Continuation 特征

例如：

``` text
slope_60 < 0
slope_30 < 0
slope_15 < 0
```

表示：

> 各时间尺度趋势方向一致，下跌趋势仍在延续。

对应：

``` text
Continuation
```

这种情况下不要因为冷门方很便宜就买入。

------------------------------------------------------------------------

## 4.2 Reversal 特征

例如：

``` text
slope_60 < 0
slope_30 < 0
slope_15 > 0
```

表示：

> 大趋势仍然向下，但短周期已经开始反向。

这是非常重要的反转候选结构。

------------------------------------------------------------------------

# 5. 第三优先级：Momentum Decay / Acceleration

真正想识别的不是简单的"涨还是跌"，而是：

> **原来的趋势正在增强还是减弱？**

可以定义：

``` text
acceleration = slope_15 - slope_60
```

或者：

``` text
momentum_decay = abs(ret_60) - abs(ret_15)
```

------------------------------------------------------------------------

## 5.1 单边

例如：

``` text
ret_60 = -0.20%
ret_30 = -0.13%
ret_15 = -0.08%
```

下跌速度仍然明显。

属于：

``` text
Continuation
```

------------------------------------------------------------------------

## 5.2 趋势衰竭

例如：

``` text
ret_60 = -0.20%
ret_30 = -0.06%
ret_15 = -0.01%
```

趋势还没有正式反转，但明显失速。

属于：

``` text
Exhaustion
```

这是重点观察区。

------------------------------------------------------------------------

## 5.3 真正反转

例如：

``` text
ret_60 = -0.20%
ret_30 = -0.05%
ret_15 = +0.03%
```

说明：

``` text
强下跌
↓
下跌速度减弱
↓
短周期反向
```

属于：

``` text
Reversal
```

------------------------------------------------------------------------

# 6. 第四优先级：距离窗口极值

目前只使用：

``` text
range = high - low
```

建议增加：

``` text
dist_high
dist_low
```

例如：

``` text
dist_low =
(TWAP - window_low) / window_low
```

用于判断：

> 当前价格是不是已经接近近期极端位置。

------------------------------------------------------------------------

## 6.1 危险状态

``` text
接近 window_low
+
持续创新低
+
短周期趋势仍向下
```

说明：

> 价格虽然已经很低，但单边行情仍然存在。

不要买。

------------------------------------------------------------------------

## 6.2 潜在反转

``` text
接近 window_low
+
很久没有创新低
+
开始离开 window_low
+
短周期 slope 转正
```

这属于：

> Extreme + Stabilization + Reversal

非常值得研究。

------------------------------------------------------------------------

# 7. 第五优先级：创新高 / 新低结构

增加：

``` text
new_low_count_15
new_low_count_30
new_low_count_60

new_high_count_15
new_high_count_30
new_high_count_60
```

以及：

``` text
time_since_low
time_since_high
```

------------------------------------------------------------------------

## 7.1 Continuation

例如：

``` text
过去 30 秒：
new_low_count = 8
```

说明市场持续打新低。

这是明显的单边状态。

------------------------------------------------------------------------

## 7.2 Reversal

例如：

``` text
过去 30 秒：
new_low_count = 1

过去 15 秒：
new_low_count = 0

同时：
ret_15 > 0
```

说明：

> 下跌趋势已经停止制造新低，并开始反向。

这是强反转候选。

------------------------------------------------------------------------

# 8. 第六优先级：冷门 Token 自身 Momentum

当前逻辑主要使用：

``` text
underdog_ask < 0.30
```

这个条件太静态。

应该进一步观察冷门方价格本身的运动方向。

增加：

``` text
ud_ask_delta_5
ud_ask_delta_10
ud_ask_delta_15
ud_ask_delta_30
ud_ask_delta_60
```

以及：

``` text
ud_mid_delta_10
ud_mid_delta_30
ud_mid_delta_60
```

其中：

``` text
ud = underdog
```

------------------------------------------------------------------------

## 8.1 单边

例如：

``` text
underdog ask：

0.30
0.28
0.25
0.22
0.19
```

冷门方仍然持续下跌。

说明：

> 市场仍然在强化热门方向。

不要买。

------------------------------------------------------------------------

## 8.2 反转

例如：

``` text
0.30
0.27
0.24
0.21
0.20
0.21
0.24
```

冷门方已经触底并开始回升。

这是重要的反转确认信号。

------------------------------------------------------------------------

# 9. 第七优先级：热门方 Momentum

不要只研究冷门方。

因为二元市场具有：

``` text
Up + Down ≈ 1
```

热门方是在继续强化还是开始衰弱，对判断市场状态非常重要。

增加：

``` text
favorite_mid_delta_10
favorite_mid_delta_30
favorite_mid_delta_60
```

以及：

``` text
favorite_ask_delta
favorite_bid_delta
```

------------------------------------------------------------------------

# 10. 第八优先级：Order Book Imbalance

现有 HF 数据已经包含：

``` text
bid size
ask size
```

因此可以计算：

``` text
OBI =
(bid_size - ask_size)
/
(bid_size + ask_size)
```

分别计算：

``` text
obi_up
obi_down
```

然后计算：

``` text
relative_obi =
obi_underdog - obi_favorite
```

------------------------------------------------------------------------

## 10.1 Continuation

例如你想买 Up：

``` text
obi_up = -0.60
obi_down = +0.55
```

盘口仍然强烈支持 Down。

这意味着：

> 冷门方虽然便宜，但订单簿仍然支持热门方向。

风险很高。

------------------------------------------------------------------------

## 10.2 潜在反转

例如：

``` text
obi_up = -0.10
obi_down = +0.05
```

但 Up 已经非常便宜。

说明：

> Token 价格已经极端弱，但订单簿并没有同步极端恶化。

值得进一步研究。

------------------------------------------------------------------------

# 11. 第九优先级：Spread / Liquidity

增加：

``` text
spread_up
spread_down
spread_underdog
spread_favorite
```

定义：

``` text
spread = ask - bid
```

同时研究：

``` text
bid_depth
ask_depth
depth_ratio
```

例如：

``` text
underdog_depth_ratio
favorite_depth_ratio
```

主要用于区分：

> 价格下跌到底是有真实资金推动，还是因为盘口非常薄导致价格容易跳动。

------------------------------------------------------------------------

# 12. 第十优先级：现货与 Polymarket Token Divergence

这是最值得深入研究的方向之一。

当前数据同时存在：

``` text
Chainlink / Spot
+
Polymarket Up / Down Token
```

因此可以研究：

> **现货走势是否已经发生变化，但 Polymarket 二元价格仍停留在旧状态。**

------------------------------------------------------------------------

## 12.1 典型反转结构

例如 BTC：

``` text
-0.20%
-0.15%
-0.08%
-0.02%
+0.03%
```

现货已经从下跌转为上涨。

但是 Up token：

``` text
0.30
0.25
0.21
0.18
0.16
```

仍在继续下跌。

这就是：

``` text
Spot reversal
+
Token lag
```

即：

> 现货已经反转，但盘口尚未反映。

这可能是策略最有价值的 Alpha 来源。

------------------------------------------------------------------------

# 13. 第十一优先级：Lead-Lag

进一步测试现货与 Token 的领先滞后关系。

计算：

``` text
spot_return_10
token_return_10
```

并测试：

``` text
corr(spot_return, token_return)
```

不同 lag：

``` text
0s
1s
2s
5s
10s
20s
30s
```

重点研究：

> Polymarket token 是否经常滞后现货几秒到几十秒？

如果存在：

``` text
Spot
↓
先反转

Token
↓
过几秒才反转
```

那么这个 lead-lag 关系可能直接形成交易信号。

------------------------------------------------------------------------

# 14. 第十二层：市场状态评分

把上述特征组合成三个状态评分。

------------------------------------------------------------------------

## 14.1 Continuation Score

可以包含：

``` text
+ slope_60 同方向
+ slope_30 同方向
+ slope_15 同方向
+ new_high / new_low 持续
+ favorite momentum 持续
+ underdog ask 持续下降
+ relative OBI 支持热门方
```

得分越高：

> 越可能是单边延续。

------------------------------------------------------------------------

# 15. Exhaustion Score

可以包含：

``` text
+ 趋势本身很强
+ acceleration 开始减弱
+ new_high / new_low 数量下降
+ 当前价格接近极值
+ favorite momentum 开始衰减
+ token 不再继续强化原趋势
```

解释：

> 趋势尚未反转，但已经进入衰竭阶段。

此时不立即买，而是进入观察区。

------------------------------------------------------------------------

# 16. Reversal Score

可以包含：

``` text
+ slope_60 与 slope_15 方向相反
+ acceleration 反向
+ 当前价格接近窗口极值
+ 不再创新高 / 新低
+ underdog ask 开始反弹
+ favorite momentum 开始反转
+ relative OBI 改善
+ spot / token divergence
```

当 Reversal Score 足够高时：

> 才进入真正的买入候选区。

------------------------------------------------------------------------

# 17. 不要一开始就做机器学习黑盒

研究阶段不建议直接：

``` text
XGBoost
→ prediction
→ BUY
```

第一阶段应该严格执行：

``` text
单变量
↓
双变量
↓
三变量
↓
状态分类
↓
Feature Importance
↓
评分模型
↓
Walk-forward
```

------------------------------------------------------------------------

# 18. 单变量条件分析

例如：

``` text
slope_60 < 0
AND slope_15 > 0
```

统计：

``` text
N
未来 30s 上涨概率
未来 60s 上涨概率
未来 120s 上涨概率
最终 outcome
MFE
MAE
PnL
```

对每个 Feature 都进行类似分析。

------------------------------------------------------------------------

# 19. 二维 Feature Matrix

例如：

``` text
X = spot_slope_30
Y = ud_ask_delta_30
```

把两个变量离散成：

``` text
强下降
弱下降
中性
弱上涨
强上涨
```

形成：

``` text
                 ud_ask momentum

                 ↓      0      ↑

spot ↓           ?      ?      ?

spot 0           ?      ?      ?

spot ↑           ?      ?      ?
```

重点寻找：

> **Spot 已经反转，而 underdog token 仍然没有反转。**

这种区域可能是高价值交易区域。

------------------------------------------------------------------------

# 20. Label 设计

不要只用：

``` text
最终 outcome = Up / Down
```

因为策略真正想预测的是：

> 买入之后未来几十秒到几分钟是否出现有利反转。

建议同时建立以下 Label。

------------------------------------------------------------------------

## Label A：未来 30 秒

``` text
future_return_30
```

------------------------------------------------------------------------

## Label B：未来 60 秒

``` text
future_return_60
```

------------------------------------------------------------------------

## Label C：未来 120 秒

``` text
future_return_120
```

------------------------------------------------------------------------

## Label D：未来 60 秒最大有利移动

``` text
MFE_60
```

即：

> Maximum Favorable Excursion。

------------------------------------------------------------------------

## Label E：未来 60 秒最大不利移动

``` text
MAE_60
```

即：

> Maximum Adverse Excursion。

------------------------------------------------------------------------

## Label F：最终结算结果

``` text
Up / Down
```

------------------------------------------------------------------------

# 21. MFE 对当前策略尤其重要

当前策略存在类似：

``` text
take_profit = 2x
```

因此：

> 最终 outcome 赢，不一定意味着这笔交易适合当前退出逻辑。

例如：

``` text
买入 0.20
↓
最高 0.30
↓
最终归零
```

如果策略目标是 2x，这仍然是失败交易。

但：

``` text
买入 0.20
↓
最高 0.42
↓
随后跌回 0
```

如果有合适的退出机制，则可能是成功交易。

因此应该重点研究：

``` text
P(MFE >= 目标)
```

而不是只研究：

``` text
P(final outcome = win)
```

------------------------------------------------------------------------

# 22. `min_entry = 0.15` 不应该继续作为绝对规则

当前类似：

``` text
ask <= 0.15
→ 太冷
→ 放弃
```

这个逻辑可能误伤真正的反转。

例如：

``` text
0.25
↓
0.18
↓
0.12
↓
0.10
↑
0.13
↑
0.16
```

`0.10` 本身非常危险。

但是：

``` text
0.10 → 0.13 → 0.16
```

已经发生反转确认。

因此以后应该把：

``` text
价格
```

理解成：

> 极端程度。

而不是：

> 是否应该交易。

最终判断应该是：

``` text
极端价格
+
动量
+
反转确认
```

共同决定。

------------------------------------------------------------------------

# 23. `entry_until` 不应该继续只是固定时间参数

当前：

``` text
entry_until = 135s
```

存在一个潜在问题：

> 最好的反转可能发生在 135 秒之后。

建议未来允许更大的观察区间，例如：

``` text
70s ~ 180s
```

只要：

``` text
ReversalScore >= threshold
```

就允许进入。

这样不会因为：

``` text
最佳反转发生在 t = 148s
```

而被机械规则过滤。

------------------------------------------------------------------------

# 24. 入场流程建议升级为四阶段

## Phase 1：Detect

检测是否进入极端状态：

``` text
价格偏离窗口极值
+
冷门 token 价格很低
```

------------------------------------------------------------------------

## Phase 2：Wait

判断是不是仍然单边：

``` text
Trend
Momentum
New High / Low
Order Book Imbalance
Favorite Momentum
```

------------------------------------------------------------------------

## Phase 3：Confirm

必须看到反转证据：

``` text
slope_15 反向
+
underdog ask momentum 反向
+
spot / token divergence
```

------------------------------------------------------------------------

## Phase 4：Entry

最终执行：

``` text
price band
+
liquidity filter
+
risk filter
+
ReversalScore
```

------------------------------------------------------------------------

# 25. 推荐的 Feature 完整清单

第一版建议直接计算以下特征。

  类别       Feature
  ---------- ----------------------------
  Trend      `ret_10`
  Trend      `ret_15`
  Trend      `ret_30`
  Trend      `ret_60`
  Trend      `ret_90`
  Trend      `ret_120`
  Trend      `slope_15`
  Trend      `slope_30`
  Trend      `slope_60`
  Momentum   `acceleration`
  Momentum   `momentum_decay`
  Extreme    `dist_high`
  Extreme    `dist_low`
  Extreme    `time_since_high`
  Extreme    `time_since_low`
  Extreme    `new_high_count_15`
  Extreme    `new_low_count_15`
  Extreme    `new_high_count_30`
  Extreme    `new_low_count_30`
  Extreme    `new_high_count_60`
  Extreme    `new_low_count_60`
  Token      `ud_ask_delta_5`
  Token      `ud_ask_delta_10`
  Token      `ud_ask_delta_15`
  Token      `ud_ask_delta_30`
  Token      `ud_ask_delta_60`
  Token      `ud_mid_delta_10`
  Token      `ud_mid_delta_30`
  Token      `ud_mid_delta_60`
  Favorite   `fav_mid_delta_10`
  Favorite   `fav_mid_delta_30`
  Favorite   `fav_mid_delta_60`
  Favorite   `fav_ask_delta`
  Favorite   `fav_bid_delta`
  Book       `obi_up`
  Book       `obi_down`
  Book       `relative_obi`
  Book       `spread_ud`
  Book       `spread_fav`
  Book       `depth_ratio_ud`
  Book       `depth_ratio_fav`
  Cross      `spot_token_divergence_10`
  Cross      `spot_token_divergence_30`
  Cross      `spot_token_divergence_60`
  Cross      `spot_lead_5`
  Cross      `spot_lead_10`
  Cross      `spot_lead_30`
  Existing   `elapsed`
  Existing   `ask`
  Existing   `range`
  Existing   volume / depth

------------------------------------------------------------------------

# 26. Feature Importance

在完成上述 Feature 后，再进入模型分析。

推荐至少使用：

``` text
XGBoost
LightGBM
Random Forest
Permutation Importance
SHAP
```

至少输出：

1.  Gain importance
2.  Split importance
3.  Permutation importance
4.  SHAP global importance
5.  SHAP dependence
6.  不同时间窗口下的重要性稳定性

------------------------------------------------------------------------

## 26.1 为什么不能只看模型 Feature Importance

例如模型可能显示：

``` text
ask_price importance = 0.32
```

这并不意味着：

> ask 本身就是 Alpha。

因为 ask 越低，本身就意味着市场认为该方向概率越低。

真正值得研究的是：

``` text
ask_price
+
momentum
+
spot divergence
```

是否共同形成预测能力。

因此 Feature Importance 的最终目标不是找"最重要变量"，而是：

``` text
Feature
↓
是否稳定预测未来反转
↓
是否样本外稳定
↓
是否真正提升 PnL
```

------------------------------------------------------------------------

# 27. Feature 研究必须关注稳定性

一个 Feature 即使在训练集很强，也不代表实盘有效。

因此每个重要 Feature 至少要验证：

``` text
Training
Validation
Test
Walk-forward
```

并观察：

``` text
importance 是否稳定
+
方向是否稳定
+
收益是否稳定
```

特别要防止：

> 某一个月份 / 某一种市场状态下偶然有效。

------------------------------------------------------------------------

# 28. 推荐的最终 Score 模型

第一版甚至不需要机器学习。

可以直接做人工评分：

``` text
reversal_score =
    w1 * trend_reversal
  + w2 * momentum_decay
  + w3 * extreme_score
  + w4 * token_reversal
  + w5 * book_reversal
  + w6 * spot_token_divergence
  - w7 * continuation_score
```

或者使用离散规则：

``` text
+2  slope_15 与 slope_60 方向相反
+2  underdog ask 开始反转
+1  spot_return 已反向
+1  接近窗口极值
+1  不再创新高/低
+1  relative OBI 改善
-3  持续创新高/低
-2  slope_15 与 slope_30 同方向且仍然强
-2  热门方盘口继续增强
```

例如：

``` text
score >= 4
→ 入场候选

score 2~3
→ 观察

score <= 1
→ 放弃
```

实际阈值必须由历史数据验证，而不是预先假定。

------------------------------------------------------------------------

# 29. 最终策略结构

推荐最终升级为：

``` text
                 5m Market
                     │
                     ▼
             Feature Engine
                     │
       ┌─────────────┼─────────────┐
       ▼             ▼             ▼
     Trend         Token          Orderbook
       │          Momentum            │
       ▼             ▼               ▼
   Momentum       Favorite        Imbalance
    Decay         Momentum          Spread
       │             │               │
       └─────────────┼───────────────┘
                     ▼
          Spot / Token Divergence
                     │
                     ▼
              State Classifier
                     │
         ┌───────────┼───────────┐
         ▼           ▼           ▼
   Continuation  Exhaustion  Reversal
         │           │           │
        放弃        等待确认      候选
                                 │
                         Price / Liquidity
                                 │
                                 ▼
                               BUY
```

------------------------------------------------------------------------

# 30. 研究顺序

建议严格按照以下顺序执行。

``` text
阶段 1
现有数据构造 Feature
        ↓
阶段 2
单变量条件胜率
        ↓
阶段 3
二维 / 三维 Feature 组合
        ↓
阶段 4
Continuation / Exhaustion / Reversal 分类
        ↓
阶段 5
Feature Importance
        ↓
阶段 6
Reversal Score
        ↓
阶段 7
Walk-forward
        ↓
阶段 8
Paper Trading
        ↓
阶段 9
小额 Live
```

不要先进行大量参数扫描。

------------------------------------------------------------------------

# 31. 为什么不应该继续只扫参数

当前已经测试过：

``` text
take_profit
min_entry
max_entry
entry_until
max_vol
stop_loss
```

现有 walk-forward 结果没有证明单纯参数调整能够解决策略的负 EV 问题。

`max_vol` 确实有过滤作用：

``` text
波动过滤越严格
→ 一部分最差交易被排除
```

但：

> 低波动过滤本身并没有识别出真正有正 EV 的反转市场。

因此继续做：

``` text
max_vol = 18
max_vol = 17
max_vol = 16
...
```

更可能是在优化一个次要维度。

真正应该进入：

> Feature Discovery → Market State Classification → Conditional EV →
> Parameter Optimization

------------------------------------------------------------------------

# 32. 优先级排序

## S 级：第一优先级

``` text
1. Spot short-term slope
2. Momentum decay / acceleration
3. Spot reversal
4. Token momentum reversal
5. Spot / Token divergence
```

这些最直接对应：

> "单边是否结束、反转是否已经发生"。

------------------------------------------------------------------------

## A 级：第二优先级

``` text
6. Distance from extreme
7. New high / low exhaustion
8. Order Book Imbalance
9. Favorite momentum
```

用于进一步确认反转质量。

------------------------------------------------------------------------

## B 级：第三优先级

``` text
10. Spread
11. Depth ratio
12. time_since_extreme
```

主要用于流动性与执行质量。

------------------------------------------------------------------------

## C 级：最后优化

``` text
13. max_vol
14. min_entry
15. entry_until
```

这些仍然需要优化，但应该建立在 Feature / State 模型之后。

------------------------------------------------------------------------

# 33. 最值得寻找的 Target Setup

最终不是寻找：

> "价格低于 0.30 的冷门方"。

而是寻找下面这种完整结构：

``` text
1. 市场前期出现强烈单边
        ↓
2. 冷门 token 被压到极低价格
        ↓
3. 现货趋势开始减速
        ↓
4. 现货短周期开始反向
        ↓
5. 不再持续创新高 / 新低
        ↓
6. 冷门 token 不再继续下跌
        ↓
7. 冷门 ask 开始回升
        ↓
8. Order Book Imbalance 开始改善
        ↓
9. 现货已经反转，但 token 仍低估
        ↓
10. BUY
```

这才是策略真正应该捕捉的：

> **短期冲击造成的错误定价 / 状态切换后的冷门反转。**

------------------------------------------------------------------------

# 34. 最终优化目标

不要把目标设成：

``` text
range < 20
ask < 0.30
```

优化成：

``` text
ContinuationScore < threshold
AND
ExhaustionScore > threshold_1
AND
ReversalScore > threshold_2
AND
spot/token divergence > threshold_3
AND
underdog ask ∈ [X1, X2]
AND
liquidity sufficient
→ BUY
```

也就是说：

> **价格只负责告诉我们"是否足够极端"，Feature 和 State
> 才负责告诉我们"这个极端状态是否正在反转"。**

------------------------------------------------------------------------

# 35. 第一轮实际研究任务

拿到历史 HF 数据后，第一轮不需要修改实盘策略，先完成：

### Step 1：构造 Feature

完整计算上述 Feature。

### Step 2：构造 Label

至少：

``` text
future_return_30
future_return_60
future_return_120
MFE_60
MAE_60
final_outcome
```

### Step 3：单 Feature 分桶

每个 Feature 分位数 / 区间统计：

``` text
N
win_rate
avg_pnl
median_pnl
MFE
MAE
```

### Step 4：二维矩阵

重点：

``` text
Spot Trend
×
Token Momentum
```

以及：

``` text
Spot Trend
×
Order Book Imbalance
```

### Step 5：Feature Importance

计算：

``` text
Gain
Split
Permutation
SHAP
```

### Step 6：识别最有价值的组合

重点寻找：

``` text
强单边
+
极端冷门
+
趋势衰竭
+
现货反转
+
Token 尚未反转
```

### Step 7：形成 Reversal Score

先规则模型，再考虑 ML。

### Step 8：Walk-forward

必须验证：

``` text
训练期
→
验证期
→
测试期
→
滚动 Walk-forward
```

### Step 9：重新评估真实策略

不仅看：

``` text
Win Rate
```

还要看：

``` text
Expectancy
PnL / trade
MFE
MAE
Max Drawdown
Profit Factor
TP hit rate
```

------------------------------------------------------------------------

# 36. 一句话总结

当前策略：

``` text
便宜的冷门
+
低波动
→
买
```

目标策略：

``` text
强单边
↓
极端冷门
↓
趋势衰竭
↓
现货率先反转
↓
盘口尚未反转
↓
冷门 Token 开始反转
↓
Order Book 改善
↓
Reversal Score 达标
→
BUY
```

真正的研究核心是：

> **识别"真正的单边延续"和"看起来像单边、实际上已经衰竭并准备反转"的区别。**

一旦 Feature Importance 和条件胜率证明哪些信号最有效，再把这些信号落到
`decisions.py` 的 `reversal_score` 中，最后才进行
`max_vol / min_entry / entry_until` 等参数优化。
