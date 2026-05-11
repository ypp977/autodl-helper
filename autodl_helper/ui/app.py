from __future__ import annotations

import builtins
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from autodl_helper.cli.shared_accounts import account_status_rows, select_accounts
from autodl_helper.cli.shared_accounts import build_client
from autodl_helper.core.auth import AuthError, resolve_authorization
from autodl_helper.core.config import load_settings
from autodl_helper.core.store import SQLiteStore
from autodl_helper.cli.commands.runtime import run_keeper_only
from autodl_helper.runtime_control import read_config_reload_status, read_daemon_status, scheduled_job_identity
from autodl_helper.services.manager import restart_service, service_status, start_service, stop_service
from autodl_helper.tasks.scheduled_start import ScheduledStartJobRuntime

from .config_wizard import run_config_wizard
from .render import BLUE, CYAN, GREEN, RED, YELLOW, clear_screen, color, print_numbered_menu, render_header, render_notice, render_section


_SCHEDULED_SUCCESS = {'started', 'already_running', 'power_on_submitted'}
_SCHEDULED_FAILED = {'deadline_failed', 'instance_missing', 'error'}
_KEEPER_SUCCESS = {'keeper_executed'}
_KEEPER_FAILED_PREFIXES = ('keeper_failed', 'shutdown_failed', 'power_on_failed')

_RESULT_LABELS = {
    'started': '已开机',
    'already_running': '已在运行',
    'power_on_submitted': '已提交开机',
    'outside_window': '未到窗口',
    'deadline_failed': '窗口失败',
    'instance_missing': '实例不存在',
    'keeper_executed': '已保活',
    'keeper_failed_power_off': '关机失败',
    'keeper_failed_power_on': '开机失败',
    'skip_not_due': '未到保活时间',
    'not_eligible': '未满足条件',
    'error': '错误',
}


def _label(value: Any) -> str:
    raw = str(value or '')
    return _RESULT_LABELS.get(raw, raw or '-')


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
    return [f'\n{render_section(title, color_enabled=color_enabled)}', *(lines or ['- 暂无数据'])]


def _daemon_line(store: SQLiteStore, *, config_path: str) -> str:
    daemon = read_daemon_status(store)
    reload_status = read_config_reload_status(store)
    try:
        service = service_status(config_path=config_path)
    except Exception as exc:
        service = {'status_label': '读取失败', 'detail': str(exc), 'running': False}
    running = bool(daemon.get('running'))
    status = color('运行中', GREEN) if running else color('未运行', YELLOW)
    service_label = color(str(service.get('status_label') or '-'), GREEN if service.get('running') else YELLOW)
    reload_label = color(str(reload_status.get('last_reload_status') or '-'), GREEN if reload_status.get('last_reload_status') == 'success' else YELLOW)
    line = (
        f"daemon {status} | service {service_label} | "
        f"最近心跳 {color(_short_time(daemon.get('last_seen_at')), BLUE)} | 配置重载 {reload_label}"
    )
    return line


def _keeper_failed(result: str) -> bool:
    return result in _SCHEDULED_FAILED or any(result.startswith(prefix) for prefix in _KEEPER_FAILED_PREFIXES)


def _keeper_due_window_label(settings: Any) -> tuple[str, timedelta]:
    keeper = getattr(getattr(settings, 'tasks', None), 'keeper', None)
    keeper_trigger_before_hours = int(getattr(keeper, 'keeper_trigger_before_hours', 6) or 6)
    if keeper_trigger_before_hours % 24 == 0:
        return f'{keeper_trigger_before_hours // 24}天内临期', timedelta(hours=keeper_trigger_before_hours)
    return f'{keeper_trigger_before_hours}小时内临期', timedelta(hours=keeper_trigger_before_hours)


def _keeper_release_deadline(item: dict[str, Any], keeper_settings: Any) -> datetime | None:
    release_at = _parse_dt(item.get('release_at'))
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


