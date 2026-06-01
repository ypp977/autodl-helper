from __future__ import annotations

import builtins
from typing import Any, Callable

from .render import BLUE, GREEN, RED, YELLOW, clear_screen, color, print_menu_groups, render_notice, render_rule, render_section, render_status


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


def _daemon_service_status_line(args: Any, service_status_fn: Callable[..., dict[str, Any]]) -> str:
    config_path = str(getattr(args, 'config', 'config.yaml'))
    try:
        status = service_status_fn(config_path=config_path)
    except Exception as exc:
        return color(f'服务状态读取失败: {exc}', RED)
    label = service_label(status)
    if status.get('running'):
        return render_status('服务', f'运行中: {label}', GREEN)
    if status.get('installed'):
        return render_status('服务', f'已停止: {label}', YELLOW)
    return render_status('服务', f'未安装: {label}', YELLOW)


def run_daemon_control_menu(
    args: Any,
    *,
    input_fn=builtins.input,
    service_status_fn: Callable[..., dict[str, Any]],
    start_service_fn: Callable[..., Any],
    stop_service_fn: Callable[..., Any],
    restart_service_fn: Callable[..., Any],
) -> str:
    notice = ''
    while True:
        clear_screen(enabled=True)
        print(f'\n{render_section("daemon 管理", color_enabled=True)}')
        print(render_rule())
        if notice:
            print(render_notice(notice, color_enabled=True))
            notice = ''
        print(_daemon_service_status_line(args, service_status_fn))
        print(color('服务入口: service install/start/stop/restart/status', BLUE))
        print_menu_groups([
            ('服务', [('1', '启动服务'), ('2', '停止服务'), ('3', '重启服务')]),
            ('返回', [('0', '返回')]),
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
        notice = '无效选择，请输入 1/2/3/0'
