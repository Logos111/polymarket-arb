# b09：特征工程落地实现计划（v2，合并版）

> 输入：[polymarket_crypto_5m_underdog_strategy_optimization.md](../src/pm_arb/strategies/polymarket_crypto_5m_underdog_strategy_optimization.md)（优化方案，研究设想）
> + [crypto_5m_underdog_特征工程_工程落地方案.md](../src/pm_arb/strategies/crypto_5m_underdog_特征工程_工程落地方案.md)（工程落地方案，工程判断）
> + b09 v1 计划（本文档上一版，已合并废弃）。
> 对应 DEV_PLAN 阶段 5.3（特征工程）+ F1（P0）。
> 状态：✅ 基建 + 批次 0/1/2 + 评分 v2 校准 + 门控 walk-forward 标定已实施（2026-09-14，见 [批次 0 报告](../src/pm_arb/strategies/crypto_5m/backtest/runtime/backtest-report-b09-features.md) / [批次 1/2 报告](../src/pm_arb/strategies/crypto_5m/backtest/runtime/backtest-report-b09-batch12.md) / [三轮报告](../src/pm_arb/strategies/crypto_5m/backtest/runtime/backtest-report-b09-score-gate.md)）；三轮判定：门控方向正确但胜率不清晰超线，**min_reversal_score 维持 None**；后续路线见三轮报告 §6（真 OOS 校准 / mfe 出场实验 / ETH 分桶 / 不对称专项）

## 0. 与 v1 计划的差异（吸收工程落地方案后的修订）

| # | v1 计划 | v2 修订（以工程落地方案为准） |
|---|---|---|
| 1 | 直接算全量 45 特征 | **§2.1 心跳地基问题**：Trend/Momentum 特征必须有新鲜度伴随字段（`sample_count`/`feed_fresh`），喂价不新鲜 → 特征置 None（"不知道"≠"走平"）。实盘数据源 A 案（加 Binance 流）/ B 案（RTDS+新鲜度）**暂定为用户决策点**，代码两案兼容（SeriesBuffer 不关心数据源），本次先落 B 案地基 |
| 2 | 一次出全部特征 | 计算层一次建齐（避免 FeatureSnapshot 反复改字段），**研究分析按三批次**出报告（批次 0 零基建 → 批次 1 需 series_buffer+F1 → 批次 2 需 BookHistory） |
| 3 | 独立 `pm-featstudy` CLI | **复用 `pm-bt5m --capture-features`** + `scripts/feature_*.py`，不膨胀 [project.scripts] |
| 4 | `decide_entry_reversal` 四阶段独立函数 | **`decide_entry_v2` 超集包装**：完全复用 `decide_entry`，仅在 ENTER 时加评分门控；`min_reversal_score=None`（默认）时逐字段等价——零回归可证。entry_until/min_entry 放宽（v1 的 rev 参数）**移出本次范围**（工程方案：参数优化在特征之后，C 级） |
| 5 | `state.py` 独立评分模块 | 评分函数并入 `features.py`（`reversal_score`），权重走 **`score_weights.json` 数据文件**（改权重不过代码 review，可 diff 留痕） |
| 6 | FeatureBuffer 放 strategies | 通用 **`data/series_buffer.py`**（实盘/回测同一类两处调用，杜绝逻辑分叉） |
| 7 | Label 直接算 | 新增 **`labels.py` 泄漏护栏**：`tests/test_no_lookahead_leak.py` 静态检查 orchestrator/decisions/context 不得 import labels（人为纪律 → CI 硬约束） |
| 8 | depth 用份数 | **§2.4 已核查**：du/dd 列在 parquet 源文件中**存在**（另有 ts_utc），仅 TICK_COLS 未读——一行修复后 depth_ratio 可用真实美元深度 |
| 9 | 无样本量纪律 | **§2.5**：复合条件必须报告触发 N，N<384 标注"方向性参考"；候选清单冻结留痕（git 时间戳） |

## 1. 本次实现范围（一次交付，测试全绿为验收线）

