# Checklist: resource, UI, and cross-platform refactor

## Governance Checklist

- [x] Impact scope assessed as cross-module.
- [x] Spec Kit required.
- [x] Spec created.
- [x] Plan created.
- [x] Tasks created.
- [x] Current dirty worktree resolved or explicitly carried forward.
  Carried forward: UI action/menu tests and Spec Kit files remain uncommitted by design.
- [x] Baseline tests captured after dirty worktree decision.
  2026-05-25 baseline: full suite 234 passed.

## Architecture Checklist

- [x] Core/runtime/tasks/services do not import CLI or UI.
- [x] UI does not import `autodl_helper.cli.app`.
- [x] CLI facade modules remain thin.
- [x] No wildcard imports under `autodl_helper`.
- [x] New shared logic lives below CLI/UI adapters.
  Runtime PID helpers and task result label helpers live under runtime/tasks; UI/CLI adapters consume them.

## Resource Checklist

- [x] Disabled tasks short-circuit before client/API creation.
  Covered by Keeper and scheduled-start runtime tests.
- [x] Paused Keeper short-circuits before task execution.
- [x] Dashboard live API calls are explicit, cached, or justified.
  Keeper dashboard uses local SQLite history; live API calls are explicit actions such as Keeper execution or account health checks.
- [x] Browser login is not triggered by passive dashboard rendering.
  Verified for password-only and token accounts by dashboard tests that fail on client creation.
- [x] Background dispatch cadence is predictable and avoids duplicate work.
  Covered by daemon dispatch interval gating test.

## UI Checklist

- [x] Main menu has clear groups.
  Main actions are grouped as 常用/配置/任务/系统 and covered by `test_run_ui_main_menu_uses_grouped_actions`.
- [x] Submenus redraw consistently.
  Keeper, daemon, account, config, and scheduled menus use page redraw with notices in focused UI tests.
- [x] Notices are shown consistently.
  Draft changes stay labeled as draft until saved; save success uses saved/reload wording.
- [x] Return behavior is consistent.
  Config returns after save, no-change paths do not mark dirty, and scheduled draft changes are saved through the config wizard.
- [x] Keeper details explain due/skip/failure states.
  Initial history-based details page shows account, instance, result, normalized reason, next keeper time, and release time.
- [x] Failure summaries are concise with optional detail path.
  Keeper failure summaries stay aggregated and point to Keeper details for drill-down.

## Cross-Platform Checklist

- [x] macOS LaunchAgent tests pass.
- [x] Linux systemd user service tests pass.
- [x] Windows Task Scheduler tests pass.
- [x] Runtime code avoids POSIX-only assumptions outside service backends.
  PID checks and detached background launch are centralized behind platform branches; focused runtime/CLI/service tests pass.
- [x] Paths are config-relative where documented.
  Config loader resolves database/cache paths from config dir; scheduled background lock/state and service logs are covered by tests.

## Verification Checklist

- [x] Focused tests pass for changed modules.
  Latest focused run: CLI + UI config + docs-adjacent checks, 92 passed.
- [x] `ruff check` passes for changed files.
- [x] `PYTHONPATH=. python -m pytest -q` passes.
  Latest run on 2026-05-25 with conda env `autodl-helper`: 248 passed.
- [x] Architecture tests pass.
- [x] User-visible docs updated where behavior changes.
  README, COMMANDS, TROUBLESHOOTING, DEVELOPMENT, and architecture guardrails reflect the updated UI/resource behavior.
