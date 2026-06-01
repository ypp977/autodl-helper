from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence, TextIO

from autodl_helper.core.auth import AuthError, alert_auth_failure, resolve_authorization
from autodl_helper.core.auth import AUTH_CODE_SIGNALS, AUTH_MESSAGE_SIGNALS
from .shared import (
    build_named_notifiers as _build_named_notifiers_impl,
    build_notifiers as _build_notifiers_impl,
    build_client as _build_client_impl,
    collect_healthcheck_errors as _collect_healthcheck_errors_impl,
    compute_cycle_interval_seconds as _compute_cycle_interval_seconds_impl,
    compute_dispatch_interval_seconds as _compute_dispatch_interval_seconds_impl,
    compute_interval_for_mode as _compute_interval_for_mode_impl,
    create_client as _create_client_impl,
    create_store as _create_store_impl,
    get_enabled_accounts as _get_enabled_accounts_impl,
    probe_path_writable as _probe_path_writable_impl,
    record_auth_event as _record_auth_event_impl,
    select_accounts as _select_accounts_impl,
    validate_settings as _validate_settings_impl,
    apply_cli_overrides as _apply_cli_overrides_impl,
)
from .commands import (
    command_interactive as _command_interactive_impl,
    command_auth_report as _command_auth_report_impl,
    command_accounts as _command_accounts_impl,
    command_config_resolve as _command_config_resolve_impl,
    command_config_edit as _command_config_edit_impl,
    command_config_show as _command_config_show_impl,
    command_db_check as _command_db_check_impl,
    command_healthcheck as _command_healthcheck_impl,
    command_init as _command_init_impl,
    command_service_install as _command_service_install_impl,
    command_service_restart as _command_service_restart_impl,
    command_service_start as _command_service_start_impl,
    command_service_status as _command_service_status_impl,
    command_service_stop as _command_service_stop_impl,
    command_service_uninstall as _command_service_uninstall_impl,
    command_history as _command_history_impl,
    command_inspect_instance as _command_inspect_instance_impl,
    command_keeper_probe as _command_keeper_probe_impl,
    command_login as _command_login_impl,
    command_list_instances as _command_list_instances_impl,
    command_run_variant as _command_run_variant_impl,
    command_test_notify as _command_test_notify_impl,
    command_validate_config as _command_validate_config_impl,
    command_watch_instance as _command_watch_instance_impl,
    run_cycle as _run_cycle_impl,
    run_keeper_only as _run_keeper_only_impl,
    run_scheduled_start_cycle as _run_scheduled_start_cycle_impl,
    watch_instance as _watch_instance_impl,
)
from .parser import build_parser as _build_parser_impl
from .renderers import (
    apply_auth_signal_patch as _apply_auth_signal_patch_impl,
    auth_report_match_label as _auth_report_match_label_impl,
    auth_report_row_to_json as _auth_report_row_to_json_impl,
    build_auth_signal_patch as _build_auth_signal_patch_impl,
    extract_instance_time as _extract_instance_time_impl,
    extract_watch_fields as _extract_watch_fields_impl,
    format_instances_table as _format_instances_table_impl,
    format_history_table as _format_history_table_impl,
    format_keeper_probe_line as _format_keeper_probe_line_impl,
    format_watch_change as _format_watch_change_impl,
    history_row_to_json as _history_row_to_json_impl,
    history_subject as _history_subject_impl,
    history_summary as _history_summary_impl,
    likely_auth_candidate as _likely_auth_candidate_impl,
    normalize_auth_signal_literal as _normalize_auth_signal_literal_impl,
    normalize_instance as _normalize_instance_impl,
    normalize_instance_debug as _normalize_instance_debug_impl,
    probe_reason_label as _probe_reason_label_impl,
    probe_result_label as _probe_result_label_impl,
    release_source_label as _release_source_label_impl,
    render_auth_signal_patch as _render_auth_signal_patch_impl,
    render_python_signal_block as _render_python_signal_block_impl,
    replace_python_signal_block as _replace_python_signal_block_impl,
)
from autodl_helper.core.config import AccountSettings, LIGHTWEIGHT_MODES, NotificationSettings, Settings, load_settings
from autodl_helper.lock import FileLock
from autodl_helper.core.models import AuthEventSummary, HistoryRecord, KeeperResult, ScheduledStartResult
from autodl_helper.runtime_control import (
    DAEMON_LAUNCH_FUSE_AFTER_FAILURES,
    DAEMON_LAUNCH_FUSE_COOLDOWN_SECONDS,
    DAEMON_LAUNCH_STARTING_TTL_SECONDS,
    claim_daemon_launch,
    clear_daemon_heartbeat,
    clear_daemon_launch_state,
    mark_daemon_heartbeat,
    mark_daemon_launch_failure,
    mark_daemon_launch_running,
    read_config_reload_status,
    read_daemon_launch_status,
    read_daemon_status,
    request_config_reload,
)
from autodl_helper.runtime.pid import terminate_pid
from autodl_helper.core.store import SQLiteStore
from autodl_helper.tasks.keeper import evaluate_keeper_instance, run_keeper_cycle
from autodl_helper.tasks.scheduled_start import run_scheduled_start_job
from autodl_helper.tracemalloc_profiler import profiler_from_env