def _keeper_live_rows(settings: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for account in getattr(settings, 'accounts', []) or []:
        if not getattr(account, 'enabled', False):
            continue
        try:
            client = build_client(settings, False, account=account)
            for item in client.list_instances():
                row = dict(item)
                row['account'] = account.name
                rows.append(row)
        except Exception:
            continue
    return rows


def _history_rows_as_live(rows: list[Any]) -> list[dict[str, Any]]:
    live_rows: list[dict[str, Any]] = []
    for row in rows:
        payload = dict(row.payload or {})
        payload.setdefault('instance_id', row.instance_id)
        live_rows.append(payload)
    return live_rows


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


def _keeper_lines(settings: Any, store: SQLiteStore) -> list[str]:
    control_lines = _keeper_control_lines(settings, store)
    due_window_label, due_window_delta = _keeper_due_window_label(settings)
    history_rows = store.read_history(task_type='keeper', limit=200)
    live_rows = _keeper_live_rows(settings)
    rows = live_rows or _history_rows_as_live(history_rows)
    if not rows:
        return [
            *control_lines,
            f"机器 {color('0 台', YELLOW)} | 上次检查 {color('-', YELLOW)} | {color(f'{due_window_label}失败 0 条', GREEN)} | {color(f'{due_window_label} 0 台', GREEN)} | 下次检查 {color('-', YELLOW)}",
        ]

    instance_ids = {_keeper_instance_id(row) for row in rows if _keeper_instance_id(row)}
    last_check_label = _short_time(datetime.now().astimezone()) if live_rows else (_short_time(history_rows[0].created_at) if history_rows else '-')

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

    due_window_failures = {
        (row.instance_id, row.payload.get('release_deadline') or row.payload.get('release_at'))
        for row in history_rows
        if _keeper_failed(row.result)
        and (deadline := _parse_dt(row.payload.get('release_deadline') or row.payload.get('release_at'))) is not None
        and now <= (deadline.astimezone() if deadline.tzinfo else deadline.astimezone()) <= now + due_window_delta
    }

    next_check_label = min(next_keeper_times).strftime('%m-%d %H:%M') if next_keeper_times else '-'
    failure_text = color(f'{due_window_label}失败 {len(due_window_failures)} 条', RED if due_window_failures else GREEN)
    due_text_count = color(f'{due_window_label} {len(due_instances)} 台', YELLOW if due_instances else GREEN)
    machine_text = color(f'机器 {len(instance_ids)} 台', GREEN if instance_ids else YELLOW)
    last_check_text = f"上次检查 {color(last_check_label, GREEN if last_check_label != '-' else YELLOW)}"
    next_check_text = f"下次检查 {color(next_check_label, BLUE if next_check_label != '-' else YELLOW)}"
    lines = [f'{machine_text} | {last_check_text} | {failure_text} | {due_text_count} | {next_check_text}']
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
        while days_ahead <= 7:
            candidate = local_now + timedelta(days=days_ahead)
            if runtime.scheduled_today(candidate):
                local_now = candidate
                break
    target = runtime.target_datetime(local_now)
    if local_now > target:
        target = target + timedelta(days=1)
    start = target - timedelta(hours=runtime.advance_hours)
    if local_now < start:
        return start, target, 'pending'
    return start, target, 'running'


def _scheduled_window_rows(store: SQLiteStore, *, account_name: str, job_name: str, window_key: str) -> list[dict[str, Any]]:
    variants = {job_name, job_name.split(':', 1)[-1], f'{account_name}:{job_name.split(":", 1)[-1]}'}
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


def _scheduled_lines(settings: Any, store: SQLiteStore) -> list[str]:
    accounts = [account for account in settings.accounts if getattr(account, 'enabled', False)]
    scheduled = settings.tasks.scheduled_start
    jobs = [job for job in (scheduled.jobs or []) if getattr(job, 'enabled', True)] if scheduled.enabled else []
    if not accounts or not jobs:
        return [f"{color('成功 0', GREEN)} | {color('近3天失败 0', GREEN)} | {color('进行中 0', GREEN)} | {color('待运行 0', GREEN)}"]

    now = datetime.now().astimezone()
    success = 0
    failed = 0
    running: list[str] = []
    pending: list[str] = []

    for account in accounts:
        for job in jobs:
            runtime = _job_runtime(job, scheduled.poll_interval_seconds)
            start, deadline, state = _job_window(runtime, now)
            window_key = runtime.window_key(deadline.replace(tzinfo=None))
            rows = _scheduled_window_rows(store, account_name=account.name, job_name=runtime.job_name, window_key=window_key)
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
                payload = latest.get('payload', {}) if latest else {}
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
        f"{color(f'成功 {success}', GREEN)} | {color(f'近3天失败 {failed}', RED if failed else GREEN)} | {color(f'进行中 {len(running)}', BLUE if running else GREEN)} | {color(f'待运行 {len(pending)}', YELLOW if pending else GREEN)}"
    ]
    if running:
        lines.append(color('进行中任务:', BLUE))
        lines.extend(running[:3])
    if pending:
        lines.append(color('待运行任务:', YELLOW))
        lines.extend(pending[:3])
    return lines


