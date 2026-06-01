# Spec: resource, UI, and cross-platform refactor

## Status

Draft for implementation planning. This refactor is high impact and must be executed in phases.

## Problem

`autodl-helper` has grown across CLI, terminal UI, daemon runtime, Keeper, scheduled-start,
storage, auth, and platform service modules. Recent improvements fixed specific UI and Keeper
issues, but the project still needs a broader refactor to reduce runtime resource usage, simplify
interaction flows, and preserve first-class support on macOS, Linux, and Windows.

The refactor must avoid heavy defensive code. The target is simpler boundaries, lower idle cost,
clearer user flows, and small testable units.

## Goals

- Reduce idle resource usage for daemon and UI paths.
- Make terminal UI interaction consistent, discoverable, and low-noise.
- Keep CLI and UI as adapters; keep business rules in reusable lower layers.
- Preserve cross-platform service support:
  - macOS LaunchAgent
  - Linux systemd user service
  - Windows Task Scheduler
- Keep code elegant and direct, with minimal defensive wrappers.
- Improve failure diagnosis for Keeper, scheduled-start, auth, and service operations.
- Maintain or strengthen current architecture tests.

## Non-Goals

- No GUI desktop app.
- No Docker-only redesign.
- No platform-specific rewrite that breaks another platform.
- No large dependency additions unless they directly reduce complexity or resource usage.
- No broad style-only churn.
- No changing AutoDL business behavior without tests that prove compatibility.

## Current Evidence

- Existing architecture guardrails are documented in `docs/architecture-slimming.md`.
- Existing cross-platform service backends:
  - `autodl_helper/services/launchd.py`
  - `autodl_helper/services/systemd.py`
  - `autodl_helper/services/windows_task.py`
- Largest modules by size include:
  - `autodl_helper/cli/app.py`
  - `autodl_helper/ui/scheduled_config.py`
  - `autodl_helper/tasks/scheduled_start.py`
  - `autodl_helper/ui/action_menus.py`
  - `autodl_helper/cli/commands/runtime.py`
  - `autodl_helper/ui/app.py`
- Current worktree has pre-existing uncommitted UI changes:
  - `autodl_helper/ui/action_menus.py`
  - `tests/ui/test_ui.py`

## Requirements

### Resource Usage

- Daemon must not do unnecessary API calls when tasks are disabled, paused, or not due.
- UI dashboard must avoid expensive live API probes unless needed for the current view.
- Background loops must use a single predictable dispatch cadence and avoid duplicate polling.
- Expensive auth/browser login paths must only run when explicitly needed.

### UI and Interaction

- All submenus must use consistent page-style redraw and notice feedback.
- Main menu, Keeper, daemon, account, config, and scheduled-start flows must share return semantics.
- Keeper operations must precheck config, pause state, and account readiness before execution.
- Failure summaries must be concise by default, with a path to details.
- Config editing must distinguish draft changes from saved effective settings.

### Cross-Platform

- Service lifecycle behavior must remain covered for macOS, Linux, and Windows.
- Runtime path handling must remain relative to config location where applicable.
- Avoid shell-only assumptions in Python runtime code.
- Platform-specific code must remain isolated under `autodl_helper/services`.

### Architecture

- `core`, `runtime`, `tasks`, and `services` must not import CLI or UI.
- CLI and UI should delegate to shared application/task functions.
- Large modules should be split only when the split creates clearer ownership.
- Compatibility facades must remain thin.

### Testing and Verification

- Every behavior change needs focused tests.
- Refactor-only moves require import and regression tests.
- Full verification gate:
  - `python -m ruff check ...` for changed files
  - `PYTHONPATH=. python -m pytest -q`
  - architecture tests
  - service backend tests

## Risk Assessment

- High risk: daemon scheduling, auth refresh, platform service install/start/stop.
- Medium risk: UI menu flow, config edit/reload, Keeper execution.
- Medium risk: scheduled-start matching and polling behavior.
- Low risk: documentation-only changes and pure renderer extraction.

## Spec Kit Decision

Spec Kit is required. The requested scope is cross-module, cross-platform, and performance-sensitive.
Implementation must proceed from this spec through plan, tasks, and checklist gates.
