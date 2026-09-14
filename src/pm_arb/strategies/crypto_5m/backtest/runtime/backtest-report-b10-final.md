# b10：真假订单识别与高精度过滤——上线判定总报告

日期：2026-09-15 | 数据：HF 全量（BTC 14,226 / ETH 11,245 窗口，
2026-03~05）| 执行：P0 重验 → P1 candidate_orders → P2 画像审计 →
P3 规则网格 → P4 rolling walk-forward，全程 walk-forward 纪律
（窗口级切分、报告只认 val 段）。

## 一句话结论

**入场侧过滤空间系统性不存在**：线性分离度坍塌（AUC ~0.55）、单规则
无转正、误杀回收无空间、max_vol 下探滤掉的好单多于坏单、depth veto
减亏不转正、rolling 验证单段依赖。**taker 全口径下策略无可上线修改，
所有参数默认值维持不变；下一步唯一通道是 maker 出场。**

## 用户四疑问的回答

1. **ud_delta 为什么只算 60s（120s 维度）**：labels 一直有三档
   （30/60/120s）。P0 全量重验发现反弹是**慢变量**——深跌桶
   ret60 +0.007（坍塌 15 倍）但 **ret120 +0.096（复现）**；60s 视野
   只捕捉零头。但结算胜率 23.9% < 盈亏线 26.8%，taker 出场费吃掉
   反弹——**120s 反弹的货币化通道 = maker 出场**（见 §5）。
2. **max_vol 再调低滤掉好单多还是坏单多**：**好单多**。val 段三处
   边际被滤组胜率 28~29% 全高于保留组（BTC 20→15 被滤组 EV +0.11/笔），
   保留组 EV 随下调单调恶化。b07 的 BTC=20 确认底部最优、ETH 0.65
   维持、0.4 有害。
3. **depth<0.27 类信号反方向挖掘**：方向全量复现（BTC 16.9% vs
   22.8%）且误杀低（7.7%），已接线为 `min_depth_ratio` hard veto
   （默认 None 零回归）。val 段最优档减亏 25~75%（BTC -0.073→-0.018）
   **但不转正**，rolling 验证不显著——**登记为已实现、默认关闭的
   减亏工具**。
4. **b09_gpt_analysis 文档价值**：meta-labeling 框架 + precision@
   recall-floor 口径 + candidate_orders 工程形态已全部吸收为 b10
   基建；其出场结论维持证伪。

## 分阶段证据链（详见各分报告）

| 阶段 | 结论 | 报告 |
|---|---|---|
| P0 全量重验 | 175 子集选择偏差实锤：方向不对称 8 倍消失、slope AUC 0.665→0.520、单规则 39.4%→24.3%；**depth 弱复现、120s 慢反弹唯一正发现** | backtest-report-b10-p0-revalidation.md |
| P1 数据集 | candidate_orders 落盘（BTC 184,391 / ETH 139,490 行），后续实验单表读取 | runtime/features/*.parquet |
| P2 画像审计 | 分离度跨币种坍塌（0.552/0.554）；hard-neg 画像无缺失特征；**误杀回收证伪**（早窗/带外高价均为口径混杂效应）；ETH relative_obi≥1 唯一转正观察项 | backtest-report-b10-p2-profile.md |
| P3 网格 | max_vol 下探滤好单（见疑问 2）；depth veto 减亏不转正；12 组合 val EV 全负 | backtest-report-b10-p3-grid.md |
| P4 rolling | 8 配置 × 5 fold：**全部未超线**（EV>0 fold 仅 1~2/5，正向集中于 fold 4 单时段）；ETH relative_obi≥1 滚动检验证伪（BTC 5/5 fold 全负） | runtime/rolling_walkforward.csv + rule_rolling_check.md |

## 上线判定（逐项）

| 候选 | 判定 | 依据 |
|---|---|---|
| max_vol BTC 10/15、ETH 0.4 | **不上线（有害）** | 被滤组好单、保留组单调恶化 |
| min_depth_ratio=0.27 | **不上线（默认 None 维持）** | 减亏方向正确但 rolling 不显著（BTC -0.165 vs -0.179±0.09；ETH 0.65 反向）；代码已接线，模拟实盘阶段可用实盘数据复验 |
| ETH relative_obi≥1 规则 | **不上线（证伪）** | 滚动检验 2/5 fold 转正 + BTC 全负 = 阈值切分巧合 |
| 评分门控 / 出场倍数 / 止损 | 维持 b09 证伪结论，默认关闭 | 四轮实验 |

**判定口径**：验证段清晰超线（>1 SE）+ recall≥60% 约束下 EV>0——
所有候选两项均不满足。策略参数默认值与 880a6db 提交时逐字一致
（唯一代码增量 `min_depth_ratio` 默认 None，零回归，202 项测试全绿）。

## 5. 下一步：maker 出场方向设计登记（P4 后第一优先）

P0 的关键正发现组合出唯一未证伪路径：

- **信号**：ud_ask_delta_30 深跌桶 ret120 +0.096（全量复现）——反弹
  是慢变量，入场后 120s 有充足挂单时间窗；
- **死锁**：taker 出场费（四轮证伪）+ 结算口径胜率 23.9% < 26.8%
  盈亏线——taker 全口径无解；
- **通道**：maker（post-only）挂单卖出省 taker 费，慢反弹 = 排队
  时间充裕；待验证问题——maker 成交率、逆行风险（反弹不来时持有到
  结算的兜底）、价格梯度设计；
- **数据**：P5 录制流（09-15 起持续积累）可提供真实盘口深度/成交
  序列估算 maker 成交率；HF 数据集无逐笔成交，只能做保守上界。
- 设计实验暂不在 b10 内展开，登记入 DEV_PLAN 待办。

## 6. P5 录制链路（并行完成）

- 故障根因：pm-record 进程内 httpx 连接池在 Clash 节点切换后僵死
  （Gamma 查询连续失败 1.5 天，WS 自带重连而自愈）→ 重启止血 +
  record_ticks.py 加 gamma_broken 自愈重建（下窗口重建客户端）
- 验证：market.jsonl 重启后持续写入（51MB+，mtime 实时）；WindowMeta
  稳定性待 24h 观察
- 模拟实盘时点：≥2~4 周连续完整录制 + 策略侧结论达标，两者谁后到
  谁卡点（当前策略侧无达标项，录制积累继续）

## 产出清单

- 代码：params `min_depth_ratio` / decisions `book_depth_ratio` +
  `decide_entry_v2` 双门控 / engine + orchestrator 同源接线 /
  record_ticks 自愈修复（+8 测试，全量 202 passed + 3 skipped）
- 脚本：scripts/{make_candidate_orders, candidate_profile,
  depth_revalidation, maxvol_depth_grid, rule_rolling_check,
  rolling_walkforward}.py
- 数据：runtime/features/{btc,eth}_features_hf + candidate_orders
  （全量 450MB）；runtime/maxvol_depth_grid.csv、rolling_walkforward.csv
- 报告：b10-p0-revalidation / b10-p2-profile / b10-p3-grid /
  本报告（runtime/ 目录）

## 限定

- outcome 为数据集推断值（非链上结算）；模拟实盘阶段用真实结算交叉核对
- 现货为 Binance 单所代理 ≠ Chainlink 多所聚合，max_vol 数值不可直接套实盘
- 数据集固定 2026-03~05，微结构可能已变化——P5 录制数据的最终价值
