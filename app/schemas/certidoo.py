import uuid
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field

CertificateType = Literal["participation", "volunteer", "achievement"]


class CertificateIssueRequest(BaseModel):
    event_id: uuid.UUID
    recipient_user_id: uuid.UUID
    certificate_type: CertificateType = "participation"
    title: str = Field(..., min_length=3, max_length=200)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EligibleRecipient(BaseModel):
    user_id: uuid.UUID
    full_name: str
    email: str
    eligibility_type: CertificateType
    detail: str
    already_issued: bool
    certificate_id: uuid.UUID | None = None
    verification_code: str | None = None


class CertificateOut(BaseModel):
    id: uuid.UUID
    club_id: uuid.UUID
    event_id: uuid.UUID
    recipient_user_id: uuid.UUID
    recipient_name: str | None = None
    recipient_email: str | None = None
    event_name: str | None = None
    certificate_type: str
    verification_code: str
    title: str
    issued_by: uuid.UUID
    issued_by_name: str | None = None
    issued_at: datetime
    revoked_at: datetime | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)

    model_config = {"from_attributes": True}


class CertificateVerificationOut(BaseModel):
    valid: bool
    verification_code: str
    title: str
    certificate_type: str
    recipient_name: str
    event_name: str
    club_name: str
    issued_at: datetime
    status: str
