import json
import os
import time
import logging

import pytest


from autodl_helper import auth, api


class DummyResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            exc = api.requests.HTTPError(f'status={self.status_code}')
            exc.response = self
            raise exc

    def json(self):
        return self._payload



def test_validate_authorization_returns_true_on_success(monkeypatch):
    def fake_post(**kwargs):
        return DummyResponse({'code': 'Success'})

    monkeypatch.setattr(auth.requests, 'post', fake_post)

    assert auth.validate_authorization('Bearer token') is True



def test_validate_authorization_returns_false_on_request_error(monkeypatch):
    def fake_post(**kwargs):
        raise auth.requests.RequestException('boom')

    monkeypatch.setattr(auth.requests, 'post', fake_post)

    assert auth.validate_authorization('Bearer token') is False


def test_resolve_authorization_force_refresh_fetches_new_token(monkeypatch):
    settings = auth.AuthSettings(
        authorization='Bearer stale',
        autodl_phone='18200000000',
        autodl_password='secret',
        cache_file='/tmp/autodl-helper-test-auth.json',
    )
    monkeypatch.setattr(auth, 'validate_authorization', lambda authorization, request_timeout=30: True)
    monkeypatch.setattr(auth, 'fetch_token_via_playwright', lambda **kwargs: 'Bearer fresh')
    auth.RUNTIME_AUTHORIZATION = None

    resolved = auth.resolve_authorization(settings, force_refresh=True)

    assert resolved == 'Bearer fresh'
    assert auth.RUNTIME_AUTHORIZATION == 'Bearer fresh'


def test_resolve_authorization_uses_cached_token_before_config_token(monkeypatch, tmp_path):
    cache_file = tmp_path / '.autodl-helper-auth.json'
    cache_file.write_text(json.dumps({'authorization': 'Bearer cache', 'cached_at': int(time.time())}))
    settings = auth.AuthSettings(
        authorization='Bearer config',
        cache_file=str(cache_file),
    )
    seen = []

    def fake_validate(authorization, request_timeout=30):
        seen.append(authorization)
        return authorization == 'Bearer cache'

    monkeypatch.setattr(auth, 'validate_authorization', fake_validate)
    auth.RUNTIME_AUTHORIZATION = None

    resolved = auth.resolve_authorization(settings)

    assert resolved == 'Bearer cache'
    assert seen[:2] == ['', 'Bearer cache']


def test_resolve_authorization_rewrites_expired_cache_when_token_still_valid(monkeypatch, tmp_path):
    cache_file = tmp_path / '.autodl-helper-auth.json'
    old_cached_at = int(time.time()) - 10_000
    cache_file.write_text(json.dumps({'authorization': 'Bearer cache', 'cached_at': old_cached_at}))
    settings = auth.AuthSettings(
        cache_file=str(cache_file),
        cache_max_age_seconds=1,
    )
    monkeypatch.setattr(auth, 'validate_authorization', lambda authorization, request_timeout=30: authorization == 'Bearer cache')
    auth.RUNTIME_AUTHORIZATION = None

    resolved = auth.resolve_authorization(settings)
    payload = json.loads(cache_file.read_text())

    assert resolved == 'Bearer cache'
    assert payload['authorization'] == 'Bearer cache'
    assert payload['cached_at'] >= old_cached_at


def test_resolve_authorization_writes_cache_file_with_restricted_permissions(monkeypatch, tmp_path):
    cache_file = tmp_path / '.autodl-helper-auth.json'
    settings = auth.AuthSettings(
        autodl_phone='18200000000',
        autodl_password='secret',
        cache_file=str(cache_file),
    )
    monkeypatch.setattr(auth, 'validate_authorization', lambda authorization, request_timeout=30: False)
    monkeypatch.setattr(auth, 'fetch_token_via_playwright', lambda **kwargs: 'Bearer fresh')
    auth.RUNTIME_AUTHORIZATION = None

    resolved = auth.resolve_authorization(settings)

    assert resolved == 'Bearer fresh'
    payload = json.loads(cache_file.read_text())
    assert payload['authorization'] == 'Bearer fresh'
    assert os.stat(cache_file).st_mode & 0o777 == 0o600