def _dashboard_lines(args: Any) -> list[str]:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    try:
        settings = load_settings(config_path)
        store = SQLiteStore(settings.storage.database_file)
        store.init_schema()
    except Exception as exc:
        return [f'状态读取失败: {exc}']

    lines: list[str] = [_daemon_line(store, config_path=config_path)]
    lines.extend(_section('Keeper', _keeper_lines(settings, store)))
    lines.extend(_section('抢机', _scheduled_lines(settings, store)))
    return lines


def _print_dashboard(args: Any, *, clear: bool = False) -> None:
    clear_screen(enabled=clear)
    print(render_header('autodl-helper dashboard', color_enabled=True))
    for line in _dashboard_lines(args):
        print(line)


def _service_label(status: dict[str, Any]) -> str:
    return str(status.get('label') or status.get('backend') or 'daemon')


def _control_daemon_service(args: Any, action: str) -> str:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    try:
        status = service_status(config_path=config_path)
    except Exception as exc:
        return f'daemon 服务状态读取失败: {exc}'

    label = _service_label(status)
    if action == 'start':
        if not status.get('installed'):
            return 'daemon 服务未安装，请先执行 service install。'
        if status.get('running'):
            return f'daemon 服务已在运行: {label}'
        result = start_service(config_path=config_path)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or 'service start failed').strip()
            return f'daemon 服务启动失败: {detail}'
        return color(f'已启动 daemon 服务: {label}', GREEN)

    if action == 'stop':
        if not status.get('installed'):
            return f'daemon 服务未安装: {label}'
        if not status.get('running'):
            return f'daemon 服务已停止: {label}'
        result = stop_service(config_path=config_path)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or 'service stop failed').strip()
            return f'daemon 服务停止失败: {detail}'
        return color(f'已停止 daemon 服务: {label}', GREEN)

    if action == 'restart':
        if not status.get('installed'):
            return 'daemon 服务未安装，请先执行 service install。'
        result = restart_service(config_path=config_path)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or 'service restart failed').strip()
            return f'daemon 服务重启失败: {detail}'
        return color(f'已重启 daemon 服务: {label}', GREEN)

    return f'未知 daemon 操作: {action}'


def _keeper_progress_bar(*, executed: int, skipped: int, failed: int, width: int = 24) -> str:
    total = executed + skipped + failed
    if total <= 0:
        return '[' + '-' * width + ']'

    segments = [
        ('#', executed),
        ('-', skipped),
        ('!', failed),
    ]
    used = 0
    parts: list[tuple[str, int, float]] = []
    for marker, count in segments:
        exact = (count / total) * width if count else 0.0
        size = int(exact)
        if count and size == 0:
            size = 1
        parts.append((marker, size, exact - int(exact)))
        used += size

    while used > width:
        candidates = [(index, size) for index, (_, size, _) in enumerate(parts) if size > 0]
        if not candidates:
            break
        index, _ = max(candidates, key=lambda item: item[1])
        marker, size, remainder = parts[index]
        parts[index] = (marker, size - 1, remainder)
        used -= 1

    while used < width:
        index, _marker_size_remainder = max(enumerate(parts), key=lambda item: item[1][2])
        marker, size, remainder = parts[index]
        parts[index] = (marker, size + 1, 0.0)
        used += 1

    return '[' + ''.join(marker * size for marker, size, _ in parts) + ']'


