import logging
from datetime import datetime

from autodl_helper.config import ScheduledStartPriority, ScheduledStartSelector
from autodl_helper.tasks import scheduled_start
from autodl_helper.state import StateStore


class DummyClient:
    def __init__(self, open_result=True, instances=None):
        self.open_result = open_result
        self.open_calls = []
        self.instances = instances if instances is not None else [{'uuid': 'iid', 'status': 'stopped', 'region_name': '北京A区', 'machine_alias': 'gpu-1'}]
        self._list_calls = 0

    def open_machine(self, instance_id, payload='non_gpu'):
        self.open_calls.append((instance_id, payload))
        return self.open_result

    def list_instances(self, page=1, page_size=100):
        self._list_calls += 1
        if self.instances and isinstance(self.instances[0], list):
            index = min(self._list_calls - 1, len(self.instances) - 1)
            return self.instances[index]
        return self.instances


class DummyNotifier:
    def __init__(self):
        self.events = []

    def notify_task_result(self, *, task_type, title, message):
        self.events.append({'task_type': task_type, 'title': title, 'message': message})



def build_job():
    return scheduled_start.ScheduledStartJobRuntime(
        job_name='gpu-1',
        instance_id='iid',
        target_time='14:00',
        advance_hours=2,
        timezone='Asia/Shanghai',
        poll_interval_seconds=300,
    )



def test_job_outside_window_does_nothing(tmp_path):
    result = scheduled_start.run_scheduled_start_job(
        client=DummyClient(),
        notifier=DummyNotifier(),
        state_store=StateStore(tmp_path / 'state.json'),
        job=build_job(),
        now=datetime(2026, 4, 7, 11, 0, 0),
    )
    assert result == 'outside_window'
    assert result.event_type == 'scheduled.wait.window'



def test_job_force_run_now_ignores_window_start(tmp_path):
    notifier = DummyNotifier()
    client = DummyClient(
        open_result=True,
        instances=[
            [{'uuid': 'iid', 'status': 'stopped', 'start_mode': 'gpu', 'gpu_idle_num': 1, 'region_name': '北京A区', 'machine_alias': 'gpu-1'}],
            [{'uuid': 'iid', 'status': 'running', 'start_mode': 'gpu', 'region_name': '北京A区', 'machine_alias': 'gpu-1'}],
        ],
    )
    state_store = StateStore(tmp_path / 'state.json')

    result = scheduled_start.run_scheduled_start_job(
        client=client,
        notifier=notifier,
        state_store=state_store,
        job=build_job(),
        now=datetime(2026, 4, 7, 11, 0, 0),
        force_run_now=True,
    )

    assert result == 'started'
    assert client.open_calls == [('iid', 'gpu')]


def test_job_success_marks_state_and_notifies_once(tmp_path):
    notifier = DummyNotifier()
    client = DummyClient(
        open_result=True,
        instances=[
            [{'uuid': 'iid', 'status': 'stopped', 'start_mode': 'gpu', 'gpu_idle_num': 1, 'region_name': '北京A区', 'machine_alias': 'gpu-1'}],
            [{'uuid': 'iid', 'status': 'running', 'start_mode': 'gpu', 'region_name': '北京A区', 'machine_alias': 'gpu-1'}],
        ],
    )
    state_store = StateStore(tmp_path / 'state.json')

    result = scheduled_start.run_scheduled_start_job(
        client=client,
        notifier=notifier,
        state_store=state_store,
        job=build_job(),
        now=datetime(2026, 4, 7, 12, 30, 0),
    )

    assert result == 'started'
    assert result.event_type == 'scheduled.started'
    assert result.severity == 'success'
    assert client.open_calls == [('iid', 'gpu')]
    assert len(notifier.events) == 1
    assert state_store.was_notified('gpu-1', 'success', '2026-04-07') is True



