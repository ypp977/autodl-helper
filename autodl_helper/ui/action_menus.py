from __future__ import annotations

import builtins
from collections import Counter
from typing import Any, Callable

from autodl_helper.core.auth import AuthError
from autodl_helper.tasks.keeper_results import keeper_reason_label

from .render import BLUE, CYAN, GREEN, RED, clear_screen, color, print_numbered_menu, render_notice, render_section


def service_label(status: dict[str, Any]) -> str:
    return str(status.get('label') or status.get('backend') or 'daemon')


def control_daemon_service(
    args: Any,
    action: str,
    *,
    service_status_fn: Callable[..., dict[str, Any]],
    start_service_fn: Callable[..., Any],
    stop_service_fn: Callable[..., Any],
    restart_service_fn: Callable[..., Any],
) -> str:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    try:
        status = service_status_fn(config_path=config_path)
    except Exception as exc:
        return f'daemon 服务状态读取失败: {exc}'

    label = service_label(status)
    if action == 'start':
        if not status.get('installed'):
            return 'daemon 服务未安装，请先执行 service install。'
        if status.get('running'):
            return f'daemon 服务已在运行: {label}'
        result = start_service_fn(config_path=config_path)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or 'service start failed').strip()
            return f'daemon 服务启动失败: {detail}'
        return color(f'已启动 daemon 服务: {label}', GREEN)

    if action == 'stop':
        if not status.get('installed'):
            return f'daemon 服务未安装: {label}'
        if not status.get('running'):
            return f'daemon 服务已停止: {label}'
        result = stop_service_fn(config_path=config_path)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or 'service stop failed').strip()
            return f'daemon 服务停止失败: {detail}'
        return color(f'已停止 daemon 服务: {label}', GREEN)

    if action == 'restart':
        if not status.get('installed'):
            return 'daemon 服务未安装，请先执行 service install。'
        result = restart_service_fn(config_path=config_path)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or 'service restart failed').strip()
            return f'daemon 服务重启失败: {detail}'
        return color(f'已重启 daemon 服务: {label}', GREEN)

    return f'未知 daemon 操作: {action}'


def keeper_progress_bar(*, executed: int, skipped: int, failed: int, width: int = 24) -> str:
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


