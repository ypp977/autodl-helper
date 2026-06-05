from __future__ import annotations

import builtins
import json
import queue
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from autodl_helper.cli.shared_accounts import account_status_rows, build_client, select_accounts
from autodl_helper.core.auth import resolve_authorization
from autodl_helper.core.config import load_settings
from autodl_helper.core.store import SQLiteStore
from autodl_helper.cli.commands.runtime import run_keeper_only
from autodl_helper.runtime_control import (
    apply_runtime_controls_to_scheduled_jobs,
    get_task_enabled,
    read_config_reload_status,
    read_daemon_status,
    scheduled_job_identity,
)
from autodl_helper.services.manager import restart_service, service_status, start_service, stop_service
from autodl_helper.storage.records import scheduled_job_name_variants
from autodl_helper.tasks.keeper_results import keeper_result_label
from autodl_helper.tasks.keeper_timing import compute_keeper_schedule
from autodl_helper.tasks.scheduled_results import scheduled_result_label
from autodl_helper.tasks.scheduled_start import ScheduledStartJobRuntime

from .config_wizard import run_config_wizard
from .background_input import BackgroundInputTask
from .action_menus import (
    run_account_menu,
    run_daemon_control_menu,
    run_keeper_menu,
)
from .render import (
    BLUE,
    CYAN,
    GREEN,
    RED,
    YELLOW,
    clear_screen,
    color,
    print_menu_groups,
    render_header,
    render_metric_row,
    render_notice,
    render_rule,
    render_section,
    render_status,
)


_SCHEDULED_SUCCESS = {'started', 'already_running', 'power_on_submitted'}
_SCHEDULED_FAILED = {'deadline_failed', 'instance_missing', 'error'}
_KEEPER_SUCCESS = {'keeper_executed'}
_KEEPER_FAILED_PREFIXES = ('keeper_failed', 'shutdown_failed', 'power_on_failed')
_SCHEDULED_CONTROL_SOURCE = 'ui_scheduled_control'


@dataclass(frozen=True)
class DashboardSnapshot:
    keeper_live_rows: list[dict[str, Any]] | None
    keeper_live_checked_at: datetime | None
    service_snapshot: dict[str, Any] | None
    message: str

def _label(value: Any) -> str:
    raw = str(value or '')
    keeper_label = keeper_result_label(raw)
    if keeper_label != raw and keeper_label != '-':
        return keeper_label
    scheduled_label = scheduled_result_label(raw)
    return scheduled_label if scheduled_label != '-' else raw or '-'


def _parse_dt(value: Any) -> datetime | None:
    raw = str(value or '').strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _short_time(value: Any) -> str:
    dt = _parse_dt(value)
    if dt is not None:
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime('%m-%d %H:%M')
    raw = str(value or '')
    return raw.replace('T', ' ').split('+', 1)[0].split('.', 1)[0] if raw else '-'


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours:
        return f'{hours}h{minutes:02d}m'
    return f'{minutes}m'


def _section(title: str, lines: list[str], *, color_enabled: bool = True) -> list[str]:
    return [f'\n{render_section(title, color_enabled=color_enabled)}', render_rule(72), *(lines or ['- 暂无数据'])]


def _service_status_value(service: dict[str, Any]) -> tuple[str, str]:
    raw_label = str(service.get('status_label') or '-')
    detail = str(service.get('detail') or '')
    if raw_label == '状态异常':
        if 'EX_CONFIG' in detail:
            return '状态异常(EX_CONFIG)', RED
        if 'last_exit=' in detail:
            return '状态异常(已退出)', RED
        return raw_label, RED
    if bool(service.get('running')):
        return raw_label, GREEN
    if raw_label == '刷新中':
        return raw_label, BLUE
    if raw_label in {'未安装', '已停止', '读取失败'}:
        return raw_label, YELLOW
    return raw_label, RED


def _daemon_line(
    store: SQLiteStore,
    *,
    config_path: str,
    refresh_status: str | None = None,
    service_snapshot: dict[str, Any] | None = None,
    service_pending: bool = False,
) -> str:
    daemon = read_daemon_status(store)
    reload_status = read_config_reload_status(store)
    if service_snapshot is not None:
        service = service_snapshot
    elif service_pending:
        service = {'status_label': '刷新中', 'detail': '', 'running': False}
    else:
        try:
            service = service_status(config_path=config_path)
        except Exception as exc:
            service = {'status_label': '读取失败', 'detail': str(exc), 'running': False}
    daemon_running = bool(daemon.get('running'))
    service_running = bool(service.get('running'))
    service_label, service_color = _service_status_value(service)
    if daemon_running:
        daemon_label = '运行中'
        daemon_color = GREEN
    elif service_running:
        daemon_label = '心跳过期'
        daemon_color = RED
    else:
        daemon_label = '未运行'
        daemon_color = YELLOW
    reload_ok = reload_status.get('last_reload_status') == 'success'
    parts = [
        render_status('守护进程', daemon_label, daemon_color),
        render_status('服务', service_label, service_color),
        render_metric_row([
            ('重载', str(reload_status.get('last_reload_status') or '-'), GREEN if reload_ok else YELLOW),
            ('心跳', _short_time(daemon.get('last_seen_at')), BLUE),
        ]),
    ]
    if refresh_status:
        parts.append(render_status('面板', refresh_status, BLUE))
    return '    '.join(parts)


