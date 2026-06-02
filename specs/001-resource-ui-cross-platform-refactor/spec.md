# 规格：资源、UI 与跨平台重构

## 状态

已完成主要实现和验证。该重构影响 CLI、终端 UI、守护进程、Keeper、抢机、存储、授权和平台服务模块，因此需要分阶段执行和验证。

## 问题

`autodl-helper` 已经覆盖多个运行面：CLI、终端 UI、守护进程、Keeper、抢机、SQLite 存储、授权和平台服务托管。此前已经修复了具体 UI 和 Keeper 问题，但仍需要系统性降低资源占用、简化交互流，并保持 macOS、Linux、Windows 的一等支持。

重构目标不是增加大量防御性代码，而是让边界更简单、空转成本更低、用户流程更清楚、模块更小且可测试。

## 目标

- 降低守护进程和 UI 空闲路径的资源占用。
- 让终端 UI 交互一致、可发现、低噪声。
- 保持 CLI 和 UI 是适配器，业务规则下沉到可复用模块。
- 保持跨平台服务托管：
  - macOS LaunchAgent
  - Linux systemd 用户服务
  - Windows Task Scheduler
- 保持代码直接、清晰，避免厚重包装。
- 改善 Keeper、抢机、授权和服务操作的失败诊断。
- 保持或加强现有架构测试。

## 非目标

- 不做图形界面桌面客户端。
- 不做仅适配 Docker 的重构。
- 不做破坏其他平台的平台专属改写。
- 除非能直接减少复杂度或资源占用，否则不引入大型依赖。
- 不做纯风格大改。
- 没有兼容性测试时，不改变 AutoDL 业务行为。

## 当前证据

- 架构约束记录在 `docs/architecture-slimming.md`。
- 跨平台服务后端：
  - `autodl_helper/services/launchd.py`
  - `autodl_helper/services/systemd.py`
  - `autodl_helper/services/windows_task.py`
- UI 菜单已按职责拆分：
  - `autodl_helper/ui/daemon_menu.py`
  - `autodl_helper/ui/keeper_menu.py`
  - `autodl_helper/ui/account_menu.py`
  - `autodl_helper/ui/action_menus.py` 保持兼容门面。
- 后台输入和自动重绘集中在 `autodl_helper/ui/background_input.py`。

## 要求

### 资源占用

- 任务停用、暂停或未到期时，守护进程不应做不必要的接口调用。
- UI 看板被动渲染不应触发昂贵的实时接口探测。
- 后台循环应有可预测的调度间隔，避免重复轮询。
- 授权刷新和浏览器登录只在明确需要时运行。

### UI 与交互

- 子菜单使用一致的页面式重绘和提示反馈。
- 主菜单、Keeper、守护进程、账户、配置、抢机页面返回语义一致。
- Keeper 操作执行前检查配置、暂停状态和账号可用性。
- 失败摘要默认简洁，并提供详情入口。
- 配置编辑区分“草稿已修改”和“保存后生效”。
- 主菜单刷新、Keeper 立即执行、账户健康检查等后台动作完成后自动重绘当前页面。

### 跨平台

- 服务生命周期行为覆盖 macOS、Linux、Windows。
- 运行时路径按文档保持相对配置目录。
- Python 运行时代码避免 shell-only 假设。
- 平台专属代码隔离在 `autodl_helper/services`。

### 架构

- `core`、`runtime`、`tasks`、`services` 不导入 CLI 或 UI。
- CLI 和 UI 委托给共享应用/任务函数。
- 大模块拆分只在能形成清晰职责时进行。
- 兼容门面必须保持轻薄。

### 测试与验证

- 每个行为变化都需要聚焦测试。
- 纯重构移动需要导入测试和回归测试。
- 完整验证门禁：
  - `python -m ruff check .`
  - `python -m pytest -q`
  - 架构测试
  - 服务后端测试

## 风险评估

- 高风险：守护进程调度、授权刷新、平台服务安装/启动/停止。
- 中风险：UI 菜单流、配置编辑/重载、Keeper 执行。
- 中风险：抢机匹配和轮询行为。
- 低风险：纯文档修改和纯渲染提取。

## 规格套件决策

该范围跨模块、跨平台且影响性能，必须使用规格、计划、任务和清单门禁推进。
