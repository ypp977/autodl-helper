# Plan: resource, UI, and cross-platform refactor

## Phase 0: Baseline and Governance

1. Preserve current uncommitted work or commit it before large refactors.
2. Capture baseline verification:
   - full tests
   - architecture tests
   - service backend tests
3. Map hot paths:
   - daemon dispatch
   - UI dashboard rendering
   - Keeper execution
   - scheduled-start execution
   - auth refresh and browser login

## Phase 1: UI Interaction Cleanup

1. Finish consistent submenu redraw and notice behavior.
2. Add Keeper precheck and detail flows.
3. Split UI rendering from UI actions where it reduces coupling.
4. Keep terminal UI dense and operational, not decorative.

Verification:

- UI tests for page redraw, notices, return behavior, and Keeper precheck.
- Manual smoke: `autodl-helper ui --config config.yaml`.

## Phase 2: Runtime Resource Reduction

1. Audit daemon loops for duplicate work.
2. Ensure disabled/paused/not-due tasks short-circuit before API work.
3. Make live dashboard probing opt-in or cached where safe.
4. Keep browser/auth refresh out of idle paths.

Verification:

- Runtime tests for dispatch cadence and task gating.
- Focused tests that assert no client/API creation when disabled or paused.

## Phase 3: Task Logic Boundaries

1. Keep Keeper timing/result rules in task modules, not UI/CLI.
2. Extract scheduled-start selection and result formatting only where ownership is clear.
3. Normalize failure categories across Keeper and scheduled-start.

Verification:

- Keeper tests.
- Scheduled-start tests.
- CLI/UI output tests for summaries.

## Phase 4: Cross-Platform Service Hardening

1. Keep platform-specific code isolated in service backends.
2. Verify service install/start/status/stop semantics for:
   - launchd
   - systemd user service
   - Windows Task Scheduler
3. Avoid adding runtime shell assumptions outside service modules.

Verification:

- `tests/services/test_service_launchd.py`
- `tests/services/test_service_systemd.py`
- `tests/services/test_service_windows_task.py`

## Phase 5: Documentation and Release Readiness

1. Update concise install/run docs only where commands change.
2. Update troubleshooting for resource usage, Keeper precheck, and account health.
3. Keep docs short and command-focused.

## Quality Gates

Before each phase is considered complete:

- No unrelated git churn.
- Focused tests pass.
- Full test suite passes.
- Changed behavior is documented if user-visible.
- Work remains cross-platform by construction or by tests.
