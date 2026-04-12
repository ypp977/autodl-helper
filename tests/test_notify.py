from autodl_helper import notify


class DummySMTP:
    def __init__(self, host, port):
        self.host = host
        self.port = port
        self.logged_in = None
        self.sent = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, username, password):
        self.logged_in = (username, password)

    def sendmail(self, from_addr, to_addrs, msg):
        self.sent = (from_addr, to_addrs, msg)


class FailingNotifier:
    def send(self, title, body):
        raise RuntimeError('boom')


class RecordingNotifier:
    def __init__(self):
        self.calls = []

    def send(self, title, body):
        self.calls.append((title, body))


def test_pushplus_notifier_posts_payload(monkeypatch):
    captured = {}

    def fake_post(url, json, timeout):
        captured['url'] = url
        captured['json'] = json
        captured['timeout'] = timeout

        class Resp:
            def raise_for_status(self):
                return None
        return Resp()

    monkeypatch.setattr(notify.requests, 'post', fake_post)
    notifier = notify.PushPlusNotifier(token='abc')
    notifier.send('title', 'body')

    assert captured['json']['token'] == 'abc'
    assert captured['json']['title'] == 'title'


def test_email_notifier_sends_message(monkeypatch):
    monkeypatch.setattr(notify.smtplib, 'SMTP_SSL', DummySMTP)
    notifier = notify.EmailNotifier(
        smtp_host='smtp.qq.com',
        smtp_port=465,
        username='a@example.com',
        password='pwd',
        to=['b@example.com'],
    )
    smtp = notifier.send('hello', 'world')
    assert smtp.logged_in == ('a@example.com', 'pwd')
    assert smtp.sent[0] == 'a@example.com'
    assert smtp.sent[1] == ['b@example.com']
    assert 'Subject: hello' in smtp.sent[2]


def test_notification_manager_continues_when_one_notifier_fails(caplog):
    ok = RecordingNotifier()
    manager = notify.NotificationManager([FailingNotifier(), ok])

    manager.notify_task_result(task_type='scheduled_start', title='job ok', message='started')

    assert ok.calls == [('[scheduled_start] job ok', 'started')]
    assert 'Notifier failed' in caplog.text
