from scripts.keeper_probe import format_probe_line
from autodl_helper.tasks.keeper import KeeperResult


def test_format_probe_line_is_clear_and_chinese():
    result = KeeperResult(
        instance_id='iid',
        status='shutdown',
        release_at='',
        release_source='stopped_at',
        started_at='2026-04-07T13:43:34+08:00',
        stopped_at='2026-04-07T21:00:04+08:00',
        status_at='',
        release_deadline='2026-04-22T21:00:04+08:00',
        next_keeper_time='2026-04-22T15:00:04+08:00',
        seconds_until_release=3600,
        seconds_until_keeper=0,
        started_duration_seconds=3600,
        shutdown_duration_seconds=7200,
        eligible=False,
        result='skip_recently_stopped',
        reason='stopped_within_cooldown',
    )

    line = format_probe_line(result)

    assert '实例ID=iid' in line
    assert 'keeper达标=否' in line
    assert '判断来源=关机时间' in line
    assert '结果=最近关机冷却中' in line
    assert '原因=最近关机时间未超过冷却窗口' in line
    assert '下次keeper时间=2026-04-22T15:00:04+08:00' in line
    assert 'eligible=' not in line
    assert 'status_at' not in line


def test_format_probe_line_only_shows_status_at_as_auxiliary_for_fallback():
    result = KeeperResult(
        instance_id='iid',
        status='shutdown',
        release_at='',
        release_source='fallback_status_at',
        started_at='',
        stopped_at='',
        status_at='2026-04-08T00:17:53+08:00',
        release_deadline='2026-04-23T00:17:53+08:00',
        next_keeper_time='2026-04-22T18:17:53+08:00',
        seconds_until_release=3600,
        seconds_until_keeper=1800,
        started_duration_seconds=None,
        shutdown_duration_seconds=7200,
        eligible=False,
        result='skip_recently_stopped',
        reason='fallback_status_at_recently_stopped',
    )

    line = format_probe_line(result)

    assert '辅助状态时间=2026-04-08T00:17:53+08:00' in line
