from datetime import datetime, timezone

import pytest

from app.services.whatsapp_event_parser import parse_whatsapp_event


@pytest.fixture
def envelope():
    return {
        "event": "MESSAGE",
        "instance": "vendas",
        "data": {
            "key": {
                "remoteJid": "5511999999999@s.whatsapp.net",
                "fromMe": False,
                "id": "3EB0ABC",
            },
            "message": {"conversation": "Olá"},
            "messageTimestamp": "1699999999",
            "pushName": "João",
        },
    }


def test_parses_incoming_documented_envelope(envelope):
    parsed = parse_whatsapp_event(envelope)
    assert parsed is not None
    assert parsed.event == "MESSAGE"
    assert parsed.instance == "vendas"
    assert parsed.message_id == "3EB0ABC"
    assert parsed.conversation_id == "5511999999999@s.whatsapp.net"
    assert parsed.sender == parsed.conversation_id
    assert parsed.from_me is False
    assert parsed.is_group is False
    assert parsed.content == "Olá"
    assert parsed.timestamp == datetime(2023, 11, 14, 22, 13, 19, tzinfo=timezone.utc)


def test_parses_send_message_and_from_me(envelope):
    envelope["event"] = "SendMessage"
    envelope["data"]["key"]["fromMe"] = True
    parsed = parse_whatsapp_event(envelope)
    assert parsed is not None
    assert parsed.event == "SEND_MESSAGE"
    assert parsed.from_me is True


def test_parses_group_without_guessing_participant(envelope):
    envelope["data"]["key"]["remoteJid"] = "120363012345678901@g.us"
    envelope["data"]["participant"] = "5511888888888@s.whatsapp.net"
    parsed = parse_whatsapp_event(envelope)
    assert parsed is not None
    assert parsed.is_group is True
    assert parsed.conversation_id == "120363012345678901@g.us"
    assert parsed.sender == parsed.conversation_id


def test_duplicate_is_from_me_mismatch_is_rejected(envelope):
    envelope["data"]["isFromMe"] = True
    assert parse_whatsapp_event(envelope) is None


def test_media_only_is_skipped(envelope):
    envelope["data"]["message"] = {"imageMessage": {"caption": "not documented text"}}
    assert parse_whatsapp_event(envelope) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data["key"].update(remoteJid="not-a-jid"),
        lambda data: data["message"].update(conversation="x" * (20 * 1024 + 1)),
        lambda data: data.update(messageTimestamp="not-a-time"),
        lambda data: data.update(messageTimestamp="999999999999999999999999999999999999999999"),
    ],
)
def test_invalid_jid_large_content_and_time_are_rejected(envelope, mutate):
    mutate(envelope["data"])
    assert parse_whatsapp_event(envelope) is None


def test_unknown_event_is_rejected(envelope):
    envelope["event"] = "CONNECTION_UPDATE"
    assert parse_whatsapp_event(envelope) is None


def test_undocumented_alternate_shape_fails_closed(envelope):
    # A build-specific fixture is required before supporting data.isFromMe/chat.
    envelope["data"] = {
        "isFromMe": False,
        "chat": "5511999999999@s.whatsapp.net",
        "message": {"conversation": "Olá"},
        "messageTimestamp": "1699999999",
    }
    assert parse_whatsapp_event(envelope) is None


def test_repr_does_not_include_content_or_logging_payload(caplog, envelope):
    secret_text = "PRIVATE-PAYLOAD-UNIQUE"
    envelope["data"]["message"]["conversation"] = secret_text
    parsed = parse_whatsapp_event(envelope)
    assert parsed is not None
    assert secret_text not in repr(parsed)
    assert secret_text not in caplog.text