def _run_keeper_once(args: Any) -> str:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    try:
        settings = load_settings(config_path)
        store = SQLiteStore(settings.storage.database_file)
        store.init_schema()
        selected_accounts = select_accounts(settings, getattr(args, 'account', None))
        paused_accounts = []
        for account in selected_accounts:
            if store.get_task_control(account.name, 'keeper') is False:
                controls = [
                    control
                    for control in store.list_task_controls(account_name=account.name)
                    if control.get('task_type') == 'keeper'
                ]
                source = controls[0].get('source') if controls else ''
                paused_accounts.append(f"{account.name}{f'({source})' if source else ''}")
        if paused_accounts and len(paused_accounts) == len(selected_accounts):
            return f"Keeper 当前已暂停，未执行: {', '.join(paused_accounts)}"
        results = run_keeper_only(
            settings=settings,
            headed=bool(getattr(args, 'headed', False)),
            account_name=getattr(args, 'account', None),
            store=store,
        )
    except Exception as exc:
        return f'Keeper 执行失败: {exc}'

    executed = sum(1 for result in results if getattr(result, 'result', '') == 'keeper_executed')
    failed_results = [result for result in results if str(getattr(result, 'result', '')).startswith('keeper_failed')]
    skipped = max(0, len(results) - executed - len(failed_results))
    progress = _keeper_progress_bar(executed=executed, skipped=skipped, failed=len(failed_results))
    summary = f'Keeper 已执行: {len(results)} 台 | 保活 {executed} | 跳过 {skipped} | 失败 {len(failed_results)}'
    progress_percent = 100 if results else 0
    progress_line = f'进度 {progress} {progress_percent}%'
    if failed_results:
        reason_counts = Counter(
            str(getattr(result, 'reason', '') or _label(getattr(result, 'result', '')) or '-')
            for result in failed_results
        )
        reason_text = ', '.join(f'{reason} x{count}' for reason, count in reason_counts.most_common(3))
        return f'{summary}\n{progress_line} | 失败 {reason_text}'
    return f'{summary}\n{progress_line}'


def _resume_keeper(args: Any) -> str:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    try:
        settings = load_settings(config_path)
        store = SQLiteStore(settings.storage.database_file)
        store.init_schema()
        accounts = select_accounts(settings, getattr(args, 'account', None))
    except Exception as exc:
        return f'Keeper 恢复失败: {exc}'

    resumed: list[str] = []
    unchanged: list[str] = []
    for account in accounts:
        if store.get_task_control(account.name, 'keeper') is False:
            store.set_task_control(account.name, 'keeper', enabled=True, source='ui_resume')
            resumed.append(account.name)
        else:
            unchanged.append(account.name)
    if resumed:
        extra = f" | 原本已启用 {len(unchanged)} 个" if unchanged else ''
        return color(f"Keeper 已恢复: {', '.join(resumed)}{extra}", GREEN)
    return color(f"Keeper 已经是启用状态: {', '.join(unchanged) or '-'}", BLUE)


def _account_status_text(args: Any) -> list[str]:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    settings = load_settings(config_path)
    store = SQLiteStore(settings.storage.database_file)
    store.init_schema()
    rows = account_status_rows(settings, store, account_name=getattr(args, 'account', None))
    lines = [color('账号              启用   状态             来源          缓存时间             凭据  token  模式', BLUE)]
    lines.append(color('-' * 82, BLUE))
    for row in rows:
        cached_at = str(row.get('cached_at_iso') or '-')
        if len(cached_at) > 19:
            cached_at = cached_at[:19]
        lines.append(
            f"{color(row['account_name'][:16], CYAN):<16} "
            f"{color('yes' if row['enabled'] else 'no', GREEN if row['enabled'] else RED):<6} "
            f"{color(row['status_label'], GREEN if row['status_label'] in {'已登录', '已授权'} else YELLOW):<16} "
            f"{color(row['auth_source_label'], BLUE):<12} "
            f"{color(cached_at, YELLOW):<19} "
            f"{color('yes' if row['has_credentials'] else 'no', GREEN if row['has_credentials'] else RED):<5} "
            f"{color('yes' if row['has_config_token'] else 'no', GREEN if row['has_config_token'] else RED):<6} "
            f"{color(row['lightweight_mode'], BLUE)}"
        )
    return lines


def _print_account_menu(args: Any, *, notice: str = '', clear: bool = False) -> None:
    clear_screen(enabled=clear)
    print(f'\n{render_section("账号管理", color_enabled=True)}')
    if notice:
        print(render_notice(notice, color_enabled=True))
    try:
        for line in _account_status_text(args):
            print(line)
    except Exception as exc:
        print(color(f'账号状态读取失败: {exc}', RED))
    print()
    print_numbered_menu([
        ('1', '刷新账号状态'),
        ('2', '登录当前账号'),
        ('3', '登录全部账号'),
        ('0', '返回'),
    ])