def _detached_popen_kwargs() -> dict[str, object]:
    if os.name == 'nt':
        return {'creationflags': getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x00000200)}
    return {'start_new_session': True}

logger = logging.getLogger(__name__)

RESET = '\033[0m'
CYAN = '\033[38;5;80m'
GREEN = '\033[38;5;114m'
YELLOW = '\033[38;5;179m'
RED = '\033[38;5;174m'


class ColorLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        text = record.getMessage()
        if '[后台轮询]' in text:
            return f'{CYAN}{message}{RESET}'
        if record.levelno >= logging.ERROR:
            return f'{RED}{message}{RESET}'
        if '结果=已开机' in text or '结果=已在运行' in text or '结果=已提交开机' in text or '结果=已执行保活' in text:
            return f'{GREEN}{message}{RESET}'
        if '[抢机检查]' in text or '[Keeper检查]' in text:
            return f'{YELLOW}{message}{RESET}'
        return message


def _configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', stream=sys.stdout, force=True)
    root_logger = logging.getLogger()
    formatter = ColorLogFormatter('%(asctime)s - %(levelname)s - %(message)s')
    for handler in root_logger.handlers:
        handler.setFormatter(formatter)
    logging.getLogger('apscheduler').setLevel(logging.WARNING)
AUTH_ERROR_SIGNALS_FILE = Path(__file__).resolve().parent.parent / 'auth' / 'errors.py'




def request_reload(store: SQLiteStore):
    return request_config_reload(store)

def build_parser():
    return _build_parser_impl()


def build_named_notifiers(notifications: NotificationSettings):
    return _build_named_notifiers_impl(notifications)


def build_notifiers(notifications: NotificationSettings):
    return _build_notifiers_impl(notifications)


def get_enabled_accounts(settings: Settings) -> list[AccountSettings]:
    return _get_enabled_accounts_impl(settings)


def select_accounts(settings: Settings, account_name: str | None = None, *, require_explicit_for_multi: bool = False):
    return _select_accounts_impl(
        settings,
        account_name,
        require_explicit_for_multi=require_explicit_for_multi,
        get_enabled_accounts_fn=get_enabled_accounts,
    )


def create_store(settings: Settings) -> SQLiteStore:
    return _create_store_impl(settings, store_cls=SQLiteStore, get_enabled_accounts_fn=get_enabled_accounts)


def _resolve_runtime_path(path_value: str | Path, *, config_path: str | Path) -> str:
    path = Path(path_value)
    if path.is_absolute():
        return str(path)
    return str((Path(config_path).resolve().parent / path).resolve())


