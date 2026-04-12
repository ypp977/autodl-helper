# Changelog

All notable changes to this project will be documented in this file.

## [2026-04-07] autodl-helper Operations And Selector Release

### Added

- Added auth token cache support with local cache file, cache TTL, startup cache reuse, and cache refresh after login.
- Added `run-all`, `run-keeper`, `run-scheduled-start`, `watch-instance`, and `healthcheck` commands.
- Added selector-based scheduled-start jobs with exact filters for region, GPU model, GPU count, and charge type.
- Added explicit priority ordering for multi-candidate selector jobs.

### Changed

- Switched scheduled-start runtime output from plain strings to structured results with `result` and `reason`.
- Upgraded scheduled-start notifications to include instance status, `gpu_idle_num`, `start_mode`, deadline, and selector metadata.
- Updated example configuration and README to document selector jobs, token cache, watch mode, healthcheck, and new daemon commands.
- Changed Docker default command to `python main.py run-all`.

### Fixed

- Refreshes auth on HTTP auth failures and business-layer auth failures, then retries the current request once.
- Debug/ops commands no longer depend on unrelated scheduled-start validation.

## [2026-04-07] autodl-helper Productization Release

### Added

- Added a modular `autodl_helper/` package with dedicated auth, API, config, lock, state, notify, and task modules.
- Added multi-instance scheduled-start support with independent per-job execution and deduplicated notifications.
- Added operational subcommands: `run`, `list-instances`, `test-notify`, and `validate-config`.
- Added JSON/table instance listing and notification smoke-test support.
- Added configuration validation for auth, scheduled-start jobs, and notification backends.
- Added test coverage for CLI commands, notification isolation, multi-job runtime behavior, and scheduled-start deduplication.

### Changed

- Renamed the project from `autodl-keeper` to `autodl-helper` in docs and packaging-facing surfaces.
- Switched CLI usage to subcommands instead of flat flags.
- Updated example configuration to show real multi-job scheduled-start usage.

### Fixed

- Isolated notifier failures so one broken notification channel no longer blocks the rest.
- Logged per-job scheduled-start results during daemon runs for easier operations debugging.
- Aligned Docker runtime entrypoint with the current CLI.

## [2026-04-03] Maintenance And Security Update

### Added

- Added `.dockerignore` to avoid packaging local secrets, Git metadata, and transient files into Docker build contexts.
- Added `CHANGELOG.md` and `RELEASE_NOTES.md` to keep repository history and release summaries easier to review.
- Added a safer Docker image workflow mode where pull requests only validate builds and main-branch runs publish images.

### Changed

- Refreshed `README.md` to match the current single-file implementation and runtime behavior.
- Updated Docker runtime setup to use `python:3.11-slim` and run the app directly with `python main.py`.

### Fixed

- Moved Playwright imports to runtime so `python main.py --help` works even when Playwright is not installed yet.
- Installed Chromium during Docker image builds so Playwright-based login can work inside containers.
- Removed the brittle shell-based `.env` export step from Docker startup.
- Updated the GitHub Actions Docker workflow to current action versions and removed unsupported legacy image targets.

### Security

- Upgraded Python dependencies to current safe versions.
- Pinned `urllib3==2.6.3` to enforce a non-vulnerable resolver outcome alongside `requests==2.33.1`.