### A. `data/series_buffer.py`（新建，纯数据结构）
`SeriesBuffer(maxlen_sec=180)`：`push(t, v)` / `value_asof(t, max_age)` / `ret(t, seconds_ago, max_age=5)` / `slope(t, window_sec, min_samples=3)` / `new_extreme_count(t, window_sec, kind)` / `time_since_extreme(t, kind)` / `sample_count(t, window_sec)`。任何一端缺失/超龄返回 None（None=不知道，绝不当 0）。同一次实现同时服务实盘（wall clock 注入）与回测（tick t 注入）。

### B. `strategies/crypto_5m/features.py`（新建，纯函数，与 decisions 平级）
- `FeatureSnapshot` dataclass（frozen）：Trend（ret_15/30/60/90/120、slope_15/30/60）+ Momentum（acceleration、momentum_decay）+ Extreme（dist_high/low、new_high/low_count_15/30/60、time_since_high/low）+ Token（ud_ask_delta_5/10/15/30/60、ud_mid_delta_10/30/60）+ Favorite（fav_mid_delta_10/30/60）+ Book 批次 0（obi_up/obi_down、relative_obi、spread_ud/fav、depth_ratio_ud/fav，**只 top-of-book**，§2.3）+ Cross（spot_token_divergence_10/30/60）+ 元数据（spot_sample_count_60、feed_fresh）。批次字段标注 docstring。
- `compute_features(spot_buf, ud_buf, fav_buf, book_now, t) -> FeatureSnapshot`：批次 0 特征无缓冲依赖直接算；批次 1/2 依赖缓冲，缓冲不足一律 None。
- `reversal_score(f, weights) -> int | None`：§28 离散规则表（+2/+2/+1/+1/+1/+1/−3/−2/−2），按 sign(slope_60) 归一方向（Up/Down 对称）；`feed_fresh=False` 返回 None（护栏）。
- `ScoreWeights` 从 `strategies/crypto_5m/score_weights.json` 加载（内置默认值兜底）。

### C. `strategies/crypto_5m/labels.py`（新建，仅回测）
`future_return_30/60/120`（ud_mid 视角）、`mfe_60/mae_60`（对 ud_ask 买入价——对齐 2x 止盈逻辑，优化方案 §21）、`final_outcome`（markets.outcome，注明推断值）。文件头声明禁止实盘 import，护栏测试强制。

### D. 回测管线扩展（零回归为前提）
- `hf_loader.py`：TICK_COLS 补 `du/dd`（§2.4 一行修复）；docstring 订正。
- `spot_vol.py`：新增 `window_twap_seq`（暴露 twap60 序列本身），`window_rng_seq` 改为其后处理（同一段计算不重复）。
- `engine.py`：`replay_ticks(..., twap_seq=None, capture=None)`；`FeatureCapture` 累加器（默认 None 行为逐字节不变）；capture 采集窗口 `[entry_after-30, min(entry_until+30, 300-60)]` 逐 tick 特征+落库字段（symbol/condition_id/window_start/elapsed/entered）。
- `run.py`（pm-bt5m）：`--capture-features` → 落盘 `runtime/features/{sym}_features_{tag}.parquet`（tag 写死数据集范围 hf_20260324_20260518）；`runtime/features/` 入 .gitignore。

### E. 实盘最小接线（默认关闭）
- `params.py`：`min_reversal_score: Decimal | None = None`、`score_weights_path: str | None = None`（None=现行为字节级不变）。
- `decisions.py`：新增 `decide_entry_v2`（超集包装，ENTER 时若评分门控开启且分数不足 → 降级 OBSERVE 并注明"评分不足"）；**不修改 decide_entry 本体**（已被测试逐字锚定）。
- `context.py`：`BookHistory`（双边 best ask/bid 各一个 SeriesBuffer）；orchestrator 主循环 `got = await hub.get_books()` 后一行 `book_hist.update(...)` + spot buffer push（RTDS last）；reversal 开启时调 decide_entry_v2。
- §2.2 粒度错位（poll 2s vs 1Hz）本次**不实施**，登记暂定（仅影响实盘特征采样密度，回测无此问题）。

