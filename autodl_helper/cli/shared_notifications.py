from __future__ import annotations

from autodl_helper.config import NotificationSettings
from autodl_helper.notify import EmailNotifier, PushPlusNotifier, ServerChanNotifier


def build_named_notifiers(notifications: NotificationSettings) -> dict[str, object]:
    notifiers: dict[str, object] = {}
    if notifications.pushplus.enabled and notifications.pushplus.token:
        notifiers['pushplus'] = PushPlusNotifier(token=notifications.pushplus.token)
    if notifications.serverchan.enabled and notifications.serverchan.token:
        notifiers['serverchan'] = ServerChanNotifier(token=notifications.serverchan.token)
    if notifications.email.enabled and notifications.email.username and notifications.email.to:
        notifiers['email'] = EmailNotifier(
            smtp_host=notifications.email.smtp_host,
            smtp_port=notifications.email.smtp_port,
            username=notifications.email.username,
            password=notifications.email.password,
            to=notifications.email.to,
        )
    return notifiers


def build_notifiers(notifications: NotificationSettings) -> list[object]:
    return list(build_named_notifiers(notifications).values())


__all__ = ["build_named_notifiers", "build_notifiers"]
