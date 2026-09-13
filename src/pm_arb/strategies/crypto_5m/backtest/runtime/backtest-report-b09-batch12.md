# b09 特征工程第二轮研究报告：批次 1/2 全量分桶 + 复合条件 + 评分 v2 校准

> 日期：2026-09-14 ｜ 数据：HF 公开数据集（2026-03-24 → 2026-05-18，BTC 子集 175 窗口 / 21,961 行）
> 输入：批次 0 已出的 [btc_features parquet](../../../../../runtime/features/)（无新采集，计算层字段全量启用）
> 上游报告：[backtest-report-b09-features.md](backtest-report-b09-features.md)（批次 0）
> 计划：[b09_reversal_feature_engine_plan.md](../../../../../docs/b09_reversal_feature_engine_plan.md)

## TL;DR

| 发现 | 证据 | 结论 |
|---|---|---|
| **冷门方自身跌幅 = 全场最强单变量** | ud_mid/ask_delta_30/60 全族 ρ=-0.60~-1.00；Q1（深跌）ret60 +0.11~+0.12 且 mae 最低 | 原评分规则"ud 被买入(>0) → +2"**方向反了**，已翻转 |
| **深反转+动能组合 = 最强复合条件** | ud30<-0.15 & mom_dec<0.0001：ret60 **+0.1606**（基准 -0.0015），N=1,562 | 反转假设成立，但形态是"超跌反弹"而非"逆势抄底" |
| **现货方向强不对称** | 深反转(现货跌) ret60 **+0.2427** vs (现货涨) +0.0312；但胜率反向 27.8% vs 33.6% | 路径收益只在现货下行侧成立；与持有到结算口径冲突，暂不入规则 |
| **极薄深度近乎必输** | dep<0.27：胜率 13.6%（N=4,548），且现行策略在该桶入场率 34.5% 最高 | 入评分罚分 -2；也是候选硬过滤条件 |
| Trend 族全线负单调 | ret_15 ρ=-0.90、slope_30 ρ=-0.60：现货越涨冷门越差 | 与批次 0 二维"逆势接刀"结论互证 |

## 1. 批次 1：Trend / Momentum / Extreme（[明细](feature_buckets_btc_batch1.md)）

- **Trend 族（ret_15/30/60/90/120、slope_15/30/60）全线负单调**（ρ=-0.20~-0.90）：现货近段涨幅越大，冷门方未来 60s 中价收益越差。ret_15 最陡（ρ=-0.90：Q1 +0.0468 → Q5 -0.0383）。含义：**入场前现货已经上涨的窗口系统性更差**——不是"反转买点"，是"接刀点"。
- **momentum_decay（|ret_60|-|ret_15|）负单调 ρ=-0.90**：Q1（动能未衰竭）ret60 +0.1285 / 胜率 34.6% / mfe 0.142 全场最高；Q5（动能衰竭）-0.1014 / 胜率 22.8%。短线动量仍在推进时买冷门，反弹最快到来。
- **time_since_high 正单调 ρ=+0.70**：距窗口高点越久（95~130s），ret60 越正（+0.0297）——高位钝化后的冷门有修复。
- 胜率普遍 **U 型**（两端桶好、中间桶差）：极端状态（无论方向）比"温吞状态"更有交易价值。
- None 占比记录：ret_120 排除 13,995 行（采集窗 [40,165]s 前 120s 历史不足属正常截断，非数据缺陷）。

## 2. 批次 2：Token / Favorite / Cross（[明细](feature_buckets_btc_batch2.md)）