def test_job_failure_after_deadline_notifies_once(tmp_path):
    notifier = DummyNotifier()
    client = DummyClient(open_result=False)
    state_store = StateStore(tmp_path / 'state.json')

    result = scheduled_start.run_scheduled_start_job(
        client=client,
        notifier=notifier,
        state_store=state_store,
        job=build_job(),
        now=datetime(2026, 4, 7, 14, 1, 0),
    )

    assert result == 'deadline_failed'
    assert result.event_type == 'scheduled.failed.deadline_missed'
    assert result.reason == 'deadline_missed'
    assert len(notifier.events) == 1
    assert 'result: deadline_failed' in notifier.events[0]['message']
    assert 'reason: deadline_missed' in notifier.events[0]['message']
    assert state_store.was_notified('gpu-1', 'failure', '2026-04-07') is True



def test_job_missing_instance_reports_reason_after_deadline(tmp_path):
    notifier = DummyNotifier()
    client = DummyClient(instances=[])
    state_store = StateStore(tmp_path / 'state.json')

    result = scheduled_start.run_scheduled_start_job(
        client=client,
        notifier=notifier,
        state_store=state_store,
        job=build_job(),
        now=datetime(2026, 4, 7, 14, 1, 0),
    )

    assert result == 'instance_missing'
    assert result.reason == 'instance_missing'
    assert 'reason: instance_missing' in notifier.events[0]['message']



def test_notification_dedup_is_per_job_name(tmp_path):
    notifier = DummyNotifier()
    client = DummyClient(
        open_result=True,
        instances=[
            [{'uuid': 'iid', 'status': 'stopped', 'start_mode': 'gpu', 'gpu_idle_num': 1, 'region_name': '北京A区', 'machine_alias': 'gpu-1'}],
            [{'uuid': 'iid', 'status': 'running', 'start_mode': 'gpu', 'region_name': '北京A区', 'machine_alias': 'gpu-1'}],
        ],
    )
    state_store = StateStore(tmp_path / 'state.json')
    now = datetime(2026, 4, 7, 12, 30, 0)

    job1 = scheduled_start.ScheduledStartJobRuntime(
        job_name='gpu-1',
        instance_id='iid',
        target_time='14:00',
        advance_hours=2,
        timezone='Asia/Shanghai',
        poll_interval_seconds=300,
    )
    job2 = scheduled_start.ScheduledStartJobRuntime(
        job_name='gpu-2',
        instance_id='iid',
        target_time='14:00',
        advance_hours=2,
        timezone='Asia/Shanghai',
        poll_interval_seconds=300,
    )

    result1 = scheduled_start.run_scheduled_start_job(
        client=client,
        notifier=notifier,
        state_store=state_store,
        job=job1,
        now=now,
    )
    result2 = scheduled_start.run_scheduled_start_job(
        client=client,
        notifier=notifier,
        state_store=state_store,
        job=job1,
        now=now,
    )
    result3 = scheduled_start.run_scheduled_start_job(
        client=client,
        notifier=notifier,
        state_store=state_store,
        job=job2,
        now=now,
    )

    assert result1 == 'started'
    assert result2 == 'already_running'
    assert result3 == 'already_running'
    assert len(notifier.events) == 2


def test_job_running_instance_short_circuits_to_success(tmp_path):
    notifier = DummyNotifier()
    client = DummyClient(instances=[{'uuid': 'iid', 'status': 'running', 'start_mode': 'gpu', 'region_name': '北京A区', 'machine_alias': 'gpu-1'}])
    state_store = StateStore(tmp_path / 'state.json')

    result = scheduled_start.run_scheduled_start_job(
        client=client,
        notifier=notifier,
        state_store=state_store,
        job=build_job(),
        now=datetime(2026, 4, 7, 12, 30, 0),
    )

    assert result == 'already_running'
    assert client.open_calls == []
    assert 'result: already_running' in notifier.events[0]['message']


