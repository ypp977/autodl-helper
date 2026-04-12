from __future__ import annotations

import argparse


def _add_common_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--config', default='config.yaml', help='Path to YAML config file')
    parser.add_argument('--headed', action='store_true', help='Use headed Playwright browser mode')
    parser.add_argument('--account', help='Only run against one configured account name')


def _add_path_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--state-file', default='.autodl-helper-state.json', help='Path to local state file')
    parser.add_argument('--lock-file', default='.autodl-helper.lock', help='Path to lock file')


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    _add_common_runtime_args(parser)
    parser.add_argument('--run-once', action='store_true', help='Run once and exit')
    _add_path_args(parser)


def _add_keeper_override_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--shutdown-release-after-hours', type=int, help='Override keeper shutdown release window in hours')
    parser.add_argument('--keeper-trigger-before-hours', type=int, help='Override keeper trigger window before release in hours')
    parser.add_argument('--start-cooldown-minutes', type=int, help='Override keeper start cooldown in minutes')
    parser.add_argument('--stop-cooldown-minutes', type=int, help='Override keeper stop cooldown in minutes')
    parser.add_argument('--fallback-to-status-at', dest='fallback_to_status_at', action='store_true', help='Enable status_at fallback for keeper')
    parser.add_argument('--no-fallback-to-status-at', dest='fallback_to_status_at', action='store_false', help='Disable status_at fallback for keeper')
    parser.set_defaults(fallback_to_status_at=None)


def _add_scheduled_override_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--scheduled-poll-interval', type=int, help='Override scheduled-start poll interval in seconds')
    parser.add_argument('--scheduled-job', help='Only use one scheduled-start job name (or instance_id) for this command')
    parser.add_argument('--target-time', help='Override target_time for the selected scheduled-start job')
    parser.add_argument('--advance-hours', type=int, help='Override advance_hours for the selected scheduled-start job')


def _add_auth_override_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--lightweight-mode', choices=['off', 'normal', 'aggressive'], help='Override account lightweight_mode')
    parser.add_argument('--runtime-auth-revalidate-seconds', type=int, help='Override runtime token revalidate window')
    parser.add_argument('--force-refresh-min-interval-seconds', type=int, help='Override Playwright force refresh minimum interval')
    parser.add_argument('--auth-failure-backoff-seconds', type=int, help='Override auth failure backoff window')