- **ud_delta 族（ask/mid × 5/10/15/30/60s）完美负单调**：ud_mid_delta_30 Q1（深跌 ≤-0.15）ret60 **+0.1182**、mae 0.1078 全场最低；Q5（上涨）-0.0798、mae 0.1556 最高。30s/60s 档 ρ=-1.00 无例外。**冷门方超跌 → 短线反弹**，这是本数据集最干净的信号。
- **fav 族为其镜像**（fav_mid_delta_30 ρ=+1.00）：热门方涨得越多冷门反弹越多——fav 与 ud 价格近互补，**不构成独立信息**，评分规则中 fav 只作反向罚分项是合理的。
- **spot_token_divergence"赢路径不赢结算"最极端**：Q5（现货涨、token 不涨）ret60 +0.0965 / mae 0.0680 全场最低，但胜率仅 **16.7%**。中价路径收益与结算方向在此特征上彻底分裂——若未来上 2x 止盈出场（优化方案 §21），该特征价值最大；持有到结算口径下应回避。

## 3. mfe_60 口径复核（[明细](feature_buckets_btc_mfe.md)）

以 mfe_60（未来 60s 最大有利偏移，对齐 2x 止盈）为目标重跑 8 个关键特征：**方向与 ret60 完全一致**（relative_obi ρ=+1.00、momentum_decay Q1 mfe 0.142 全场最高、slope_30 负单调）。结论稳健，不依赖目标口径选择。

## 4. 复合条件与三因子交叉（[明细](feature_cond_btc.md)，新增 scripts/feature_cond_stats.py）

| 条件 | N | 胜率 | ret60 | mfe60 | mae60 |
|---|---|---|---|---|---|
| 基准（全行） | 21,961 | 31.2% | -0.0015 | +0.1115 | +0.1219 |
| 深反转: ud30<-0.15 | 4,049 | 30.8% | +0.1228 | +0.1329 | +0.1073 |
| 动能增强: mom_dec<0.0001 | 6,087 | 35.8% | +0.0844 | +0.1378 | +0.1333 |
| 三因子: ud30<-0.08 & rel_obi>0.53 | 3,368 | 29.7% | +0.1109 | +0.1287 | +0.1007 |
| 三因子+深度>0.45 | 1,575 | **37.5%** | +0.0629 | +0.1418 | +0.1204 |
| **深反转+动能** | 1,562 | 29.0% | **+0.1606** | +0.1416 | +0.1199 |
| **深反转+动能（现货跌）** | 694 | 28.5% | **+0.2980** | +0.1617 | +0.1054 |
| 深反转+动能（现货涨） | 868 | 29.4% | +0.0495 | +0.1255 | +0.1314 |
| 深度薄: dep<0.27（负） | 4,548 | **13.6%** | +0.0176 | 0.0661 | 0.0696 |
| 陡涨 slope30>0.48（负对照） | 4,200 | 26.7% | -0.0622 | +0.0869 | +0.1082 |

- 负对照（陡涨接刀 -0.0622）与正条件方向相反，通过一致性检验。
- **因子半独立性**：ud_delta 与 momentum_decay 组合增益显著（+0.12 → +0.16），relative_obi 在其上再叠加（三因子 +0.11 但 N 更小）——三个信号源不同（token 盘口 / 现货动量 / 双边挂单簿），条件独立性初步成立。
- **方向不对称是最大的未决问题**：现货下行侧 ret60 高 8 倍，但上行侧胜率高 5.8 个点。若出场逻辑仍是持有到结算（tp=0.99），上行侧可能反而更优；不对称项**暂不入评分规则**，待 mfe 口径出场实验后复核。

## 5. 评分规则 v2 校准（features.py reversal_score + score_weights.json）