def test_autodl_client_post_json_uses_json_body(monkeypatch):
    captured = {}

    class DummySession:
        def post(self, **kwargs):
            captured.update(kwargs)
            return DummyResponse({'code': 'Success', 'data': {'ok': True}})

    client = api.AutoDLClient(authorization='Bearer token', min_day=7, session=DummySession())
    result = client.post_json('https://example.com', {'hello': 'world'})

    assert result['code'] == 'Success'
    assert captured['json'] == {'hello': 'world'}
    assert captured['headers']['Authorization'] == 'Bearer token'


def test_autodl_client_post_json_refreshes_auth_once_on_401():
    calls = []

    class DummySession:
        def post(self, **kwargs):
            calls.append(kwargs['headers']['Authorization'])
            if len(calls) == 1:
                return DummyResponse({'code': 'Unauthorized'}, status_code=401)
            return DummyResponse({'code': 'Success', 'data': {'ok': True}})

    refresh_calls = []

    def refresh_auth():
        refresh_calls.append(True)
        return 'Bearer fresh'

    client = api.AutoDLClient(
        authorization='Bearer stale',
        min_day=7,
        session=DummySession(),
        auth_refresh_callback=refresh_auth,
    )
    result = client.post_json('https://example.com', {'hello': 'world'})

    assert result['code'] == 'Success'
    assert calls == ['Bearer stale', 'Bearer fresh']
    assert refresh_calls == [True]
    assert client.authorization == 'Bearer fresh'


def test_autodl_client_post_json_refreshes_auth_once_on_business_auth_failure():
    calls = []

    class DummySession:
        def post(self, **kwargs):
            calls.append(kwargs['headers']['Authorization'])
            if len(calls) == 1:
                return DummyResponse({'code': 'Unauthorized', 'msg': 'token expired'})
            return DummyResponse({'code': 'Success', 'data': {'ok': True}})

    refresh_calls = []

    def refresh_auth():
        refresh_calls.append(True)
        return 'Bearer fresh'

    client = api.AutoDLClient(
        authorization='Bearer stale',
        min_day=7,
        session=DummySession(),
        auth_refresh_callback=refresh_auth,
    )
    result = client.post_json('https://example.com', {'hello': 'world'})

    assert result['code'] == 'Success'
    assert calls == ['Bearer stale', 'Bearer fresh']
    assert refresh_calls == [True]
    assert client.authorization == 'Bearer fresh'


def test_autodl_client_post_json_does_not_refresh_on_non_auth_business_failure():
    refresh_calls = []

    class DummySession:
        def post(self, **kwargs):
            return DummyResponse({'code': 'InsufficientBalance', 'msg': 'balance not enough'})

    def refresh_auth():
        refresh_calls.append(True)
        return 'Bearer fresh'

    client = api.AutoDLClient(
        authorization='Bearer stale',
        min_day=7,
        session=DummySession(),
        auth_refresh_callback=refresh_auth,
    )
    result = client.post_json('https://example.com', {'hello': 'world'})

    assert result['code'] == 'InsufficientBalance'
    assert refresh_calls == []


def test_autodl_client_open_machine_supports_explicit_payload():
    captured = {}

    class DummySession:
        def post(self, **kwargs):
            captured.update(kwargs)
            return DummyResponse({'code': 'Success'})

    client = api.AutoDLClient(authorization='Bearer token', min_day=7, session=DummySession())
    assert client.open_machine('iid', payload='gpu') is True
    assert captured['json'] == {'instance_uuid': 'iid', 'payload': 'gpu'}


def test_autodl_client_power_logs_redact_sensitive_response(caplog):
    class DummySession:
        def post(self, **kwargs):
            return DummyResponse({'code': 'Success', 'data': {'token': 'secret-token', 'authorization': 'Bearer secret'}})

    client = api.AutoDLClient(authorization='Bearer token', min_day=7, session=DummySession())

    with caplog.at_level(logging.INFO):
        assert client.open_machine('iid') is True

    assert 'secret-token' not in caplog.text
    assert 'Bearer secret' not in caplog.text
    assert '<redacted>' in caplog.text


