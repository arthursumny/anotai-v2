"""Safe API responses for WhatsApp instance setup."""

from pydantic import BaseModel


class WhatsAppConnectResponse(BaseModel):
    status: str
    instance_id: str


class WhatsAppQrResponse(BaseModel):
    qr: str


class WhatsAppStatusResponse(BaseModel):
    status: str
    phone: str | None = None


class WhatsAppDisconnectResponse(BaseModel):
    status: str
    message: str


class WhatsAppMetadataResponse(BaseModel):
    jid: str
    display_name: str
    is_group: bool