def test_job_waits_when_gpu_is_not_available(tmp_path):
    notifier = DummyNotifier()
    client = DummyClient(
        instances=[{'uuid': 'iid', 'status': 'stopped', 'start_mode': 'gpu', 'gpu_idle_num': 0, 'region_name': '北京A区', 'machine_alias': 'gpu-1'}]
    )
    state_store = StateStore(tmp_path / 'state.json')

    result = scheduled_start.run_scheduled_start_job(
        client=client,
        notifier=notifier,
        state_store=state_store,
        job=build_job(),
        now=datetime(2026, 4, 7, 12, 30, 0),
    )

    assert result == 'waiting_for_gpu'
    assert result.event_type == 'scheduled.wait.gpu'
    assert result.reason == 'gpu_idle_zero'
    assert client.open_calls == []
    assert notifier.events == []


def test_job_running_without_gpu_is_not_treated_as_success(tmp_path):
    notifier = DummyNotifier()
    client = DummyClient(
        instances=[{'uuid': 'iid', 'status': 'running', 'start_mode': 'non_gpu', 'region_name': '北京A区', 'machine_alias': 'gpu-1'}]
    )
    state_store = StateStore(tmp_path / 'state.json')

    result = scheduled_start.run_scheduled_start_job(
        client=client,
        notifier=notifier,
        state_store=state_store,
        job=build_job(),
        now=datetime(2026, 4, 7, 12, 30, 0),
    )

    assert result == 'waiting_for_gpu'
    assert result.reason == 'running_without_gpu'
    assert client.open_calls == []
    assert notifier.events == []


def test_job_does_not_report_success_when_platform_starts_without_gpu(tmp_path):
    notifier = DummyNotifier()
    client = DummyClient(
        open_result=True,
        instances=[
            [{'uuid': 'iid', 'status': 'stopped', 'start_mode': 'gpu', 'gpu_idle_num': 1, 'region_name': '北京A区', 'machine_alias': 'gpu-1'}],
            [{'uuid': 'iid', 'status': 'running', 'start_mode': 'non_gpu', 'region_name': '北京A区', 'machine_alias': 'gpu-1'}],
        ],
    )
    state_store = StateStore(tmp_path / 'state.json')

    result = scheduled_start.run_scheduled_start_job(
        client=client,
        notifier=notifier,
        state_store=state_store,
        job=build_job(),
        now=datetime(2026, 4, 7, 12, 30, 0),
    )

    assert result == 'started_without_gpu'
    assert client.open_calls == [('iid', 'gpu')]
    assert notifier.events == []


def test_job_does_not_start_without_gpu_idle_signal_even_if_start_mode_is_gpu(tmp_path):
    notifier = DummyNotifier()
    client = DummyClient(
        instances=[{'uuid': 'iid', 'status': 'stopped', 'start_mode': 'gpu', 'region_name': '北京A区', 'machine_alias': 'gpu-1'}]
    )
    state_store = StateStore(tmp_path / 'state.json')

    result = scheduled_start.run_scheduled_start_job(
        client=client,
        notifier=notifier,
        state_store=state_store,
        job=build_job(),
        now=datetime(2026, 4, 7, 12, 30, 0),
    )

    assert result == 'waiting_for_gpu'
    assert result.reason == 'missing_gpu_idle_num'
    assert client.open_calls == []
    assert notifier.events == []


def test_job_logs_status_gpu_idle_start_mode_and_result(tmp_path, caplog):
    notifier = DummyNotifier()
    client = DummyClient(
        instances=[{'uuid': 'iid', 'status': 'stopped', 'start_mode': 'gpu', 'gpu_idle_num': 0, 'region_name': '北京A区', 'machine_alias': 'gpu-1'}]
    )
    state_store = StateStore(tmp_path / 'state.json')

    with caplog.at_level(logging.INFO):
        result = scheduled_start.run_scheduled_start_job(
            client=client,
            notifier=notifier,
            state_store=state_store,
            job=build_job(),
            now=datetime(2026, 4, 7, 12, 30, 0),
        )

    assert result == 'waiting_for_gpu'
    assert 'instance_id=iid' in caplog.text
    assert 'status=stopped' in caplog.text
    assert 'gpu_idle_num=0' in caplog.text
    assert 'start_mode=gpu' in caplog.text
    assert 'result=waiting_for_gpu' in caplog.text
    assert 'reason=gpu_idle_zero' in caplog.text


