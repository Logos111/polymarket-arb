# b09 特征工程第一轮研究报告：批次 0 分桶 + 二维交叉

> 日期：2026-09-13 ｜ 数据：HF 公开数据集（2026-03-24 → 2026-05-18，BTC 子集）
> 管线：`pm-bt5m --spot --capture-features --limit 200` → `runtime/features/btc_features_hf_20260324_20260518.parquet`
> → `scripts/feature_bucket_analysis.py` / `scripts/feature_matrix_2d.py`
> 计划：[b09_reversal_feature_engine_plan.md](../../../../docs/b09_reversal_feature_engine_plan.md)

## TL;DR

| 特征 | 发现 | 证据 | 结论 |
|---|---|---|---|
| relative_obi | **强单调** | 桶均 ret60：-0.024 → +0.047，Spearman ρ=+0.90 | 冷门方相对买压占优 → 未来 60s 中价收益单调升，**进入二维验证与评分规则** |
| depth_ratio_underdog | **胜率单调（强）** | 桶胜率 13.3% → 42.0% 逐桶递增；ret60 非单调（ρ=-0.30） | 冷门方深度越厚胜率越高；极薄深度（Q1）近乎必输，**候选过滤条件** |
| obi_up / obi_down | 无信号 | ρ=±0.10，桶间胜率 30-32% | 单边 OBI 无区分度（与 relative_obi 对照印证"相对差"才是信息） |
| spread_underdog / favorite | 分布退化 | 0.01 tick 占 4/5 桶 | HF 数据集盘口价差最小单位 0.01，分桶失效，**该数据集上不可用** |

## 1. 数据与口径

| 项 | 值 |
|---|---|
| 币种 | btc（子集冒烟，--limit 200） |
| 窗口数 | 175（outcome ∈ Up/Down） |
| 采集行 | 21,961（采集窗 [40, 165]s 逐秒） |
| 入场行 | 4,618（21.0%） |
| Label | future_return_{30,60,120}（cand 中价）、mfe_60/mae_60（对 cand_ask）、final_outcome（**数据集推断值，非链上结算**） |
| 批次 1/2 特征 | 二维矩阵含 slope_30 × ud_ask_delta_30（N=21,261，feed_fresh 过滤后） |

限定（必读）：outcome 为数据集作者推断；Binance 1s K 线 ≠ Chainlink TWAP；数据集微结构与当前可能不同。

## 2. 批次 0 单变量分桶（5 等频桶）

数字全部取自 feature_buckets_btc.md（脚本直出，禁止手算）。

### relative_obi（obi_cand − obi_fav）——本轮最强发现

| 桶 | 区间 | N | 胜率 | ret60 | mfe60 | mae60 |
|---|---|---|---|---|---|---|
| Q1 | [-2.00, -1.32] | 4,392 | 33.2% | -0.0241 | 0.1024 | 0.1347 |
| Q2 | [-1.32, -0.43] | 4,392 | 31.6% | -0.0258 | 0.1068 | 0.1285 |
| Q3 | [-0.43, 0.53] | 4,392 | 30.3% | -0.0134 | 0.1078 | 0.1207 |
| Q4 | [0.53, 1.35] | 4,392 | 30.2% | +0.0085 | 0.1159 | 0.1160 |
| Q5 | [1.35, 2.00] | 4,393 | 30.6% | **+0.0474** | 0.1246 | 0.1097 |

- 单调 ρ=+0.90（桶均 ret60 vs 桶序）；Q5 与 Q1 差 0.072（中价相对收益）。
- mae60 随桶单调下降（0.135→0.110）：买压占优同时压低回撤，方向一致。
- **注意胜率列不单调（33%→30%）**：信息在中价路径（ret/mfe/mae），不在最终结算方向——与"持有到结算"现行出场逻辑存在口径差异，后续以 mfe_60 为主目标（对齐 2x 止盈逻辑，优化方案 §21）。

### depth_ratio_underdog（ud ask 名义 / fav bid 名义）

| 桶 | 区间 | N | 胜率 | ret60 |
|---|---|---|---|---|
| Q1 | [0.01, 0.27] | 4,383 | **13.3%** | +0.0208 |
| Q2 | [0.27, 0.45] | 4,384 | 26.0% | -0.0251 |
| Q3 | [0.45, 0.61] | 4,383 | 34.9% | +0.0084 |
| Q4 | [0.61, 0.79] | 4,384 | 39.6% | -0.0069 |
| Q5 | [0.79, 1.27] | 4,384 | **42.0%** | -0.0048 |

- 胜率单调性极强（13%→42%，每桶递增无例外）；ret60 不单调（ρ=-0.30）。
- 机理假设：冷门方深度极薄 = 市场对方向分歧极小（已成定局），冷门方确实该输；深度接近热门方 = 分歧大，冷门方有真实翻盘流动性支撑。
- **入场率与深度强相关**（Q1 34.9% vs Q5 9.4%）：现行策略天然多在薄深度入场——这本身可能就是负 EV 来源之一，待批次 1/2 联合验证。

### 无信号项

- obi_up/obi_down：桶间胜率 30~32%、ρ=±0.10——单边 OBI 无区分度。
- spread_underdog/spread_favorite：0.01 tick 占 4/5 桶（分布退化），该数据集上分桶失效。

## 3. 二维交叉表（slope_30 × ud_ask_delta_30 / relative_obi）

N=21,261（feed_fresh 行），每格 N 643–1,008（全部 ≥384，非方向性参考）。
完整表见 feature_matrix_btc.md。要点：

- **slope_30 列向梯度稳定**：ud_ask_delta_30 任一行内，胜率沿 slope_30 Q1→Q4 大多上升（如 Q5 行：28%→39%→36%→43%）；slope_30 Q5（最陡上行）全线最差（胜率 19–32%，ret 多为负）——**现货陡涨时买冷门 = 逆势接刀**，与反转假设方向相反但幅度一致。
- relative_obi 行向弱梯度：Q1（冷门买压劣势）在 slope Q5 列 ret -0.067 vs Q5 行 -0.009，与单变量结论同向。
- 无一格胜率 ≥50%：二维组合仍未翻正，符合"冷门方结构性负 EV"三轮回测背景（important_decision_experience）。

## 4. 下一步（按优化方案 §30 顺序）

1. **批次 1/2 全量分桶**（Trend/Momentum/Extreme/Token/Favorite/Cross，计算层已建齐，只换 --features 参数即可）；
2. relative_obi + depth_ratio + slope_30 三因子二维/三维交叉，检验条件独立性；
3. score 规则表（§28）按本轮证据校权重（current: relative_obi +1 → 考虑提高；depth_ratio 未入规则表 → 候选新增）；
4. mfe_60 为主目标重跑分桶（对齐 2x 止盈出场逻辑）；
5. 门槛线：任一复合条件在验证段清晰超 27% 且 N≥384 才进入 FI/SHAP（research 依赖组已声明未安装）。

## 附录：复现命令

```
.venv\Scripts\python.exe -m pm_arb.strategies.crypto_5m.backtest.run --symbols btc --spot --capture-features --limit 200
.venv\Scripts\python.exe scripts\feature_bucket_analysis.py --symbols btc --out src\pm_arb\strategies\crypto_5m\backtest\runtime\feature_buckets_btc.md
.venv\Scripts\python.exe scripts\feature_matrix_2d.py --symbols btc --out src\pm_arb\strategies\crypto_5m\backtest\runtime\feature_matrix_btc.md
```

原始输出：runtime/capture_smoke.txt；分桶明细：feature_buckets_btc.md；矩阵明细：feature_matrix_btc.md。