def _keeper_failed(result: str) -> bool:
    return result in _SCHEDULED_FAILED or any(result.startswith(prefix) for prefix in _KEEPER_FAILED_PREFIXES)


def _keeper_due_window_label(settings: Any) -> tuple[str, timedelta]:
    keeper = getattr(getattr(settings, 'tasks', None), 'keeper', None)
    keeper_trigger_before_hours = int(getattr(keeper, 'keeper_trigger_before_hours', 6) or 6)
    if keeper_trigger_before_hours % 24 == 0:
        return f'{keeper_trigger_before_hours // 24}天内临期', timedelta(hours=keeper_trigger_before_hours)
    return f'{keeper_trigger_before_hours}小时内临期', timedelta(hours=keeper_trigger_before_hours)


def _keeper_release_deadline(item: dict[str, Any], keeper_settings: Any) -> datetime | None:
    release_at = _parse_dt(item.get('release_at') or item.get('release_deadline'))
    if release_at is not None:
        return release_at
    status = str(item.get('status') or '').strip()
    if not getattr(keeper_settings, 'fallback_to_status_at', True):
        return None
    if status not in {'shutdown', 'stopped', 'off'}:
        return None
    stopped_payload = item.get('stopped_at')
    stopped_at_value = stopped_payload.get('Time') if isinstance(stopped_payload, dict) and stopped_payload.get('Valid') else stopped_payload
    stopped_at = _parse_dt(stopped_at_value)
    if stopped_at is not None:
        return stopped_at + timedelta(hours=int(getattr(keeper_settings, 'shutdown_release_after_hours', 360) or 360))
    status_at = _parse_dt(item.get('status_at'))
    if status_at is not None:
        return status_at + timedelta(hours=int(getattr(keeper_settings, 'shutdown_release_after_hours', 360) or 360))
    return None


def _keeper_instance_id(row: dict[str, Any]) -> str:
    return str(row.get('uuid') or row.get('instance_id') or '').strip()


def _history_rows_as_keeper_rows(rows: list[Any]) -> list[dict[str, Any]]:
    keeper_rows: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row.payload or {})
        payload.setdefault('instance_id', row.instance_id)
        keeper_rows.append(payload)
    return keeper_rows


def _keeper_control_lines(settings: Any, store: SQLiteStore) -> list[str]:
    lines: list[str] = []
    for account in getattr(settings, 'accounts', []) or []:
        if not getattr(account, 'enabled', False):
            continue
        control = store.get_task_control(account.name, 'keeper')
        if control is False:
            rows = [
                row
                for row in store.list_task_controls(account_name=account.name)
                if row.get('task_type') == 'keeper'
            ]
            source = rows[0].get('source') if rows else ''
            lines.append(color(f'运行时暂停: {account.name}{f" ({source})" if source else ""}', YELLOW))
    return lines


def _keeper_live_rows(settings: Any, store: SQLiteStore) -> tuple[list[dict[str, Any]], datetime]:
    keeper_settings = getattr(getattr(settings, 'tasks', None), 'keeper', None)
    accounts = [account for account in getattr(settings, 'accounts', []) or [] if getattr(account, 'enabled', False)]

    def fetch_account(account: Any) -> list[dict[str, Any]]:
        account_rows: list[dict[str, Any]] = []
        client = build_client(settings, False, account=account, store=store)
        for item in client.list_instances():
            row = dict(item)
            row['account'] = account.name
            row.setdefault('instance_id', row.get('uuid'))
            if keeper_settings is not None:
                row.update(compute_keeper_schedule(
                    item=row,
                    shutdown_release_after_hours=int(getattr(keeper_settings, 'shutdown_release_after_hours', 360) or 360),
                    keeper_trigger_before_hours=int(getattr(keeper_settings, 'keeper_trigger_before_hours', 6) or 6),
                    fallback_to_status_at=bool(getattr(keeper_settings, 'fallback_to_status_at', True)),
                ))
            account_rows.append(row)
        return account_rows

    rows: list[dict[str, Any]] = []
    if len(accounts) <= 1:
        for account in accounts:
            rows.extend(fetch_account(account))
    else:
        with ThreadPoolExecutor(max_workers=min(4, len(accounts))) as executor:
            futures = [executor.submit(fetch_account, account) for account in accounts]
            for future in as_completed(futures):
                rows.extend(future.result())
    return rows, datetime.now().astimezone()