def test_selector_returns_waiting_for_instance_when_no_candidates_match(tmp_path):
    notifier = DummyNotifier()
    client = DummyClient(
        instances=[{'uuid': 'iid', 'status': 'stopped', 'gpu_idle_num': 1, 'machine_alias': 'RTX 2080 Ti * 1卡', 'region_name': '北京A区'}]
    )
    state_store = StateStore(tmp_path / 'state.json')
    job = scheduled_start.ScheduledStartJobRuntime(
        job_name='selector-job',
        selector=ScheduledStartSelector(regions=['上海A区'], gpu_model='RTX 3080 Ti', gpu_count=1, charge_types=[]),
        target_time='14:00',
        advance_hours=2,
        timezone='Asia/Shanghai',
        poll_interval_seconds=300,
    )

    result = scheduled_start.run_scheduled_start_job(
        client=client,
        notifier=notifier,
        state_store=state_store,
        job=job,
        now=datetime(2026, 4, 7, 12, 30, 0),
    )

    assert result == 'waiting_for_instance'
    assert result.reason == 'selector_no_match'
    assert result.candidate_count == 0


def test_selector_chooses_priority_candidate_among_multiple_eligible_instances(tmp_path):
    notifier = DummyNotifier()
    client = DummyClient(
        open_result=True,
        instances=[
            [
                {'uuid': 'iid-low', 'status': 'stopped', 'start_mode': 'gpu', 'gpu_idle_num': 1, 'gpu_all_num': 1, 'charge_type': 'payg', 'region_name': '北京A区', 'machine_alias': '351机', 'spec': 'RTX 3080 Ti * 1卡'},
                {'uuid': 'iid-high', 'status': 'stopped', 'start_mode': 'gpu', 'gpu_idle_num': 1, 'gpu_all_num': 1, 'charge_type': 'payg', 'region_name': '北京B区', 'machine_alias': '926机', 'spec': 'RTX 3080 Ti * 1卡'},
            ],
            [
                {'uuid': 'iid-low', 'status': 'stopped', 'start_mode': 'gpu', 'gpu_idle_num': 1, 'gpu_all_num': 1, 'charge_type': 'payg', 'region_name': '北京A区', 'machine_alias': '351机', 'spec': 'RTX 3080 Ti * 1卡'},
                {'uuid': 'iid-high', 'status': 'running', 'start_mode': 'gpu', 'gpu_idle_num': 0, 'gpu_all_num': 1, 'charge_type': 'payg', 'region_name': '北京B区', 'machine_alias': '926机', 'spec': 'RTX 3080 Ti * 1卡'},
            ],
        ],
    )
    state_store = StateStore(tmp_path / 'state.json')
    job = scheduled_start.ScheduledStartJobRuntime(
        job_name='selector-job',
        selector=ScheduledStartSelector(regions=['北京A区', '北京B区'], gpu_model='RTX 3080 Ti', gpu_count=1, charge_types=['payg']),
        priority=[ScheduledStartPriority(region='北京B区', machine_alias='926机')],
        target_time='14:00',
        advance_hours=2,
        timezone='Asia/Shanghai',
        poll_interval_seconds=300,
    )

    result = scheduled_start.run_scheduled_start_job(
        client=client,
        notifier=notifier,
        state_store=state_store,
        job=job,
        now=datetime(2026, 4, 7, 12, 30, 0),
    )

    assert result == 'started'
    assert result.selected_instance_id == 'iid-high'
    assert result.candidate_count == 2
    assert len(result.candidate_details) == 2
    assert result.candidate_details[0].instance_id == 'iid-high'
    assert result.candidate_details[0].selected is True
    assert result.candidate_details[0].reason == 'eligible'
    assert result.candidate_details[1].instance_id == 'iid-low'
    assert result.candidate_details[1].selected is False
    assert client.open_calls == [('iid-high', 'gpu')]
    assert 'selected_instance_id: iid-high' in notifier.events[0]['message']
    assert 'candidate_details:' in notifier.events[0]['message']
    assert 'iid-high' in notifier.events[0]['message']


