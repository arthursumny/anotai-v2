"""Fail-closed parser for documented Evolution Go text-message events.

Webhook authentication belongs at the API boundary; this module only parses
the already-authenticated payload and deliberately has no side effects.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


_MAX_CONTENT_BYTES = 20 * 1024
_MAX_INSTANCE_LENGTH = 256
_MAX_ID_LENGTH = 256


@dataclass(frozen=True)
class ParsedWhatsAppMessage:
    """Validated message fields; text is excluded from repr to avoid leaks."""

    event: str
    instance: str
    message_id: str
    conversation_id: str
    is_group: bool
    from_me: bool
    sender: str
    timestamp: datetime
    content: str = field(repr=False)


def parse_whatsapp_event(payload: Any) -> ParsedWhatsAppMessage | None:
    """Parse a documented MESSAGE/SEND_MESSAGE text envelope, else return None.

    Unsupported and malformed events (including media-only messages) are
    intentionally ignored instead of being guessed into a message schema.
    """
    if not isinstance(payload, dict):
        return None

    event = payload.get("event")
    if not isinstance(event, str):
        return None
    event_token = event.strip().upper()
    # The official wiki uses both SendMessage and SEND_MESSAGE spellings.
    normalized_event = "SEND_MESSAGE" if event_token.replace("_", "") == "SENDMESSAGE" else event_token
    if normalized_event not in {"MESSAGE", "SEND_MESSAGE"}:
        return None

    instance = payload.get("instance")
    if not isinstance(instance, str) or not instance.strip() or len(instance) > _MAX_INSTANCE_LENGTH:
        return None

    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    key = data.get("key")
    if not isinstance(key, dict):
        # Do not infer alternate undocumented shapes such as data.isFromMe.
        return None

    remote_jid = key.get("remoteJid")
    from_me = key.get("fromMe")
    message_id = key.get("id")
    if not isinstance(remote_jid, str) or not isinstance(from_me, bool):
        return None
    if not isinstance(message_id, str) or not message_id.strip() or len(message_id) > _MAX_ID_LENGTH:
        return None
    if any(character.isspace() for character in message_id):
        return None

    # Evolution's documented user and group JIDs consist of a numeric address
    # and one of these exact suffixes. Participant fields are not guessed.
    is_group = remote_jid.endswith("@g.us")
    suffix = "@g.us" if is_group else "@s.whatsapp.net"
    address = remote_jid[:-len(suffix)] if remote_jid.endswith(suffix) else ""
    if not address or not address.isascii() or not address.isdigit():
        return None

    # Some payloads duplicate the flag. If present, it must be a matching bool.
    if "isFromMe" in data:
        duplicate = data["isFromMe"]
        if not isinstance(duplicate, bool) or duplicate != from_me:
            return None

    message = data.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("conversation")
    if not isinstance(content, str) or not content.strip():
        return None
    try:
        if len(content.encode("utf-8")) > _MAX_CONTENT_BYTES:
            return None
    except UnicodeEncodeError:
        return None

    raw_timestamp = data.get("messageTimestamp")
    if isinstance(raw_timestamp, bool):
        return None
    if isinstance(raw_timestamp, int):
        timestamp_text = str(raw_timestamp)
    elif isinstance(raw_timestamp, str):
        timestamp_text = raw_timestamp
    else:
        return None
    if not timestamp_text.isascii() or not timestamp_text.isdecimal():
        return None
    try:
        timestamp = datetime.fromtimestamp(int(timestamp_text), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None

    return ParsedWhatsAppMessage(
        event=normalized_event,
        instance=instance,
        message_id=message_id,
        conversation_id=remote_jid,
        is_group=is_group,
        from_me=from_me,
        sender=remote_jid,
        timestamp=timestamp,
        content=content,
    )