### F. 研究脚本（scripts/，风格对齐 settlement_study.py）
- `feature_bucket_analysis.py`：单变量 5 分位桶 → N/win_rate/avg_pnl/median_pnl/MFE/MAE + 单调性检验（复用 take_profit 分桶方法论）。
- `feature_matrix_2d.py`：spot_slope_30 × ud_ask_delta_30、spot_slope_30 × relative_obi 5×5 交叉表（N + 胜率）。
- ML/Feature Importance 不实现（门槛条件未达），pyproject 加 `[dependency-groups] research`（pandas/scikit-learn/lightgbm/shap）**只声明不安装**。

### G. 测试（对齐工程落地方案 §8）
| 文件 | 覆盖 |
|---|---|
| `test_series_buffer.py` | push/value_asof/ret/slope/new_extreme_count/time_since_extreme/sample_count 边界（空、单点、超龄、样本不足、maxlen 淘汰） |
| `test_features.py` | 合成数据正确性；**feed_fresh=False 时全部 Trend 字段为 None**（§2.1 护栏验证）；top-of-book 口径；方向对称 |
| `test_labels.py` | future_return/MFE/MAE 计算正确性 |
| `test_no_lookahead_leak.py` | 静态 import 护栏：orchestrator/decisions/context 不得 import labels |
| `test_decisions.py` 追加 | decide_entry_v2 在 min_reversal_score=None 时与 decide_entry 逐字段相等（含 log 文案） |
| `test_hf_engine.py` 追加 | replay_ticks(capture=None) 与现结果完全一致；capture 模式产出 schema/行数正确 |

### H. 冒烟验证与交付
1. `pm-bt5m --capture-features --limit 200`（BTC 子集）跑通 capture → parquet → 分桶脚本全链路；出具批次 0 分桶报告到 `backtest/runtime/backtest-report-b09-features.md`（格式按既有报告纪律：双栏指标表、数字全部取自运行输出）。
2. 全量 pytest + ruff；DEV_PLAN v2.2→v2.3（5.3 子阶段 5.3.1/5.3.2 登记 + 暂定项 + 优化方案/工程方案吸收记录）；提交（COMMIT_MSG.txt + -F 纪律）。

## 2. 暂定项（登记后做，颗粒度不损失）

| 项 | 暂定原因 | 恢复条件 |
|---|---|---|
| **§2.1 A 案：实盘加 Binance 1s 现货流** | 用户决策点（成本 vs 有效性）；B 案地基本次已落 | 决策后接数据源，SeriesBuffer 无需改动 |
| §2.2 实盘 poll 提 1s | 仅影响实盘采样密度 | reversal 上线实盘前 |
| walk-forward 接 grid（5.3.6） | score 阈值须先经分桶数据标定 | 批次 0/1 分桶报告出具后 |
| Feature Importance / SHAP（5.3.8） | 规则模型未在验证段跑出正 EV 前不启动 | 规则模型验证段清晰超 27% 且 N≥384 |
| entry_until/min_entry 放宽（优化方案 §22/§23） | 参数优化建立在特征之后（C 级） | 特征验证通过后 |
| Paper trading 对照（5.3.7）、影子仓 A/B | 依赖评分门控验证 | 5.3.6 通过后 |

## 3. 验收清单（工程落地方案 §10 中本次可完成部分）
- [ ] series_buffer.py + 单测（边界全覆盖）
- [ ] 批次 0 特征在 HF 数据集跑出分桶报告，capture→parquet→分桶管线打通
- [ ] decide_entry_v2 零回归（min_reversal_score=None 逐字段一致）
- [ ] labels.py 泄漏护栏测试通过
- [ ] feed_fresh 护栏测试通过（不新鲜 → Trend 字段 None）
- [ ] du/dd 修复 + FeatureSnapshot 全字段计算层可用（批次 1/2 特征就绪待数据验证）
