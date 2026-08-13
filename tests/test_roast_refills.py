from __future__ import annotations

import datetime as dt
import os
import unittest

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

os.environ.setdefault("ROLLPIG_CLOUD_DATABASE_URL", "sqlite+pysqlite:///:memory:")

from rollpig_cloud.db import Base
from rollpig_cloud.main import app
from rollpig_cloud.migrations import ensure_runtime_migrations
from rollpig_cloud.models import (
    GroupDailyActiveUser,
    GroupRoastRefillRequest,
    GroupRoll,
    RoastEvent,
    UserUsage,
)
from rollpig_cloud.schemas import GroupRoastRefillPrepareRequest
from rollpig_cloud.services.roast_refills import (
    ROAST_REFILL_THRESHOLD_POLICY,
    bind_refill_message,
    complete_refill,
    get_active_refill,
    legacy_refill_threshold,
    mark_group_active_users,
    prepare_refill,
    refill_threshold,
)


DATE = dt.date(2026, 8, 8)
NOW_TS = 1_786_118_400.0


class CloudRoastRefillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _mark(self, group_id: str, *user_ids: str) -> None:
        mark_group_active_users(
            self.session,
            date_str=DATE,
            group_id=group_id,
            user_ids=list(user_ids),
        )
        self.session.commit()

    def _prepare(
        self,
        group_id: str = "100",
        now_ts: float = NOW_TS,
        threshold_policy: str | None = ROAST_REFILL_THRESHOLD_POLICY,
    ):
        request_data = {
            "group_id": group_id,
            "initiator_id": "admin",
            "initiator_name": "管理员",
            "delivery_bot_id": "bot",
            "date_str": DATE,
            "now_ts": now_ts,
        }
        if threshold_policy is not None:
            request_data["threshold_policy"] = threshold_policy
        response = prepare_refill(
            self.session,
            GroupRoastRefillPrepareRequest(**request_data),
        )
        self.session.commit()
        return response

    def test_threshold_matrix(self):
        self.assertEqual([refill_threshold(5, index)[1] for index in range(5)], [2, 2, 3, 3, 3])
        self.assertEqual([refill_threshold(10, index)[1] for index in range(5)], [3, 4, 5, 6, 6])
        self.assertEqual([refill_threshold(20, index)[1] for index in range(5)], [5, 7, 9, 11, 11])
        self.assertEqual([refill_threshold(50, index)[1] for index in range(5)], [8, 12, 16, 20, 20])
        self.assertEqual(refill_threshold(100, 99), (55, 20))

    def test_prepare_negotiates_capped_policy_and_preserves_legacy_default(self):
        user_ids = [f"pig-{index}" for index in range(40)]
        self._mark("capped", *user_ids)
        self._mark("legacy", *user_ids)

        capped = self._prepare("capped")
        created = self._prepare("legacy", threshold_policy=None)

        self.assertEqual(
            (capped.request.active_count_snapshot, capped.request.required_ratio, capped.request.required_votes),
            (40, 25, 8),
        )
        self.assertEqual(
            (created.request.active_count_snapshot, created.request.required_ratio, created.request.required_votes),
            (40, 25, 10),
        )
        self.assertEqual(legacy_refill_threshold(100, 99), (65, 65))

    def test_prepare_freezes_snapshot_and_keeps_one_active_request(self):
        self._mark("100", "a", "b")
        self.assertEqual(self._prepare().status, "insufficient_active")
        self._mark("100", "c", "d", "e", "f", "g", "h", "i", "j")
        created = self._prepare()
        existing = self._prepare()
        self.assertEqual((created.status, existing.status), ("created", "existing"))
        self.assertEqual((created.request.active_count_snapshot, created.request.required_votes), (10, 3))

        self._mark("100", "late")
        current = get_active_refill(self.session, date_str=DATE, group_id="100", now_ts=NOW_TS + 30)
        self.assertEqual((current.active_count_snapshot, current.required_votes), (10, 3))

    def test_complete_is_atomic_resets_latest_active_users_and_advances_ratio(self):
        self._mark("100", "a", "b", "c", "d", "e", "bot")
        self.session.add(UserUsage(
            user_id="a",
            last_roast_ts=123,
            roast_charges=0,
            roast_charge_updated_ts=456,
            last_force_date=DATE,
        ))
        self.session.commit()
        created = self._prepare()
        row = bind_refill_message(
            self.session,
            request_id=created.request.request_id,
            message_id="message-1",
            now_ts=NOW_TS + 30,
        )
        self.session.commit()
        self._mark("100", "late")

        completed = complete_refill(
            self.session,
            request_id=row.request_id,
            message_id="message-1",
            voter_ids=["bot", "a", "b", "inactive", "c"],
            excluded_user_ids=["bot"],
            max_charges=4,
            now_ts=NOW_TS + 60,
        )
        self.session.commit()
        duplicate = complete_refill(
            self.session,
            request_id=row.request_id,
            message_id="message-1",
            voter_ids=["a", "b", "c"],
            excluded_user_ids=["bot"],
            now_ts=NOW_TS + 61,
        )
        self.session.commit()

        self.assertTrue(completed.completed)
        self.assertFalse(duplicate.completed)
        self.assertEqual(set(completed.valid_voter_ids), {"a", "b", "c"})
        self.assertEqual(set(completed.benefited_user_ids), {"a", "b", "c", "d", "e", "late"})
        usage = self.session.scalars(select(UserUsage)).all()
        self.assertEqual({row.user_id for row in usage}, set(completed.benefited_user_ids))
        self.assertTrue(all((row.roast_charges, row.roast_charge_updated_ts) == (4, int(NOW_TS + 60)) for row in usage))
        existing = next(row for row in usage if row.user_id == "a")
        self.assertEqual((existing.last_roast_ts, existing.last_force_date), (123, DATE))

        next_round = self._prepare(now_ts=NOW_TS + 62)
        self.assertEqual((next_round.request.success_count_before, next_round.request.required_ratio), (1, 35))

    def test_complete_rejects_request_without_bound_poll_message(self):
        self._mark("100", "a", "b", "c", "d", "e")
        created = self._prepare()

        completed = complete_refill(
            self.session,
            request_id=created.request.request_id,
            message_id="",
            voter_ids=["a", "b", "c"],
            excluded_user_ids=[],
            now_ts=NOW_TS + 30,
        )

        self.assertFalse(completed.completed)
        self.assertEqual(completed.status, "message_mismatch")
        self.assertFalse(self.session.scalars(select(UserUsage)).all())

    def test_bind_rejects_and_expires_request_after_ttl(self):
        self._mark("100", "a", "b", "c", "d", "e")
        created = self._prepare()

        bound = bind_refill_message(
            self.session,
            request_id=created.request.request_id,
            message_id="message-late",
            now_ts=NOW_TS + 601,
        )
        self.session.commit()
        row = self.session.scalar(
            select(GroupRoastRefillRequest).where(
                GroupRoastRefillRequest.request_id == created.request.request_id
            )
        )

        self.assertIsNone(bound)
        self.assertEqual((row.status, row.active_key, row.message_id), ("expired", None, ""))

    def test_expiry_persists_and_does_not_increase_success_count(self):
        self._mark("100", "a", "b", "c", "d", "e")
        created = self._prepare()
        self.assertIsNone(get_active_refill(self.session, date_str=DATE, group_id="100", now_ts=NOW_TS + 601))
        self.session.commit()
        row = self.session.scalar(
            select(GroupRoastRefillRequest).where(
                GroupRoastRefillRequest.request_id == created.request.request_id
            )
        )
        self.assertEqual((row.status, row.active_key), ("expired", None))
        next_round = self._prepare(now_ts=NOW_TS + 602)
        self.assertEqual((next_round.request.success_count_before, next_round.request.required_ratio), (0, 25))

    def test_groups_are_independent_but_usage_is_global(self):
        self._mark("100", "shared", "a", "b")
        self._mark("200", "shared", "c", "d")
        first = self._prepare("100")
        second = self._prepare("200")
        self.assertNotEqual(first.request.request_id, second.request.request_id)
        self.assertEqual((first.request.required_votes, second.request.required_votes), (2, 2))

    def test_migration_backfills_roll_and_roast_activity_without_bot_target(self):
        self.session.add(GroupRoll(group_id="300", user_id="rolled", pig_id="pig", date_str=DATE))
        self.session.add(GroupRoll(
            group_id="300",
            user_id="historical",
            pig_id="pig-old",
            date_str=DATE - dt.timedelta(days=30),
        ))
        self.session.add(RoastEvent(
            date_str=DATE,
            group_id="300",
            event_type="success",
            attacker_id="attacker",
            target_id="target",
        ))
        self.session.add(RoastEvent(
            date_str=DATE,
            group_id="300",
            event_type="bot_backfire",
            attacker_id="challenger",
            target_id="bot",
        ))
        self.session.commit()

        ensure_runtime_migrations(
            self.engine,
            activity_start=DATE,
            activity_end=DATE,
        )

        active = set(self.session.scalars(
            select(GroupDailyActiveUser.user_id).where(
                GroupDailyActiveUser.date_str == DATE,
                GroupDailyActiveUser.group_id == "300",
            )
        ))
        self.assertEqual(active, {"rolled", "attacker", "target", "challenger"})
        historical = set(self.session.scalars(
            select(GroupDailyActiveUser.user_id).where(
                GroupDailyActiveUser.date_str == DATE - dt.timedelta(days=30),
                GroupDailyActiveUser.group_id == "300",
            )
        ))
        self.assertFalse(historical)

    def test_migration_can_skip_activity_backfill_after_initial_table_creation(self):
        self.session.add(GroupRoll(group_id="400", user_id="late", pig_id="pig", date_str=DATE))
        self.session.commit()

        ensure_runtime_migrations(
            self.engine,
            backfill_group_activity=False,
            activity_start=DATE,
            activity_end=DATE,
        )

        active = set(self.session.scalars(
            select(GroupDailyActiveUser.user_id).where(
                GroupDailyActiveUser.date_str == DATE,
                GroupDailyActiveUser.group_id == "400",
            )
        ))
        self.assertFalse(active)

    def test_application_exposes_all_refill_routes(self):
        self.assertEqual(app.version, "0.4.1")
        paths = set(app.openapi()["paths"])
        self.assertTrue({
            "/v1/group-roast-refills/active-users/mark",
            "/v1/group-roast-refills/active-users",
            "/v1/group-roast-refills/prepare",
            "/v1/group-roast-refills/bind-message",
            "/v1/group-roast-refills/active",
            "/v1/group-roast-refills/fail",
            "/v1/group-roast-refills/complete",
        }.issubset(paths))
