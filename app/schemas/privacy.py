"""Strict content-free schemas for owner privacy controls."""

from typing import Literal

from pydantic import BaseModel, ConfigDict

PrivacySource = Literal["audio", "gmail", "whatsapp", "all"]


class PrivacyDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: PrivacySource
    confirm: Literal["APAGAR"]


class PrivacyCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: int
    generated_documents: int
    tasks: int
    reminders: int
    action_suggestions: int
    pending_conversation_items: int
    follow_ups: int
    failed: int


class PrivacySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: PrivacySource
    events: int
    generated_documents: int
    notes_retained: int
    reminders_to_cancel: int
    follow_ups_to_cancel: int


class PrivacyDeleteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: PrivacySource
    counts: PrivacyCounts
    notes_retained: int
    complete: bool
    notice: str