def start_background_scheduled_polling(args) -> tuple[int, str]:
    main_py = Path(__file__).resolve().parent.parent.parent / 'main.py'
    config_path = str(Path(args.config).resolve())
    lock_path = _resolve_runtime_path(args.lock_file, config_path=config_path)
    state_path = _resolve_runtime_path(args.state_file, config_path=config_path)
    settings = load_settings(args.config)
    store = create_store(settings)
    account_name = getattr(args, 'account', None)
    launch_status = read_daemon_launch_status(store, starting_ttl_seconds=DAEMON_LAUNCH_STARTING_TTL_SECONDS)
    if launch_status.get('launch_state') == 'running' and launch_status.get('launch_pid'):
        return 0, f'pid={launch_status["launch_pid"]} (already running)'
    if launch_status.get('launch_state') == 'starting':
        return 0, '后台轮询启动中'
    if launch_status.get('launch_state') == 'fused':
        return 1, '后台轮询启动已熔断，请稍后重试'
    claim = claim_daemon_launch(store, account=account_name, starting_ttl_seconds=DAEMON_LAUNCH_STARTING_TTL_SECONDS)
    if not claim.get('claimed'):
        state = claim.get('launch_state')
        if state == 'running' and claim.get('launch_pid'):
            return 0, f'pid={claim["launch_pid"]} (already running)'
        if state == 'starting':
            return 0, '后台轮询启动中'
        if state == 'fused':
            return 1, '后台轮询启动已熔断，请稍后重试'

    cmd = [
        sys.executable,
        str(main_py),
        'run',
        'scheduled',
        '--config',
        config_path,
        '--lock-file',
        lock_path,
        '--state-file',
        state_path,
    ]
    if account_name:
        cmd.extend(['--account', account_name])
    if getattr(args, 'headed', False):
        cmd.append('--headed')
    env = os.environ.copy()
    env['AUTODL_HELPER_DAEMON_ORIGIN'] = str(getattr(args, 'daemon_origin', None) or 'interactive-auto')
    with tempfile.NamedTemporaryFile(prefix='autodl-helper-scheduled-', suffix='.log', delete=False) as stderr_log:
        stderr_path = Path(stderr_log.name)
    stderr_handle = open(stderr_path, 'wb')
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr_handle,
            cwd=str(Path(config_path).resolve().parent),
            env=env,
            **_detached_popen_kwargs(),
        )
    except Exception as exc:
        stderr_handle.close()
        mark_daemon_launch_failure(
            store,
            account=account_name,
            error=str(exc),
            fuse_after_failures=DAEMON_LAUNCH_FUSE_AFTER_FAILURES,
            cooldown_seconds=DAEMON_LAUNCH_FUSE_COOLDOWN_SECONDS,
        )
        return 1, str(exc)
    stderr_handle.close()
    deadline = time.time() + 2.0
    while time.time() < deadline:
        status = read_daemon_status(store)
        if status.get('running') and status.get('pid') == proc.pid:
            mark_daemon_launch_running(store, account=account_name, pid=proc.pid)
            return 0, f'pid={proc.pid}'
        if proc.poll() is not None:
            detail = '后台轮询启动失败'
            try:
                text = stderr_path.read_text(encoding='utf-8').strip()
                if text:
                    detail = text.splitlines()[-1]
            except Exception:
                pass
            mark_daemon_launch_failure(
                store,
                account=account_name,
                error=detail,
                fuse_after_failures=DAEMON_LAUNCH_FUSE_AFTER_FAILURES,
                cooldown_seconds=DAEMON_LAUNCH_FUSE_COOLDOWN_SECONDS,
            )
            return proc.returncode or 1, detail
        time.sleep(0.1)
    return 0, f'pid={proc.pid} (starting)'


def stop_background_polling(settings: Settings, store: SQLiteStore) -> tuple[int, str]:
    status = read_daemon_status(store)
    pid = status.get('pid')
    if not pid:
        clear_daemon_heartbeat(store)
        clear_daemon_launch_state(store)
        return 1, '未找到后台轮询进程'
    try:
        terminate_pid(pid)
    except ProcessLookupError:
        clear_daemon_heartbeat(store)
        clear_daemon_launch_state(store)
        return 0, '后台轮询已停止'
    except PermissionError:
        return 1, f'无权限停止进程 pid={pid}'
    deadline = time.time() + 2.0
    while time.time() < deadline:
        current = read_daemon_status(store)
        if not current.get('running'):
            clear_daemon_launch_state(store)
            return 0, f'pid={pid}'
        time.sleep(0.1)
    clear_daemon_heartbeat(store)
    clear_daemon_launch_state(store)
    return 0, f'pid={pid}'


