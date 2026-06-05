# 命令说明

公开入口：

```bash
autodl-helper <command> [options]
python main.py <command> [options]
python -m autodl_helper <command> [options]
```

示例统一使用 `autodl-helper`。从源码运行时，把它替换成 `python main.py`。

## 快速参考

```bash
autodl-helper init
autodl-helper login --config config.yaml --account main
autodl-helper login --config config.yaml --all
autodl-helper accounts --config config.yaml
autodl-helper list --config config.yaml
autodl-helper list --config config.yaml --json
autodl-helper run daemon --config config.yaml
autodl-helper run daemon --config config.yaml --run-once
autodl-helper run keeper --config config.yaml
autodl-helper run scheduled --config config.yaml
autodl-helper ui --config config.yaml
autodl-helper service status --config config.yaml
autodl-helper debug health --config config.yaml --json
autodl-helper debug history --config config.yaml --limit 50
autodl-helper config show --config config.yaml
autodl-helper config validate --config config.yaml --json
```

## 运行时选项

通用选项：

- `--config`
- `--account`
- `--headed`
- `--state-file` / `--lock-file`

任务专属覆盖参数放在对应命令下，例如 Keeper 时间参数在 `run keeper`，抢机时间参数在 `run scheduled`。

## 后台服务

```bash
autodl-helper service install --config config.yaml
autodl-helper service start --config config.yaml
autodl-helper service status --config config.yaml
autodl-helper service restart --config config.yaml
autodl-helper service stop --config config.yaml
autodl-helper service uninstall --config config.yaml
```

后台托管方式按平台自动选择：macOS 使用 LaunchAgent，Linux 使用 systemd 用户服务，Windows 使用 Task Scheduler。

## 诊断

```bash
autodl-helper debug health --config config.yaml
autodl-helper debug health --config config.yaml --json
autodl-helper debug health --config config.yaml --smoke
autodl-helper debug db --config config.yaml
autodl-helper debug db --config config.yaml --json
autodl-helper debug auth --config config.yaml
autodl-helper debug auth --config config.yaml --json
autodl-helper debug auth --config config.yaml --only-unmapped
autodl-helper debug history --config config.yaml --limit 50
autodl-helper debug history --config config.yaml --task keeper
```

## 配置

```bash
autodl-helper config show --config config.yaml
autodl-helper config show --config config.yaml --json
autodl-helper config validate --config config.yaml
autodl-helper config validate --config config.yaml --json
```

交互式配置编辑入口是 `autodl-helper ui --config config.yaml`。

## 终端 UI 布局

`autodl-helper ui` 的主菜单按职责分层：

- `1` 刷新状态。
- `2` 业务操作：Keeper 管理、抢机管理。
- `3` 设置管理：账户管理、配置管理。
- `4` 守护进程管理。
- `0` 退出。

业务操作页只写运行时控制状态，不直接改 `config.yaml`；完整配置仍从“配置管理”进入。

配置管理里常用输入规则：

- Keeper 核心参数按“关机后最长保留时间”和“释放前多久开始保活”理解，单位仍是小时。
- 抢机目标时间支持 `9`、`930`、`09:30`、`1430`。
- 抢机提前时间支持 `90m`、`1.5h`、`2h`、`2`。
- 每周几支持 `1,3,5`、`135`、`周一三五`、`工作日`、`周末`。
- 单次抢机会要求填写执行日期，按 `YYYY-MM-DD + 目标时间` 执行一次。

刷新类操作保持非阻塞：

- 主菜单“刷新状态”会提交后台刷新任务，状态栏先显示“刷新中”，任务完成后自动重绘看板。
- Keeper 管理里的“立即执行”和账户管理里的“账户健康检查”同样后台执行，完成后会在当前页面自动回显结果。
- 被动看板渲染只读本地 SQLite 历史；实时 AutoDL 接口请求只发生在显式操作中。

## JSON 输出契约

带 `--json` 的命令会保持成功输出结构稳定。状态类命令，例如
`config validate --json`、`debug db --json` 和 `debug health --json`，返回：

```json
{"ok": true, "data": {"status": "valid"}}
```

当 `--json` 命令失败时，标准错误使用稳定错误结构，并对敏感字段脱敏：

```json
{"ok": false, "error": {"code": "config_invalid", "message": "配置无效。", "details": {"errors": []}}}
```

`debug auth --apply-suggested-patch` 已禁用，因为运行时事件数据不能直接修改源码。
需要建议内容时使用 `debug auth --suggest-patch`，再人工审查后应用。

## 旧别名

已移除的扁平命令请改用分组命令：

| 旧命令 | 新命令 |
| --- | --- |
| `run-all` / `run-daemon` | `run daemon` |
| `run-keeper` / `keep` | `run keeper` |
| `run-scheduled` / `grab` | `run scheduled` |
| `list-instances` | `list` |
| `healthcheck` | `debug health` |
| `db-check` | `debug db` |
| `auth-report` | `debug auth` |
| `validate-config` | `config validate` |
| `config-show` | `config show` |
| `interactive` | `ui` |
