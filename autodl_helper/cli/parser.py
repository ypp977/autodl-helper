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


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--config', default='config.yaml', help='Path to YAML config file')


def _add_service_command(subparsers: argparse._SubParsersAction, name: str, help_text: str) -> None:
    command_parser = subparsers.add_parser(name, help=help_text)
    _add_config_arg(command_parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='autodl-helper')
    subparsers = parser.add_subparsers(dest='command')
    subparsers.required = True

    init_parser = subparsers.add_parser('init', help='Bootstrap local .env and config.yaml for first run')
    _add_config_arg(init_parser)
    init_parser.add_argument('--force', action='store_true', help='Overwrite existing local bootstrap files')
    init_parser.add_argument('--yes', action='store_true', help='Accept defaults without interactive prompts')

    login_parser = subparsers.add_parser('login', help='Refresh login/token for one or all accounts')
    _add_common_runtime_args(login_parser)
    login_parser.add_argument('--all', action='store_true', help='Refresh all enabled accounts')

    accounts_parser = subparsers.add_parser('accounts', help='Show configured account auth status')
    _add_config_arg(accounts_parser)
    accounts_parser.add_argument('--account', help='Only show one configured account name')
    accounts_parser.add_argument('--json', action='store_true', help='Output JSON instead of table')

    list_parser = subparsers.add_parser('list', help='List AutoDL instances')
    _add_common_runtime_args(list_parser)
    list_parser.add_argument('--json', action='store_true', help='Output JSON instead of table')

    run_parser = subparsers.add_parser('run', help='Run foreground tasks')
    run_subparsers = run_parser.add_subparsers(dest='run_command')
    run_subparsers.required = True
    for name, mode, help_text in (
        ('daemon', 'all', 'Run daemon tasks in foreground'),
        ('keeper', 'keeper', 'Run keep-alive only'),
        ('scheduled', 'scheduled_start', 'Run scheduled-start only'),
    ):
        command_parser = run_subparsers.add_parser(name, help=help_text)
        command_parser.set_defaults(run_mode=mode)
        _add_run_args(command_parser)
        _add_runtime_override_args(command_parser)

    service_parser = subparsers.add_parser('service', help='Manage background service')
    service_subparsers = service_parser.add_subparsers(dest='service_command')
    service_subparsers.required = True
    _add_service_command(service_subparsers, 'install', 'Install background service for run daemon')
    _add_service_command(service_subparsers, 'start', 'Start installed background service')
    _add_service_command(service_subparsers, 'stop', 'Stop installed background service')
    _add_service_command(service_subparsers, 'restart', 'Restart installed background service')
    _add_service_command(service_subparsers, 'status', 'Show background service and daemon status')
    _add_service_command(service_subparsers, 'uninstall', 'Uninstall background service')

    ui_parser = subparsers.add_parser('ui', help='Launch interactive control panel')
    _add_common_runtime_args(ui_parser)
    _add_path_args(ui_parser)

    debug_parser = subparsers.add_parser('debug', help='Run diagnostic commands')
    debug_subparsers = debug_parser.add_subparsers(dest='debug_command')
    debug_subparsers.required = True

    health_parser = debug_subparsers.add_parser('health', help='Run local operational checks')
    _add_common_runtime_args(health_parser)
    _add_path_args(health_parser)
    health_parser.add_argument('--smoke', action='store_true', help='Also perform auth and instance list smoke test')
    health_parser.add_argument('--json', action='store_true', help='Output JSON status/error envelope')

    db_parser = debug_subparsers.add_parser('db', help='Check SQLite schema and writability')
    _add_common_runtime_args(db_parser)
    db_parser.add_argument('--json', action='store_true', help='Output JSON status/error envelope')

    auth_parser = debug_subparsers.add_parser('auth', help='Summarize observed auth failure signals from SQLite event log')
    _add_common_runtime_args(auth_parser)
    auth_parser.add_argument('--limit', type=int, default=50, help='Maximum grouped rows to print')
    auth_parser.add_argument('--json', action='store_true', help='Output JSON for troubleshooting')
    auth_parser.add_argument('--only-unmapped', action='store_true', help='Only show currently uncovered code/msg pairs')
    auth_parser.add_argument('--only-likely-auth', action='store_true', help='Only keep likely auth-related signals and filter obvious noise')
    auth_parser.add_argument('--suggest-patch', action='store_true', help='Generate suggested patch content for auth_error_signals.py')
    auth_parser.add_argument('--apply-suggested-patch', action='store_true', help='Deprecated unsafe option; use --suggest-patch and apply manually')

    history_parser = debug_subparsers.add_parser('history', help='Show recent keeper/scheduled-start history from SQLite')
    _add_common_runtime_args(history_parser)
    history_parser.add_argument('--task', choices=['keeper', 'scheduled_start'], help='Filter by task type')
    history_parser.add_argument('--event-type', help='Filter by exact event_type, e.g. scheduled.started')
    history_parser.add_argument('--limit', type=int, default=20, help='Maximum rows to print')
    history_parser.add_argument('--json', action='store_true', help='Output JSON for troubleshooting')

    config_parser = subparsers.add_parser('config', help='Inspect and validate configuration')
    config_subparsers = config_parser.add_subparsers(dest='config_command')
    config_subparsers.required = True

    config_show_parser = config_subparsers.add_parser('show', help='Show loaded configuration from file/env')
    _add_config_arg(config_show_parser)
    config_show_parser.add_argument('--account', help='Only show one configured account name')
    config_show_parser.add_argument('--json', action='store_true', help='Output JSON and JSON errors')

    config_validate_parser = config_subparsers.add_parser('validate', help='Validate configuration only')
    _add_config_arg(config_validate_parser)
    config_validate_parser.add_argument('--account', help='Only resolve one configured account name')
    config_validate_parser.add_argument('--json', action='store_true', help='Output JSON status/error envelope')
    _add_runtime_override_args(config_validate_parser)

    return parser
