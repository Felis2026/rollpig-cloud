from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, Field, field_serializer, field_validator


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
    outcome_snapshot: "DailyRollOutcomeSnapshot | None" = None


class DailyRollOutcomeSnapshot(BaseModel):
    # 成长结果会先于资源外观写入；调用方必须显式声明快照是否已经完整。
    snapshot_available: bool
    collection_size_after_roll: int
    resource_version: str = ""
    resolved_variant_level: int = 0
    resolved_image_name: str = ""
    unlocked_variant_levels: list[int] = Field(default_factory=list)
    unlocked_variant_fields: list[str] = Field(default_factory=list)


class DailyRollSnapshotRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=64)
    date_str: dt.date
    pig_id: str = Field(min_length=1, max_length=128)
    resource_version: str = Field(min_length=1, max_length=192)
    resolved_variant_level: int = Field(default=0, ge=0, le=5)
    resolved_image_name: str = Field(default="", max_length=192)
    unlocked_variant_levels: tuple[int, ...] = Field(default=(), max_length=5)
    unlocked_variant_fields: tuple[Literal["image", "description", "analysis"], ...] = Field(
        default=(),
        max_length=3,
    )

    @field_validator("resolved_image_name")
    @classmethod
    def validate_resolved_image_name(cls, value: str) -> str:
        """图片引用只能是资源包内文件名，不能借补全接口写入路径或 URL。"""

        if not value:
            return value
        if value in {".", ".."} or any(character in value for character in ("/", "\\", ":", "\x00")):
            raise ValueError("resolved_image_name 必须是安全文件名")
        if any(ord(character) < 32 for character in value):
            raise ValueError("resolved_image_name 不能包含控制字符")
        return value

    @field_validator("unlocked_variant_levels")
    @classmethod
    def normalize_unlocked_variant_levels(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        if any(level < 1 or level > 5 for level in value):
            raise ValueError("unlocked_variant_levels 只能包含 1～5")
        return tuple(sorted(set(value)))

    @field_validator("unlocked_variant_fields")
    @classmethod
    def normalize_unlocked_variant_fields(
        cls,
        value: tuple[Literal["image", "description", "analysis"], ...],
    ) -> tuple[Literal["image", "description", "analysis"], ...]:
        order = {"image": 0, "description": 1, "analysis": 2}
        return tuple(sorted(set(value), key=order.__getitem__))


class DailyRollSnapshotUpdateResponse(BaseModel):
    ok: bool = True
    outcome_snapshot: DailyRollOutcomeSnapshot


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


# ================================ 猪圈日报投递 ================================ #


def _serialize_utc_coordination_time(value: dt.datetime | None) -> str | None:
    """为日报协调时间补回 UTC 标识，避免客户端把 UTC-naive 当作本地时间。"""

    if value is None:
        return None
    aware = (
        value.replace(tzinfo=dt.timezone.utc)
        if value.tzinfo is None
        else value.astimezone(dt.timezone.utc)
    )
    return aware.isoformat().replace("+00:00", "Z")


class DailyReportDeliveryCandidate(BaseModel):
    group_id: str = Field(min_length=1, max_length=64)
    delivery_bot_id: str = Field(min_length=1, max_length=64)


class DailyReportProfileRequest(BaseModel):
    """一次读取日报候选用户的历史稳定排行资料。"""

    date_str: dt.date
    group_id: str = Field(min_length=1, max_length=64)
    cutoff_at: dt.datetime
    user_ids: list[str] = Field(default_factory=list, max_length=2048)

    @field_validator("user_ids")
    @classmethod
    def normalize_user_ids(cls, value: list[str]) -> list[str]:
        normalized = sorted({str(user_id).strip() for user_id in value if str(user_id).strip()})
        if any(len(user_id) > 64 for user_id in normalized):
            raise ValueError("user_ids 中的用户标识不能超过 64 个字符")
        return normalized


class DailyReportProfileItem(BaseModel):
    user_id: str
    daily_pig_id: str = ""
    daily_ex_level: int | None = Field(default=None, ge=0, le=5)
    daily_achieved_at: dt.datetime | None = None
    catalog_count: int = Field(default=0, ge=0)
    catalog_achieved_at: dt.datetime | None = None
    recent_pig_id: str = ""
    recent_ex_level: int | None = Field(default=None, ge=0, le=5)


class DailyReportProfileResponse(BaseModel):
    items: list[DailyReportProfileItem] = Field(default_factory=list)


class DailyReportClaimRequest(BaseModel):
    date_str: dt.date
    cutoff_at: dt.datetime
    instance_id: str = Field(min_length=1, max_length=64)
    candidates: list[DailyReportDeliveryCandidate] = Field(default_factory=list, max_length=256)


class DailyReportClaimItem(BaseModel):
    date_str: dt.date
    group_id: str
    delivery_bot_id: str
    cutoff_at: dt.datetime
    claim_token: str
    status: str = "claimed"
    attempt_count: int = 1

    @field_serializer("cutoff_at", when_used="json")
    def serialize_cutoff_at(self, value: dt.datetime) -> str:
        """投递表内部保持 UTC-naive；仅在 HTTP JSON 中恢复明确的 UTC 时区。"""

        return str(_serialize_utc_coordination_time(value))


class DailyReportClaimResponse(BaseModel):
    items: list[DailyReportClaimItem] = Field(default_factory=list)
    next_claim_at: dt.datetime | None = None

    @field_serializer("next_claim_at", when_used="json")
    def serialize_next_claim_at(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_coordination_time(value)


class DailyReportTransitionRequest(BaseModel):
    date_str: dt.date
    group_id: str = Field(min_length=1, max_length=64)
    claim_token: str = Field(min_length=1, max_length=64)
    action: Literal["sending", "sent", "release", "uncertain", "skip"]
    message_id: str = Field(default="", max_length=128)
    error: str = Field(default="", max_length=512)


class DailyReportTransitionResponse(BaseModel):
    ok: bool
    status: str = ""
    attempt_count: int = 0
    next_attempt_at: dt.datetime | None = None

    @field_serializer("next_attempt_at", when_used="json")
    def serialize_next_attempt_at(self, value: dt.datetime | None) -> str | None:
        return _serialize_utc_coordination_time(value)


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
    special_reason: str = ""


class EventItem(BaseModel):
    event_id: str = ""
    created_at: dt.datetime | None = None
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
    special_reason: str = ""


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