def test_autodl_client_list_instances_error_redacts_sensitive_response():
    class DummySession:
        def post(self, **kwargs):
            return DummyResponse({
                'code': 'Failed',
                'msg': 'token=secret-token Authorization=Bearer secret',
                'data': {'authorization': 'Bearer nested-secret', 'cookie': 'session-secret'},
            })

    client = api.AutoDLClient(authorization='Bearer token', min_day=7, session=DummySession())

    with pytest.raises(RuntimeError) as exc_info:
        client.list_instances()

    message = str(exc_info.value)
    assert 'secret-token' not in message
    assert 'Bearer secret' not in message
    assert 'nested-secret' not in message
    assert 'session-secret' not in message
    assert '<redacted>' in message


def test_build_browser_launch_kwargs_prefers_explicit_executable():
    kwargs = auth.build_browser_launch_kwargs(headed=False, executable_path='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome')
    assert kwargs['headless'] is True
    assert kwargs['executable_path'] == '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'


def test_resolve_authorization_reads_and_updates_sqlite_cache(monkeypatch, tmp_path):
    from autodl_helper.core.store import SQLiteStore

    store = SQLiteStore(tmp_path / 'data.db')
    store.init_schema()
    store.set_auth_cache('main', 'Bearer cache', 123)
    settings = auth.AuthSettings(cache_file=str(tmp_path / 'fallback.json'))
    monkeypatch.setattr(auth, 'validate_authorization', lambda authorization, request_timeout=30: authorization == 'Bearer cache')
    auth.RUNTIME_AUTHORIZATION = None
    auth.RUNTIME_AUTHORIZATIONS.clear()

    resolved = auth.resolve_authorization(settings, store=store, account_name='main')

    assert resolved == 'Bearer cache'
    assert store.get_auth_cache('main')['authorization'] == 'Bearer cache'


def test_resolve_authorization_skips_revalidation_for_recent_runtime_token(monkeypatch, tmp_path):
    settings = auth.AuthSettings(cache_file=str(tmp_path / 'fallback.json'), lightweight_mode='normal')
    seen: list[str] = []

    monkeypatch.setattr(auth, 'validate_authorization', lambda authorization, request_timeout=30: seen.append(authorization) or True)
    auth.RUNTIME_AUTHORIZATION = None
    auth.RUNTIME_AUTHORIZATIONS.clear()
    auth.RUNTIME_AUTH_VALIDATED_AT.clear()
    auth._set_runtime_authorization('main', 'Bearer hot-cache')
    auth._mark_runtime_authorization_valid('main')

    resolved = auth.resolve_authorization(settings, account_name='main')

    assert resolved == 'Bearer hot-cache'
    assert seen == []


def test_resolve_auth_runtime_policy_supports_profiles():
    off_policy = auth.resolve_auth_runtime_policy(auth.AuthSettings(lightweight_mode='off'))
    normal_policy = auth.resolve_auth_runtime_policy(auth.AuthSettings(lightweight_mode='normal'))
    aggressive_policy = auth.resolve_auth_runtime_policy(auth.AuthSettings(lightweight_mode='aggressive'))

    assert off_policy.runtime_auth_revalidate_seconds == 0
    assert off_policy.force_refresh_min_interval_seconds == 0
    assert off_policy.auth_failure_backoff_seconds == 0
    assert normal_policy.runtime_auth_revalidate_seconds == 60
    assert normal_policy.force_refresh_min_interval_seconds == 90
    assert normal_policy.auth_failure_backoff_seconds == 30
    assert aggressive_policy.runtime_auth_revalidate_seconds == 180
    assert aggressive_policy.force_refresh_min_interval_seconds == 180
    assert aggressive_policy.auth_failure_backoff_seconds == 60


def test_resolve_auth_runtime_policy_zero_overrides_fall_back_to_profile():
    policy = auth.resolve_auth_runtime_policy(
        auth.AuthSettings(
            lightweight_mode='normal',
            runtime_auth_revalidate_seconds=0,
            force_refresh_min_interval_seconds=0,
            auth_failure_backoff_seconds=0,
        )
    )

    assert policy.runtime_auth_revalidate_seconds == 60
    assert policy.force_refresh_min_interval_seconds == 90
    assert policy.auth_failure_backoff_seconds == 30