def test_selector_reports_no_eligible_candidate_when_matches_exist_but_none_can_start(tmp_path):
    notifier = DummyNotifier()
    client = DummyClient(
        instances=[
            {'uuid': 'iid-1', 'status': 'stopped', 'gpu_idle_num': 0, 'gpu_all_num': 1, 'charge_type': 'payg', 'region_name': '北京A区', 'machine_alias': '351机', 'spec': 'RTX 3080 Ti * 1卡'},
            {'uuid': 'iid-2', 'status': 'running', 'start_mode': 'non_gpu', 'gpu_idle_num': 0, 'gpu_all_num': 1, 'charge_type': 'payg', 'region_name': '北京B区', 'machine_alias': '926机', 'spec': 'RTX 3080 Ti * 1卡'},
        ]
    )
    state_store = StateStore(tmp_path / 'state.json')
    job = scheduled_start.ScheduledStartJobRuntime(
        job_name='selector-job',
        selector=ScheduledStartSelector(regions=['北京A区', '北京B区'], gpu_model='RTX 3080 Ti', gpu_count=1, charge_types=['payg']),
        target_time='14:00',
        advance_hours=2,
        timezone='Asia/Shanghai',
        poll_interval_seconds=300,
    )

    result = scheduled_start.run_scheduled_start_job(
        client=client,
        notifier=notifier,
        state_store=state_store,
        job=job,
        now=datetime(2026, 4, 7, 12, 30, 0),
    )

    assert result == 'waiting_for_gpu'
    assert result.reason == 'no_eligible_candidate'
    assert result.candidate_count == 2
    assert len(result.candidate_details) == 2
    assert result.candidate_details[0].instance_id == 'iid-1'
    assert result.candidate_details[0].reason == 'gpu_idle_zero'
    assert result.candidate_details[0].reason_label == 'GPU 空闲数为 0'
    assert result.candidate_details[0].selected is False
    assert result.candidate_details[1].instance_id == 'iid-2'
    assert result.candidate_details[1].reason == 'running_without_gpu'
    assert result.candidate_details[1].reason_label == '实例已运行但不是 GPU 模式'


def test_selector_deadline_failure_notification_includes_candidate_details(tmp_path):
    notifier = DummyNotifier()
    client = DummyClient(
        instances=[
            {'uuid': 'iid-1', 'status': 'stopped', 'gpu_idle_num': 0, 'gpu_all_num': 1, 'charge_type': 'payg', 'region_name': '北京A区', 'machine_alias': '351机', 'spec': 'RTX 3080 Ti * 1卡'},
            {'uuid': 'iid-2', 'status': 'running', 'start_mode': 'non_gpu', 'gpu_idle_num': 0, 'gpu_all_num': 1, 'charge_type': 'payg', 'region_name': '北京B区', 'machine_alias': '926机', 'spec': 'RTX 3080 Ti * 1卡'},
        ]
    )
    state_store = StateStore(tmp_path / 'state.json')
    job = scheduled_start.ScheduledStartJobRuntime(
        job_name='selector-job',
        selector=ScheduledStartSelector(regions=['北京A区', '北京B区'], gpu_model='RTX 3080 Ti', gpu_count=1, charge_types=['payg']),
        target_time='14:00',
        advance_hours=2,
        timezone='Asia/Shanghai',
        poll_interval_seconds=300,
    )

    result = scheduled_start.run_scheduled_start_job(
        client=client,
        notifier=notifier,
        state_store=state_store,
        job=job,
        now=datetime(2026, 4, 7, 14, 1, 0),
    )

    assert result == 'deadline_failed'
    assert result.reason == 'deadline_missed'
    assert len(notifier.events) == 1
    assert 'candidate_details:' in notifier.events[0]['message']
    assert 'iid-1' in notifier.events[0]['message']
    assert 'GPU 空闲数为 0' in notifier.events[0]['message']
    assert 'iid-2' in notifier.events[0]['message']
    assert '实例已运行但不是 GPU 模式' in notifier.events[0]['message']
