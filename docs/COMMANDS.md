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
autodl-helper debug health --config config.yaml
autodl-helper debug history --config config.yaml --limit 50
autodl-helper config show --config config.yaml
autodl-helper config validate --config config.yaml
```

## Runtime options

- `--config`
- `--headed`
- `--account`
- `--state-file`
- `--lock-file`
- keeper overrides such as `--shutdown-release-after-hours`, `--keeper-trigger-before-hours`
- scheduled overrides such as `--scheduled-job`, `--target-time`, `--advance-hours`, `--schedule-mode`, `--weekdays`
- auth overrides such as `--lightweight-mode`, `--runtime-auth-revalidate-seconds`

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
autodl-helper debug health --config config.yaml --smoke
autodl-helper debug db --config config.yaml
autodl-helper debug auth --config config.yaml
autodl-helper debug auth --config config.yaml --json
autodl-helper debug auth --config config.yaml --only-unmapped
autodl-helper debug history --config config.yaml --limit 50
autodl-helper debug history --config config.yaml --task keeper
```

## Config

```bash
autodl-helper config show --config config.yaml
autodl-helper config validate --config config.yaml
autodl-helper config validate --config config.example.yaml
```

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
