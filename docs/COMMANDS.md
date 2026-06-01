# Commands

Public entrypoints:

```bash
autodl-helper <command> [options]
python main.py <command> [options]
python -m autodl_helper <command> [options]
```

Examples use `autodl-helper`; replace it with `python main.py` when running from source.

## Quick reference

```bash
autodl-helper init
autodl-helper login --config config.yaml --account main
autodl-helper login --config config.yaml --all
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

## Runtime options

Common options:

- `--config`
- `--account`
- `--headed`
- `--state-file` / `--lock-file`

Task-specific overrides stay on their owning command, for example Keeper timing options on `run keeper` and scheduled-start timing options on `run scheduled`.

## Service

```bash
autodl-helper service install --config config.yaml
autodl-helper service start --config config.yaml
autodl-helper service status --config config.yaml
autodl-helper service restart --config config.yaml
autodl-helper service stop --config config.yaml
autodl-helper service uninstall --config config.yaml
```

Backend is selected by platform: macOS LaunchAgent, Linux systemd user service, Windows Task Scheduler.

## Diagnostics

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

## Config

```bash
autodl-helper config show --config config.yaml
autodl-helper config show --config config.yaml --json
autodl-helper config validate --config config.yaml
autodl-helper config validate --config config.yaml --json
```

Interactive config editing is available from `autodl-helper ui --config config.yaml`.

## Terminal UI layout

`autodl-helper ui` 的主菜单按职责分层：

- `1` 刷新状态。
- `2` 业务操作：Keeper 管理、抢机管理。
- `3` 设置管理：账户管理、配置管理。
- `4` daemon 管理。
- `0` 退出。

业务操作页只写运行时控制状态，不直接改 `config.yaml`；完整配置仍从“配置管理”进入。

## JSON output contract

Commands that expose `--json` keep their successful data output stable. Status-style commands such as
`config validate --json`, `debug db --json`, and `debug health --json` return:

```json
{"ok": true, "data": {"status": "valid"}}
```

When a `--json` command fails, stderr uses a stable envelope and redacts sensitive fields:

```json
{"ok": false, "error": {"code": "config_invalid", "message": "Configuration invalid.", "details": {"errors": []}}}
```

`debug auth --apply-suggested-patch` is disabled because runtime event data must not directly edit source files.
Use `debug auth --suggest-patch` and apply reviewed changes manually.

## Old aliases

Use grouped commands instead of removed flat aliases:

| Old command | New command |
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
