from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

# ================================ Direct Script Bootstrap ================================ #
# Running `python tools/import_legacy_json.py` only adds `tools/` to sys.path.
# Insert the project root explicitly so `rollpig_cloud` can be imported in the container.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select

from rollpig_cloud.db import SessionLocal, init_db
from rollpig_cloud.models import Collection, DailyRoll, GroupProtection, GroupRoll, RoastEvent, UserUsage


def parse_date(value: str) -> dt.date:
    return dt.date.fromisoformat(str(value))


# ================================ 导入幂等检查 ================================ #
# 迁移脚本实际部署时很容易出现“先试跑一次、修正后再跑一次”的情况。
# 这里把关键表的重复判断单独收口，避免重复导入直接制造脏数据或触发唯一约束报错。
def has_group_roll(session, *, date_str: str, group_id: str, user_id: str) -> bool:
    return session.execute(
        select(GroupRoll).where(
            GroupRoll.date_str == parse_date(date_str),
            GroupRoll.group_id == str(group_id),
            GroupRoll.user_id == str(user_id),
        )
    ).scalar_one_or_none() is not None


def has_roast_event(session, *, date_str: str, event: dict) -> bool:
    return session.execute(
        select(RoastEvent).where(
            RoastEvent.date_str == parse_date(date_str),
            RoastEvent.group_id == str(event.get("group_id") or ""),
            RoastEvent.event_type == str(event.get("type") or ""),
            RoastEvent.attacker_id == str(event.get("attacker") or ""),
            RoastEvent.target_id == str(event.get("target") or ""),
            RoastEvent.attacker_name == str(event.get("attacker_name") or ""),
            RoastEvent.target_name == str(event.get("target_name") or ""),
            RoastEvent.food_name == str(event.get("food") or ""),
        )
    ).scalar_one_or_none() is not None


def has_group_protection(session, *, protect_date: str, group_id: str, user_id: str) -> bool:
    return session.execute(
        select(GroupProtection).where(
            GroupProtection.protect_date == parse_date(protect_date),
            GroupProtection.group_id == str(group_id),
            GroupProtection.user_id == str(user_id),
        )
    ).scalar_one_or_none() is not None


def get_or_create_user_usage(session, usage_cache: dict[str, UserUsage], *, user_id: str) -> UserUsage:
    """Return a reusable UserUsage row for the current import batch."""
    normalized_user_id = str(user_id)
    cached = usage_cache.get(normalized_user_id)
    if cached is not None:
        return cached

    usage = session.execute(select(UserUsage).where(UserUsage.user_id == normalized_user_id)).scalar_one_or_none()
    if usage is None:
        usage = UserUsage(user_id=normalized_user_id)
        session.add(usage)
    usage_cache[normalized_user_id] = usage
    return usage


def main():
    parser = argparse.ArgumentParser(description="Import legacy pig_data.json into rollpig-cloud")
    parser.add_argument("--file", required=True, help="legacy pig_data.json path")
    args = parser.parse_args()

    source = Path(args.file)
    data = json.loads(source.read_text(encoding="utf-8"))
    init_db()

    with SessionLocal() as session:
        usage_cache: dict[str, UserUsage] = {}

        for date_str, records in data.get("history", {}).items():
            if not isinstance(records, dict):
                continue
            for user_id, pig_id in records.items():
                existing = session.execute(
                    select(DailyRoll).where(DailyRoll.user_id == str(user_id), DailyRoll.date_str == parse_date(date_str))
                ).scalar_one_or_none()
                if not existing and pig_id:
                    session.add(DailyRoll(user_id=str(user_id), pig_id=str(pig_id), date_str=parse_date(date_str)))

        for date_str, group_map in data.get("group_rolls", {}).items():
            if not isinstance(group_map, dict):
                continue
            for group_id, records in group_map.items():
                if not isinstance(records, dict):
                    continue
                for user_id, pig_id in records.items():
                    existing = has_group_roll(
                        session,
                        date_str=date_str,
                        group_id=str(group_id),
                        user_id=str(user_id),
                    )
                    if not existing and pig_id:
                        session.add(
                            GroupRoll(
                                date_str=parse_date(date_str),
                                group_id=str(group_id),
                                user_id=str(user_id),
                                pig_id=str(pig_id),
                            )
                        )

        for user_id, pig_ids in data.get("collection", {}).items():
            if not isinstance(pig_ids, list):
                continue
            for pig_id in pig_ids:
                existing = session.execute(
                    select(Collection).where(Collection.user_id == str(user_id), Collection.pig_id == str(pig_id))
                ).scalar_one_or_none()
                if not existing:
                    session.add(Collection(user_id=str(user_id), pig_id=str(pig_id)))

        usage_map = data.get("usage", {})
        if isinstance(usage_map, dict):
            for user_id, last_roast_ts in usage_map.items():
                usage = get_or_create_user_usage(session, usage_cache, user_id=str(user_id))
                usage.last_roast_ts = float(last_roast_ts or 0)

        force_usage_map = data.get("force_usage", {})
        if isinstance(force_usage_map, dict):
            for user_id, last_force_date in force_usage_map.items():
                usage = get_or_create_user_usage(session, usage_cache, user_id=str(user_id))
                usage.last_force_date = parse_date(last_force_date)

        for date_str, events in data.get("daily_events", {}).items():
            if not isinstance(events, list):
                continue
            for event in events:
                if not isinstance(event, dict):
                    continue
                if has_roast_event(session, date_str=date_str, event=event):
                    continue
                session.add(
                    RoastEvent(
                        date_str=parse_date(date_str),
                        group_id=str(event.get("group_id") or ""),
                        event_type=str(event.get("type") or ""),
                        attacker_id=str(event.get("attacker") or ""),
                        target_id=str(event.get("target") or ""),
                        attacker_name=str(event.get("attacker_name") or ""),
                        target_name=str(event.get("target_name") or ""),
                        food_name=str(event.get("food") or ""),
                    )
                )

        protected = data.get("protected", {})
        if isinstance(protected, dict) and "date" in protected and isinstance(protected.get("users"), list):
            protect_date = str(protected.get("date") or "")
            for user_id in protected.get("users", []):
                if has_group_protection(session, protect_date=protect_date, group_id="__all__", user_id=str(user_id)):
                    continue
                session.add(GroupProtection(protect_date=parse_date(protect_date), group_id="__all__", user_id=str(user_id)))
        else:
            for protect_date, group_map in protected.items():
                if not isinstance(group_map, dict):
                    continue
                for group_id, user_ids in group_map.items():
                    if not isinstance(user_ids, list):
                        continue
                    for user_id in user_ids:
                        if has_group_protection(
                            session,
                            protect_date=str(protect_date),
                            group_id=str(group_id),
                            user_id=str(user_id),
                        ):
                            continue
                        session.add(
                            GroupProtection(
                                protect_date=parse_date(protect_date),
                                group_id=str(group_id),
                                user_id=str(user_id),
                            )
                        )

        session.commit()


if __name__ == "__main__":
    main()