def _record_auth_event(store: SQLiteStore | None, account_name: str, payload: dict[str, object]) -> None:
    _record_auth_event_impl(store, account_name, payload)


def create_client(settings: Settings, headed: bool, account: AccountSettings | None = None, store: SQLiteStore | None = None):
    return _create_client_impl(
        settings,
        headed,
        account=account,
        store=store,
        get_enabled_accounts_fn=get_enabled_accounts,
        resolve_authorization_fn=resolve_authorization,
    )


def _build_client(settings: Settings, headed: bool, account: AccountSettings | None = None, store: SQLiteStore | None = None):
    return _build_client_impl(settings, headed, account=account, store=store, create_client_fn=create_client)


def _resolve_cli_symbol(name: str, fallback):
    package = sys.modules.get('autodl_helper.cli')
    if package is not None:
        target = getattr(package, name, None)
        if target is not None and target is not fallback:
            return target
    return fallback


def build_client(settings: Settings, headed: bool, account: AccountSettings | None = None, store: SQLiteStore | None = None):
    create_client_fn = _resolve_cli_symbol('create_client', create_client)
    return _build_client_impl(settings, headed, account=account, store=store, create_client_fn=create_client_fn)


def compute_cycle_interval_seconds(settings: Settings) -> int:
    return _compute_cycle_interval_seconds_impl(settings)


def compute_dispatch_interval_seconds(settings: Settings) -> int:
    return _compute_dispatch_interval_seconds_impl(settings)


def compute_interval_for_mode(settings: Settings, mode: str) -> int:
    return _compute_interval_for_mode_impl(settings, mode)


def normalize_instance(item: dict[str, object], *, account_name: str = '') -> dict[str, object]:
    return _normalize_instance_impl(item, account_name=account_name)


def _extract_instance_time(item: dict[str, object], field_name: str) -> str:
    return _extract_instance_time_impl(item, field_name)


def normalize_instance_debug(item: dict[str, object], keeper_settings=None, *, account_name: str = '') -> dict[str, object]:
    return _normalize_instance_debug_impl(item, keeper_settings=keeper_settings, account_name=account_name)


def extract_watch_fields(item: dict[str, object], keeper_settings=None) -> dict[str, object]:
    return _extract_watch_fields_impl(item, keeper_settings=keeper_settings)


def format_watch_change(snapshot: dict[str, object]) -> str:
    return _format_watch_change_impl(snapshot)


def format_instances_table(instances: list[dict[str, object]]) -> str:
    return _format_instances_table_impl(instances)


def _release_source_label(value: str) -> str:
    return _release_source_label_impl(value)


def _probe_result_label(value: str) -> str:
    return _probe_result_label_impl(value)


def _probe_reason_label(value: str) -> str:
    return _probe_reason_label_impl(value)


def format_keeper_probe_line(result: KeeperResult, *, account_name: str = '', executed_in_cycle: bool = False) -> str:
    return _format_keeper_probe_line_impl(result, account_name=account_name, executed_in_cycle=executed_in_cycle)


def validate_settings(settings: Settings, purpose: str = 'all') -> list[str]:
    return _validate_settings_impl(
        settings,
        purpose=purpose,
        get_enabled_accounts_fn=get_enabled_accounts,
        lightweight_modes=LIGHTWEIGHT_MODES,
    )


def apply_cli_overrides(args, settings: Settings) -> Settings:
    return _apply_cli_overrides_impl(args, settings)


