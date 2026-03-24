from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class DailyRollGetOrCreateRequest(BaseModel):
    user_id: str
    proposed_pig_id: str
    date_str: dt.date
    group_id: str = ""


class DailyRollLookupResponse(BaseModel):
    pig_id: str | None = None
    created: bool = False


class DailyRollItem(BaseModel):
    user_id: str
    pig_id: str


class DailyRollListResponse(BaseModel):
    items: list[DailyRollItem] = Field(default_factory=list)


class GroupRollMarkSeenRequest(BaseModel):
    group_id: str
    user_id: str
    pig_id: str
    date_str: dt.date


class GroupRollItem(BaseModel):
    user_id: str
    pig_id: str


class GroupRollListResponse(BaseModel):
    items: list[GroupRollItem] = Field(default_factory=list)


class ConsumeRoastRequest(BaseModel):
    user_id: str
    now_ts: float | None = None
    cooldown_seconds: int | None = None


class ConsumeRoastResponse(BaseModel):
    allowed: bool
    remaining_seconds: int = 0


class ConsumeForceRequest(BaseModel):
    user_id: str
    date_str: dt.date


class SimpleAllowedResponse(BaseModel):
    allowed: bool


class EventCreateRequest(BaseModel):
    event_type: str
    attacker_id: str
    target_id: str
    attacker_name: str = ""
    target_name: str = ""
    food: str = ""
    group_id: str = ""
    date_str: dt.date | None = None


class EventItem(BaseModel):
    type: str
    attacker: str
    target: str
    attacker_name: str = ""
    target_name: str = ""
    food: str = ""
    group_id: str = ""


class EventListResponse(BaseModel):
    items: list[EventItem] = Field(default_factory=list)


class ReplaceGroupProtectionsRequest(BaseModel):
    group_id: str
    user_ids: list[str] = Field(default_factory=list)
    protect_date: dt.date


class ProtectedResponse(BaseModel):
    protected: bool


class CollectionResponse(BaseModel):
    pig_ids: list[str] = Field(default_factory=list)


class ActiveGroupsResponse(BaseModel):
    group_ids: list[str] = Field(default_factory=list)
