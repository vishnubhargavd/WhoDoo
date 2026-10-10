import uuid
from datetime import datetime
from pydantic import BaseModel, Field


class AnnouncementCreate(BaseModel):
    title: str = Field(..., min_length=3, max_length=150)
    content: str = Field(..., min_length=5)
    target_audience: str = Field(default="all", pattern="^(all|members|volunteers)$")
    is_pinned: bool = Field(default=False)


class AnnouncementOut(BaseModel):
    id: uuid.UUID
    club_id: uuid.UUID
    author_id: uuid.UUID
    author_name: str
    title: str
    content: str
    target_audience: str
    is_pinned: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