def run_scheduled_start_cycle(
    *,
    settings: Settings,
    headed: bool,
    state_file: str | Path,
    account_name: str | None = None,
    force_run_now: bool = False,
    store: SQLiteStore | None = None,
) -> list[ScheduledStartResult]:
    return _run_scheduled_start_cycle_impl(
        settings=settings,
        headed=headed,
        state_file=state_file,
        account_name=account_name,
        force_run_now=force_run_now,
        store=store,
        create_store_fn=create_store,
        select_accounts_fn=select_accounts,
        get_enabled_accounts_fn=get_enabled_accounts,
        build_client_fn=_resolve_cli_symbol('build_client', build_client),
        run_scheduled_start_job_fn=_resolve_cli_symbol('run_scheduled_start_job', run_scheduled_start_job),
        build_notifiers_fn=build_notifiers,
    )


def run_keeper_only(*, settings: Settings, headed: bool, account_name: str | None = None, store: SQLiteStore | None = None) -> list[KeeperResult]:
    return _run_keeper_only_impl(
        settings=settings,
        headed=headed,
        account_name=account_name,
        store=store,
        create_store_fn=create_store,
        select_accounts_fn=select_accounts,
        build_client_fn=_resolve_cli_symbol('build_client', build_client),
        run_keeper_cycle_fn=_resolve_cli_symbol('run_keeper_cycle', run_keeper_cycle),
        build_notifiers_fn=build_notifiers,
    )


def run_cycle(*, settings: Settings, headed: bool, state_file: str | Path, account_name: str | None = None) -> list[ScheduledStartResult]:
    return _run_cycle_impl(
        settings=settings,
        headed=headed,
        state_file=state_file,
        account_name=account_name,
        create_store_fn=create_store,
        run_keeper_only_fn=run_keeper_only,
        run_scheduled_start_cycle_fn=run_scheduled_start_cycle,
    )


def watch_instance(*, client, keeper_settings=None, instance_id: str, interval_seconds: int, json_output: bool, output: TextIO, sleep_fn=time.sleep, max_iterations: int | None = None, account_name: str = '') -> int:
    return _watch_instance_impl(
        client=client,
        keeper_settings=keeper_settings,
        instance_id=instance_id,
        interval_seconds=interval_seconds,
        json_output=json_output,
        output=output,
        sleep_fn=sleep_fn,
        max_iterations=max_iterations,
        account_name=account_name,
        normalize_instance_debug_fn=normalize_instance_debug,
        extract_watch_fields_fn=extract_watch_fields,
        format_watch_change_fn=format_watch_change,
    )


def _probe_path_writable(path: str | Path) -> bool:
    return _probe_path_writable_impl(path)


def collect_healthcheck_errors(*, settings: Settings, state_file: str | Path, lock_file: str | Path, smoke: bool, headed: bool, permission_probe=_probe_path_writable) -> list[str]:
    return _collect_healthcheck_errors_impl(
        settings=settings,
        state_file=state_file,
        lock_file=lock_file,
        smoke=smoke,
        headed=headed,
        permission_probe=permission_probe,
        validate_settings_fn=validate_settings,
        get_enabled_accounts_fn=get_enabled_accounts,
        create_store_fn=create_store,
        build_client_fn=_resolve_cli_symbol('build_client', build_client),
    )


def _history_subject(row: HistoryRecord) -> str:
    return _history_subject_impl(row)


def _history_summary(row: HistoryRecord) -> str:
    return _history_summary_impl(row)


def _history_row_to_json(row: HistoryRecord) -> dict[str, object]:
    return _history_row_to_json_impl(row)


def _format_history_table(rows: Sequence[HistoryRecord]) -> str:
    return _format_history_table_impl(rows, history_subject_fn=_history_subject, history_summary_fn=_history_summary)


def _auth_report_row_to_json(row: AuthEventSummary) -> dict[str, object]:
    return _auth_report_row_to_json_impl(row)


def _auth_report_match_label(row: AuthEventSummary) -> str:
    return _auth_report_match_label_impl(row)


def _normalize_auth_signal_literal(value: str) -> str:
    return _normalize_auth_signal_literal_impl(value)


def _likely_auth_candidate(row: AuthEventSummary) -> bool:
    return _likely_auth_candidate_impl(row)


def _build_auth_signal_patch(rows):
    return _build_auth_signal_patch_impl(rows)


