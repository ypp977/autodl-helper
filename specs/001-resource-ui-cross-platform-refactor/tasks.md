# Tasks: resource, UI, and cross-platform refactor

## Phase 0 Tasks

- [x] Decide how to handle current uncommitted UI changes.
  Current UI precheck/menu changes are explicitly carried forward in the working tree.
- [x] Run baseline full test suite.
  Baseline on 2026-05-25: `PYTHONPATH=. /Users/yangpengpeng/miniconda3/envs/autodl-helper/bin/python -m pytest -q` -> 234 passed.
- [x] Run architecture tests.
  Baseline on 2026-05-25: architecture, layering, and service backend tests -> 24 passed.
- [x] Record current largest modules and risk areas.
  Largest modules remain CLI app, UI scheduled config/action/app, scheduled-start, Keeper, runtime, storage, and auth.
- [x] Identify daemon/UI paths that instantiate clients or hit APIs.
  Dashboard live Keeper rows were identified as a passive client creation path and replaced with local history reads.

## Phase 1 Tasks: UI

- [x] Ensure daemon submenu uses page redraw and notice feedback.
  Verified after menu split on 2026-05-25: UI/architecture focused tests 44 passed.
- [x] Ensure Keeper submenu uses page redraw and notice feedback.
  Verified after menu split on 2026-05-25: UI/architecture focused tests 44 passed.
- [x] Ensure account/config/scheduled flows use consistent return semantics.
  Config save uses saved/reload wording; scheduled job edits and top-level scheduled changes use draft wording until save.
- [x] Add Keeper details view for per-instance due/skip/failure reasons.
  First version uses SQLite keeper history and does not perform live API probes.
- [x] Add concise failure detail drill-down from summaries.
  Keeper failed run summaries point users to the history-backed Keeper details page.
- [x] Keep UI action code independent from CLI app entrypoint.
  `action_menus.py` is now a compatibility facade; daemon, Keeper, and account actions live in ownership-specific UI modules.

## Phase 2 Tasks: Resource Usage

- [x] Add tests proving passive dashboard render does not build clients for password-only accounts.
- [x] Add tests proving disabled Keeper does not build clients.
- [x] Add tests proving paused Keeper does not run task logic.
- [x] Add tests proving scheduled-start does not poll when disabled or no due jobs exist.
  Covered disabled scheduled-start and daemon dispatch interval gating.
- [x] Reduce UI dashboard live API calls or make them explicit/cached.
  Keeper dashboard now reads SQLite history only; explicit API work remains in Keeper execution and account health checks.
- [x] Audit auth refresh paths so browser login is never triggered by passive dashboard render.
  Passive dashboard rendering no longer creates AutoDL clients for token or password-only accounts.

## Phase 3 Tasks: Task Boundaries

- [x] Review Keeper task modules for remaining UI/CLI concerns.
  Keeper result/reason labels now live with Keeper task result helpers; UI/CLI consume the shared helpers.
- [x] Review scheduled-start module for extraction candidates.
  Scheduled-start result, reason, and candidate labels were extracted from CLI/task-local dictionaries into task-level helpers.
- [x] Normalize task failure labels/categories where output still differs.
  Keeper and scheduled-start labels have shared task-level entrypoints with focused coverage in `tests/tasks/test_result_labels.py`.
- [x] Keep result models stable for history and UI.
  No result dataclass or SQLite schema changes were needed; label normalization is presentation-only.

## Phase 4 Tasks: Cross-Platform

- [x] Verify service manager chooses the correct backend by platform.
  Covered by `tests/services/test_service_manager.py` for macOS, Linux, and Windows platform branches.
- [x] Keep launchd/systemd/Windows Task Scheduler tests green.
  Latest focused platform run includes service manager, launchd, systemd, and Windows Task Scheduler tests.
- [x] Review path handling for config-relative state, lock, data, and log files.
  Existing config-relative database/cache/lock/state/log behavior is covered by config, CLI launch, and service backend tests.
- [x] Avoid POSIX-only commands in Python runtime paths.
  PID existence checks and detached subprocess launch now use small platform-specific helpers outside service backends.

## Phase 5 Tasks: Docs

- [x] Update troubleshooting for Keeper precheck and resource usage.
  Troubleshooting now documents passive SQLite-backed Keeper dashboard behavior and current grouped commands.
- [x] Update command docs only if user-facing commands change.
  README/COMMANDS install and command references were compressed and aligned with current public commands.
- [x] Update architecture notes after module boundaries change.
  Architecture/development notes document UI action module ownership and task-level result label helpers.

## Always-On Tasks

- [x] Preserve user changes.
  Existing dirty worktree was carried forward; no unrelated file was reverted.
- [x] Use small commits by phase.
  Changes are organized by phase in the working tree; no commit was created because this session did not receive an explicit commit request.
- [x] Run focused tests before full tests.
  Latest focused run on 2026-05-25: CLI, UI config, and docs-adjacent checks -> 92 passed.
- [x] Do not mark the full refactor complete until every spec requirement is verified.
  Completion audit was performed after focused tests, architecture/service tests, docs updates, and full test suite passed.