def _keeper_lines(
    settings: Any,
    store: SQLiteStore,
    *,
    live_rows: list[dict[str, Any]] | None = None,
    live_checked_at: datetime | None = None,
) -> list[str]:
    control_lines = _keeper_control_lines(settings, store)
    due_window_label, due_window_delta = _keeper_due_window_label(settings)
    history_rows = store.read_history(task_type='keeper', limit=200)
    rows = live_rows if live_rows is not None else _history_rows_as_keeper_rows(history_rows)
    if not rows:
        return [
            *control_lines,
            render_metric_row([
                ('机器', '0 台', YELLOW),
                ('上次检查', '-', YELLOW),
                (f'{due_window_label}失败', '0 条', GREEN),
                (due_window_label, '0 台', GREEN),
                ('下次检查', '-', YELLOW),
            ]),
        ]

    instance_ids = {_keeper_instance_id(row) for row in rows if _keeper_instance_id(row)}
    last_check_label = _short_time(live_checked_at) if live_checked_at is not None else (_short_time(history_rows[0].created_at) if history_rows else '-')

    now = datetime.now().astimezone()
    due_instances: dict[str, datetime] = {}
    next_keeper_times: list[datetime] = []
    keeper_settings = getattr(getattr(settings, 'tasks', None), 'keeper', None)
    for row in rows:
        deadline = _keeper_release_deadline(row, keeper_settings)
        if deadline is not None:
            deadline_local = deadline.astimezone() if deadline.tzinfo else deadline.astimezone()
            instance_id = _keeper_instance_id(row)
            if now <= deadline_local <= now + due_window_delta and instance_id:
                current = due_instances.get(instance_id)
                if current is None or deadline_local < current:
                    due_instances[instance_id] = deadline_local
        next_time = _parse_dt(row.get('next_keeper_time'))
        if next_time is not None:
            next_local = next_time.astimezone() if next_time.tzinfo else next_time.astimezone()
            if next_local >= now:
                next_keeper_times.append(next_local)

    if live_rows is not None:
        due_window_failures = {
            (row.instance_id, row.payload.get('release_deadline') or row.payload.get('release_at'))
            for row in history_rows
            if row.instance_id in due_instances
            and _keeper_failed(row.result)
            and (deadline := _parse_dt(row.payload.get('release_deadline') or row.payload.get('release_at'))) is not None
            and now <= (deadline.astimezone() if deadline.tzinfo else deadline.astimezone()) <= now + due_window_delta
        }
    else:
        due_window_failures = {
            (row.instance_id, row.payload.get('release_deadline') or row.payload.get('release_at'))
            for row in history_rows
            if _keeper_failed(row.result)
            and (deadline := _parse_dt(row.payload.get('release_deadline') or row.payload.get('release_at'))) is not None
            and now <= (deadline.astimezone() if deadline.tzinfo else deadline.astimezone()) <= now + due_window_delta
        }

    next_check_label = min(next_keeper_times).strftime('%m-%d %H:%M') if next_keeper_times else '-'
    lines = [
        render_metric_row([
            ('机器', f'{len(instance_ids)} 台', GREEN if instance_ids else YELLOW),
            ('上次检查', last_check_label, GREEN if last_check_label != '-' else YELLOW),
            (f'{due_window_label}失败', f'{len(due_window_failures)} 条', RED if due_window_failures else GREEN),
            (due_window_label, f'{len(due_instances)} 台', YELLOW if due_instances else GREEN),
            ('下次检查', next_check_label, BLUE if next_check_label != '-' else YELLOW),
        ])
    ]
    if due_instances:
        due_text = ', '.join(f'{instance_id}({deadline.strftime("%m-%d %H:%M")})' for instance_id, deadline in sorted(due_instances.items(), key=lambda item: item[1])[:5])
        if len(due_instances) > 5:
            due_text += f', +{len(due_instances) - 5}'
        lines.append(color(f'即将临期: {due_text}', YELLOW))
    return [*control_lines, *lines]


def _job_runtime(job: Any, poll_interval_seconds: int) -> ScheduledStartJobRuntime:
    return ScheduledStartJobRuntime(
        job_name=scheduled_job_identity(job),
        instance_id=job.instance_id,
        target_time=job.target_time,
        advance_hours=job.advance_hours,
        schedule_mode=str(getattr(job, 'schedule_mode', 'daily') or 'daily'),
        weekdays=list(getattr(job, 'weekdays', []) or []),
        run_date=str(getattr(job, 'run_date', '') or ''),
        timezone=getattr(job, 'timezone', 'Asia/Shanghai') or 'Asia/Shanghai',
        poll_interval_seconds=poll_interval_seconds,
        selector=job.selector,
        priority=job.priority,
    )


def _job_window(runtime: ScheduledStartJobRuntime, now: datetime) -> tuple[datetime, datetime, str]:
    tz = ZoneInfo(runtime.timezone)
    local_now = now.astimezone(tz)
    if not runtime.scheduled_today(local_now):
        days_ahead = 1
        max_days = 370 if runtime.schedule_mode == 'once' and runtime.run_date else 7
        while days_ahead <= max_days:
            candidate = local_now + timedelta(days=days_ahead)
            if runtime.scheduled_today(candidate):
                local_now = candidate
                break
            days_ahead += 1
    target = runtime.target_datetime(local_now)
    if local_now > target and not (runtime.schedule_mode == 'once' and runtime.run_date):
        target = target + timedelta(days=1)
    start = target - timedelta(hours=runtime.advance_hours)
    if local_now < start:
        return start, target, 'pending'
    return start, target, 'running'


def _scheduled_window_rows(store: SQLiteStore, *, account_name: str, job_name: str, window_key: str) -> list[dict[str, Any]]:
    variants = set(scheduled_job_name_variants(job_name, account_name=account_name))
    placeholders = ','.join('?' for _ in variants)
    query = f"""
        SELECT created_at, result, reason, instance_id, payload
        FROM scheduled_history
        WHERE account_name = ? AND job_name IN ({placeholders}) AND window_key = ?
        ORDER BY created_at DESC
    """
    with store.connect() as conn:
        rows = conn.execute(query, [account_name, *sorted(variants), window_key]).fetchall()
    parsed: list[dict[str, Any]] = []
    for row in rows:
        try:
            payload = json.loads(row['payload'] or '{}')
        except Exception:
            payload = {}
        parsed.append(
            {
                'created_at': str(row['created_at']),
                'result': str(row['result']),
                'reason': str(row['reason']),
                'instance_id': str(row['instance_id']),
                'payload': payload,
            }
        )
    return parsed