def _add_runtime_override_args(parser: argparse.ArgumentParser) -> None:
    _add_keeper_override_args(parser)
    _add_scheduled_override_args(parser)
    _add_auth_override_args(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='autodl-helper')
    subparsers = parser.add_subparsers(dest='command')
    subparsers.required = True

    run_daemon_parser = subparsers.add_parser('run-daemon', help='Run daemon tasks in foreground')
    _add_run_args(run_daemon_parser)
    _add_runtime_override_args(run_daemon_parser)

    run_all_parser = subparsers.add_parser('run-all', help='Compatibility alias for run-daemon')
    _add_run_args(run_all_parser)
    _add_runtime_override_args(run_all_parser)

    run_keeper_parser = subparsers.add_parser('run-keeper', aliases=['keep'], help='Run keep-alive only')
    _add_run_args(run_keeper_parser)
    _add_runtime_override_args(run_keeper_parser)

    run_scheduled_parser = subparsers.add_parser('run-scheduled-start', aliases=['grab'], help='Run scheduled-start only')
    _add_run_args(run_scheduled_parser)
    _add_runtime_override_args(run_scheduled_parser)

    service_install_parser = subparsers.add_parser('service-install', help='Install macOS LaunchAgent for run-daemon')
    service_install_parser.add_argument('--config', default='config.yaml', help='Path to YAML config file')

    service_start_parser = subparsers.add_parser('service-start', help='Start installed macOS LaunchAgent')
    service_start_parser.add_argument('--config', default='config.yaml', help='Path to YAML config file')

    service_stop_parser = subparsers.add_parser('service-stop', help='Stop installed macOS LaunchAgent')
    service_stop_parser.add_argument('--config', default='config.yaml', help='Path to YAML config file')

    service_restart_parser = subparsers.add_parser('service-restart', help='Restart installed macOS LaunchAgent')
    service_restart_parser.add_argument('--config', default='config.yaml', help='Path to YAML config file')

    service_status_parser = subparsers.add_parser('service-status', help='Show macOS LaunchAgent and daemon status')
    service_status_parser.add_argument('--config', default='config.yaml', help='Path to YAML config file')

    service_uninstall_parser = subparsers.add_parser('service-uninstall', help='Uninstall macOS LaunchAgent')
    service_uninstall_parser.add_argument('--config', default='config.yaml', help='Path to YAML config file')

    accounts_parser = subparsers.add_parser('accounts', help='Show configured account and login status')
    accounts_parser.add_argument('--config', default='config.yaml', help='Path to YAML config file')
    accounts_parser.add_argument('--account', help='Only show one configured account name')
    accounts_parser.add_argument('--json', action='store_true', help='Output JSON instead of text')

    login_parser = subparsers.add_parser('login', help='Refresh login/token for one or all accounts')
    _add_common_runtime_args(login_parser)
    login_parser.add_argument('--all', action='store_true', help='Refresh all enabled accounts')

    list_parser = subparsers.add_parser('list-instances', help='List AutoDL instances')
    _add_common_runtime_args(list_parser)
    list_parser.add_argument('--json', action='store_true', help='Output JSON instead of table')

    inspect_parser = subparsers.add_parser('inspect-instance', help='Show debug fields for one AutoDL instance')
    _add_common_runtime_args(inspect_parser)
    inspect_parser.add_argument('--instance-id', required=True, help='Target AutoDL instance UUID')

    watch_parser = subparsers.add_parser('watch-instance', help='Watch key instance fields continuously')
    _add_common_runtime_args(watch_parser)
    watch_parser.add_argument('--instance-id', required=True, help='Target AutoDL instance UUID')
    watch_parser.add_argument('--interval', type=int, default=5, help='Polling interval in seconds')
    watch_parser.add_argument('--json', action='store_true', help='Emit full JSON snapshot every poll')

    probe_parser = subparsers.add_parser('keeper-probe', help='Explain keeper timing for instances')
    _add_common_runtime_args(probe_parser)
    probe_parser.add_argument('--only-eligible', action='store_true', help='Only show instances meeting keeper conditions')

    history_parser = subparsers.add_parser('history', help='Show recent keeper/scheduled-start history from SQLite')
    _add_common_runtime_args(history_parser)
    history_parser.add_argument('--task', choices=['keeper', 'scheduled_start'], help='Filter by task type')
    history_parser.add_argument('--event-type', help='Filter by exact event_type, e.g. scheduled.started')
    history_parser.add_argument('--limit', type=int, default=20, help='Maximum rows to print')
    history_parser.add_argument('--json', action='store_true', help='Output JSON for troubleshooting')

    auth_report_parser = subparsers.add_parser('auth-report', help='Summarize observed auth failure signals from SQLite event log')
    _add_common_runtime_args(auth_report_parser)
    auth_report_parser.add_argument('--limit', type=int, default=50, help='Maximum grouped rows to print')
    auth_report_parser.add_argument('--json', action='store_true', help='Output JSON for troubleshooting')
    auth_report_parser.add_argument('--only-unmapped', action='store_true', help='Only show currently uncovered code/msg pairs')
    auth_report_parser.add_argument('--only-likely-auth', action='store_true', help='Only keep likely auth-related signals and filter obvious noise')
    auth_report_parser.add_argument('--suggest-patch', action='store_true', help='Generate suggested patch content for auth_error_signals.py')
    auth_report_parser.add_argument('--apply-suggested-patch', action='store_true', help='Apply suggested patch to auth_error_signals.py automatically')

    db_check_parser = subparsers.add_parser('db-check', help='Check SQLite schema and writability')
    _add_common_runtime_args(db_check_parser)

    healthcheck_parser = subparsers.add_parser('healthcheck', help='Run local operational checks')
    _add_common_runtime_args(healthcheck_parser)
    _add_path_args(healthcheck_parser)
    healthcheck_parser.add_argument('--smoke', action='store_true', help='Also perform auth and list-instances smoke test')

    notify_parser = subparsers.add_parser('test-notify', help='Send a test notification')
    notify_parser.add_argument('--config', default='config.yaml', help='Path to YAML config file')
    notify_parser.add_argument('--channel', choices=['pushplus', 'serverchan', 'email', 'all'], default='all', help='Notification channel to test')

    validate_parser = subparsers.add_parser('validate-config', help='Validate configuration only')
    validate_parser.add_argument('--config', default='config.yaml', help='Path to YAML config file')
    validate_parser.add_argument('--account', help='Only resolve one configured account name')
    _add_runtime_override_args(validate_parser)

    config_show_parser = subparsers.add_parser('config-show', help='Show loaded configuration from file/env')
    config_show_parser.add_argument('--config', default='config.yaml', help='Path to YAML config file')
    config_show_parser.add_argument('--account', help='Only show one configured account name')

    config_resolve_parser = subparsers.add_parser('config-resolve', help='Show effective configuration after CLI overrides')
    config_resolve_parser.add_argument('--config', default='config.yaml', help='Path to YAML config file')
    config_resolve_parser.add_argument('--account', help='Only resolve one configured account name')
    _add_runtime_override_args(config_resolve_parser)

    config_edit_parser = subparsers.add_parser('config-edit', help='Persist supported settings into config.yaml')
    config_edit_parser.add_argument('--config', default='config.yaml', help='Path to YAML config file')
    config_edit_parser.add_argument('--account', help='Only edit one configured account name')
    _add_runtime_override_args(config_edit_parser)

    interactive_parser = subparsers.add_parser('interactive', help='Launch interactive control panel')
    _add_common_runtime_args(interactive_parser)
    _add_path_args(interactive_parser)

    return parser
