# Claude Code 对话记录归档

本目录保存开发过程中 Claude Code 会话的原始记录（JSONL），用于复盘、追溯决策与上下文交接。

- 来源：`~/.claude/projects/d--kimi-polymarket/<session-id>.jsonl`
- 格式：每行一条消息/工具事件的 JSON（Claude Code 原生 transcript 格式）
- 仅归档**主会话**；子代理（subagents）记录与错误日志不含在内

## 文件

| 文件 | 日期 (UTC+8) | 内容概要 |
|---|---|---|
| `2026-09-08_session-1_e4976771.jsonl` | 2026-09-08 20:00 | 早期短会话 |
| `2026-09-08_session-2_dde43d9d.jsonl` | 2026-09-08 22:01 | 本地仓库与 GitHub 对齐；反推重建数据层（data 层）；实盘拉取 BTC/ETH 5min Up/Down 盘口 |

## 注意

- 这些是**开发时点的快照**，不会随新对话自动更新；如需归档新会话，手动从
  `~/.claude/projects/d--kimi-polymarket/` 复制对应 `.jsonl` 到本目录。
- 仓库当前为**私有 (Private)**。提交前仍应确认记录中不含真实私钥 / API secret
  （`.env` 已在 `.gitignore` 中忽略，私钥不应出现在对话里）。