def _login_accounts(args: Any, *, all_accounts: bool) -> str:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    settings = load_settings(config_path)
    store = SQLiteStore(settings.storage.database_file)
    store.init_schema()
    try:
        if all_accounts:
            accounts = select_accounts(settings, None)
        elif getattr(args, 'account', None):
            accounts = select_accounts(settings, getattr(args, 'account'))
        else:
            accounts = select_accounts(settings, None, require_explicit_for_multi=True)
    except ValueError as exc:
        return str(exc)

    ok = 0
    failed: list[str] = []
    for account in accounts:
        try:
            resolve_authorization(
                account.to_auth_settings(),
                headed=bool(getattr(args, 'headed', False)),
                force_refresh=True,
                store=store,
                account_name=account.name,
            )
            ok += 1
        except AuthError as exc:
            failed.append(f'{account.name}: {exc}')
    if failed:
        return f'账号登录完成: 成功 {ok} 个 | 失败 {len(failed)} 个 | {"; ".join(failed[:3])}'
    return f'账号登录完成: 成功 {ok} 个 | 失败 0 个'


def _run_account_menu(args: Any, *, input_fn=builtins.input) -> str:
    notice = ''
    while True:
        _print_account_menu(args, notice=notice, clear=True)
        notice = ''
        choice = input_fn('选择编号: ').strip().lower()
        if choice == '0':
            return ''
        if choice in {'', '1'}:
            notice = '已刷新账号状态'
            continue
        if choice == '2':
            notice = _login_accounts(args, all_accounts=False)
            continue
        if choice == '3':
            notice = _login_accounts(args, all_accounts=True)
            continue
        notice = '无效选择，请输入 1/2/3/0'


def _run_daemon_control_menu(args: Any, *, input_fn=builtins.input) -> str:
    while True:
        print(f'\n{render_section("daemon 控制", color_enabled=True)}')
        print_numbered_menu([
            ('1', '启动 daemon'),
            ('2', '停止 daemon'),
            ('3', '重启 daemon'),
            ('0', '返回'),
        ])
        choice = input_fn('选择编号: ').strip().lower()
        if choice == '0':
            return ''
        if choice == '1':
            return _control_daemon_service(args, 'start')
        if choice == '2':
            return _control_daemon_service(args, 'stop')
        if choice == '3':
            return _control_daemon_service(args, 'restart')
        print('无效选择，请输入 1/2/3/0')


def _run_keeper_menu(args: Any, *, input_fn=builtins.input) -> str:
    while True:
        print(f'\n{render_section("Keeper 操作", color_enabled=True)}')
        print_numbered_menu([
            ('1', '立即执行一次 Keeper'),
            ('2', '恢复 Keeper'),
            ('0', '返回'),
        ])
        choice = input_fn('选择编号: ').strip().lower()
        if choice == '0':
            return ''
        if choice == '1':
            return _run_keeper_once(args)
        if choice == '2':
            return _resume_keeper(args)
        print('无效选择，请输入 1/2/0')


def run_ui(args: Any, *, input_fn=builtins.input) -> int:
    interactive = input_fn is not builtins.input or sys.stdin.isatty()
    if not interactive:
        _print_dashboard(args)
        return 0

    notice = ''
    while True:
        _print_dashboard(args, clear=True)
        if notice:
            print(f'\n{render_notice(notice, color_enabled=True)}')
        print(f'\n{render_section("操作", color_enabled=True)}')
        print('[常用]')
        print_numbered_menu([
            ('1', '刷新状态'),
        ])
        print('[配置]')
        print_numbered_menu([
            ('2', '配置管理'),
            ('3', '账号管理'),
        ])
        print('[任务]')
        print_numbered_menu([
            ('4', 'Keeper 操作'),
        ])
        print('[系统]')
        print_numbered_menu([
            ('5', 'daemon 控制'),
            ('0', '退出'),
        ])
        choice = input_fn('选择编号: ').strip().lower()
        if choice == '0':
            return 0
        if choice in {'', '1'}:
            notice = ''
            print()
            continue
        if choice == '2':
            saved = run_config_wizard(str(getattr(args, 'config', 'config.yaml')), input_fn=input_fn, clear_screen_enabled=True)
            notice = '配置已保存并已请求重载' if saved else '配置未变更'
        elif choice == '3':
            notice = _run_account_menu(args, input_fn=input_fn)
        elif choice == '4':
            notice = _run_keeper_menu(args, input_fn=input_fn)
        elif choice == '5':
            notice = _run_daemon_control_menu(args, input_fn=input_fn)
        else:
            print('无效选择，请输入 1/2/3/4/5/0')
        print()