def _scheduled_window_rows_batch(
    store: SQLiteStore,
    requests: list[tuple[str, str, str]],
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    if not requests:
        return {}

    account_names = sorted({account_name for account_name, _job_name, _window_key in requests})
    window_keys = sorted({window_key for _account_name, _job_name, window_key in requests})
    request_variants: dict[tuple[str, str, str], set[str]] = {
        (account_name, job_name, window_key): set(
            scheduled_job_name_variants(job_name, account_name=account_name)
        )
        for account_name, job_name, window_key in requests
    }
    job_names = sorted({variant for variants in request_variants.values() for variant in variants})
    if not account_names or not window_keys or not job_names:
        return {request: [] for request in requests}

    account_placeholders = ','.join('?' for _ in account_names)
    window_placeholders = ','.join('?' for _ in window_keys)
    job_placeholders = ','.join('?' for _ in job_names)
    query = f"""
        SELECT account_name, job_name, window_key, created_at, result, reason, instance_id, payload
        FROM scheduled_history
        WHERE account_name IN ({account_placeholders})
          AND window_key IN ({window_placeholders})
          AND job_name IN ({job_placeholders})
        ORDER BY created_at DESC
    """
    with store.connect() as conn:
        rows = conn.execute(query, [*account_names, *window_keys, *job_names]).fetchall()

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {request: [] for request in requests}
    for row in rows:
        account_name = str(row['account_name'])
        job_name = str(row['job_name'])
        window_key = str(row['window_key'])
        try:
            payload = json.loads(row['payload'] or '{}')
        except Exception:
            payload = {}
        parsed = {
            'created_at': str(row['created_at']),
            'result': str(row['result']),
            'reason': str(row['reason']),
            'instance_id': str(row['instance_id']),
            'payload': payload,
        }
        for request_key, variants in request_variants.items():
            request_account, _request_job, request_window = request_key
            if request_account == account_name and request_window == window_key and job_name in variants:
                grouped[request_key].append(parsed)
    return grouped


def _scheduled_lines(settings: Any, store: SQLiteStore) -> list[str]:
    accounts = [account for account in settings.accounts if getattr(account, 'enabled', False)]
    scheduled = settings.tasks.scheduled_start
    config_jobs = [job for job in (scheduled.jobs or []) if getattr(job, 'enabled', True)] if scheduled.enabled else []
    if not accounts or not config_jobs:
        return [
            render_metric_row([
                ('成功', '0', GREEN),
                ('近3天失败', '0', GREEN),
                ('进行中', '0', GREEN),
                ('待运行', '0', GREEN),
            ])
        ]

    now = datetime.now().astimezone()
    success = 0
    failed = 0
    running: list[str] = []
    pending: list[str] = []
    job_states: list[tuple[Any, ScheduledStartJobRuntime, datetime, datetime, str]] = []

    for account in accounts:
        if not get_task_enabled(store, account.name, 'scheduled_start', default_enabled=bool(scheduled.enabled)):
            continue
        jobs = apply_runtime_controls_to_scheduled_jobs(store, account.name, list(config_jobs))
        for job in jobs:
            runtime = _job_runtime(job, scheduled.poll_interval_seconds)
            start, deadline, state = _job_window(runtime, now)
            window_key = runtime.window_key(deadline.replace(tzinfo=None))
            job_states.append((account, runtime, start, deadline, state))

    history_by_window = _scheduled_window_rows_batch(
        store,
        [
            (account.name, runtime.job_name, runtime.window_key(deadline.replace(tzinfo=None)))
            for account, runtime, _start, deadline, _state in job_states
        ],
    )

    for account, runtime, start, deadline, state in job_states:
        window_key = runtime.window_key(deadline.replace(tzinfo=None))
        rows = history_by_window.get((account.name, runtime.job_name, window_key), [])
        counts = Counter(row['result'] for row in rows)
        success += sum(counts[item] for item in _SCHEDULED_SUCCESS)
        failed += sum(
            1
            for row in rows
            if row['result'] in _SCHEDULED_FAILED
            and (created_at := _parse_dt(row.get('created_at'))) is not None
            and ((created_at.astimezone() if created_at.tzinfo else created_at.astimezone()) >= now - timedelta(days=3))
        )
        has_success = any(row['result'] in _SCHEDULED_SUCCESS for row in rows)
        if state == 'running' and not has_success:
            latest = rows[0] if rows else None
            refresh_count = len(rows)
            last_refresh = _short_time(latest.get('created_at')) if latest else '-'
            window_label = f"{start.strftime('%m-%d %H:%M')}~{deadline.strftime('%m-%d %H:%M')}"
            running.append(
                f"- {color(f'{account.name}/{runtime.job_name}', BLUE)} "
                f"{color(window_label, CYAN)} "
                f"剩余 {color(_duration((deadline - now).total_seconds()), YELLOW)} | {color(f'刷新 {refresh_count} 次', BLUE)} | 上次 {color(last_refresh, CYAN)}"
            )
        else:
            if state == 'pending':
                job_label = color(f'{account.name}/{runtime.job_name}', YELLOW)
                window_label = f"{start.strftime('%m-%d %H:%M')}~{deadline.strftime('%m-%d %H:%M')}"
                pending.append(f'- {job_label} {color(window_label, CYAN)} | 距开始 {color(_duration((start - now).total_seconds()), YELLOW)}')
            else:
                job_label = color(f'{account.name}/{runtime.job_name}', GREEN)
                pending.append(f'- {job_label} 本窗口已完成 | 截止 {color(deadline.strftime("%m-%d %H:%M"), CYAN)}')

    lines = [
        render_metric_row([
            ('成功', str(success), GREEN),
            ('近3天失败', str(failed), RED if failed else GREEN),
            ('进行中', str(len(running)), BLUE if running else GREEN),
            ('待运行', str(len(pending)), YELLOW if pending else GREEN),
        ])
    ]
    if running:
        lines.append(color('进行中任务:', BLUE))
        lines.extend(running[:3])
    if pending:
        lines.append(color('待运行任务:', YELLOW))
        lines.extend(pending[:3])
    return lines


def _dashboard_lines(
    args: Any,
    *,
    keeper_live_rows: list[dict[str, Any]] | None = None,
    keeper_live_checked_at: datetime | None = None,
    refresh_status: str | None = None,
    service_snapshot: dict[str, Any] | None = None,
    service_pending: bool = False,
) -> list[str]:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    try:
        settings = load_settings(config_path)
        store = SQLiteStore(settings.storage.database_file)
        store.init_schema()
    except Exception as exc:
        return [f'状态读取失败: {exc}']

    lines: list[str] = [
        _daemon_line(
            store,
            config_path=config_path,
            refresh_status=refresh_status,
            service_snapshot=service_snapshot,
            service_pending=service_pending,
        )
    ]
    lines.extend(_section('Keeper', _keeper_lines(settings, store, live_rows=keeper_live_rows, live_checked_at=keeper_live_checked_at)))
    lines.extend(_section('抢机', _scheduled_lines(settings, store)))
    return lines


def _print_dashboard(
    args: Any,
    *,
    clear: bool = False,
    keeper_live_rows: list[dict[str, Any]] | None = None,
    keeper_live_checked_at: datetime | None = None,
    refresh_status: str | None = None,
    service_snapshot: dict[str, Any] | None = None,
    service_pending: bool = False,
) -> None:
    clear_screen(enabled=clear)
    print(render_header('autodl-helper dashboard', color_enabled=True))
    print(render_rule())
    for line in _dashboard_lines(
        args,
        keeper_live_rows=keeper_live_rows,
        keeper_live_checked_at=keeper_live_checked_at,
        refresh_status=refresh_status,
        service_snapshot=service_snapshot,
        service_pending=service_pending,
    ):
        print(line)


def _print_main_menu() -> None:
    print(f'\n{render_section("操作", color_enabled=True)}')
    print(render_rule())
    print_menu_groups([
        ('状态看板', [('1', '刷新状态')]),
        ('业务操作', [('2', '进入业务操作')]),
        ('设置管理', [('3', '进入设置管理')]),
        ('后台服务', [('4', 'daemon 管理'), ('0', '退出')]),
    ])


def _select_business_menu(*, input_fn=builtins.input) -> str:
    notice = ''
    while True:
        clear_screen(enabled=True)
        print(f'\n{render_section("业务操作", color_enabled=True)}')
        print(render_rule())
        if notice:
            print(render_notice(notice, color_enabled=True))
            notice = ''
        print_menu_groups([
            ('任务', [('1', 'Keeper 管理'), ('2', '抢机管理')]),
            ('返回', [('0', '返回主菜单')]),
        ])
        choice = input_fn('选择编号: ').strip().lower()
        if choice in {'0', '1', '2'}:
            return choice
        notice = '无效选择，请输入 1/2/0'


def _select_settings_menu(*, input_fn=builtins.input) -> str:
    notice = ''
    while True:
        clear_screen(enabled=True)
        print(f'\n{render_section("设置管理", color_enabled=True)}')
        print(render_rule())
        if notice:
            print(render_notice(notice, color_enabled=True))
            notice = ''
        print_menu_groups([
            ('账户与配置', [('1', '账户管理'), ('2', '配置管理')]),
            ('返回', [('0', '返回主菜单')]),
        ])
        choice = input_fn('选择编号: ').strip().lower()
        if choice in {'0', '1', '2'}:
            return choice
        notice = '无效选择，请输入 1/2/0'


def _scheduled_job_control_entry(store: SQLiteStore, account_name: str, job: Any, *, scheduled_enabled: bool) -> dict[str, Any]:
    job_name = scheduled_job_identity(job)
    task_control = store.get_task_control(account_name, 'scheduled_start')
    job_control = store.get_scheduled_job_control(account_name, job_name)
    config_enabled = scheduled_enabled and bool(getattr(job, 'enabled', True))
    runtime_enabled = not (task_control is False or (job_control and not job_control.get('enabled')))
    if not config_enabled:
        status = '配置停用'
        status_color = YELLOW
    elif runtime_enabled:
        status = '运行中'
        status_color = GREEN
    else:
        status = '已暂停'
        status_color = RED
    return {
        'account_name': account_name,
        'job': job,
        'job_name': job_name,
        'config_enabled': config_enabled,
        'job_control': job_control,
        'status': status,
        'status_color': status_color,
    }


def _scheduled_control_entries(settings: Any, store: SQLiteStore, accounts: list[Any]) -> list[dict[str, Any]]:
    scheduled = getattr(settings.tasks, 'scheduled_start', None)
    scheduled_enabled = bool(getattr(scheduled, 'enabled', False))
    jobs = list(getattr(scheduled, 'jobs', []) or [])
    entries: list[dict[str, Any]] = []
    for account in accounts:
        for job in jobs:
            entries.append(_scheduled_job_control_entry(store, account.name, job, scheduled_enabled=scheduled_enabled))
    return entries


def _scheduled_entry_line(index: int, entry: dict[str, Any]) -> str:
    job = entry['job']
    label = color(f"{entry['account_name']}/{entry['job_name']}", BLUE)
    status = color(str(entry['status']), entry['status_color'])
    if getattr(job, 'instance_id', ''):
        detail = f"固定实例 {color(str(job.instance_id), YELLOW)}"
    else:
        selector = getattr(job, 'selector', None)
        gpu_model = getattr(selector, 'gpu_model', '-') if selector is not None else '-'
        gpu_count = getattr(selector, 'gpu_count', '-') if selector is not None else '-'
        detail = f"筛选 {color(str(gpu_model or '-'), YELLOW)} x{color(str(gpu_count or '-'), YELLOW)}"
    target = color(str(getattr(job, 'target_time', '-') or '-'), CYAN)
    return f'{index:>2}. [{status}] {label} | {target} | {detail}'


def _print_scheduled_control_entries(entries: list[dict[str, Any]]) -> None:
    if not entries:
        print('暂无抢机任务')
        return
    print(color('任务运行态:', BLUE))
    for index, entry in enumerate(entries, start=1):
        print(_scheduled_entry_line(index, entry))


def _select_scheduled_control_entry(entries: list[dict[str, Any]], input_fn: Any) -> tuple[dict[str, Any] | None, str]:
    if not entries:
        return None, '暂无抢机任务'
    raw = input_fn('任务编号，0 返回: ').strip().lower()
    if raw in {'0', 'q', 'quit', 'cancel'}:
        return None, '已取消'
    try:
        index = int(raw) - 1
    except ValueError:
        return None, '任务编号必须是数字'
    if index < 0 or index >= len(entries):
        return None, '任务编号不存在'
    return entries[index], ''


def _set_scheduled_job_runtime(entry: dict[str, Any], store: SQLiteStore, *, enabled: bool) -> str:
    account_name = str(entry['account_name'])
    job_name = str(entry['job_name'])
    if enabled and not entry['config_enabled']:
        return f'配置停用任务不能通过运行时恢复: {account_name}/{job_name}'
    if not enabled and not entry['config_enabled']:
        return f'配置停用任务无需运行时暂停: {account_name}/{job_name}'
    existing = entry.get('job_control') or {}
    store.upsert_scheduled_job_control(
        account_name,
        job_name,
        enabled=enabled,
        target_time_override=str(existing.get('target_time_override') or ''),
        advance_hours_override=existing.get('advance_hours_override'),
        source=_SCHEDULED_CONTROL_SOURCE,
    )
    action = '恢复' if enabled else '暂停'
    return color(f'抢机任务已{action}: {account_name}/{job_name}', GREEN)


def _set_all_scheduled_runtime(accounts: list[Any], store: SQLiteStore, *, enabled: bool) -> str:
    if not accounts:
        return '没有可操作的账户'
    for account in accounts:
        store.set_task_control(account.name, 'scheduled_start', enabled=enabled, source=_SCHEDULED_CONTROL_SOURCE)
    action = '恢复' if enabled else '暂停'
    return color(f"抢机已全部{action}: {', '.join(account.name for account in accounts)}", GREEN)


def _run_scheduled_management_menu(args: Any, *, input_fn=builtins.input) -> str:
    notice = ''
    while True:
        clear_screen(enabled=True)
        print(f'\n{render_section("抢机管理", color_enabled=True)}')
        print(render_rule())
        if notice:
            print(render_notice(notice, color_enabled=True))
            notice = ''
        config_path = str(getattr(args, 'config', 'config.yaml'))
        entries: list[dict[str, Any]] = []
        accounts: list[Any] = []
        store: SQLiteStore | None = None
        try:
            settings = load_settings(config_path)
            store = SQLiteStore(settings.storage.database_file)
            store.init_schema()
            accounts = select_accounts(settings, getattr(args, 'account', None))
            for line in _scheduled_lines(settings, store):
                print(line)
            print()
            entries = _scheduled_control_entries(settings, store, accounts)
            _print_scheduled_control_entries(entries)
        except Exception as exc:
            print(color(f'抢机状态读取失败: {exc}', RED))
        print()
        print(color('配置入口: 配置管理 > 抢机配置', BLUE))
        print_menu_groups([
            ('查看', [('1', '刷新状态')]),
            ('任务', [('2', '暂停单个任务'), ('3', '恢复单个任务')]),
            ('全局', [('4', '暂停全部抢机'), ('5', '恢复全部抢机')]),
            ('返回', [('0', '返回')]),
        ])
        choice = input_fn('选择编号: ').strip().lower()
        if choice == '0':
            return ''
        if choice in {'', '1'}:
            notice = '已刷新抢机状态'
            continue
        if store is None:
            notice = '抢机状态不可用，无法操作'
            continue
        if choice == '2':
            entry, message = _select_scheduled_control_entry(entries, input_fn)
            notice = message if entry is None else _set_scheduled_job_runtime(entry, store, enabled=False)
            continue
        if choice == '3':
            entry, message = _select_scheduled_control_entry(entries, input_fn)
            notice = message if entry is None else _set_scheduled_job_runtime(entry, store, enabled=True)
            continue
        if choice == '4':
            notice = '暂无抢机任务' if not entries else _set_all_scheduled_runtime(accounts, store, enabled=False)
            continue
        if choice == '5':
            notice = '暂无抢机任务' if not entries else _set_all_scheduled_runtime(accounts, store, enabled=True)
            continue
        notice = '无效选择，请输入 1/2/3/4/5/0'


def _refresh_keeper_dashboard(args: Any) -> tuple[list[dict[str, Any]] | None, datetime | None, str]:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    try:
        settings = load_settings(config_path)
        store = SQLiteStore(settings.storage.database_file)
        store.init_schema()
        rows, checked_at = _keeper_live_rows(settings, store)
        return rows, checked_at, f'已刷新最新状态: Keeper {len(rows)} 台'
    except Exception as exc:
        return None, None, f'刷新最新状态失败: {exc}'


def _refresh_service_status(args: Any) -> dict[str, Any]:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    try:
        return service_status(config_path=config_path)
    except Exception as exc:
        return {'status_label': '读取失败', 'detail': str(exc), 'running': False}


def _refresh_dashboard_snapshot(args: Any) -> DashboardSnapshot:
    keeper_rows, keeper_checked_at, keeper_message = _refresh_keeper_dashboard(args)
    service = _refresh_service_status(args)
    service_label = str(service.get('status_label') or '-')
    if keeper_rows is None:
        message = keeper_message
    else:
        message = f'已刷新最新状态: Keeper {len(keeper_rows)} 台 | 服务 {service_label}'
    return DashboardSnapshot(
        keeper_live_rows=keeper_rows,
        keeper_live_checked_at=keeper_checked_at,
        service_snapshot=service,
        message=message,
    )


class _RefreshTask:
    def __init__(self, args: Any):
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        self._thread = threading.Thread(target=self._run, args=(args,), name='ui-refresh-keeper-dashboard', daemon=True)
        self._thread.start()

    def _run(self, args: Any) -> None:
        try:
            self._queue.put(('ok', _refresh_dashboard_snapshot(args)))
        except Exception as exc:
            self._queue.put(('error', exc))

    def done(self) -> bool:
        return not self._queue.empty()

    def result(self) -> DashboardSnapshot:
        status, payload = self._queue.get_nowait()
        if status == 'error':
            raise payload
        return payload


def _start_refresh_task(args: Any) -> _RefreshTask:
    return _RefreshTask(args)


class _ServiceStatusTask:
    def __init__(self, args: Any):
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        self._thread = threading.Thread(target=self._run, args=(args,), name='ui-refresh-service-status', daemon=True)
        self._thread.start()

    def _run(self, args: Any) -> None:
        try:
            self._queue.put(('ok', _refresh_service_status(args)))
        except Exception as exc:
            self._queue.put(('error', exc))

    def done(self) -> bool:
        return not self._queue.empty()

    def result(self) -> dict[str, Any]:
        status, payload = self._queue.get_nowait()
        if status == 'error':
            raise payload
        return payload


def _start_service_status_task(args: Any) -> _ServiceStatusTask:
    return _ServiceStatusTask(args)


def _consume_service_status_task(
    task: Any | None,
    current_snapshot: dict[str, Any] | None,
) -> tuple[Any | None, dict[str, Any] | None, str]:
    if task is None:
        return None, current_snapshot, ''
    if not task.done():
        return task, current_snapshot, ''
    try:
        return None, task.result(), '服务状态已刷新'
    except Exception as exc:
        return None, current_snapshot, f'服务状态刷新失败: {exc}'


def _consume_refresh_task(
    task: Any | None,
    current_rows: list[dict[str, Any]] | None,
    current_checked_at: datetime | None,
    current_service_snapshot: dict[str, Any] | None,
) -> tuple[Any | None, list[dict[str, Any]] | None, datetime | None, dict[str, Any] | None, str]:
    if task is None:
        return None, current_rows, current_checked_at, current_service_snapshot, ''
    if not task.done():
        return task, current_rows, current_checked_at, current_service_snapshot, '刷新中，完成后将更新状态栏'
    try:
        snapshot = task.result()
    except Exception as exc:
        return None, current_rows, current_checked_at, current_service_snapshot, f'刷新最新状态失败: {exc}'
    if isinstance(snapshot, DashboardSnapshot):
        if snapshot.keeper_live_rows is None:
            return None, current_rows, current_checked_at, snapshot.service_snapshot or current_service_snapshot, snapshot.message
        return (
            None,
            snapshot.keeper_live_rows,
            snapshot.keeper_live_checked_at,
            snapshot.service_snapshot or current_service_snapshot,
            snapshot.message,
        )
    rows, checked_at, message = snapshot
    if rows is None:
        return None, current_rows, current_checked_at, current_service_snapshot, message
    return None, rows, checked_at, current_service_snapshot, message


def _read_main_choice_with_background_repaint(
    args: Any,
    *,
    input_fn: Any,
    refresh_task: Any | None,
    service_task: Any | None,
    keeper_live_rows: list[dict[str, Any]] | None,
    keeper_live_checked_at: datetime | None,
    service_snapshot: dict[str, Any] | None,
) -> tuple[str, Any | None, Any | None, list[dict[str, Any]] | None, datetime | None, dict[str, Any] | None, str]:
    if (refresh_task is None or refresh_task.done()) and (service_task is None or service_task.done()):
        return input_fn('选择编号: ').strip().lower(), refresh_task, service_task, keeper_live_rows, keeper_live_checked_at, service_snapshot, ''

    input_task = BackgroundInputTask(input_fn, '选择编号: ')
    notice = ''
    while not input_task.done():
        changed = False
        refresh_task, keeper_live_rows, keeper_live_checked_at, service_snapshot, refresh_notice = _consume_refresh_task(
            refresh_task,
            keeper_live_rows,
            keeper_live_checked_at,
            service_snapshot,
        )
        if refresh_notice and refresh_notice != '刷新中，完成后将更新状态栏':
            notice = refresh_notice
            changed = True
        service_task, service_snapshot, service_notice = _consume_service_status_task(
            service_task,
            service_snapshot,
        )
        if service_notice:
            notice = service_notice if not notice else notice
            changed = True
        if changed:
            _print_dashboard(
                args,
                clear=True,
                keeper_live_rows=keeper_live_rows,
                keeper_live_checked_at=keeper_live_checked_at,
                refresh_status='刷新中' if refresh_task is not None and not refresh_task.done() else None,
                service_snapshot=service_snapshot,
                service_pending=service_snapshot is None or (service_task is not None and not service_task.done()),
            )
            if notice:
                print(f'\n{render_notice(notice, color_enabled=True)}')
            _print_main_menu()
            print('选择编号: ', end='', flush=True)
        time.sleep(0.05)

    return input_task.result().strip().lower(), refresh_task, service_task, keeper_live_rows, keeper_live_checked_at, service_snapshot, notice


def run_ui(args: Any, *, input_fn=builtins.input) -> int:
    interactive = input_fn is not builtins.input or sys.stdin.isatty()
    if not interactive:
        _print_dashboard(args)
        return 0

    notice = ''
    keeper_live_rows: list[dict[str, Any]] | None = None
    keeper_live_checked_at: datetime | None = None
    refresh_task: Any | None = None
    service_snapshot: dict[str, Any] | None = None
    service_task: Any | None = None
    while True:
        refresh_task, keeper_live_rows, keeper_live_checked_at, service_snapshot, refresh_notice = _consume_refresh_task(
            refresh_task,
            keeper_live_rows,
            keeper_live_checked_at,
            service_snapshot,
        )
        if refresh_notice:
            notice = refresh_notice
        service_task, service_snapshot, service_notice = _consume_service_status_task(
            service_task,
            service_snapshot,
        )
        if service_notice and not notice:
            notice = service_notice
        _print_dashboard(
            args,
            clear=True,
            keeper_live_rows=keeper_live_rows,
            keeper_live_checked_at=keeper_live_checked_at,
            refresh_status='刷新中' if refresh_task is not None and not refresh_task.done() else None,
            service_snapshot=service_snapshot,
            service_pending=service_snapshot is None or (service_task is not None and not service_task.done()),
        )
        if service_task is None and service_snapshot is None:
            service_task = _start_service_status_task(args)
        if notice:
            print(f'\n{render_notice(notice, color_enabled=True)}')
        _print_main_menu()
        (
            choice,
            refresh_task,
            service_task,
            keeper_live_rows,
            keeper_live_checked_at,
            service_snapshot,
            input_notice,
        ) = _read_main_choice_with_background_repaint(
            args,
            input_fn=input_fn,
            refresh_task=refresh_task,
            service_task=service_task,
            keeper_live_rows=keeper_live_rows,
            keeper_live_checked_at=keeper_live_checked_at,
            service_snapshot=service_snapshot,
        )
        if input_notice:
            notice = input_notice
        if choice == '0':
            return 0
        if choice in {'', '1'}:
            if refresh_task is not None and not refresh_task.done():
                notice = '刷新中，请稍后查看状态栏'
            else:
                refresh_task = _start_refresh_task(args)
                notice = '刷新任务已提交，状态栏显示刷新中'
            if service_task is None or service_task.done():
                service_task = _start_service_status_task(args)
            continue
        if choice == '2':
            business_choice = _select_business_menu(input_fn=input_fn)
            if business_choice == '1':
                notice = run_keeper_menu(
                    args,
                    input_fn=input_fn,
                    load_settings_fn=load_settings,
                    store_cls=SQLiteStore,
                    select_accounts_fn=select_accounts,
                    run_keeper_only_fn=run_keeper_only,
                    result_label_fn=_label,
                )
            elif business_choice == '2':
                notice = _run_scheduled_management_menu(args, input_fn=input_fn)
            else:
                notice = ''
        elif choice == '3':
            settings_choice = _select_settings_menu(input_fn=input_fn)
            if settings_choice == '1':
                notice = run_account_menu(
                    args,
                    input_fn=input_fn,
                    load_settings_fn=load_settings,
                    store_cls=SQLiteStore,
                    select_accounts_fn=select_accounts,
                    account_status_rows_fn=account_status_rows,
                    resolve_authorization_fn=resolve_authorization,
                    build_client_fn=build_client,
                )
            elif settings_choice == '2':
                saved = run_config_wizard(str(getattr(args, 'config', 'config.yaml')), input_fn=input_fn, clear_screen_enabled=True)
                notice = '配置已保存并已请求重载' if saved else '配置未变更'
            else:
                notice = ''
        elif choice == '4':
            notice = run_daemon_control_menu(
                args,
                input_fn=input_fn,
                service_status_fn=service_status,
                start_service_fn=start_service,
                stop_service_fn=stop_service,
                restart_service_fn=restart_service,
            )
        else:
            notice = '无效选择，请输入 1/2/3/4/0'
        print()
