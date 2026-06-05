# 配置说明

本文说明 `autodl-helper` 当前公开支持的 `config.yaml` 配置面。

默认配置文件：

```text
config.yaml
```

可提交的示例配置：

```text
config.example.yaml
```

## 顶层结构

```yaml
storage:
interactive:
accounts:
notifications:
tasks:
```

## `storage`

### `storage.database_file`

本地 SQLite 数据库路径。

示例：

```yaml
storage:
  database_file: "data/autodl-helper.db"
```

当前数据库保存：

- Keeper 历史
- 抢机历史
- 运行时暂停/恢复控制
- 守护进程心跳
- 事件日志

## `interactive`

### `interactive.max_workers`

终端 UI 内部异步任务池并发数。

示例：

```yaml
interactive:
  max_workers: 6
```

说明：

- 这不是守护进程数量。
- 数值较高时，终端 UI 中部分异步操作会更灵敏。
- 当前默认值是 `6`。

## `accounts`

每个账户条目表示一个 AutoDL 账号运行配置。

示例：

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

### 字段

#### `name`

账户名。CLI、UI、SQLite 运行状态都会用它识别账号。

#### `enabled`

是否参与守护进程执行和默认业务操作。

#### `authorization`

AutoDL 接口或网页请求使用的 `Bearer` 令牌。已有可用令牌时优先使用它。

#### `autodl_phone` / `autodl_password`

没有令牌或需要重新登录时使用的手机号和密码。不要提交真实值。

#### `cache_file`

当前账户的本地授权缓存文件路径。

#### `cache_max_age_seconds`

授权缓存最长有效时间，超过后需要重新校验或刷新。

#### `lightweight_mode`

授权刷新策略。

支持值：

- `off`
- `normal`
- `aggressive`

#### `runtime_auth_revalidate_seconds`

运行时令牌重新校验窗口。为 `0` 时使用默认策略。

#### `force_refresh_min_interval_seconds`

Playwright/浏览器强制刷新令牌的最小重试间隔。

#### `auth_failure_backoff_seconds`

鉴权失败后的退避时间，避免短时间内反复登录或请求。

## `notifications`

可选通知渠道。

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

## `tasks.keeper`

Keeper 用于在实例释放前接管保活，避免关机实例到期被释放。

普通使用只需要先理解两个时间：

- `shutdown_release_after_hours`：关机后最长保留时间。AutoDL 关机后大约多久会释放实例，例如 `360` 表示 15 天。
- `keeper_trigger_before_hours`：释放前多久开始保活。例如 `72` 表示预计释放前 3 天进入 Keeper 接管窗口。

看板里的“几小时/几天内临期”口径来自 `keeper_trigger_before_hours`。

示例：

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

### 字段

#### `enabled`

是否启用 Keeper。

#### `shutdown_release_after_hours`

关机后最长保留时间，单位小时。这个值用于按“关机时间 + 保留时间”估算释放时间。

常见填写：

- `360`：关机后约 15 天释放。
- `168`：关机后约 7 天释放。

#### `keeper_trigger_before_hours`

释放前多久开始保活，单位小时。

常见填写：

- `72`：释放前 3 天开始保活。
- `24`：释放前 1 天开始保活。
- `6`：释放前 6 小时开始保活。

#### `interval_minutes`

守护进程执行 Keeper 检查的间隔。

#### `power_on_wait_seconds`

执行开机动作后的等待时间。

#### `power_off_wait_seconds`

执行关机动作后的等待时间。

#### `start_cooldown_minutes`

近期刚开机后的冷却时间，冷却期内避免重复动作。

#### `stop_cooldown_minutes`

近期刚关机后的冷却时间，冷却期内避免重复动作。

#### `fallback_to_status_at`

当官方数据缺少明确开关机时间时，是否使用 `status_at` 作为兜底时间。

## `tasks.scheduled_start`

`scheduled_start` 用于在目标时间前开始抢机或开机。

示例：

