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
    is_new_pig: bool = False
    previous_copies: int = 0
    copies: int = 0
    previous_duplicate_streak: int = 0
    duplicate_streak: int = 0


class PigProgressItem(BaseModel):
    copies: int = 0
    first_obtained_at: dt.datetime | None = None


class DrawStateResponse(BaseModel):
    pig_ids: list[str] = Field(default_factory=list)
    progress: dict[str, PigProgressItem] = Field(default_factory=dict)
    duplicate_streak: int = 0


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
    max_charges: int | None = None


class ConsumeRoastResponse(BaseModel):
    allowed: bool
    remaining_seconds: int = 0
    charges_left: int = 0
    max_charges: int = 1
    next_recover_seconds: int = 0


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
    reservation_id: str = ""
    participant_ids: list[str] = Field(default_factory=list)
    participant_names: list[str] = Field(default_factory=list)
    participant_count: int = 0
    backfire_victim_id: str = ""
    backfire_victim_name: str = ""


class EventItem(BaseModel):
    type: str
    attacker: str
    target: str
    attacker_name: str = ""
    target_name: str = ""
    food: str = ""
    group_id: str = ""
    reservation_id: str = ""
    participant_ids: list[str] = Field(default_factory=list)
    participant_names: list[str] = Field(default_factory=list)
    participant_count: int = 0
    backfire_victim_id: str = ""
    backfire_victim_name: str = ""


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


class RecentRollItem(BaseModel):
    date_str: dt.date
    pig_id: str


class CatalogSnapshotResponse(BaseModel):
    pig_ids: list[str] = Field(default_factory=list)
    progress: dict[str, PigProgressItem] = Field(default_factory=dict)
    duplicate_streak: int = 0
    recent_rolls: list[RecentRollItem] = Field(default_factory=list)
    roasted_7d: int = 0
    roast_events_7d: int = 0


# ================================ 预约烤猪 API ================================ #

class UnrolledRoastAttemptRequest(BaseModel):
    user_id: str
    date_str: dt.date


class UnrolledRoastAttemptResponse(BaseModel):
    user_id: str
    date_str: dt.date
    count: int


class RoastReservationParticipantItem(BaseModel):
    user_id: str
    display_name: str = ""
    pig_id: str = ""


class RoastReservationItem(BaseModel):
    reservation_id: str
    date_str: dt.date
    group_id: str
    target_id: str
    target_name: str = ""
    target_pig_id: str = ""
    owner_id: str
    owner_name: str = ""
    owner_pig_id: str
    participants: list[RoastReservationParticipantItem] = Field(default_factory=list)
    delivery_bot_id: str
    force_mode: str | None = None
    status: str
    outcome_snapshot: dict | None = None
    claim_token: str = ""


class RoastReservationPrepareRequest(BaseModel):
    attacker_id: str
    attacker_name: str = ""
    attacker_pig_id: str
    target_id: str
    target_name: str = ""
    group_id: str
    delivery_bot_id: str
    force_mode: str | None = None
    date_str: dt.date
    now_ts: float | None = None
    cooldown_seconds: int | None = None
    max_charges: int | None = None


class RoastReservationPrepareResponse(BaseModel):
    status: str
    reservation: RoastReservationItem | None = None
    cooldown: ConsumeRoastResponse | None = None
    target_pig_id: str = ""
    protection_broken: bool = False


class RoastReservationOwnedResponse(BaseModel):
    has_owned: bool


class RoastReservationClaimRequest(BaseModel):
    delivery_bot_id: str
    date_str: dt.date
    limit: int = 12
    # 旧 Plus 未声明该能力；Cloud 会把固定快照作为它认识的 sending 返回。
    supports_prepared: bool = False
    excluded_reservation_ids: list[str] = Field(default_factory=list)


class RoastReservationClaimResponse(BaseModel):
    items: list[RoastReservationItem] = Field(default_factory=list)
    has_owned: bool = False


class RoastReservationOutcomeRequest(BaseModel):
    reservation_id: str
    claim_token: str
    outcome_snapshot: dict


class RoastReservationMutationRequest(BaseModel):
    reservation_id: str
    claim_token: str


class RoastReservationCompleteRequest(RoastReservationMutationRequest):
    # 新 Plus 会把预约事件一并提交，使完成状态与日报数据在同一事务中落库。
    event: EventCreateRequest | None = None


class RoastReservationMutationResponse(BaseModel):
    ok: bool
    reservation: RoastReservationItem | None = None
    event_recorded: bool = False


# ================================ 烤箱补货 API ================================ #

class GroupActiveUsersMarkRequest(BaseModel):
    group_id: str
    user_ids: list[str] = Field(default_factory=list)
    date_str: dt.date


class GroupActiveUsersResponse(BaseModel):
    user_ids: list[str] = Field(default_factory=list)


class GroupRoastRefillItem(BaseModel):
    request_id: str
    date_str: dt.date
    group_id: str
    initiator_id: str
    initiator_name: str = ""
    delivery_bot_id: str
    message_id: str = ""
    active_count_snapshot: int
    required_ratio: int
    required_votes: int
    success_count_before: int
    status: str
    created_at: dt.datetime
    expires_at: dt.datetime
    completed_at: dt.datetime | None = None
    benefited_user_ids: list[str] = Field(default_factory=list)
    failure_reason: str = ""


class GroupRoastRefillPrepareRequest(BaseModel):
    group_id: str
    initiator_id: str
    initiator_name: str = ""
    delivery_bot_id: str
    date_str: dt.date
    now_ts: float | None = None
    threshold_policy: str = ""


class GroupRoastRefillPrepareResponse(BaseModel):
    status: str
    request: GroupRoastRefillItem | None = None
    active_user_ids: list[str] = Field(default_factory=list)


class GroupRoastRefillBindRequest(BaseModel):
    request_id: str
    message_id: str


class GroupRoastRefillLookupResponse(BaseModel):
    request: GroupRoastRefillItem | None = None


class GroupRoastRefillFailRequest(BaseModel):
    request_id: str
    message_id: str = ""
    reason: str = "failed"


class GroupRoastRefillCompleteRequest(BaseModel):
    request_id: str
    message_id: str
    voter_ids: list[str] = Field(default_factory=list)
    excluded_user_ids: list[str] = Field(default_factory=list)
    max_charges: int = 2
    now_ts: float | None = None


class GroupRoastRefillCompleteResponse(BaseModel):
    completed: bool
    status: str
    request: GroupRoastRefillItem | None = None
    valid_voter_ids: list[str] = Field(default_factory=list)
    benefited_user_ids: list[str] = Field(default_factory=list)
