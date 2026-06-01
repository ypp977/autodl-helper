from __future__ import annotations

import builtins
import getpass
import queue
import threading
from typing import Any, Callable

from autodl_helper.core.auth import AuthError
from autodl_helper.core.config import read_raw_settings, write_raw_settings
from autodl_helper.runtime_control import request_config_reload

from .render import BLUE, CYAN, GREEN, RED, YELLOW, clear_screen, color, pad_display, print_menu_groups, render_notice, render_rule, render_section


_ACCOUNT_COLUMNS: tuple[tuple[str, int], ...] = (
    ('账户', 18),
    ('启用', 8),
    ('状态', 16),
    ('来源', 12),
    ('缓存时间', 21),
    ('凭据', 7),
    ('token', 8),
    ('模式', 10),
)


def _account_table_row(cells: list[tuple[str, str]]) -> str:
    parts: list[str] = []
    for (text, ansi), (_, width) in zip(cells, _ACCOUNT_COLUMNS, strict=True):
        parts.append(color(pad_display(text, width), ansi))
    return '  '.join(parts).rstrip()


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
    header_cells = [(label, BLUE) for label, _ in _ACCOUNT_COLUMNS]
    lines = [_account_table_row(header_cells)]
    lines.append(color('-' * 106, BLUE))
    for row in rows:
        cached_at = str(row.get('cached_at_iso') or '-')
        if len(cached_at) > 19:
            cached_at = cached_at[:19]
        lines.append(
            _account_table_row([
                (str(row['account_name'])[:16], CYAN),
                ('是' if row['enabled'] else '否', GREEN if row['enabled'] else RED),
                (str(row['status_label']), GREEN if str(row['status_label']).startswith(('已登录', '已缓存', '已配置')) else YELLOW),
                (str(row['auth_source_label']), BLUE),
                (cached_at, BLUE),
                ('是' if row['has_credentials'] else '否', GREEN if row['has_credentials'] else RED),
                ('是' if row['has_config_token'] else '否', GREEN if row['has_config_token'] else RED),
                (str(row['lightweight_mode']), BLUE),
            ])
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
    print(f'\n{render_section("账户管理", color_enabled=True)}')
    print(render_rule())
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
        print(color(f'账户状态读取失败: {exc}', RED))
    print()
    print_menu_groups([
        ('状态', [('1', '刷新账户状态'), ('4', '账户健康检查')]),
        ('账户', [('2', '添加账户'), ('5', '编辑账户凭据'), ('6', '启用/停用账户'), ('7', '删除账户')]),
        ('登录', [('3', '选择账户登录')]),
        ('返回', [('0', '返回')]),
    ])


def _account_payloads(raw: dict[str, Any]) -> list[dict[str, Any]]:
    accounts = raw.get('accounts')
    if isinstance(accounts, list) and accounts:
        return [dict(item or {}) for item in accounts]
    auth = dict(raw.get('auth') or {})
    return [
        {
            'name': 'default',
            'enabled': True,
            'authorization': str(auth.get('authorization') or ''),
            'autodl_phone': str(auth.get('autodl_phone') or ''),
            'autodl_password': str(auth.get('autodl_password') or ''),
            'cache_file': '.cache/default-auth.json',
            'lightweight_mode': str(auth.get('lightweight_mode') or 'normal'),
        }
    ]


def _secret_input(input_fn: Any, prompt: str) -> str:
    if input_fn is builtins.input:
        return getpass.getpass(prompt)
    return input_fn(prompt)


def _default_account_name(accounts: list[dict[str, Any]], phone: str = '') -> str:
    existing = {str(item.get('name') or '').strip() for item in accounts}
    if phone and phone.isdigit() and len(phone) >= 4:
        candidate = f'account-{phone[-4:]}'
        if candidate not in existing:
            return candidate
    index = len(accounts) + 1
    while f'account-{index}' in existing:
        index += 1
    return f'account-{index}'


def _normalize_account_name(raw: str, *, fallback: str, existing_names: set[str]) -> tuple[str, str]:
    name = raw.strip() or fallback
    if name in {'0', 'q', 'quit', 'cancel'}:
        return '', '已取消'
    if name.isdigit():
        return '', '账户名不能是纯数字；已取消添加，避免把手机号或菜单编号当成账号名'
    if name in existing_names:
        return '', '账户名已存在'
    return name, ''


def _save_accounts_and_request_reload(
    config_path: str,
    raw: dict[str, Any],
    accounts: list[dict[str, Any]],
    *,
    load_settings_fn: Callable[[str], Any],
    store_cls: type,
    write_raw_settings_fn: Callable[[str, dict[str, Any]], None] = write_raw_settings,
    request_config_reload_fn: Callable[[Any], dict[str, Any]] = request_config_reload,
) -> None:
    raw['accounts'] = accounts
    write_raw_settings_fn(config_path, raw)
    settings = load_settings_fn(config_path)
    store = store_cls(settings.storage.database_file)
    store.init_schema()
    request_config_reload_fn(store)


def _select_account_payload(accounts: list[dict[str, Any]], input_fn: Any, *, prompt: str = '账户编号，0 取消: ') -> tuple[int | None, str]:
    if not accounts:
        return None, '没有可操作的账户'
    print(color('选择账户:', BLUE))
    for index, account in enumerate(accounts, start=1):
        enabled = '启用' if bool(account.get('enabled', True)) else '停用'
        print(f'  {color(str(index), CYAN)}  {account.get("name") or "-"} ({enabled})')
    raw = input_fn(prompt).strip().lower()
    if raw in {'', '0', 'q', 'quit', 'cancel'}:
        return None, '已取消'
    try:
        index = int(raw) - 1
    except ValueError:
        return None, '账户编号必须是数字'
    if index < 0 or index >= len(accounts):
        return None, '账户编号不存在'
    return index, ''


def add_account(
    args: Any,
    *,
    input_fn: Any,
    load_settings_fn: Callable[[str], Any],
    store_cls: type,
    read_raw_settings_fn: Callable[[str], dict[str, Any]] = read_raw_settings,
    write_raw_settings_fn: Callable[[str, dict[str, Any]], None] = write_raw_settings,
    request_config_reload_fn: Callable[[Any], dict[str, Any]] = request_config_reload,
) -> str:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    raw = dict(read_raw_settings_fn(config_path) or {})
    accounts = _account_payloads(raw)
    phone = input_fn('手机号，0 取消: ').strip()
    if phone in {'0', 'q', 'quit', 'cancel'}:
        return '已取消添加账户'
    password = _secret_input(input_fn, '密码，可留空: ').strip()
    token = _secret_input(input_fn, 'Authorization token，可留空: ').strip()
    if not token and not (phone and password):
        return '未提供可用登录信息，未添加账户'
    fallback_name = _default_account_name(accounts, phone)
    raw_name = input_fn(f'账户名，回车自动使用 {fallback_name}: ').strip()
    existing_names = {str(item.get('name') or '').strip() for item in accounts}
    name, error = _normalize_account_name(raw_name, fallback=fallback_name, existing_names=existing_names)
    if error:
        return error
    account_payload: dict[str, Any] = {
        'name': name,
        'enabled': True,
        'authorization': token,
        'autodl_phone': phone,
        'autodl_password': password,
        'cache_file': f'.cache/{name}-auth.json',
        'cache_max_age_seconds': 86400,
        'lightweight_mode': 'normal',
        'runtime_auth_revalidate_seconds': 0,
        'force_refresh_min_interval_seconds': 0,
        'auth_failure_backoff_seconds': 0,
    }
    accounts.append(account_payload)
    _save_accounts_and_request_reload(
        config_path,
        raw,
        accounts,
        load_settings_fn=load_settings_fn,
        store_cls=store_cls,
        write_raw_settings_fn=write_raw_settings_fn,
        request_config_reload_fn=request_config_reload_fn,
    )
    return f'账户已添加并已请求重载: {name}'


def edit_account_credentials(
    args: Any,
    *,
    input_fn: Any,
    load_settings_fn: Callable[[str], Any],
    store_cls: type,
    read_raw_settings_fn: Callable[[str], dict[str, Any]] = read_raw_settings,
) -> str:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    raw = dict(read_raw_settings_fn(config_path) or {})
    accounts = _account_payloads(raw)
    index, message = _select_account_payload(accounts, input_fn)
    if index is None:
        return message
    account = accounts[index]
    phone = input_fn(f"手机号，回车保留当前值({account.get('autodl_phone') or '-'}): ").strip()
    password = _secret_input(input_fn, '密码，回车保留当前值: ').strip()
    token = _secret_input(input_fn, 'Authorization token，回车保留当前值: ').strip()
    if phone:
        account['autodl_phone'] = phone
    if password:
        account['autodl_password'] = password
    if token:
        account['authorization'] = token
    _save_accounts_and_request_reload(config_path, raw, accounts, load_settings_fn=load_settings_fn, store_cls=store_cls)
    return f"账户凭据已更新并已请求重载: {account.get('name') or '-'}"


def toggle_account_enabled(
    args: Any,
    *,
    input_fn: Any,
    load_settings_fn: Callable[[str], Any],
    store_cls: type,
    read_raw_settings_fn: Callable[[str], dict[str, Any]] = read_raw_settings,
) -> str:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    raw = dict(read_raw_settings_fn(config_path) or {})
    accounts = _account_payloads(raw)
    index, message = _select_account_payload(accounts, input_fn)
    if index is None:
        return message
    account = accounts[index]
    account['enabled'] = not bool(account.get('enabled', True))
    _save_accounts_and_request_reload(config_path, raw, accounts, load_settings_fn=load_settings_fn, store_cls=store_cls)
    state = '启用' if account['enabled'] else '停用'
    return f"账户已{state}并已请求重载: {account.get('name') or '-'}"


def delete_account(
    args: Any,
    *,
    input_fn: Any,
    load_settings_fn: Callable[[str], Any],
    store_cls: type,
    read_raw_settings_fn: Callable[[str], dict[str, Any]] = read_raw_settings,
) -> str:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    raw = dict(read_raw_settings_fn(config_path) or {})
    accounts = _account_payloads(raw)
    if len(accounts) <= 1:
        return '至少保留一个账户，未删除'
    index, message = _select_account_payload(accounts, input_fn)
    if index is None:
        return message
    account = accounts[index]
    confirm = input_fn(f"确认删除 {account.get('name') or '-'}？输入 yes 确认: ").strip().lower()
    if confirm != 'yes':
        return '已取消删除账户'
    removed = accounts.pop(index)
    _save_accounts_and_request_reload(config_path, raw, accounts, load_settings_fn=load_settings_fn, store_cls=store_cls)
    return f"账户已删除并已请求重载: {removed.get('name') or '-'}"


def _select_login_account(
    args: Any,
    settings: Any,
    *,
    input_fn: Any,
    select_accounts_fn: Callable[..., list[Any]],
) -> tuple[Any | None, str]:
    if getattr(args, 'account', None):
        try:
            return select_accounts_fn(settings, getattr(args, 'account'))[0], ''
        except ValueError as exc:
            return None, str(exc)
    try:
        accounts = select_accounts_fn(settings, None)
    except ValueError as exc:
        return None, str(exc)
    if not accounts:
        return None, '没有可登录的启用账户'
    if len(accounts) == 1:
        return accounts[0], ''
    print(color('选择要登录的账户:', BLUE))
    for index, account in enumerate(accounts, start=1):
        print(f'  {color(str(index), CYAN)}  {account.name}')
    raw = input_fn('账户编号，0 取消: ').strip().lower()
    if raw in {'0', 'q', 'quit', 'cancel', ''}:
        return None, '已取消登录'
    try:
        index = int(raw) - 1
    except ValueError:
        return None, '账户编号必须是数字'
    if index < 0 or index >= len(accounts):
        return None, '账户编号不存在'
    return accounts[index], ''


def login_accounts(
    args: Any,
    *,
    input_fn: Any,
    load_settings_fn: Callable[[str], Any],
    store_cls: type,
    select_accounts_fn: Callable[..., list[Any]],
    resolve_authorization_fn: Callable[..., Any],
) -> str:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    settings = load_settings_fn(config_path)
    store = store_cls(settings.storage.database_file)
    store.init_schema()
    account, message = _select_login_account(args, settings, input_fn=input_fn, select_accounts_fn=select_accounts_fn)
    if account is None:
        return message
    try:
        resolve_authorization_fn(
            account.to_auth_settings(),
            headed=bool(getattr(args, 'headed', False)),
            force_refresh=True,
            store=store,
            account_name=account.name,
        )
    except AuthError as exc:
        return f'{account.name} 登录失败: {exc}'
    return f'账户登录成功: {account.name}'


def check_account_health(
    args: Any,
    *,
    load_settings_fn: Callable[[str], Any],
    store_cls: type,
    select_accounts_fn: Callable[..., list[Any]],
    build_client_fn: Callable[..., Any],
) -> str:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    try:
        settings = load_settings_fn(config_path)
        store = store_cls(settings.storage.database_file)
        store.init_schema()
        accounts = select_accounts_fn(settings, getattr(args, 'account', None))
    except Exception as exc:
        return f'账户健康检查失败: {exc}'

    ok: list[str] = []
    failed: list[str] = []
    for account in accounts:
        try:
            client = build_client_fn(settings, bool(getattr(args, 'headed', False)), account=account, store=store)
            instances = list(client.list_instances())
            ok.append(f'{account.name}({len(instances)} 台)')
        except Exception as exc:
            failed.append(f'{account.name}: {exc}')

    summary = f'账户健康检查: 正常 {len(ok)} 个 | 异常 {len(failed)} 个'
    details: list[str] = []
    if ok:
        details.append('正常 ' + ', '.join(ok[:3]))
    if failed:
        details.append('异常 ' + '; '.join(failed[:3]))
    return summary if not details else f'{summary} | {" | ".join(details)}'


class _AccountHealthTask:
    def __init__(
        self,
        args: Any,
        *,
        load_settings_fn: Callable[[str], Any],
        store_cls: type,
        select_accounts_fn: Callable[..., list[Any]],
        build_client_fn: Callable[..., Any],
    ):
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        self._thread = threading.Thread(
            target=self._run,
            kwargs={
                'args': args,
                'load_settings_fn': load_settings_fn,
                'store_cls': store_cls,
                'select_accounts_fn': select_accounts_fn,
                'build_client_fn': build_client_fn,
            },
            name='ui-account-health-check',
            daemon=True,
        )
        self._thread.start()

    def _run(
        self,
        *,
        args: Any,
        load_settings_fn: Callable[[str], Any],
        store_cls: type,
        select_accounts_fn: Callable[..., list[Any]],
        build_client_fn: Callable[..., Any],
    ) -> None:
        try:
            self._queue.put((
                'ok',
                check_account_health(
                    args,
                    load_settings_fn=load_settings_fn,
                    store_cls=store_cls,
                    select_accounts_fn=select_accounts_fn,
                    build_client_fn=build_client_fn,
                ),
            ))
        except Exception as exc:
            self._queue.put(('error', exc))

    def done(self) -> bool:
        return not self._queue.empty()

    def wait(self, timeout: float) -> bool:
        if self.done():
            return True
        try:
            status, payload = self._queue.get(timeout=timeout)
        except queue.Empty:
            return False
        self._queue.put((status, payload))
        return True

    def result(self) -> str:
        status, payload = self._queue.get_nowait()
        if status == 'error':
            raise payload
        return str(payload)


def _consume_account_health_task(task: Any | None) -> tuple[Any | None, str]:
    if task is None:
        return None, ''
    if not task.done():
        return task, ''
    try:
        return None, task.result()
    except Exception as exc:
        return None, f'账户健康检查失败: {exc}'


def run_account_menu(
    args: Any,
    *,
    input_fn=builtins.input,
    load_settings_fn: Callable[[str], Any],
    store_cls: type,
    select_accounts_fn: Callable[..., list[Any]],
    account_status_rows_fn: Callable[..., list[dict[str, Any]]],
    resolve_authorization_fn: Callable[..., Any],
    build_client_fn: Callable[..., Any],
) -> str:
    notice = ''
    health_task: Any | None = None
    while True:
        health_task, health_notice = _consume_account_health_task(health_task)
        if health_notice:
            notice = health_notice
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
            notice = '已刷新账户状态'
            continue
        if choice == '2':
            notice = add_account(
                args,
                input_fn=input_fn,
                load_settings_fn=load_settings_fn,
                store_cls=store_cls,
            )
            continue
        if choice == '3':
            notice = login_accounts(
                args,
                input_fn=input_fn,
                load_settings_fn=load_settings_fn,
                store_cls=store_cls,
                select_accounts_fn=select_accounts_fn,
                resolve_authorization_fn=resolve_authorization_fn,
            )
            continue
        if choice == '4':
            if health_task is not None and not health_task.done():
                notice = '账户健康检查中，请稍后查看结果'
                continue
            health_task = _AccountHealthTask(
                args,
                load_settings_fn=load_settings_fn,
                store_cls=store_cls,
                select_accounts_fn=select_accounts_fn,
                build_client_fn=build_client_fn,
            )
            if health_task.wait(0.05):
                health_task, notice = _consume_account_health_task(health_task)
            else:
                notice = '账户健康检查已提交'
            continue
        if choice == '5':
            notice = edit_account_credentials(
                args,
                input_fn=input_fn,
                load_settings_fn=load_settings_fn,
                store_cls=store_cls,
            )
            continue
        if choice == '6':
            notice = toggle_account_enabled(
                args,
                input_fn=input_fn,
                load_settings_fn=load_settings_fn,
                store_cls=store_cls,
            )
            continue
        if choice == '7':
            notice = delete_account(
                args,
                input_fn=input_fn,
                load_settings_fn=load_settings_fn,
                store_cls=store_cls,
            )
            continue
        notice = '无效选择，请输入 1/2/3/4/5/6/7/0'