| 规则项 | v1（优化方案 §28 原设） | v2（本轮证据） | 证据来源 |
|---|---|---|---|
| ud_ask_delta_30 | > 0 → +2（"真金白银投票反转"） | **≤ -0.15 → +2；≤ -0.08 → +1** | ud 族 ρ=-0.60~-1.00；>0 桶全族最差；深反转 ret60 +0.12 |
| obi_improve | 0.05 | **0.5** | relative_obi Q4 边界 0.53 处 ret60 转正，ρ=+0.90 |
| 极薄深度罚 | 无 | **depth_ratio < 0.27 → -2** | 胜率 13.6% 近必输；现行策略在该桶入场率 34.5%（负 EV 重灾区） |
| 动能未衰竭 | 无 | **momentum_decay ≤ 0.0001 → +1** | Q1 ret60 +0.128 / mfe 0.142；组合信号最强 +0.30 |
| 其余 6 项 | 不变 | 不变 | 单变量证据不足或方向未证伪（见各桶明细） |

测试：test_reversal_score_symmetric 期望 6→7（情景改为证据支持形态）；新增 test_reversal_score_v2_terms 覆盖 4 项校准；全量 188 passed 零回归。`min_reversal_score` 默认仍 None——**校准后规则未经 walk-forward 验证前不进实盘**。

## 6. 限定（必读）

- 单数据集（BTC 2026-03~05）全量统计，**无 train/validation 切分**——阈值数值有过拟合风险，方向结论（相对等级序）较稳；
- outcome 为数据集作者推断值，非链上结算；
- ret60/mfe 均为中价口径，未含 taker fee 与滑点；
- 胜率与路径收益多处分叉（divergence Q5 最极端），"哪个目标对齐真实 PnL"依赖出场逻辑（当前 tp=0.99 持有到结算）。

## 7. 下一步（按依赖顺序）

1. **score 阈值 walk-forward 标定（5.3.6）**：把 reversal_score(v2) 接入回测引擎，扫 min_reversal_score ∈ {2..6}，前 70% 训练 / 后 30% 验证——验证段胜率清晰超 27% 盈亏平衡线才谈上线；
2. **ETH 全量复跑**（capture + 三批次分桶）：检验信号跨币种稳健性；
3. **mfe 口径出场实验**：若 2x 止盈可行，divergence Q5 类"赢路径输结算"形态翻正，重估不对称项；
4. 方向不对称专项验证（按 slope_60 分段 walk-forward）。

## 附录：复现命令

```
.venv\Scripts\python.exe scripts\feature_bucket_analysis.py --symbols btc --features ret_15,ret_30,ret_60,ret_90,ret_120,slope_15,slope_30,slope_60,acceleration,momentum_decay,dist_high,dist_low,new_low_count_15,new_low_count_30,new_high_count_15,new_high_count_30,time_since_low,time_since_high --label future_return_60 --out src\pm_arb\strategies\crypto_5m\backtest\runtime\feature_buckets_btc_batch1.md
.venv\Scripts\python.exe scripts\feature_bucket_analysis.py --symbols btc --features ud_ask_delta_5,ud_ask_delta_10,ud_ask_delta_15,ud_ask_delta_30,ud_ask_delta_60,ud_mid_delta_10,ud_mid_delta_30,ud_mid_delta_60,fav_mid_delta_10,fav_mid_delta_30,fav_mid_delta_60,spot_token_divergence_10,spot_token_divergence_30,spot_token_divergence_60 --label future_return_60 --out src\pm_arb\strategies\crypto_5m\backtest\runtime\feature_buckets_btc_batch2.md
.venv\Scripts\python.exe scripts\feature_bucket_analysis.py --symbols btc --features relative_obi,depth_ratio_underdog,ud_ask_delta_30,ud_mid_delta_30,momentum_decay,time_since_high,slope_30,spot_token_divergence_30 --label mfe_60 --out src\pm_arb\strategies\crypto_5m\backtest\runtime\feature_buckets_btc_mfe.md
.venv\Scripts\python.exe scripts\feature_cond_stats.py --symbols btc --out src\pm_arb\strategies\crypto_5m\backtest\runtime\feature_cond_btc.md
```

数字全部取自脚本直出文件：feature_buckets_btc_batch1.md / feature_buckets_btc_batch2.md / feature_buckets_btc_mfe.md / feature_cond_btc.md。
