"""通知：订单确认邮件与短信。"""
from __future__ import annotations

from src.common.models import Order
from src.common.mq import task
from src.common.gateway import mailer, sms

RETRY_LIMIT = 3


class NotifyError(Exception):
    """通知发送失败。"""


def send_confirmation(order: Order) -> None:
    """异步发送确认邮件 + 短信，失败重试 3 次。"""
    rendered = _render(order)
    try:
        mailer.send(
            to=order.user_email,
            subject="订单 %s 已确认" % order.no,
            body=rendered.text,
        )
        sms.send(
            phone=order.user_phone,
            text=rendered.sms,
        )
    except (MailError, SmsError) as exc:
        raise NotifyError(str(exc)) from exc


def _render(order: Order) -> Rendered:
    """渲染邮件正文与短信模板。"""
    return Rendered(
        text=EMAIL_TEMPLATE.format(order=order),
        sms=SMS_TEMPLATE.format(no=order.no, total=order.total),
    )
