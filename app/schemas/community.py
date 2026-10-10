import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Audience = Literal["everyone", "members", "volunteers", "officers", "event"]
CertificateKind = Literal["participation", "volunteering", "achievement"]
HandoverRole = Literal["president", "treasurer", "event_lead", "volunteer", "everyone"]


class _Clean(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)


# --- GateSync ---------------------------------------------------------------------------


class ScanIn(_Clean):
    event_id: uuid.UUID
    credential: str = Field(min_length=1, max_length=200)


class ManualCheckIn(BaseModel):
    event_id: uuid.UUID
    ticket_id: uuid.UUID


# --- CrewDoo Connect --------------------------------------------------------------------


class AnnouncementIn(_Clean):
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=5000)
    audience: Audience = "everyone"
    event_id: uuid.UUID | None = None
    pinned: bool = False

    @model_validator(mode="after")
    def _event_goes_with_event_audience(self):
        if (self.audience == "event") != (self.event_id is not None):
            raise ValueError("Choose an event for an event announcement, and only for one")
        return self


class AnnouncementOut(BaseModel):
    id: uuid.UUID
    title: str
    body: str
    audience: Audience
    event_id: uuid.UUID | None
    event_name: str | None = None
    pinned: bool
    author_name: str
    created_at: datetime
    read: bool
    can_remove: bool = False
    # Shown to the author and officers only.
    read_count: int | None = None


# --- CertiDoo ---------------------------------------------------------------------------


class CertificateIn(_Clean):
    recipient_user_id: uuid.UUID
    kind: CertificateKind = "achievement"
    title: str = Field(min_length=1, max_length=200)
    note: str | None = Field(None, max_length=500)
    event_id: uuid.UUID | None = None


class BulkCertificatesIn(BaseModel):
    event_id: uuid.UUID
    kind: Literal["participation", "volunteering"]


class CertificateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: CertificateKind
    title: str
    note: str | None
    recipient_user_id: uuid.UUID
    recipient_name: str
    event_id: uuid.UUID | None
    event_name: str | None = None
    club_name: str = ""
    issued_by_name: str = ""
    verify_code: str
    issued_at: datetime
    revoked_at: datetime | None
    revoke_reason: str | None


class BulkCertificatesOut(BaseModel):
    issued: int
    already_had: int


class RevokeIn(_Clean):
    reason: str = Field(min_length=3, max_length=500)


# --- LegacyVault ------------------------------------------------------------------------


class HandoverNoteIn(_Clean):
    role: HandoverRole
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=20000)


class HandoverNoteUpdate(_Clean):
    title: str = Field(min_length=1, max_length=160)
    body: str = Field(min_length=1, max_length=20000)


class HandoverNoteOut(BaseModel):
    id: uuid.UUID
    role: HandoverRole
    title: str
    body: str
    version: int
    updated_by_name: str
    updated_at: datetime
    archived: bool
    can_edit: bool
