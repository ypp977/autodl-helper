# Architecture

This document describes the high-level structure of `autodl-helper`.

## Project shape

`autodl-helper` is a **CLI-first local automation tool**.

It has three main execution surfaces:

1. **one-shot CLI commands**
2. **foreground daemon**
3. **interactive terminal console**

It uses **SQLite** for local persistence and a platform-specific service backend for long-running background execution.

---

## Core components

### 1. Configuration layer

Files:

- `autodl_helper/config.py`

Responsibilities:

- parse YAML config
- resolve environment overrides
- produce typed settings objects

Important note:

- public example config should stay sanitized
- runtime config can still be overridden from CLI flags or environment

### 2. CLI layer

Files:

- `autodl_helper/cli.py`
- `autodl_helper/cli_parser.py`
- `autodl_helper/cli_handlers.py`
- `autodl_helper/cli_renderers.py`
- `main.py`

Responsibilities:

- parse commands
- dispatch command handlers
- render terminal output
- coordinate daemon/service/runtime flows

Separation:

- `cli_parser.py`: command-line interface definition
- `cli.py`: main entry wiring
- `cli_handlers.py`: runtime operations
- `cli_renderers.py`: text output helpers

### 3. Task layer

Files:

- `autodl_helper/tasks/keeper.py`
- `autodl_helper/tasks/scheduled_start.py`

Responsibilities:

- implement domain logic for keeper
- implement scheduled-start / grabbing logic

These modules are the core business logic of the project.

### 4. Runtime control layer

Files:

- `autodl_helper/runtime_control.py`
- `autodl_helper/lock.py`
- `autodl_helper/state.py`
- `autodl_helper/services/`

Responsibilities:

- daemon heartbeat
- launch state / launch fuse
- runtime task enable/disable flags
- config reload state
- local locking/state files
- service backend selection and lifecycle management

### 5. Storage layer

Files:

- `autodl_helper/storage.py`
- `autodl_helper/events.py`
- `autodl_helper/models.py`

Responsibilities:

- SQLite schema management
- history reads/writes
- event summaries
- shared result/history models

### 6. Auth and API layer

Files:

- `autodl_helper/api.py`
- `autodl_helper/auth.py`
- `autodl_helper/auth_login.py`
- `autodl_helper/auth_cache.py`
- `autodl_helper/auth_policy.py`
- `autodl_helper/auth_error_signals.py`

Responsibilities:

- AutoDL request/session operations
- login refresh flow
- auth cache reuse
- auth-related failure classification

### 7. Interactive terminal UI

Files:

- `autodl_helper/interactive_app.py`
- `autodl_helper/interactive_actions.py`
- `autodl_helper/interactive_runtime.py`
- `autodl_helper/interactive_views.py`
- `autodl_helper/interactive_models.py`

Responsibilities:

- terminal menu system
- live refresh loop
- snapshot/task manager
- page rendering
- interactive user actions

Current state:

- this part is feature-rich
- `interactive_app.py` is still large
- future refactoring should split page rendering by domain, but avoid risky large rewrites before release

---

## Runtime modes

## One-shot CLI

Examples:

- `run-keeper`
- `run-scheduled-start`
- `keeper-probe`
- `history`
- `healthcheck`

Use when:

- debugging
- manual runs
- smoke tests

## Foreground daemon

Command:

```bash
python main.py run-daemon --config config.yaml
```

Responsibilities:

- maintain heartbeat
- periodically run keeper
- periodically run scheduled-start checks
- keep writing runtime state into SQLite

## Interactive console

Command:

```bash
python main.py interactive --config config.yaml
```

Responsibilities:

- show current state
- trigger manual actions
- inspect daemon/service health
- browse keeper and scheduled-start progress

Important:

- interactive is a console/controller
- it is not the daemon itself

## Service backend mode

Long-running background execution is handled by the platform service backend selected by the service manager.

Current backends:

- macOS → LaunchAgent
- Linux → systemd user service
- Windows → Task Scheduler

This adds:

- service install/start/stop/restart/status
- one managed daemon process
- persistent local logs

---

## Persistence model

SQLite stores:

- keeper history
- scheduled-start history
- runtime control flags
- daemon heartbeat
- event logs
- per-job overrides

This means:

- interactive pages can recover state after restart
- daemon and interactive can communicate through local persisted state
- history and diagnosis remain local

---

## Data flow

## Keeper flow

1. load config
2. select account(s)
3. inspect instances
4. evaluate keeper window
5. skip / execute keeper action
6. write keeper history
7. update event/runtime state

## Scheduled-start flow

1. load config
2. compute active window from target time and advance hours
3. inspect fixed instance or selector candidates
4. decide result
   - outside window
   - waiting
   - started
   - failed
5. write scheduled history
6. update runtime state for daemon/interactive views

## Interactive flow

1. submit background snapshot task
2. collect task result into snapshot store
3. render page from snapshot + runtime rows
4. auto-refresh live pages
5. allow manual actions to enqueue work

---

## Repository structure

Current top-level layout:

```text
autodl_helper/   # package source
tests/           # pytest suite
docs/            # user/developer docs
scripts/         # helper scripts
autodl_helper/services/  # platform service backend helpers
```

Supporting files:

- `README.md`
- `CONTRIBUTING.md`
- `CHANGELOG.md`
- `pyproject.toml`
- `requirements.txt`

---

## What should stay stable

For open-source maintainability, these boundaries should remain clear:

- config parsing stays in `config.py`
- daemon/runtime control stays in `runtime_control.py`
- business logic stays in `tasks/`
- SQLite persistence stays in `storage.py`
- service backend selection and platform-specific lifecycle stay under `services/`
- terminal UI stays in `interactive_*`

Avoid mixing:

- UI rendering into storage
- AutoDL API logic into CLI parser
- SQLite schema logic into interactive pages

---

## Recommended next refactors

Not required immediately, but good medium-term cleanup targets:

1. split `interactive_app.py` by page domain
2. introduce dedicated formatting/helpers module for human-readable terminal values
3. add linting (`ruff`) and dev dependency separation
