from __future__ import annotations

import argparse


def _add_common_runtime_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--config', default='config.yaml', help='YAML 配置文件路径')
    parser.add_argument('--headed', action='store_true', help='使用有界面的 Playwright 浏览器模式')
    parser.add_argument('--account', help='只操作指定配置账户')


def _add_path_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--state-file', default='.autodl-helper-state.json', help='本地运行状态文件路径')
    parser.add_argument('--lock-file', default='.autodl-helper.lock', help='本地锁文件路径')


def _add_run_args(parser: argparse.ArgumentParser) -> None:
    _add_common_runtime_args(parser)
    parser.add_argument('--run-once', action='store_true', help='只运行一次后退出')
    _add_path_args(parser)


def _add_keeper_override_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--shutdown-release-after-hours', type=int, help='覆盖 Keeper 关机后最长保留时间，单位小时')
    parser.add_argument('--keeper-trigger-before-hours', type=int, help='覆盖 Keeper 释放前多久开始保活，单位小时')
    parser.add_argument('--start-cooldown-minutes', type=int, help='覆盖 Keeper 开机冷却时间，单位分钟')
    parser.add_argument('--stop-cooldown-minutes', type=int, help='覆盖 Keeper 关机冷却时间，单位分钟')
    parser.add_argument('--fallback-to-status-at', dest='fallback_to_status_at', action='store_true', help='启用 Keeper status_at 时间兜底')
    parser.add_argument('--no-fallback-to-status-at', dest='fallback_to_status_at', action='store_false', help='关闭 Keeper status_at 时间兜底')
    parser.set_defaults(fallback_to_status_at=None)


def _add_scheduled_override_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--scheduled-poll-interval', type=int, help='覆盖抢机轮询间隔，单位秒')
    parser.add_argument('--scheduled-job', help='只运行指定抢机任务名或实例 ID')
    parser.add_argument('--target-time', help='覆盖抢机目标时间，支持 9、930、09:30、1430')
    parser.add_argument('--advance-hours', type=float, help='覆盖抢机提前多久，单位小时')


def _add_auth_override_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--lightweight-mode', choices=['off', 'normal', 'aggressive'], help='覆盖账户轻量认证模式')
    parser.add_argument('--runtime-auth-revalidate-seconds', type=int, help='覆盖运行时 token 复检窗口，单位秒')
    parser.add_argument('--force-refresh-min-interval-seconds', type=int, help='覆盖 Playwright 强制刷新最小间隔，单位秒')
    parser.add_argument('--auth-failure-backoff-seconds', type=int, help='覆盖认证失败退避时间，单位秒')


def _add_runtime_override_args(parser: argparse.ArgumentParser) -> None:
    _add_keeper_override_args(parser)
    _add_scheduled_override_args(parser)
    _add_auth_override_args(parser)


def _add_config_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument('--config', default='config.yaml', help='YAML 配置文件路径')


