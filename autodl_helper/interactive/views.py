from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from typing import Any

RESET = '\033[0m'
DIM = '\033[38;5;245m'
BLUE = '\033[38;5;75m'
CYAN = '\033[38;5;80m'
GREEN = '\033[38;5;114m'
YELLOW = '\033[38;5;179m'
RED = '\033[38;5;174m'
ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')


def _style(text: str, color: str, *, bold: bool = False) -> str:
    prefix = '\033[1m' if bold else ''
    return f'{prefix}{color}{text}{RESET}'


def _strip_ansi(text: str) -> str:
    return ANSI_RE.sub('', text)


def _display_width(text: str) -> int:
    width = 0
    for ch in _strip_ansi(text):
        if unicodedata.combining(ch):
            continue
        width += 2 if unicodedata.east_asian_width(ch) in {'W', 'F'} else 1
    return width


def _pad_display(text: str, width: int) -> str:
    return text + (' ' * max(0, width - _display_width(text)))


def _line(title: str, value: object) -> str:
    return f'{_pad_display(_style(title, DIM), 29)}: {value}'


def _parse_iso(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except ValueError:
        return None


def _human_time(raw: str | None) -> str:
    dt = _parse_iso(raw)
    if dt is None:
        return '暂无记录'
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone().strftime('%Y-%m-%d %H:%M:%S')


def _auth_status_label(row: dict[str, Any] | None) -> str:
    if not row:
        return '未配置'
    mapping = {
        'logged_in': '已登录(runtime)',
        'cached': '已缓存登录',
        'token_configured': 'token 可用',
        'login_ready': '可密码登录',
        'not_configured': '未配置',
    }
    return mapping.get(str(row.get('status') or ''), str(row.get('status') or '未配置'))


def _auth_source_label(row: dict[str, Any] | None) -> str:
    if not row:
        return '-'
    mapping = {
        'runtime': 'runtime',
        'sqlite-cache': 'sqlite-cache',
        'file-cache': 'file-cache',
        'config': 'config',
        'password-login-ready': 'password',
        'missing': '-',
    }
    return mapping.get(str(row.get('auth_source') or ''), str(row.get('auth_source') or '-'))


def _status_chip(label: str, tone: str) -> str:
    color = {
        'ok': GREEN,
        'warn': YELLOW,
        'bad': RED,
        'info': CYAN,
        'muted': DIM,
    }.get(tone, DIM)
    return _style(label, color, bold=True)


def render_dashboard(view: dict[str, Any]) -> str:
    current_account_row = view.get('current_account_row') or {}
    scheduled_jobs = view.get('scheduled_jobs') or []
    paused_jobs = sum(1 for job in scheduled_jobs if not job.get('enabled'))
    failed_jobs = sum(1 for job in scheduled_jobs if str(job.get('latest_result') or '') in {'deadline_failed', 'instance_missing'})
    total_jobs = len(scheduled_jobs)
    keeper_enabled = bool(view.get('effective_keeper_enabled'))
    keeper_status = _status_chip('运行中', 'ok') if keeper_enabled else _status_chip('已暂停', 'warn')
    login_status = _auth_status_label(current_account_row)
    login_tone = 'ok' if '已' in login_status or '可密码登录' in login_status else 'warn'
    enabled_jobs = sum(1 for job in scheduled_jobs if job.get('enabled'))
    keeper_summary = view.get('keeper_summary') or {}
    spotlight_jobs = [job for job in scheduled_jobs if job.get('enabled') or str(job.get('latest_result') or '') in {'deadline_failed', 'instance_missing'}]
    spotlight_jobs.sort(key=lambda job: (0 if str(job.get('latest_result') or '') in {'deadline_failed', 'instance_missing'} else 1, job.get('job_name') or ''))
    failure_jobs = [job for job in scheduled_jobs if str(job.get('latest_result') or '') in {'deadline_failed', 'instance_missing'}]
    failure_jobs.sort(key=lambda job: str(job.get('latest_created_at') or ''), reverse=True)
    latest_failed_job = failure_jobs[0].get('job_name') if failure_jobs else '暂无失败任务'
    service_state_label = str(view.get('service_state_label') or '已停止')
    service_state_tone = str(view.get('service_state_tone') or 'warn')
    if service_state_label == '运行中':
        service_detail = f"pid={view.get('service_pid') or '待确认'} / 最近心跳 {_human_time(view.get('service_last_seen_at'))}"
    elif service_state_label == '状态异常':
        service_detail = '最近心跳延迟或超时，建议去诊断页重启服务'
    elif service_state_label in {'已停止', '未安装'}:
        service_detail = '可去诊断页启动或重启服务'
    elif service_state_label == '启动中':
        service_detail = '后台正在拉起，请稍后刷新'
    else:
        service_detail = '可去诊断页检查后台服务状态'
    lines = [
        _style('AutoDL Helper CLI', BLUE, bold=True),
        _style('────────────────────────────────────────────────────────────────────────', DIM),
    ]
    page_status_lines = list(view.get('page_status_lines') or [])
    if page_status_lines:
        lines.extend(page_status_lines)
        lines.append('')
    lines.extend([
        _style('[当前会话]', DIM),
        _line('当前账号', view.get('current_account') or '未选择'),
        _line('登录状态', _status_chip(login_status, login_tone)),
        _line('最近登录时间', _human_time(current_account_row.get('cached_at_iso'))),
        _line('后台服务状态', _status_chip(service_state_label, service_state_tone)),
        _line('服务详情', service_detail),
        '',
        _style('[主任务]', DIM),
        _line('抢机器任务', f'总数 {total_jobs} / 已启用 {enabled_jobs} / 已暂停 {paused_jobs} / 失败 {failed_jobs}'),
        _line('最近失败任务', latest_failed_job),
        _line('Keeper 状态', keeper_status),
        _line('Keeper 任务', f"本次应接管 {keeper_summary.get('pending', 0)} / 未到窗口 {keeper_summary.get('not_due', 0)} / 状态异常 {keeper_summary.get('abnormal', 0)} / 一周内到期 {keeper_summary.get('expiring_soon', 0)}"),
    ])
    if spotlight_jobs:
        lines.extend(['', _style('[重点任务]', DIM)])
        for job in spotlight_jobs[:3]:
            tone = str(job.get('task_status_tone') or '')
            state_label = str(job.get('task_status_label') or '')
            if not state_label:
                tone = 'bad' if str(job.get('latest_result') or '') in {'deadline_failed', 'instance_missing'} else ('ok' if job.get('enabled') else 'warn')
                state_label = '失败' if tone == 'bad' else ('已启用' if job.get('enabled') else '已暂停')
            lines.append(_style(f"• {job.get('job_name') or '未命名任务'}", CYAN, bold=True))
            lines.append(_line('目标时间', job.get('target_time') or '未设置'))
            lines.append(_line('提前启动', f"{job.get('advance_hours')}h"))
            lines.append(_line('任务状态', _status_chip(state_label, tone)))
            lines.append('')
        if lines[-1] == '':
            lines.pop()
    return '\n'.join(lines)


def render_controls_snapshot(snapshot: dict[str, Any]) -> str:
    lines = ['守护进程状态', '=' * 72, '[runtime_control]']
    runtime = snapshot.get('runtime', {})
    if runtime:
        for key, value in sorted(runtime.items()):
            lines.append(f'- {key}={value}')
    else:
        lines.append('- 空')
    lines.append('')
    lines.append('[task_control]')
    task_controls = snapshot.get('task_controls', [])
    if task_controls:
        for row in task_controls:
            lines.append(f"- {row['account_name']} {row['task_type']} enabled={row['enabled']} source={row['source']}")
    else:
        lines.append('- 空')
    lines.append('')
    lines.append('[scheduled_job_control]')
    job_controls = snapshot.get('job_controls', [])
    if job_controls:
        for row in job_controls:
            lines.append(
                f"- {row['account_name']} {row['job_name']} enabled={row['enabled']} target_time={row['target_time_override'] or '-'} advance_hours={row['advance_hours_override']}"
            )
    else:
        lines.append('- 空')
    return '\n'.join(lines)


def render_candidate_explanation(view: dict[str, Any] | None) -> str:
    if not view:
        return '候选明细\n========================================================================\n无可用候选记录。'
    lines = [
        '候选明细',
        '=' * 72,
        f"时间: {view.get('created_at') or '-'}",
        f"账号: {view.get('account_name') or '-'}",
        f"job: {view.get('job_name') or '-'}",
        f"result/reason: {view.get('result') or '-'} / {view.get('reason') or '-'}",
        f"selector: {view.get('selector_summary') or '-'}",
        f"target_time: {view.get('target_time') or '-'}",
        f"advance_hours: {view.get('advance_hours') if view.get('advance_hours') is not None else '-'}",
        '',
        '排序解释',
        '1. 先按 selector 或 instance_id 过滤候选。',
        '2. 若配置了 priority，按 priority 列表顺序优先。',
        '3. priority 相同后，按 region_name -> machine_alias -> uuid 排序。',
        '4. 选择顺序：running_with_gpu 优先；否则选择第一个 eligible；否则没有可开机候选。',
    ]
    priority = view.get('priority') or []
    if priority:
        lines.append('5. 当前 priority 规则：')
        for item in priority:
            matcher = ', '.join(
                part for part in [
                    f"instance_id={item.get('instance_id')}" if item.get('instance_id') else '',
                    f"region={item.get('region')}" if item.get('region') else '',
                    f"machine_alias={item.get('machine_alias')}" if item.get('machine_alias') else '',
                ] if part
            ) or '-'
            lines.append(f"   - #{item.get('index')}: {matcher}")
    lines.extend(['', '候选列表'])
    details = view.get('candidate_details') or []
    for index, item in enumerate(details, start=1):
        selected = '*' if item.get('selected') else ' '
        gpu_idle = item.get('gpu_idle_num')
        priority_hit = (
            f" priority=#{item.get('matched_priority_index')}[{item.get('matched_priority_rule')}]"
            if item.get('matched_priority_index') is not None
            else ' priority=none'
        )
        lines.append(
            f"{selected} #{index} instance_id={item.get('instance_id')} label={item.get('label')} "
            f"status={item.get('status')} start_mode={item.get('start_mode')} "
            f"gpu_idle_num={'-' if gpu_idle is None else gpu_idle} "
            f"reason={item.get('reason')} ({item.get('reason_label')}){priority_hit}"
        )
    if details:
        selected_item = next((item for item in details if item.get('selected')), None)
        lines.extend(['', '逐候选淘汰解释'])
        for index, item in enumerate(details, start=1):
            label = item.get('label') or item.get('instance_id') or f'candidate-{index}'
            reason = item.get('reason') or ''
            priority_note = (
                f"命中 priority #{item.get('matched_priority_index')} ({item.get('matched_priority_rule')})。"
                if item.get('matched_priority_index') is not None
                else '未命中任何 priority 规则。'
            )
            if item.get('selected'):
                if reason == 'running_with_gpu':
                    explain = f'已选中：实例已在 GPU 模式运行，优先级最高。{priority_note}'
                elif reason == 'eligible':
                    explain = f'已选中：这是排序后的第一个 eligible 候选。{priority_note}'
                else:
                    explain = f"已选中：当前排序最优，reason={reason or '-'}。{priority_note}"
            else:
                if selected_item and selected_item.get('reason') == 'running_with_gpu':
                    explain = f'未选中：已有 running_with_gpu 候选优先。{priority_note}'
                elif reason != 'eligible':
                    explain = f"未选中：当前不满足开机条件，原因是 {item.get('reason_label') or reason or '-'}。{priority_note}"
                elif selected_item:
                    explain = f'未选中：虽然 eligible，但排序落后于已选中候选。{priority_note}'
                else:
                    explain = f"未选中：当前没有被选中，原因是 {item.get('reason_label') or reason or '-'}。{priority_note}"
            lines.append(f"- #{index} {label}: {explain}")
    selected_id = view.get('selected_instance_id')
    if selected_id:
        lines.extend([
            '',
            f"最终选中: {selected_id} {view.get('selected_instance_label') or ''}".rstrip(),
        ])
    return '\n'.join(lines)
