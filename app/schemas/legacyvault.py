import uuid
from datetime import datetime
from typing import Any, Literal
from pydantic import BaseModel, Field

HandoverCategory = Literal[
    "handover_note",
    "event_summary",
    "finance_summary",
    "contact_directory",
    "governance",
]


class HandoverRecordCreate(BaseModel):
    term_year: str = Field(..., min_length=4, max_length=16, description="e.g. 2025-2026")
    title: str = Field(..., min_length=3, max_length=200)
    category: HandoverCategory = "handover_note"
    content: str = Field(..., min_length=5)
    event_id: uuid.UUID | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class HandoverRecordOut(BaseModel):
    id: uuid.UUID
    club_id: uuid.UUID
    term_year: str
    title: str
    category: str
    content: str
    author_id: uuid.UUID
    author_name: str | None = None
    event_id: uuid.UUID | None = None
    event_name: str | None = None
    metadata_json: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class HandoverSnapshotOut(BaseModel):
    total_records: int
    past_events_count: int
    active_officers_count: int
    net_finance_paise: int