def _render_auth_signal_patch(rows) -> str:
    return _render_auth_signal_patch_impl(rows, file_path=globals().get('AUTH_ERROR_SIGNALS_FILE', AUTH_ERROR_SIGNALS_FILE))


def _render_python_signal_block(name: str, values, *, collection_type: str) -> str:
    return _render_python_signal_block_impl(name, values, collection_type=collection_type)


def _replace_python_signal_block(source: str, name: str, rendered_block: str, *, collection_type: str) -> str:
    return _replace_python_signal_block_impl(source, name, rendered_block, collection_type=collection_type)


def _apply_auth_signal_patch(rows) -> tuple[int, int, str]:
    return _apply_auth_signal_patch_impl(rows, file_path=globals().get('AUTH_ERROR_SIGNALS_FILE', AUTH_ERROR_SIGNALS_FILE))


def _command_run_variant(args, mode: str) -> int:
    return _command_run_variant_impl(
        args,
        mode,
        load_settings_fn=load_settings,
        validate_settings_fn=validate_settings,
        file_lock_cls=FileLock,
        create_store_fn=create_store,
        run_keeper_only_fn=run_keeper_only,
        run_scheduled_start_cycle_fn=run_scheduled_start_cycle,
        run_cycle_fn=run_cycle,
        compute_interval_for_mode_fn=compute_interval_for_mode,
        compute_dispatch_interval_seconds_fn=compute_dispatch_interval_seconds,
        alert_auth_failure_fn=alert_auth_failure,
    )


def _command_list_instances(args) -> int:
    return _command_list_instances_impl(
        args,
        load_settings_fn=load_settings,
        validate_settings_fn=validate_settings,
        create_store_fn=create_store,
        select_accounts_fn=select_accounts,
        build_client_fn=_resolve_cli_symbol('build_client', build_client),
        get_enabled_accounts_fn=get_enabled_accounts,
        normalize_instance_fn=normalize_instance,
        normalize_instance_debug_fn=normalize_instance_debug,
        format_instances_table_fn=format_instances_table,
    )


def _command_inspect_instance(args) -> int:
    return _command_inspect_instance_impl(
        args,
        load_settings_fn=load_settings,
        validate_settings_fn=validate_settings,
        select_accounts_fn=select_accounts,
        create_store_fn=create_store,
        build_client_fn=_resolve_cli_symbol('build_client', build_client),
        normalize_instance_debug_fn=normalize_instance_debug,
    )


def _command_watch_instance(args) -> int:
    return _command_watch_instance_impl(
        args,
        load_settings_fn=load_settings,
        validate_settings_fn=validate_settings,
        select_accounts_fn=select_accounts,
        create_store_fn=create_store,
        build_client_fn=_resolve_cli_symbol('build_client', build_client),
        watch_instance_fn=watch_instance,
        normalize_instance_debug_fn=normalize_instance_debug,
        extract_watch_fields_fn=extract_watch_fields,
        format_watch_change_fn=format_watch_change,
    )


def _command_keeper_probe(args) -> int:
    return _command_keeper_probe_impl(
        args,
        load_settings_fn=load_settings,
        validate_settings_fn=validate_settings,
        create_store_fn=create_store,
        select_accounts_fn=select_accounts,
        build_client_fn=_resolve_cli_symbol('build_client', build_client),
        evaluate_keeper_instance_fn=evaluate_keeper_instance,
        format_keeper_probe_line_fn=format_keeper_probe_line,
    )


def _command_history(args) -> int:
    return _command_history_impl(
        args,
        load_settings_fn=load_settings,
        create_store_fn=create_store,
        select_accounts_fn=select_accounts,
        history_row_to_json_fn=_history_row_to_json,
        format_history_table_fn=_format_history_table,
    )


