# 贡献指南

欢迎提交议题和拉取请求。这个项目优先保持命令行工具清晰、稳定、低资源占用。

## 提交拉取请求前

1. 运行 `python -m pytest -q`。
2. 运行 `python -m ruff check .`。
3. 确认没有加入密钥、令牌、账号标识、实例 ID 或本地缓存。
4. 如果命令、配置、终端 UI 或用户可见行为发生变化，同步更新 `README.md`、`docs/` 和 `CHANGELOG.md`。
5. 如果新增 `--json` 输出，保持稳定 JSON 结构，并确认错误输出会脱敏。

## 开发环境

详细开发说明见：

- `docs/DEVELOPMENT.md`
- `docs/TROUBLESHOOTING.md`

公共示例配置见：

- `config.example.yaml`
- `config.test.yaml`

## 范围约束

本仓库是命令行优先的 AutoDL 自动化工具，改动应围绕：

- 抢机轮询。
- Keeper 保活与止损。
- 多账号运行时控制。
- 终端 UI 交互。
- 本地 SQLite 状态与历史。
- 后台服务托管。

避免混入：

- 网页服务端。
- 桌面客户端。
- 与终端工具无关的前端资源。
- 会扩大敏感信息暴露面的调试输出。
