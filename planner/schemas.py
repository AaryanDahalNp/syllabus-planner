from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class CandidateEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    kind: Literal["exam", "assignment", "quiz", "project", "class", "holiday", "other"]
    action: Literal["event", "cancel"]
    start_date: Optional[str]
    start_time: Optional[str]
    end_date: Optional[str]
    end_time: Optional[str]
    date_text: str = Field(max_length=200)
    source_page: Optional[int]
    source_excerpt: str = Field(min_length=1, max_length=600)
    issues: list[str] = Field(max_length=10)


class ExtractionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[CandidateEvent] = Field(max_length=200)
    warnings: list[str] = Field(max_length=30)