def _command_auth_report(args) -> int:
    return _command_auth_report_impl(
        args,
        load_settings_fn=load_settings,
        create_store_fn=create_store,
        select_accounts_fn=select_accounts,
        auth_report_row_to_json_fn=_auth_report_row_to_json,
        auth_report_match_label_fn=_auth_report_match_label,
        likely_auth_candidate_fn=_likely_auth_candidate,
        render_auth_signal_patch_fn=_render_auth_signal_patch,
        apply_auth_signal_patch_fn=_apply_auth_signal_patch,
        known_code_signals=AUTH_CODE_SIGNALS,
        known_message_signals=AUTH_MESSAGE_SIGNALS,
    )


def _command_accounts(args) -> int:
    return _command_accounts_impl(args, load_settings_fn=load_settings, create_store_fn=create_store)


def _command_login(args) -> int:
    return _command_login_impl(
        args,
        load_settings_fn=load_settings,
        create_store_fn=create_store,
        select_accounts_fn=select_accounts,
        resolve_authorization_fn=resolve_authorization,
    )


def _command_db_check(args) -> int:
    return _command_db_check_impl(args, load_settings_fn=load_settings, create_store_fn=create_store)


def _command_test_notify(args) -> int:
    return _command_test_notify_impl(
        args,
        load_settings_fn=load_settings,
        validate_settings_fn=validate_settings,
        build_named_notifiers_fn=build_named_notifiers,
    )


def _command_validate_config(args) -> int:
    return _command_validate_config_impl(args, load_settings_fn=load_settings, validate_settings_fn=validate_settings)


def _command_config_show(args) -> int:
    return _command_config_show_impl(args, load_settings_fn=load_settings)


def _command_config_resolve(args) -> int:
    return _command_config_resolve_impl(args, load_settings_fn=load_settings, validate_settings_fn=validate_settings)


def _command_config_edit(args) -> int:
    return _command_config_edit_impl(args, load_settings_fn=load_settings, validate_settings_fn=validate_settings)


def _command_healthcheck(args) -> int:
    return _command_healthcheck_impl(args, load_settings_fn=load_settings, collect_healthcheck_errors_fn=collect_healthcheck_errors)


def _command_init(args) -> int:
    return _command_init_impl(args, validate_config_fn=_command_validate_config, launch_interactive_fn=_command_interactive)


def _command_service_install(args) -> int:
    return _command_service_install_impl(args)


def _command_service_start(args) -> int:
    return _command_service_start_impl(args)


def _command_service_stop(args) -> int:
    return _command_service_stop_impl(args)


def _command_service_restart(args) -> int:
    return _command_service_restart_impl(args)


def _command_service_status(args) -> int:
    return _command_service_status_impl(args, load_settings_fn=load_settings, create_store_fn=create_store)


def _command_service_uninstall(args) -> int:
    return _command_service_uninstall_impl(args)


def _command_interactive(args) -> int:
    from autodl_helper.ui import run_ui

    return run_ui(args)


def main(argv: Sequence[str] | None = None) -> int:
    _configure_logging()
    parser = build_parser()
    with profiler_from_env():
        try:
            args = parser.parse_args(list(argv) if argv is not None else None)
        except SystemExit as exc:
            return int(exc.code)

        if args.command == 'run':
            return _command_run_variant(args, args.run_mode)
        if args.command == 'service':
            service_handlers = {
                'install': _command_service_install,
                'start': _command_service_start,
                'stop': _command_service_stop,
                'restart': _command_service_restart,
                'status': _command_service_status,
                'uninstall': _command_service_uninstall,
            }
            return service_handlers[args.service_command](args)
        if args.command == 'debug':
            debug_handlers = {
                'health': _command_healthcheck,
                'db': _command_db_check,
                'auth': _command_auth_report,
                'history': _command_history,
            }
            return debug_handlers[args.debug_command](args)
        if args.command == 'config':
            config_handlers = {
                'show': _command_config_show,
                'validate': _command_validate_config,
            }
            return config_handlers[args.config_command](args)
        if args.command == 'init':
            return _command_init(args)
        if args.command == 'login':
            return _command_login(args)
        if args.command == 'accounts':
            return _command_accounts(args)
        if args.command == 'list':
            return _command_list_instances(args)
        if args.command == 'ui':
            return _command_interactive(args)

        parser.print_help(sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())


__all__ = [name for name in globals() if not name.startswith('__')]