def _add_service_command(subparsers: argparse._SubParsersAction, name: str, help_text: str) -> None:
    command_parser = subparsers.add_parser(name, help=help_text)
    _add_config_arg(command_parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description='autodl-helper：终端 UI 主控制台 + CLI 高级/自动化入口')
    subparsers = parser.add_subparsers(dest='command')
    subparsers.required = True

    init_parser = subparsers.add_parser('init', help='初始化本地 .env 和 config.yaml')
    _add_config_arg(init_parser)
    init_parser.add_argument('--force', action='store_true', help='覆盖已有初始化文件')
    init_parser.add_argument('--yes', action='store_true', help='不交互，直接使用默认值')

    login_parser = subparsers.add_parser('login', help='刷新/获取一个或全部账户凭据')
    _add_common_runtime_args(login_parser)
    login_parser.add_argument('--all', action='store_true', help='刷新全部启用账户')

    accounts_parser = subparsers.add_parser('accounts', help='显示账户认证状态')
    _add_config_arg(accounts_parser)
    accounts_parser.add_argument('--account', help='只显示指定账户')
    accounts_parser.add_argument('--json', action='store_true', help='输出 JSON 而不是表格')

    list_parser = subparsers.add_parser('list', help='列出 AutoDL 实例')
    _add_common_runtime_args(list_parser)
    list_parser.add_argument('--json', action='store_true', help='输出 JSON 而不是表格')

    run_parser = subparsers.add_parser('run', help='运行前台任务')
    run_subparsers = run_parser.add_subparsers(dest='run_command')
    run_subparsers.required = True
    for name, mode, help_text in (
        ('daemon', 'all', '以前台方式运行 daemon 任务'),
        ('keeper', 'keeper', '只运行 Keeper 保活'),
        ('scheduled', 'scheduled_start', '只运行抢机任务'),
    ):
        command_parser = run_subparsers.add_parser(name, help=help_text)
        command_parser.set_defaults(run_mode=mode)
        _add_run_args(command_parser)
        _add_runtime_override_args(command_parser)

    service_parser = subparsers.add_parser('service', help='管理后台服务')
    service_subparsers = service_parser.add_subparsers(dest='service_command')
    service_subparsers.required = True
    _add_service_command(service_subparsers, 'install', '安装 run daemon 后台服务')
    _add_service_command(service_subparsers, 'start', '启动已安装的后台服务')
    _add_service_command(service_subparsers, 'stop', '停止已安装的后台服务')
    _add_service_command(service_subparsers, 'restart', '重启已安装的后台服务')
    _add_service_command(service_subparsers, 'status', '显示后台服务和 daemon 状态')
    _add_service_command(service_subparsers, 'uninstall', '卸载后台服务')

    ui_parser = subparsers.add_parser('ui', help='启动终端 UI 主控制台')
    _add_common_runtime_args(ui_parser)
    _add_path_args(ui_parser)

    debug_parser = subparsers.add_parser('debug', help='诊断和排障命令')
    debug_subparsers = debug_parser.add_subparsers(dest='debug_command')
    debug_subparsers.required = True

    health_parser = debug_subparsers.add_parser('health', help='运行本地运行状态检查')
    _add_common_runtime_args(health_parser)
    _add_path_args(health_parser)
    health_parser.add_argument('--smoke', action='store_true', help='同时执行认证和实例列表冒烟检查')
    health_parser.add_argument('--json', action='store_true', help='输出 JSON 状态/错误信封')

    db_parser = debug_subparsers.add_parser('db', help='检查 SQLite schema 和可写性')
    _add_common_runtime_args(db_parser)
    db_parser.add_argument('--json', action='store_true', help='输出 JSON 状态/错误信封')

    auth_parser = debug_subparsers.add_parser('auth', help='汇总 SQLite 事件日志中的认证失败信号')
    _add_common_runtime_args(auth_parser)
    auth_parser.add_argument('--limit', type=int, default=50, help='最多输出的分组行数')
    auth_parser.add_argument('--json', action='store_true', help='输出排障 JSON')
    auth_parser.add_argument('--only-unmapped', action='store_true', help='只显示当前未覆盖的 code/msg 组合')
    auth_parser.add_argument('--only-likely-auth', action='store_true', help='只保留疑似认证相关信号并过滤明显噪音')
    auth_parser.add_argument('--suggest-patch', action='store_true', help='生成 auth_error_signals.py 建议补丁内容')
    auth_parser.add_argument('--apply-suggested-patch', action='store_true', help='已废弃的不安全选项；请使用 --suggest-patch 后手动审查')

    history_parser = debug_subparsers.add_parser('history', help='显示 SQLite 中近期 Keeper/抢机历史')
    _add_common_runtime_args(history_parser)
    history_parser.add_argument('--task', choices=['keeper', 'scheduled_start'], help='按任务类型过滤')
    history_parser.add_argument('--event-type', help='按精确 event_type 过滤，例如 scheduled.started')
    history_parser.add_argument('--limit', type=int, default=20, help='最多输出行数')
    history_parser.add_argument('--json', action='store_true', help='输出排障 JSON')

    config_parser = subparsers.add_parser('config', help='查看和校验配置')
    config_subparsers = config_parser.add_subparsers(dest='config_command')
    config_subparsers.required = True

    config_show_parser = config_subparsers.add_parser('show', help='显示从文件/env 加载后的配置')
    _add_config_arg(config_show_parser)
    config_show_parser.add_argument('--account', help='只显示指定账户配置')
    config_show_parser.add_argument('--json', action='store_true', help='输出 JSON 和 JSON 错误')

    config_validate_parser = config_subparsers.add_parser('validate', help='只校验配置')
    _add_config_arg(config_validate_parser)
    config_validate_parser.add_argument('--account', help='只解析指定账户配置')
    config_validate_parser.add_argument('--json', action='store_true', help='输出 JSON 状态/错误信封')
    _add_runtime_override_args(config_validate_parser)

    return parser
