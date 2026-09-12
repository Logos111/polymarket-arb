# pm-arb — Polymarket 量化套利交易系统

Polymarket（Polygon 链上预测市场）自动化套利交易框架。项目规划见 [docs/PROJECT_PLAN.md](docs/PROJECT_PLAN.md)。

> ⚠️ 仅供技术研究。请确认所在地法律与平台服务条款；交易钱包请使用小额专用钱包，私钥切勿提交入库。

## 快速开始

```bash
# 1. 安装依赖（uv 会自动准备 Python 3.12）
uv sync

# 2. 环境自检
uv run pm-doctor

# 3. （交易功能才需要）配置凭证
cp .env.example .env
# 编辑 .env，填入 PM_PRIVATE_KEY / PM_POLYGON_RPC_URL 等

# 4. 跑测试
uv run pytest -q
```

5min 加密多笔交易实盘（`pm_arb.app.trade5m`）运行时会把行情轨迹与决策写入
`runtime/logs/trade5m_<时间戳>.log`，可实时跟踪：

```bash
# 实时查看最新一次会话日志
tail -f "$(ls -t runtime/logs/trade5m_*.log | head -1)"
# 停止正在运行的实例
pkill -f "pm_arb.app.trade5m"
```

只读行情功能（市场发现、盘口订阅、订单簿构建）**无需任何凭证**；
下单与链上操作（split/merge/redeem）需要配置私钥。

## 目录结构

```
src/pm_arb/
├── infra/       # 配置（SecretStr 凭证）、结构化日志、SQLite 持仓库
├── data/        # Gamma / CLOB REST / WS / 本地订单簿 / tick 录制与 Parquet 压实
├── execution/   # CLOB 下单撤单、订单状态机、链上 split/merge/redeem
├── portfolio/   # 头寸、对账、PnL（预留）
├── risk/        # 风控闸门：限额/熔断/重复入场/Kill Switch/余额 fail-closed
├── strategies/  # crypto_5m：决策（decisions）/编排（orchestrator）/参数 + 回测引擎
└── app/         # CLI 入口：doctor / setup / watch / trade5m / record / redeem / compact
```

## 开发

```bash
uv run ruff check src tests   #  lint
uv run pytest                 #  测试
```

## 路线图

- [x] 阶段 0：脚手架与基础设施（配置 / 日志 / 自检）
- [x] 阶段 1：数据层（Gamma 同步、CLOB REST、WS 订单簿、tick 录制）— 实网联调通过
- [x] 阶段 2：交易接口（L1/L2 认证、下单封装、链上 split/merge/redeem）+ paper 模拟成交 + SQLite 结算闭环
- [ ] 阶段 2.5：配置交易钱包私钥，小额真实验证下单/merge 闭环
- [x] 阶段 4（最小集）：下单前风控闸门（限额/熔断/重复入场/Kill Switch/余额 fail-closed）
- [ ] 阶段 5：特征工程（Chainlink 更新频率等 F1-F8）与策略迭代
- [ ] 阶段 6+：NegRisk、跨平台、做市，详见项目规划
