from __future__ import annotations

from typing import Any

from ...support.rendering import _render_scoped_list_page
from ...presentation import CYAN, _format_human_datetime, _format_relative_deadline, _heading, _key_value, _separator
from ...scheduled import _keeper_reason_label, _keeper_result_label
from ...shared import _instance_gpu_summary, _instance_idle_gpu_summary, _normalize_charge_type, _normalize_instance_status, _normalize_start_mode

__all__ = [
    "_render_instance_list_page",
    "_render_instance_detail",
    "_render_keeper_probe_list_page",
    "_render_keeper_probe_detail",
]


def _render_instance_list_page(account_label: str, rows: list[dict[str, Any]], *, page_status_lines: list[str] | None = None) -> str:
    running = sum(1 for row in rows if str(row.get("status") or "").lower() == "running")
    shutdown = sum(1 for row in rows if str(row.get("status") or "").lower() == "shutdown")
    return _render_scoped_list_page(
        "实例列表",
        account_label=account_label,
        metric_items=[("实例总数", len(rows)), ("运行中", running), ("已关机", shutdown)],
        page_status_lines=page_status_lines,
    )


def _render_instance_detail(row: dict[str, Any], account_label: str) -> str:
    gpu_total_raw = row.get("gpu_all_num")
    gpu_total = "" if gpu_total_raw is None else str(gpu_total_raw).strip()
    gpu_idle_raw = row.get("gpu_idle_num")
    gpu_idle = "" if gpu_idle_raw is None else str(gpu_idle_raw).strip()
    if gpu_idle not in {"", "-"} and gpu_total not in {"", "-"}:
        idle_summary = f"{gpu_idle} / {gpu_total}"
    elif gpu_idle not in {"", "-"}:
        idle_summary = gpu_idle
    else:
        idle_summary = "-"
    lines = [
        _heading("实例详情", color=CYAN),
        _separator(),
        _key_value("当前账号", account_label),
        _key_value("实例 ID", row.get("instance_id") or "-"),
        _key_value("名称", row.get("name") or "-"),
        _key_value("地区", row.get("region") or "-"),
        _key_value("状态", _normalize_instance_status(row.get("status"))),
        _key_value("机器/规格", row.get("machine_alias") or "-"),
        _key_value("规格", row.get("spec") or row.get("machine_alias") or "-"),
        _key_value("GPU 配置", f"{gpu_total} 卡" if gpu_total not in {"", "-"} else "-"),
        _key_value("空闲 GPU", idle_summary),
        _key_value("启动模式", _normalize_start_mode(row.get("start_mode"))),
        _key_value("计费方式", _normalize_charge_type(row.get("charge_type"))),
        _key_value("最近状态时间", _format_human_datetime(str(row.get("status_at") or ""))),
    ]
    raw_release_at = str(row.get("release_at") or "").strip()
    if raw_release_at:
        lines.append(_key_value("预计释放时间", _format_human_datetime(raw_release_at)))
    return "\n".join(lines)


def _render_keeper_probe_list_page(account_label: str, rows: list[dict[str, Any]], *, page_status_lines: list[str] | None = None) -> str:
    eligible = sum(1 for row in rows if bool(row.get("eligible")))
    return _render_scoped_list_page(
        "Keeper 检测",
        account_label=account_label,
        metric_items=[("实例总数", len(rows)), ("本次可执行", eligible)],
        page_status_lines=page_status_lines,
    )


def _render_keeper_probe_detail(row: dict[str, Any], account_label: str) -> str:
    release_text = _format_relative_deadline(str(row.get("release_deadline") or "")) if row.get("release_deadline") else "暂无结果"
    keeper_text = _format_relative_deadline(str(row.get("next_keeper_time") or "")) if row.get("next_keeper_time") else "暂无结果"
    lines = [
        _heading("Keeper 检测详情", color=CYAN),
        _separator(),
        _key_value("当前账号", account_label),
        _key_value("下次执行时间", row.get("_keeper_next_run_text") or "暂无结果"),
        _key_value("上次执行时间", row.get("_keeper_last_run_text") or "暂无结果"),
        _key_value("实例 ID", row.get("instance_id") or "未设置"),
        _key_value("当前状态", row.get("status") or "未知"),
        _key_value("当前结论", _keeper_result_label(str(row.get("result") or "暂无结果"))),
        _key_value("下一步动作", _keeper_reason_label(str(row.get("reason") or "暂无结果"))),
        _key_value("距离释放", release_text),
        _key_value("距离下次 Keeper", keeper_text),
        _key_value("最近关机时间", _format_human_datetime(str(row.get("stopped_at") or "")) if row.get("stopped_at") else "暂无结果"),
    ]
    if row.get("executed_in_cycle"):
        lines.append(_key_value("本周期状态", "已执行过"))
    return "\n".join(lines)
