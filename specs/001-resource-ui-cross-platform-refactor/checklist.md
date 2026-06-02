# 清单：资源、UI 与跨平台重构

## 治理清单

- [x] 影响范围已评估为跨模块。
- [x] 需要规格套件。
- [x] 已创建规格。
- [x] 已创建计划。
- [x] 已创建任务。
- [x] 当前脏工作树已处理或明确纳入。
- [x] 脏工作树处理后已记录基线测试。

## 架构清单

- [x] `core` / `runtime`、`tasks`、`services` 不导入 CLI 或 UI。
- [x] UI 不导入 `autodl_helper.cli.app`。
- [x] CLI 门面模块保持轻薄。
- [x] `autodl_helper` 下没有通配符导入。
- [x] 新共享逻辑放在 CLI/UI 适配器下层或 UI 专属共享工具中。
  runtime PID 辅助函数和任务结果标签辅助函数位于 `runtime` / `tasks`；UI 后台输入重绘辅助函数位于 UI 包内。

## 资源清单

- [x] 停用任务在创建客户端/接口调用前短路。
- [x] 暂停 Keeper 在任务执行前短路。
- [x] 看板实时接口调用都是显式、缓存或有理由的。
  Keeper 看板使用本地 SQLite 历史；实时接口调用保留在 Keeper 执行和账户健康检查等显式操作中。
- [x] 被动看板渲染不触发浏览器登录。
- [x] 后台调度间隔可预测，避免重复工作。

## UI 清单

- [x] 主菜单分组清晰。
- [x] 子菜单一致重绘。
- [x] 提示展示一致。
- [x] 返回行为一致。
- [x] Keeper 详情解释临期、跳过和失败状态。
- [x] 失败摘要简洁，并提供详情入口。
- [x] 主看板刷新完成后自动重绘。
- [x] Keeper 立即执行完成后自动重绘。
- [x] 账户健康检查完成后自动重绘。

## 跨平台清单

- [x] macOS LaunchAgent 测试通过。
- [x] Linux systemd 用户服务测试通过。
- [x] Windows Task Scheduler 测试通过。
- [x] 服务后端外的 `runtime` 代码避免 POSIX-only 假设。
- [x] 路径按文档相对配置目录解析。

## 验证清单

- [x] 变更模块聚焦测试通过。
- [x] `ruff check .` 通过。
- [x] `python -m pytest -q` 通过。
  最新全量记录：297 个测试通过。
- [x] 架构测试通过。
- [x] 用户可见行为已更新文档。
  README、COMMANDS、TROUBLESHOOTING、DEVELOPMENT、架构约束和规格文档均已校准到当前 UI/资源行为。
