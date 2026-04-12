# Configuration

This document explains the public configuration surface of `autodl-helper`.

Default config file:

```text
config.yaml
```

Public-safe example:

```text
config.example.yaml
```

## Top-level structure

```yaml
storage:
interactive:
accounts:
notifications:
tasks:
```

---

## `storage`

### `storage.database_file`

Path to the local SQLite database.

Example:

```yaml
storage:
  database_file: "data/autodl-helper.db"
```

What it stores:

- keeper history
- scheduled-start history
- runtime control state
- daemon heartbeat
- event logs

---

## `interactive`

### `interactive.max_workers`

Controls the internal async worker pool used by the interactive terminal UI.

Example:

```yaml
interactive:
  max_workers: 6
```

Notes:

- this is **not** the daemon process count
- higher values can make the interactive UI more responsive
- current default is `6`

---

## `accounts`

Each account entry defines one AutoDL account runtime profile.

Example:

```yaml
accounts:
  - name: "main"
    enabled: true
    authorization: "Bearer <your-token>"
    autodl_phone: ""
    autodl_password: ""
    cache_file: ".cache/main-auth.json"
    cache_max_age_seconds: 86400
    lightweight_mode: "normal"
    runtime_auth_revalidate_seconds: 0
    force_refresh_min_interval_seconds: 0
    auth_failure_backoff_seconds: 0
```

### Fields

#### `name`

Human-readable account key used across CLI commands and runtime state.

#### `enabled`

Whether this account participates in daemon execution.

#### `authorization`

Bearer token for AutoDL API or web requests.

Use this when you already have a usable token.

#### `autodl_phone` / `autodl_password`

Alternative login path when token-based auth is not used.

Do not publish real values.

#### `cache_file`

Path to the local auth cache for this account.

#### `cache_max_age_seconds`

How long auth cache remains valid before refresh is required.

#### `lightweight_mode`

Auth/runtime refresh behavior.

Supported values:

- `off`
- `normal`
- `aggressive`

#### `runtime_auth_revalidate_seconds`

Optional runtime token revalidation window.

#### `force_refresh_min_interval_seconds`

Minimum interval before Playwright/browser-based forced refresh is retried.

#### `auth_failure_backoff_seconds`

Backoff duration after auth failure before retrying again.

---

## `notifications`

Optional notification backends.

### `notifications.pushplus`

```yaml
notifications:
  pushplus:
    enabled: false
    token: "<your-pushplus-token>"
```

### `notifications.serverchan`

```yaml
notifications:
  serverchan:
    enabled: false
    token: "<your-serverchan-sendkey>"
```

### `notifications.email`

```yaml
notifications:
  email:
    enabled: false
    smtp_host: "smtp.example.com"
    smtp_port: 465
    username: "you@example.com"
    password: "<your-smtp-password>"
    to:
      - "you@example.com"
```

---

## `tasks.keeper`

Keeper prevents a target instance from reaching release by operating within a computed keeper window.

Example:

```yaml
tasks:
  keeper:
    enabled: true
    shutdown_release_after_hours: 360
    keeper_trigger_before_hours: 6
    interval_minutes: 60
    power_on_wait_seconds: 60
    power_off_wait_seconds: 5
    start_cooldown_minutes: 60
    stop_cooldown_minutes: 360
    fallback_to_status_at: true
```

### Fields

#### `enabled`

Enable or disable keeper globally.

#### `shutdown_release_after_hours`

How long after shutdown an instance is considered near release.

#### `keeper_trigger_before_hours`

How long before release keeper should start taking over.

#### `interval_minutes`

Keeper polling interval for daemon execution.

#### `power_on_wait_seconds`

Wait time after power-on action.

#### `power_off_wait_seconds`

Wait time after power-off action.

#### `start_cooldown_minutes`

Cooldown after a recent start.

#### `stop_cooldown_minutes`

Cooldown after a recent stop.

#### `fallback_to_status_at`

Whether to use `status_at` as a fallback time source when explicit shutdown/start timestamps are missing.

---

## `tasks.scheduled_start`

scheduled-start handles instance grabbing / opening around a target time.

Example:

```yaml
tasks:
  scheduled_start:
    enabled: true
    poll_interval_seconds: 5
    jobs:
      - instance_id: "<your-instance-id>"
        name: "daily-fixed-instance"
        target_time: "14:00"
        advance_hours: 2
        timezone: "Asia/Shanghai"
```

### `enabled`

Enable or disable all scheduled-start jobs globally.

### `poll_interval_seconds`

How often the daemon checks scheduled-start jobs.

### `jobs`

List of scheduled-start jobs.

Each job supports either:

- fixed `instance_id`
- selector-based targeting

### Job fields

#### `instance_id`

Use this for a fixed instance target.

#### `name`

Job display name and runtime identity.

Recommended to keep unique per account.

#### `target_time`

Daily target time, format:

```text
HH:MM
```

#### `advance_hours`

How many hours before target time polling should begin.

#### `schedule_mode`

Supported values:

- `daily`
- `once`

#### `timezone`

Time zone used to interpret `target_time`.

### `selector`

Use selector when grabbing any matching candidate rather than one fixed instance.

```yaml
selector:
  regions:
    - "<region-a>"
  gpu_model: "RTX 3080 Ti"
  gpu_count: 1
  charge_types:
    - "payg"
```

Fields:

- `regions`
- `gpu_model`
- `gpu_count`
- `charge_types`

### `priority`

Optional preferred candidate order.

```yaml
priority:
  - region: "<preferred-region>"
    machine_alias: "<preferred-machine>"
```

Supported fields:

- `instance_id`
- `region`
- `machine_alias`

---

## Environment variables

The project also supports selected environment overrides during config loading.

Examples used in code:

- `Authorization`
- `AUTODL_PHONE`
- `AUTODL_PASSWORD`
- `AUTODL_AUTH_CACHE_FILE`
- `AUTODL_DB_PATH`
- `AUTODL_LOGIN_RETRIES`
- `AUTODL_LOGIN_TIMEOUT_MS`
- `AUTODL_POST_LOGIN_WAIT_SECONDS`
- `AUTODL_AUTH_CACHE_MAX_AGE_SECONDS`

For public/open-source usage, prefer storing real values in local `.env` and keeping committed config sanitized.

---

## Recommended public/open-source practice

- commit `config.example.yaml`
- keep `config.yaml` local only
- never publish real tokens
- never publish real phone/password credentials
- treat instance IDs as potentially sensitive in screenshots and issue reports