def run_keeper_once(
    args: Any,
    *,
    load_settings_fn: Callable[[str], Any],
    store_cls: type,
    select_accounts_fn: Callable[..., list[Any]],
    run_keeper_only_fn: Callable[..., list[Any]],
    result_label_fn: Callable[[Any], str],
) -> str:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    try:
        settings = load_settings_fn(config_path)
        store = store_cls(settings.storage.database_file)
        store.init_schema()
        selected_accounts = select_accounts_fn(settings, getattr(args, 'account', None))
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
        results = run_keeper_only_fn(
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
    progress = keeper_progress_bar(executed=executed, skipped=skipped, failed=len(failed_results))
    summary = f'Keeper 已执行: {len(results)} 台 | 保活 {executed} | 跳过 {skipped} | 失败 {len(failed_results)}'
    progress_percent = 100 if results else 0
    progress_line = f'进度 {progress} {progress_percent}%'
    if failed_results:
        reason_counts = Counter(
            keeper_reason_label(str(getattr(result, 'reason', '') or result_label_fn(getattr(result, 'result', '')) or '-'))
            for result in failed_results
        )
        reason_text = ', '.join(f'{reason} x{count}' for reason, count in reason_counts.most_common(3))
        return f'{summary}\n{progress_line} | 失败 {reason_text}'
    return f'{summary}\n{progress_line}'


def resume_keeper(
    args: Any,
    *,
    load_settings_fn: Callable[[str], Any],
    store_cls: type,
    select_accounts_fn: Callable[..., list[Any]],
) -> str:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    try:
        settings = load_settings_fn(config_path)
        store = store_cls(settings.storage.database_file)
        store.init_schema()
        accounts = select_accounts_fn(settings, getattr(args, 'account', None))
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


def account_status_text(
    args: Any,
    *,
    load_settings_fn: Callable[[str], Any],
    store_cls: type,
    account_status_rows_fn: Callable[..., list[dict[str, Any]]],
) -> list[str]:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    settings = load_settings_fn(config_path)
    store = store_cls(settings.storage.database_file)
    store.init_schema()
    rows = account_status_rows_fn(settings, store, account_name=getattr(args, 'account', None))
    lines = [color('账号              启用   状态             来源          缓存时间             凭据  token  模式', BLUE)]
    lines.append(color('-' * 82, BLUE))
    for row in rows:
        cached_at = str(row.get('cached_at_iso') or '-')
        if len(cached_at) > 19:
            cached_at = cached_at[:19]
        lines.append(
            f"{color(row['account_name'][:16], CYAN):<16} "
            f"{color('yes' if row['enabled'] else 'no', GREEN if row['enabled'] else RED):<6} "
            f"{color(row['status_label'], GREEN if row['status_label'] in {'已登录', '已授权'} else BLUE):<16} "
            f"{color(row['auth_source_label'], BLUE):<12} "
            f"{color(cached_at, BLUE):<19} "
            f"{color('yes' if row['has_credentials'] else 'no', GREEN if row['has_credentials'] else RED):<5} "
            f"{color('yes' if row['has_config_token'] else 'no', GREEN if row['has_config_token'] else RED):<6} "
            f"{color(row['lightweight_mode'], BLUE)}"
        )
    return lines


def print_account_menu(
    args: Any,
    *,
    notice: str = '',
    clear: bool = False,
    load_settings_fn: Callable[[str], Any],
    store_cls: type,
    account_status_rows_fn: Callable[..., list[dict[str, Any]]],
) -> None:
    clear_screen(enabled=clear)
    print(f'\n{render_section("账号管理", color_enabled=True)}')
    if notice:
        print(render_notice(notice, color_enabled=True))
    try:
        for line in account_status_text(
            args,
            load_settings_fn=load_settings_fn,
            store_cls=store_cls,
            account_status_rows_fn=account_status_rows_fn,
        ):
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


def login_accounts(
    args: Any,
    *,
    all_accounts: bool,
    load_settings_fn: Callable[[str], Any],
    store_cls: type,
    select_accounts_fn: Callable[..., list[Any]],
    resolve_authorization_fn: Callable[..., Any],
) -> str:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    settings = load_settings_fn(config_path)
    store = store_cls(settings.storage.database_file)
    store.init_schema()
    try:
        if all_accounts:
            accounts = select_accounts_fn(settings, None)
        elif getattr(args, 'account', None):
            accounts = select_accounts_fn(settings, getattr(args, 'account'))
        else:
            accounts = select_accounts_fn(settings, None, require_explicit_for_multi=True)
    except ValueError as exc:
        return str(exc)

    ok = 0
    failed: list[str] = []
    for account in accounts:
        try:
            resolve_authorization_fn(
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


def run_account_menu(
    args: Any,
    *,
    input_fn=builtins.input,
    load_settings_fn: Callable[[str], Any],
    store_cls: type,
    select_accounts_fn: Callable[..., list[Any]],
    account_status_rows_fn: Callable[..., list[dict[str, Any]]],
    resolve_authorization_fn: Callable[..., Any],
) -> str:
    notice = ''
    while True:
        print_account_menu(
            args,
            notice=notice,
            clear=True,
            load_settings_fn=load_settings_fn,
            store_cls=store_cls,
            account_status_rows_fn=account_status_rows_fn,
        )
        notice = ''
        choice = input_fn('选择编号: ').strip().lower()
        if choice == '0':
            return ''
        if choice in {'', '1'}:
            notice = '已刷新账号状态'
            continue
        if choice == '2':
            notice = login_accounts(
                args,
                all_accounts=False,
                load_settings_fn=load_settings_fn,
                store_cls=store_cls,
                select_accounts_fn=select_accounts_fn,
                resolve_authorization_fn=resolve_authorization_fn,
            )
            continue
        if choice == '3':
            notice = login_accounts(
                args,
                all_accounts=True,
                load_settings_fn=load_settings_fn,
                store_cls=store_cls,
                select_accounts_fn=select_accounts_fn,
                resolve_authorization_fn=resolve_authorization_fn,
            )
            continue
        notice = '无效选择，请输入 1/2/3/0'


def run_daemon_control_menu(
    args: Any,
    *,
    input_fn=builtins.input,
    service_status_fn: Callable[..., dict[str, Any]],
    start_service_fn: Callable[..., Any],
    stop_service_fn: Callable[..., Any],
    restart_service_fn: Callable[..., Any],
) -> str:
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
            return control_daemon_service(
                args,
                'start',
                service_status_fn=service_status_fn,
                start_service_fn=start_service_fn,
                stop_service_fn=stop_service_fn,
                restart_service_fn=restart_service_fn,
            )
        if choice == '2':
            return control_daemon_service(
                args,
                'stop',
                service_status_fn=service_status_fn,
                start_service_fn=start_service_fn,
                stop_service_fn=stop_service_fn,
                restart_service_fn=restart_service_fn,
            )
        if choice == '3':
            return control_daemon_service(
                args,
                'restart',
                service_status_fn=service_status_fn,
                start_service_fn=start_service_fn,
                stop_service_fn=stop_service_fn,
                restart_service_fn=restart_service_fn,
            )
        print('无效选择，请输入 1/2/3/0')


def run_keeper_menu(
    args: Any,
    *,
    input_fn=builtins.input,
    load_settings_fn: Callable[[str], Any],
    store_cls: type,
    select_accounts_fn: Callable[..., list[Any]],
    run_keeper_only_fn: Callable[..., list[Any]],
    result_label_fn: Callable[[Any], str],
) -> str:
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
            return run_keeper_once(
                args,
                load_settings_fn=load_settings_fn,
                store_cls=store_cls,
                select_accounts_fn=select_accounts_fn,
                run_keeper_only_fn=run_keeper_only_fn,
                result_label_fn=result_label_fn,
            )
        if choice == '2':
            return resume_keeper(
                args,
                load_settings_fn=load_settings_fn,
                store_cls=store_cls,
                select_accounts_fn=select_accounts_fn,
            )
        print('无效选择，请输入 1/2/0')
