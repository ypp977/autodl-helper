from __future__ import annotations

import builtins
import queue
import threading
import time
from collections import Counter
from typing import Any, Callable

from autodl_helper.cli.shared_settings import validate_settings
from autodl_helper.tasks.keeper_results import keeper_reason_label, keeper_result_label

from .background_input import BackgroundInputTask
from .render import BLUE, CYAN, GREEN, RED, YELLOW, clear_screen, color, print_menu_groups, render_notice, render_rule, render_section


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
        precheck_errors: list[str] = []
        keeper_settings = getattr(getattr(settings, 'tasks', None), 'keeper', None)
        if not bool(getattr(keeper_settings, 'enabled', False)):
            precheck_errors.append('Keeper 未启用')
        precheck_errors.extend(validate_settings(settings, purpose='run_keeper'))
        if precheck_errors:
            return 'Keeper 预检失败: ' + '; '.join(precheck_errors[:3])
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
        return f'{summary}\n{progress_line} | 失败 {reason_text} | 详情见执行详情'
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


def _short_time(value: Any) -> str:
    raw = str(value or '').strip()
    if not raw:
        return '-'
    text = raw.replace('T', ' ').split('+', 1)[0].split('.', 1)[0]
    parts = text.split(' ')
    if len(parts) == 2 and len(parts[0]) >= 10:
        return f'{parts[0][5:]} {parts[1][:5]}'
    return text or '-'


def keeper_details(
    args: Any,
    *,
    load_settings_fn: Callable[[str], Any],
    store_cls: type,
    limit: int = 12,
) -> str:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    try:
        settings = load_settings_fn(config_path)
        store = store_cls(settings.storage.database_file)
        store.init_schema()
        rows = store.read_history(
            account_name=getattr(args, 'account', None),
            task_type='keeper',
            limit=limit,
        )
    except Exception as exc:
        return f'执行详情读取失败: {exc}'

    if not rows:
        return '执行详情: 暂无历史记录'

    lines = [color('执行详情', BLUE)]
    for row in rows[:limit]:
        payload = row.payload or {}
        next_keeper = payload.get('next_keeper_time') or payload.get('next_time') or ''
        deadline = payload.get('release_deadline') or payload.get('release_at') or ''
        result_label = '失败' if row.result.startswith('keeper_failed') else keeper_result_label(row.result)
        status_color = RED if row.result.startswith('keeper_failed') else (GREEN if row.result == 'keeper_executed' else YELLOW)
        lines.append(
            f"- {color(row.account_name, CYAN)} {color(row.instance_id or '-', CYAN)} | "
            f"{color(result_label, status_color)} | 原因 {keeper_reason_label(row.reason)} | "
            f"下次 {_short_time(next_keeper)} | 释放 {_short_time(deadline)}"
        )
    return '\n'.join(lines)


class _KeeperRunTask:
    def __init__(
        self,
        args: Any,
        *,
        load_settings_fn: Callable[[str], Any],
        store_cls: type,
        select_accounts_fn: Callable[..., list[Any]],
        run_keeper_only_fn: Callable[..., list[Any]],
        result_label_fn: Callable[[Any], str],
    ):
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
        self._thread = threading.Thread(
            target=self._run,
            kwargs={
                'args': args,
                'load_settings_fn': load_settings_fn,
                'store_cls': store_cls,
                'select_accounts_fn': select_accounts_fn,
                'run_keeper_only_fn': run_keeper_only_fn,
                'result_label_fn': result_label_fn,
            },
            name='ui-keeper-run-once',
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
        run_keeper_only_fn: Callable[..., list[Any]],
        result_label_fn: Callable[[Any], str],
    ) -> None:
        try:
            self._queue.put((
                'ok',
                run_keeper_once(
                    args,
                    load_settings_fn=load_settings_fn,
                    store_cls=store_cls,
                    select_accounts_fn=select_accounts_fn,
                    run_keeper_only_fn=run_keeper_only_fn,
                    result_label_fn=result_label_fn,
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


def _consume_keeper_run_task(task: Any | None) -> tuple[Any | None, str]:
    if task is None:
        return None, ''
    if not task.done():
        return task, ''
    try:
        return None, task.result()
    except Exception as exc:
        return None, f'Keeper 执行失败: {exc}'


def _print_keeper_menu(notice: str) -> None:
    clear_screen(enabled=True)
    print(f'\n{render_section("Keeper 管理", color_enabled=True)}')
    print(render_rule())
    if notice:
        print(render_notice(notice, color_enabled=True))
    print(color('配置入口: 配置管理 > Keeper 配置', BLUE))
    print(color('立即执行 Keeper 会访问 AutoDL 官方接口，完成后自动回显结果。', BLUE))
    print_menu_groups([
        ('执行', [('1', '立即执行'), ('2', '恢复任务')]),
        ('查看', [('3', '执行详情')]),
        ('返回', [('0', '返回')]),
    ])


def _read_keeper_choice_with_background_repaint(
    *,
    input_fn: Any,
    keeper_task: Any | None,
) -> tuple[str, Any | None, str]:
    if keeper_task is None:
        return input_fn('选择编号: ').strip().lower(), keeper_task, ''
    if keeper_task.done():
        keeper_task, notice = _consume_keeper_run_task(keeper_task)
        if notice:
            _print_keeper_menu(notice)
        return input_fn('选择编号: ').strip().lower(), keeper_task, notice

    input_task = BackgroundInputTask(input_fn, '选择编号: ')
    notice = ''
    while not input_task.done():
        keeper_task, keeper_notice = _consume_keeper_run_task(keeper_task)
        if keeper_notice:
            notice = keeper_notice
            _print_keeper_menu(notice)
            print('选择编号: ', end='', flush=True)
        time.sleep(0.05)
    return input_task.result().strip().lower(), keeper_task, notice


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
    notice = ''
    keeper_task: Any | None = None
    while True:
        keeper_task, keeper_notice = _consume_keeper_run_task(keeper_task)
        if keeper_notice:
            notice = keeper_notice
        _print_keeper_menu(notice)
        notice = ''
        choice, keeper_task, input_notice = _read_keeper_choice_with_background_repaint(
            input_fn=input_fn,
            keeper_task=keeper_task,
        )
        if input_notice:
            notice = input_notice
        if choice == '0':
            return ''
        if choice == '1':
            if keeper_task is not None and not keeper_task.done():
                notice = 'Keeper 执行中，请稍后查看结果'
                continue
            keeper_task = _KeeperRunTask(
                args,
                load_settings_fn=load_settings_fn,
                store_cls=store_cls,
                select_accounts_fn=select_accounts_fn,
                run_keeper_only_fn=run_keeper_only_fn,
                result_label_fn=result_label_fn,
            )
            if keeper_task.wait(0.05):
                keeper_task, notice = _consume_keeper_run_task(keeper_task)
            else:
                notice = 'Keeper 执行任务已提交'
            continue
        if choice == '2':
            return resume_keeper(
                args,
                load_settings_fn=load_settings_fn,
                store_cls=store_cls,
                select_accounts_fn=select_accounts_fn,
            )
        if choice == '3':
            return keeper_details(
                args,
                load_settings_fn=load_settings_fn,
                store_cls=store_cls,
            )
        notice = '无效选择，请输入 1/2/3/0'
