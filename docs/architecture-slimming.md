# 架构瘦身约束

项目正在向分层架构收敛：业务规则放在适配器下层，CLI 和 UI 只负责输入输出。

这些约束由 `.importlinter` 和 `tests/architecture/test_structure.py` 共同维护。

## 分层

- `autodl_helper.core`、`api`、`auth`、`config`、`notify`、`runtime`、`storage`：可复用的核心和运行时支撑。
- `autodl_helper.tasks`：Keeper 和抢机的任务编排。
- `autodl_helper.services`：平台后台服务后端和服务管理。
- `autodl_helper.ui`：终端 UI 适配器。
- `autodl_helper.cli`：命令行适配器和命令分发。

## 依赖方向

允许方向是适配器依赖业务/运行时层，反向不允许。

- `core` / `runtime`、`tasks`、`services` 不能导入 `autodl_helper.cli` 或 `autodl_helper.ui`。
- UI 可以调用可复用的下层模块，但不能导入 CLI 入口 `autodl_helper.cli.app`。
- CLI 内部模块不能导入 CLI 入口，例如 `autodl_helper.cli.app`。

## 导入风格

`autodl_helper` 包内禁止 `from ... import *`。

显式导入能让模块归属更清楚，也便于架构测试约束边界。

## CLI 瘦身规则

CLI 应当是适配器：解析参数、委托给下层应用/任务/服务函数、渲染输出。

业务逻辑应迁移到 `core` / `task` / `service` 模块，让它们可以被 CLI 和 UI 复用并独立测试。

结构测试会约束兼容门面保持轻薄，并防止 CLI 边界模块继续增长业务辅助函数或类。

## UI 瘦身规则

终端 UI 模块应把用户交互留在 UI 适配器内，同时消费可复用的任务/运行时辅助函数来组合业务标签和状态。

`autodl_helper/ui/action_menus.py` 是兼容门面；职责明确的菜单行为应放在 `daemon_menu.py`、`keeper_menu.py` 和 `account_menu.py`。

耗时 UI 后台动作必须保持非阻塞。完成后需要自动重绘的场景，应复用 `autodl_helper/ui/background_input.py`，不要在各菜单里重复实现输入线程。

被动看板渲染只能读取本地状态和历史。实时 AutoDL 接口请求应保留为显式操作，例如 Keeper 执行、抢机执行、list、login 或账户健康检查。

## 已移除的旧规则

引用旧交互兼容层的规则已经移除。新规则针对当前包边界，也就是 `ui` 和 `cli.app`。
