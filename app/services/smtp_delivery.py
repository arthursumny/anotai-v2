"""Blocking SMTP adapter; IntegrationService calls it in an AnyIO worker thread."""

import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import settings


def deliver_email(message: EmailMessage, sender: str, recipient: str) -> None:
    """Send one MIME message with a bounded socket timeout and explicit TLS."""
    timeout = settings.SMTP_TIMEOUT_SECONDS
    if not 0 < timeout <= 60:
        raise ValueError("SMTP timeout inválido")
    match settings.SMTP_TLS:
        case "ssl":
            client = smtplib.SMTP_SSL(
                settings.SMTP_HOST, settings.SMTP_PORT, timeout=timeout,
                context=ssl.create_default_context(),
            )
        case "starttls" | "none":
            client = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=timeout)
        case _:
            raise ValueError("SMTP TLS inválido")
    with client:
        if settings.SMTP_TLS == "starttls":
            client.starttls(context=ssl.create_default_context())
        if settings.SMTP_USERNAME:
            client.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        refused = client.send_message(message, from_addr=sender, to_addrs=[recipient])
        if refused:
            raise smtplib.SMTPRecipientsRefused(refused)
