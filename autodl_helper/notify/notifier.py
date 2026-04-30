from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

import requests


logger = logging.getLogger(__name__)


class Notifier(Protocol):
    def send(self, title: str, body: str): ...


class PushPlusNotifier:
    endpoint = 'http://www.pushplus.plus/send'

    def __init__(self, token: str, timeout: int = 10):
        self.token = token
        self.timeout = timeout

    def send(self, title: str, body: str):
        response = requests.post(
            self.endpoint,
            json={'token': self.token, 'title': title, 'content': body},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response


class ServerChanNotifier:
    def __init__(self, token: str, timeout: int = 10):
        self.token = token
        self.timeout = timeout

    def send(self, title: str, body: str):
        response = requests.post(
            f'https://sctapi.ftqq.com/{self.token}.send',
            json={'title': title, 'desp': body},
            timeout=self.timeout,
        )
        response.raise_for_status()
        return response


class EmailNotifier:
    def __init__(self, smtp_host: str, smtp_port: int, username: str, password: str, to: list[str]):
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.to = to

    def send(self, title: str, body: str):
        msg = EmailMessage()
        msg['Subject'] = title
        msg['From'] = self.username
        msg['To'] = ', '.join(self.to)
        msg.set_content(body)
        with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port) as smtp:
            smtp.login(self.username, self.password)
            smtp.sendmail(self.username, self.to, msg.as_string())
            return smtp


class NotificationManager:
    def __init__(self, notifiers: list[Notifier] | None = None):
        self.notifiers = notifiers or []

    def notify_task_result(self, *, task_type: str, title: str, message: str) -> None:
        full_title = f'[{task_type}] {title}'
        for notifier in self.notifiers:
            try:
                notifier.send(full_title, message)
            except Exception:
                logger.exception('Notifier failed while delivering %s', full_title)
