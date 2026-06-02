# 排障手册

## 1. `最近检查时间` 不更新

先确认守护进程是否真的在写入新的抢机或 Keeper 历史。

命令：

```bash
autodl-helper service status --config config.yaml
tail -n 20 logs/service.stdout.log
```

需要确认：

- 守护进程正在运行。
- 日志中仍有新的 `[抢机检查]`。
- 日志中仍有新的 `[后台轮询]`。

必要时重启后台服务：

```bash
autodl-helper service restart --config config.yaml
```

## 2. 页面一直显示“刷新中”

当前版本的主菜单“刷新状态”、Keeper“立即执行”、账户“健康检查”都是后台提交任务，并会在任务完成后自动重绘页面。

如果仍然一直显示“刷新中”，优先检查：

- 是否运行了最新代码。
- 后台守护进程是否仍在产生新数据。
- 当前页面是否展示的是本地历史，而不是实时接口结果。
- 是否有异常卡在 `logs/service.stderr.log`。

重新进入 UI：

```bash
autodl-helper ui --config config.yaml
```

说明：

- 看板的 Keeper 数据默认来自本地 SQLite 历史。
- 需要实时探测 AutoDL 官方数据时，使用 Keeper“立即执行”或账户“健康检查”等显式操作。

## 3. 后台服务状态异常

检查：

```bash
autodl-helper service status --config config.yaml
tail -n 50 logs/service.stderr.log
tail -n 50 logs/service.stdout.log
```

常见原因：

- 标准错误日志中仍保留旧崩溃日志。
- 守护进程心跳停止。
- 配置重载失败。

按平台继续检查：

- macOS：确认 LaunchAgent 已安装并加载。
- Linux：执行 `systemctl --user status autodl-helper`。
- Windows：确认 Task Scheduler 中的任务存在并启用。

可先尝试：

```bash
autodl-helper service restart --config config.yaml
```

## 4. Keeper 计划看起来不对

运行：

```bash
autodl-helper run keeper --config config.yaml --run-once
autodl-helper debug history --config config.yaml --task keeper --limit 20
```

重点检查：

- 预计释放时间。
- 下次 Keeper 时间。
- 实例是否处于开机/关机冷却期。
- 当前释放周期是否已经执行过 Keeper。
- 官方数据是否缺少明确时间，导致使用 `status_at` 兜底。

## 5. Keeper 统计数量看起来过大

当前版本按执行批次聚合 Keeper 历史。

如果旧历史是在批次聚合之前生成的，历史摘要可能和新记录略有差异。可查看原始历史：

```bash
autodl-helper debug history --config config.yaml --task keeper --limit 50
```

## 6. 修改配置后没有生效

运行：

```bash
autodl-helper config validate --config config.yaml
autodl-helper config show --config config.yaml
autodl-helper service restart --config config.yaml
```

说明：

- UI 配置管理保存后会请求守护进程重载配置。
- 如果重载失败，守护进程会继续使用上一份有效配置。
- 运行时暂停/恢复操作只写 SQLite 运行时控制，不修改 `config.yaml`。

## 7. 登录校验失败

检查：

```bash
autodl-helper login --config config.yaml --account main
autodl-helper debug auth --config config.yaml
```

需要确认：

- 令牌仍有效。
- 手机号/密码登录流程仍有效。
- 本地缓存没有过期或损坏。
- 账户是否已启用。

## 8. 数据库或运行时文件异常

常见现象：

- 无法打开数据库文件。
- 锁文件残留。
- 本地运行状态不一致。

检查：

```bash
autodl-helper debug db --config config.yaml
ls -la data logs .cache
```

如果要手动清理，先停止守护进程：

```bash
autodl-helper service stop --config config.yaml
```

删除前先备份并检查本地文件，不要直接清空包含凭据或历史的目录。

## 9. 提交反馈前需要准备的信息

请提供脱敏后的信息：

- 完整命令。
- 脱敏后的配置片段。
- 脱敏后的日志。
- 使用的是 UI 还是守护进程。
- 操作系统和 Python 版本。