```yaml
tasks:
  scheduled_start:
    enabled: true
    poll_interval_seconds: 5
    jobs:
      - enabled: true
        instance_id: "<your-instance-id>"
        name: "daily-fixed-instance"
        target_time: "14:00"
        advance_hours: 2
        schedule_mode: "daily"
        weekdays: []
        run_date: ""
        timezone: "Asia/Shanghai"
```

### `enabled`

是否启用全部抢机任务。

### `poll_interval_seconds`

守护进程检查抢机任务的间隔。

### `jobs`

抢机任务列表。每个任务支持两类目标：

- 固定 `instance_id`
- 按 `selector` 筛选候选机器

### 任务字段

#### `enabled`

是否启用这个任务。配置停用的任务不能通过运行时“恢复任务”重新启用，需要在配置管理里修改。

#### `instance_id`

固定实例 ID。适合只抢或开启指定实例。

#### `name`

任务显示名和运行时身份。建议在同一账户下保持唯一。

#### `target_time`

目标时间。配置文件里使用 `HH:MM`：

```text
HH:MM
```

终端 UI 支持更省事的写法：

- `9` -> `09:00`
- `930` -> `09:30`
- `09:30` -> `09:30`
- `1430` -> `14:30`

#### `advance_hours`

目标时间前多久开始进入抢机窗口，单位是小时。支持小数，例如 `1.5` 表示 1 小时 30 分钟。

终端 UI 支持：

- `90m`
- `1.5h`
- `2h`
- `2`

#### `schedule_mode`

执行频率。

支持值：

- `daily`：每天
- `once`：单次
- `weekly`：每周

`once` 表示按 `run_date + target_time` 执行一次，成功、超过截止时间或目标不存在后会自动暂停这个任务。

#### `run_date`

仅 `schedule_mode: "once"` 使用，格式为 `YYYY-MM-DD`。

示例：

```yaml
schedule_mode: "once"
run_date: "2026-05-20"
target_time: "09:30"
```

#### `weekdays`

仅 `schedule_mode: "weekly"` 使用，按周序号：

- `1`：周一
- `2`：周二
- `3`：周三
- `4`：周四
- `5`：周五
- `6`：周六
- `7`：周日

终端 UI 支持：

- `1,3,5`
- `135`
- `周一三五`
- `工作日`
- `周末`

#### `timezone`

解释 `target_time` 使用的时区。默认使用 `Asia/Shanghai`。

### `selector`

不指定固定实例时，可以用 `selector` 抢符合条件的候选机器。

```yaml
selector:
  regions:
    - "<region-a>"
  gpu_model: "RTX 3080 Ti"
  gpu_count: 1
  charge_types:
    - "payg"
```

支持字段：

- `regions`
- `gpu_model`
- `gpu_count`
- `charge_types`

### `priority`

可选的候选优先级。

```yaml
priority:
  - region: "<preferred-region>"
    machine_alias: "<preferred-machine>"
```

支持字段：

- `instance_id`
- `region`
- `machine_alias`

## 环境变量覆盖

配置加载时支持部分环境变量覆盖。

代码中使用的环境变量包括：

- `Authorization`
- `AUTODL_PHONE`
- `AUTODL_PASSWORD`
- `AUTODL_AUTH_CACHE_FILE`
- `AUTODL_DB_PATH`
- `AUTODL_LOGIN_RETRIES`
- `AUTODL_LOGIN_TIMEOUT_MS`
- `AUTODL_POST_LOGIN_WAIT_SECONDS`
- `AUTODL_AUTH_CACHE_MAX_AGE_SECONDS`

公开仓库中建议只提交 `config.example.yaml` 和 `.env.template`，真实值放在本地 `.env` 或本地 `config.yaml`。

## 公开仓库建议

- 提交 `config.example.yaml`。
- `config.yaml` 只保留在本地。
- 不发布真实令牌。
- 不发布真实手机号和密码。
- 截图和反馈中把实例 ID 视为潜在敏感信息。
