"""Workspace localization settings schemas."""

from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, StrictStr, field_validator, model_validator


Locale = Literal["pt-BR", "en-US", "es-ES"]


class LocalizationPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    timezone: StrictStr | None = None
    locale: Locale | None = None

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        if value is None:
            raise ValueError("timezone não pode ser nulo")
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone deve ser um identificador IANA válido") from exc
        return value

    @field_validator("locale")
    @classmethod
    def validate_locale(cls, value: Locale | None) -> Locale:
        if value is None:
            raise ValueError("locale não pode ser nulo")
        return value

    @model_validator(mode="after")
    def require_setting(self):
        if not self.model_fields_set or not any(
            getattr(self, field) is not None for field in self.model_fields_set
        ):
            raise ValueError("informe ao menos timezone ou locale")
        return self


class LocalizationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timezone: str
    locale: str
