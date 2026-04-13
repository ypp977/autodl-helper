# Commands

This document summarizes the public CLI commands exposed by `autodl-helper`.

General form:

```bash
python main.py <command> --config config.yaml
```

or, after editable install:

```bash
autodl-helper <command> --config config.yaml
```

---

## Runtime and daemon commands

### `run-daemon`

Run the combined daemon in the foreground.

```bash
python main.py run-daemon --config config.yaml
```

Common options:

- `--config`
- `--headed`
- `--account`
- `--run-once`
- `--state-file`
- `--lock-file`

### `run-all`

Compatibility alias for `run-daemon`.

```bash
python main.py run-all --config config.yaml
```

### `run-keeper` / `keep`

Run keeper only.

```bash
python main.py run-keeper --config config.yaml
python main.py keep --config config.yaml
```

### `run-scheduled-start` / `grab`

Run scheduled-start only.

```bash
python main.py run-scheduled-start --config config.yaml
python main.py grab --config config.yaml
```

---

## Bootstrap

### `init`

Run the first-run bootstrap wizard: create local `.env` and `config.yaml` from templates, validate the config, and optionally jump into `interactive`.

```bash
python main.py init
python main.py init --config custom.yaml
python main.py init --yes
python main.py init --force
```

Use this as the default first-run command before `interactive`, `login`, or `service-install`.

---

## Service management

These commands manage the background service for the current platform.

The service backend is selected automatically:

- macOS → LaunchAgent
- Linux → systemd user service
- Windows → Task Scheduler

### `service-install`

Install the background service for the current platform.

```bash
python main.py service-install --config config.yaml
```

### `service-start`

Start the installed background service.

```bash
python main.py service-start --config config.yaml
```

### `service-stop`

Stop the installed background service.

```bash
python main.py service-stop --config config.yaml
```

### `service-restart`

Restart the installed background service.

```bash
python main.py service-restart --config config.yaml
```

### `service-status`

Show the background service and daemon status.

```bash
python main.py service-status --config config.yaml
```

### `service-uninstall`

Remove the background service.

```bash
python main.py service-uninstall --config config.yaml
```

---

## Account and auth commands

### `accounts`

Show configured account and login status.

```bash
python main.py accounts --config config.yaml
```

Options:

- `--account`
- `--json`

### `login`

Refresh login or token state for one or all accounts.

```bash
python main.py login --config config.yaml --account main
python main.py login --config config.yaml --all
```

---

## Instance inspection commands

### `list-instances`

List instances from AutoDL.

```bash
python main.py list-instances --config config.yaml
```

Options:

- `--json`

### `inspect-instance`

Show one instance in detail.

```bash
python main.py inspect-instance --config config.yaml --instance-id <id>
```

### `watch-instance`

Continuously watch one instance.

```bash
python main.py watch-instance --config config.yaml --instance-id <id>
```

Options:

- `--interval`
- `--json`

---

## Keeper and scheduled-start diagnostics

### `keeper-probe`

Explain keeper timing and keeper eligibility.

```bash
python main.py keeper-probe --config config.yaml
```

Option:

- `--only-eligible`

### `history`

Read local keeper / scheduled-start history from SQLite.

```bash
python main.py history --config config.yaml --limit 50
```

Options:

- `--task keeper|scheduled_start`
- `--event-type <exact-event-type>`
- `--limit <n>`
- `--json`

### `auth-report`

Summarize observed auth-related failures from the event log.

```bash
python main.py auth-report --config config.yaml
```

Options:

- `--limit`
- `--json`
- `--only-unmapped`
- `--only-likely-auth`
- `--suggest-patch`
- `--apply-suggested-patch`

### `db-check`

Check SQLite schema and writability.

```bash
python main.py db-check --config config.yaml
```

### `healthcheck`

Run local operational checks.

```bash
python main.py healthcheck --config config.yaml
```

Options:

- `--smoke`
- `--state-file`
- `--lock-file`

---

## Notification command

### `test-notify`

Send a test notification.

```bash
python main.py test-notify --config config.yaml
```

Options:

- `--channel pushplus|serverchan|email|all`

---

## Config commands

### `validate-config`

Validate config only.

```bash
python main.py validate-config --config config.yaml
```

### `config-show`

Show loaded config from file/environment.

```bash
python main.py config-show --config config.yaml
```

### `config-resolve`

Show effective config after CLI overrides.

```bash
python main.py config-resolve --config config.yaml
```

### `config-edit`

Persist supported settings back into config file.

```bash
python main.py config-edit --config config.yaml
```

---

## Interactive command

### `interactive`

Launch the interactive control panel.

```bash
python main.py interactive --config config.yaml
```

Common options:

- `--config`
- `--headed`
- `--account`
- `--state-file`
- `--lock-file`

---

## Common global/runtime overrides

Some commands support runtime overrides:

- `--shutdown-release-after-hours`
- `--keeper-trigger-before-hours`
- `--start-cooldown-minutes`
- `--stop-cooldown-minutes`
- `--fallback-to-status-at`
- `--no-fallback-to-status-at`
- `--scheduled-poll-interval`
- `--scheduled-job`
- `--target-time`
- `--advance-hours`
- `--lightweight-mode`
- `--runtime-auth-revalidate-seconds`
- `--force-refresh-min-interval-seconds`
- `--auth-failure-backoff-seconds`

Use these for:

- one-off experiments
- runtime testing
- diagnosing config behavior

Prefer editing `config.yaml` for persistent changes.