def test_resolve_authorization_force_refresh_is_rate_limited(monkeypatch, tmp_path):
    settings = auth.AuthSettings(
        cache_file=str(tmp_path / 'fallback.json'),
        autodl_phone='18200000000',
        autodl_password='secret',
        lightweight_mode='normal',
        force_refresh_min_interval_seconds=600,
    )
    auth.RUNTIME_AUTHORIZATION = None
    auth.RUNTIME_AUTHORIZATIONS.clear()
    auth.RUNTIME_AUTH_VALIDATED_AT.clear()
    auth.FORCE_REFRESH_LAST_ATTEMPT_AT.clear()
    auth.FORCE_REFRESH_LAST_FAILURE_AT.clear()
    auth._set_runtime_authorization('main', 'Bearer warm')
    auth.FORCE_REFRESH_LAST_ATTEMPT_AT['main'] = int(time.time())
    monkeypatch.setattr(auth, 'fetch_token_via_playwright', lambda **kwargs: (_ for _ in ()).throw(AssertionError('should not call playwright')))

    resolved = auth.resolve_authorization(settings, force_refresh=True, account_name='main')

    assert resolved == 'Bearer warm'


def test_resolve_authorization_force_refresh_obeys_failure_backoff(monkeypatch, tmp_path):
    settings = auth.AuthSettings(
        cache_file=str(tmp_path / 'fallback.json'),
        autodl_phone='18200000000',
        autodl_password='secret',
        lightweight_mode='normal',
        auth_failure_backoff_seconds=300,
    )
    auth.RUNTIME_AUTHORIZATION = None
    auth.RUNTIME_AUTHORIZATIONS.clear()
    auth.RUNTIME_AUTH_VALIDATED_AT.clear()
    auth.FORCE_REFRESH_LAST_ATTEMPT_AT.clear()
    auth.FORCE_REFRESH_LAST_FAILURE_AT.clear()
    auth.FORCE_REFRESH_LAST_FAILURE_AT['main'] = int(time.time())
    monkeypatch.setattr(auth, 'fetch_token_via_playwright', lambda **kwargs: (_ for _ in ()).throw(AssertionError('should not call playwright')))

    with pytest.raises(auth.AuthError) as exc:
        auth.resolve_authorization(settings, force_refresh=True, account_name='main')

    assert '退避' in str(exc.value)


def test_autodl_client_records_auth_failure_callback_on_business_auth_failure():
    seen = []

    class DummySession:
        def post(self, **kwargs):
            return DummyResponse({'code': 'Unauthorized', 'msg': 'token expired'})

    client = api.AutoDLClient(authorization='Bearer stale', min_day=7, session=DummySession(), auth_failure_event_callback=lambda payload: seen.append(payload))
    result = client.post_json('https://example.com', {'hello': 'world'})

    assert result['code'] == 'Unauthorized'
    assert seen[0]['msg'] == 'token expired'


class FakeLocator:
    def __init__(self, visible=False, text='', count=1):
        self._visible = visible
        self._text = text
        self._count = count
        self.first = self

    def count(self):
        return self._count

    def is_visible(self, timeout=None):
        return self._visible

    def text_content(self):
        return self._text

    def fill(self, value):
        self._text = value

    def type(self, value, delay=0):
        self._text = value

    def click(self):
        return None


class FakePage:
    def __init__(self, mapping=None, title='AutoDL 登录', url='https://www.autodl.com/login'):
        self.mapping = mapping or {}
        self._title = title
        self.url = url

    def locator(self, selector):
        return self.mapping.get(selector, FakeLocator(visible=False, count=0))

    def title(self):
        return self._title


def test_find_first_visible_locator_supports_fallback_selectors():
    page = FakePage({
        'input[name="phone"]': FakeLocator(visible=False, count=0),
        'input[placeholder*="手机号"]': FakeLocator(visible=True),
    })

    locator, selector = auth.find_first_visible_locator(page, auth.PHONE_INPUT_SELECTORS)

    assert locator is not None
    assert selector == 'input[placeholder*="手机号"]'


def test_detect_login_blocker_reports_captcha_text():
    page = FakePage({'body': FakeLocator(visible=True, text='请先完成验证码后继续登录')})

    message = auth.detect_login_blocker(page)

    assert '验证码' in message


def test_resolve_login_form_reports_missing_elements_clearly():
    page = FakePage({'body': FakeLocator(visible=True, text='普通登录页文本')})

    with pytest.raises(auth.AuthError) as exc:
        auth.resolve_login_form(page, 1000)

    assert '未找到登录表单关键元素' in str(exc.value)
    assert '手机号输入框' in str(exc.value)
