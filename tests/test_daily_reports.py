from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

os.environ.setdefault("ROLLPIG_CLOUD_DATABASE_URL", "sqlite+pysqlite:///:memory:")

from rollpig_cloud.db import Base
from rollpig_cloud.migrations import ensure_runtime_migrations
from rollpig_cloud.models import (
    Collection,
    DailyReportDelivery,
    DailyRoll,
    GroupDailyActiveUser,
    GroupRoll,
    RoastEvent,
)
from rollpig_cloud.routers.daily_reports import (
    DAILY_REPORT_CLAIM_TIMEOUT,
    DAILY_REPORT_RETRY_DELAYS,
    claim_daily_reports,
    get_daily_report_profiles,
    transition_daily_report,
)
from rollpig_cloud.routers.events import list_events
from rollpig_cloud.routers.group_rolls import get_group_rolls
from rollpig_cloud.routers.roast_refills import list_active_users
from rollpig_cloud.schemas import (
    DailyReportClaimRequest,
    DailyReportDeliveryCandidate,
    DailyReportProfileRequest,
    DailyReportTransitionRequest,
)


DATE = dt.date(2026, 8, 30)
CUTOFF = dt.datetime(2026, 8, 30, 23, 45, tzinfo=dt.timezone(dt.timedelta(hours=8)))
NOW = dt.datetime(2026, 8, 30, 15, 46)


def _claim_request(
    instance_id: str,
    *,
    group_id: str = "100",
    delivery_bot_id: str = "bot-a",
) -> DailyReportClaimRequest:
    return DailyReportClaimRequest(
        date_str=DATE,
        cutoff_at=CUTOFF,
        instance_id=instance_id,
        candidates=[
            DailyReportDeliveryCandidate(
                group_id=group_id,
                delivery_bot_id=delivery_bot_id,
            )
        ],
    )


class DailyReportDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)
        self.now = NOW

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _claim(self, instance_id: str, group_id: str = "100"):
        with patch("rollpig_cloud.routers.daily_reports._utc_now", return_value=self.now):
            return claim_daily_reports(
                _claim_request(instance_id, group_id=group_id),
                self.session,
            )

    def _transition(self, claim, action: str):
        with patch("rollpig_cloud.routers.daily_reports._utc_now", return_value=self.now):
            return transition_daily_report(
                DailyReportTransitionRequest(
                    date_str=claim.date_str,
                    group_id=claim.group_id,
                    claim_token=claim.claim_token,
                    action=action,
                ),
                self.session,
            )

    def test_competing_instances_only_claim_group_once(self) -> None:
        first = self._claim("instance-a")
        second = self._claim("instance-b")

        self.assertEqual(len(first.items), 1)
        self.assertEqual(second.items, [])
        self.assertEqual(
            second.next_claim_at,
            self.now + DAILY_REPORT_CLAIM_TIMEOUT,
        )
        self.assertEqual(
            self.session.scalar(select(DailyReportDelivery).where(DailyReportDelivery.group_id == "100")).instance_id,
            "instance-a",
        )

    def test_concurrent_claims_have_one_winner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "daily-reports.sqlite3"
            engine = create_engine(
                f"sqlite+pysqlite:///{database_path.as_posix()}",
                future=True,
                connect_args={"check_same_thread": False, "timeout": 10},
            )
            Base.metadata.create_all(engine)
            barrier = Barrier(2)

            def run_claim(instance_id: str) -> int:
                with Session(engine, expire_on_commit=False) as session:
                    barrier.wait(timeout=5)
                    return len(claim_daily_reports(_claim_request(instance_id), session).items)

            with (
                patch("rollpig_cloud.routers.daily_reports._utc_now", return_value=self.now),
                ThreadPoolExecutor(max_workers=2) as executor,
            ):
                results = list(executor.map(run_claim, ("instance-a", "instance-b")))

            with Session(engine) as session:
                rows = session.scalars(select(DailyReportDelivery)).all()
            engine.dispose()

        self.assertEqual(sorted(results), [0, 1])
        self.assertEqual(len(rows), 1)

    def test_expired_claim_lease_can_be_reclaimed(self) -> None:
        first = self._claim("instance-a").items[0]
        row = self.session.scalar(
            select(DailyReportDelivery).where(DailyReportDelivery.group_id == "100")
        )
        row.claimed_at = (
            self.now
            - DAILY_REPORT_CLAIM_TIMEOUT
            - dt.timedelta(seconds=1)
        )
        self.session.commit()

        reclaimed = self._claim("instance-b").items[0]

        self.assertNotEqual(reclaimed.claim_token, first.claim_token)
        self.assertEqual(reclaimed.delivery_bot_id, "bot-a")

    def test_sending_terminal_and_uncertain_states_are_not_reclaimed(self) -> None:
        actions = {
            "sending": ("sending",),
            "sent": ("sending", "sent"),
            "uncertain": ("sending", "uncertain"),
            "skipped": ("skip",),
        }
        for index, (expected_status, transitions) in enumerate(actions.items(), start=1):
            group_id = str(100 + index)
            with self.subTest(status=expected_status):
                claim = self._claim("instance-a", group_id).items[0]
                for action in transitions:
                    result = self._transition(claim, action)
                    self.assertTrue(result.ok)
                competing = self._claim("instance-b", group_id)
                self.assertEqual(competing.items, [])
                row = self.session.scalar(
                    select(DailyReportDelivery).where(DailyReportDelivery.group_id == group_id)
                )
                self.assertEqual(row.status, expected_status)

    def test_release_makes_claim_available_again(self) -> None:
        first = self._claim("instance-a").items[0]

        released = self._transition(first, "release")
        too_early = self._claim("instance-b")
        self.now = released.next_attempt_at
        second = self._claim("instance-b").items[0]

        self.assertTrue(released.ok)
        self.assertEqual(too_early.items, [])
        self.assertEqual(too_early.next_claim_at, released.next_attempt_at)
        self.assertNotEqual(first.claim_token, second.claim_token)
        self.assertEqual(second.attempt_count, 2)

    def test_sent_requires_sending_and_old_token_cannot_transition(self) -> None:
        first = self._claim("instance-a").items[0]
        invalid = self._transition(first, "sent")
        self.assertFalse(invalid.ok)

        released = self._transition(first, "release")
        self.assertTrue(released.ok)
        self.now = released.next_attempt_at
        second = self._claim("instance-b").items[0]
        stale = self._transition(first, "sending")

        self.assertFalse(stale.ok)
        self.assertTrue(self._transition(second, "sending").ok)

    def test_release_uses_fixed_delays_then_marks_delivery_failed(self) -> None:
        claim = self._claim("instance-a").items[0]

        for attempt, delay in enumerate(DAILY_REPORT_RETRY_DELAYS, start=1):
            released = self._transition(claim, "release")
            self.assertEqual(released.status, "pending")
            self.assertEqual(released.next_attempt_at, self.now + delay)
            self.now = released.next_attempt_at
            claim = self._claim(f"instance-{attempt}").items[0]
            self.assertEqual(claim.attempt_count, attempt + 1)

        exhausted = self._transition(claim, "release")
        self.assertEqual(exhausted.status, "failed")
        self.assertIsNone(exhausted.next_attempt_at)
        self.assertEqual(self._claim("instance-final").items, [])

    def test_release_does_not_schedule_retry_past_daily_deadline(self) -> None:
        self.now = dt.datetime(2026, 8, 30, 16, 9, 50)
        claim = self._claim("instance-a").items[0]

        released = self._transition(claim, "release")

        self.assertEqual(released.status, "failed")
        self.assertIsNone(released.next_attempt_at)


class DailyReportProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)
        before = dt.datetime(2026, 8, 30, 15, 44)
        after = dt.datetime(2026, 8, 30, 15, 46)
        self.session.add_all(
            [
                GroupRoll(
                    date_str=DATE,
                    group_id="100",
                    user_id="daily",
                    pig_id="pig-daily",
                    seen_at=before,
                ),
                GroupRoll(
                    date_str=DATE,
                    group_id="100",
                    user_id="late",
                    pig_id="pig-late",
                    seen_at=after,
                ),
                DailyRoll(
                    date_str=DATE,
                    user_id="daily",
                    pig_id="pig-daily",
                    copies_after_roll=4,
                    collection_size_after_roll=2,
                    created_at=before,
                ),
                DailyRoll(
                    date_str=DATE - dt.timedelta(days=1),
                    user_id="recent",
                    pig_id="pig-recent",
                    copies_after_roll=2,
                    collection_size_after_roll=1,
                    created_at=before - dt.timedelta(days=1),
                ),
                DailyRoll(
                    date_str=DATE,
                    user_id="late",
                    pig_id="pig-late",
                    copies_after_roll=5,
                    collection_size_after_roll=1,
                    created_at=after,
                ),
                Collection(user_id="daily", pig_id="pig-one", first_seen_at=before - dt.timedelta(days=2)),
                Collection(user_id="daily", pig_id="pig-daily", first_seen_at=before),
                Collection(user_id="daily", pig_id="pig-after", first_seen_at=after),
                Collection(user_id="recent", pig_id="pig-recent", first_seen_at=before - dt.timedelta(days=1)),
            ]
        )
        self.session.commit()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def test_profiles_use_cutoff_and_return_all_requested_users(self) -> None:
        response = get_daily_report_profiles(
            DailyReportProfileRequest(
                date_str=DATE,
                group_id="100",
                cutoff_at=CUTOFF,
                user_ids=["recent", "daily", "late", "daily"],
            ),
            self.session,
        )
        profiles = {item.user_id: item for item in response.items}

        self.assertEqual(list(profiles), ["daily", "late", "recent"])
        self.assertEqual(profiles["daily"].daily_pig_id, "pig-daily")
        self.assertEqual(profiles["daily"].daily_ex_level, 3)
        self.assertEqual(profiles["daily"].catalog_count, 2)
        self.assertEqual(profiles["daily"].recent_pig_id, "pig-daily")
        self.assertEqual(profiles["recent"].daily_pig_id, "")
        self.assertEqual(profiles["recent"].recent_pig_id, "pig-recent")
        self.assertEqual(profiles["recent"].recent_ex_level, 1)
        self.assertEqual(profiles["late"].daily_pig_id, "")
        self.assertEqual(profiles["late"].catalog_count, 0)

    def test_profiles_chunk_large_group_without_dropping_users(self) -> None:
        user_ids = [f"user-{index:04d}" for index in range(1200)]

        response = get_daily_report_profiles(
            DailyReportProfileRequest(
                date_str=DATE,
                group_id="100",
                cutoff_at=CUTOFF,
                user_ids=user_ids,
            ),
            self.session,
        )

        self.assertEqual(len(response.items), len(user_ids))
        self.assertEqual(response.items[0].user_id, "user-0000")
        self.assertEqual(response.items[-1].user_id, "user-1199")


class DailyReportMigrationTests(unittest.TestCase):
    def test_runtime_migration_adds_retry_columns_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "legacy-daily-reports.sqlite3"
            engine = create_engine(
                f"sqlite+pysqlite:///{database_path.as_posix()}",
                future=True,
            )
            with engine.begin() as connection:
                connection.execute(text(
                    "CREATE TABLE daily_report_deliveries ("
                    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "date_str DATE NOT NULL, "
                    "group_id VARCHAR(64) NOT NULL, "
                    "status VARCHAR(16) NOT NULL DEFAULT 'pending', "
                    "instance_id VARCHAR(64) NOT NULL DEFAULT '', "
                    "delivery_bot_id VARCHAR(64) NOT NULL DEFAULT '', "
                    "claim_token VARCHAR(64), "
                    "cutoff_at DATETIME NOT NULL, "
                    "claimed_at DATETIME, "
                    "sent_at DATETIME, "
                    "message_id VARCHAR(128) NOT NULL DEFAULT '', "
                    "last_error VARCHAR(512) NOT NULL DEFAULT '', "
                    "created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, "
                    "CONSTRAINT uq_daily_report_delivery_date_group UNIQUE (date_str, group_id))"
                ))
                connection.execute(text(
                    "CREATE INDEX ix_daily_report_delivery_status "
                    "ON daily_report_deliveries (date_str, status)"
                ))
                connection.execute(text(
                    "INSERT INTO daily_report_deliveries "
                    "(date_str, group_id, cutoff_at) "
                    "VALUES ('2026-08-30', '100', '2026-08-30 15:45:00')"
                ))

            ensure_runtime_migrations(engine, backfill_group_activity=False)
            ensure_runtime_migrations(engine, backfill_group_activity=False)

            columns = {
                column["name"]
                for column in inspect(engine).get_columns("daily_report_deliveries")
            }
            self.assertTrue({"attempt_count", "next_attempt_at"}.issubset(columns))
            with Session(engine) as session:
                row = session.scalar(select(DailyReportDelivery))
                self.assertIsNotNone(row)
                self.assertEqual(row.attempt_count, 0)
                self.assertIsNone(row.next_attempt_at)
            engine.dispose()

    def test_delivery_identifiers_reject_values_longer_than_database_columns(self) -> None:
        with self.assertRaises(ValueError):
            DailyReportClaimRequest(
                date_str=DATE,
                cutoff_at=CUTOFF,
                instance_id="instance-a",
                candidates=[
                    DailyReportDeliveryCandidate(
                        group_id="1" * 65,
                        delivery_bot_id="bot-a",
                    )
                ],
            )


class DailyReportCutoffQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)
        before = dt.datetime(2026, 8, 30, 15, 44)
        after = dt.datetime(2026, 8, 30, 15, 46)
        self.session.add_all(
            [
                GroupRoll(
                    date_str=DATE,
                    group_id="100",
                    user_id="before",
                    pig_id="pig-a",
                    seen_at=before,
                ),
                GroupRoll(
                    date_str=DATE,
                    group_id="100",
                    user_id="after",
                    pig_id="pig-b",
                    seen_at=after,
                ),
                GroupDailyActiveUser(date_str=DATE, group_id="100", user_id="before", active_at=before),
                GroupDailyActiveUser(date_str=DATE, group_id="100", user_id="after", active_at=after),
                RoastEvent(
                    date_str=DATE,
                    group_id="100",
                    event_type="success",
                    attacker_id="before",
                    target_id="target-a",
                    created_at=before,
                ),
                RoastEvent(
                    date_str=DATE,
                    group_id="100",
                    event_type="escape",
                    attacker_id="after",
                    target_id="target-b",
                    created_at=after,
                ),
            ]
        )
        self.session.commit()

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def test_events_group_rolls_and_active_users_share_cutoff(self) -> None:
        events = list_events(DATE, group_id="100", cutoff_at=CUTOFF, session=self.session)
        rolls = get_group_rolls("100", DATE, cutoff_at=CUTOFF, session=self.session)
        active = list_active_users("100", DATE, cutoff_at=CUTOFF, session=self.session)

        self.assertEqual([item.attacker for item in events.items], ["before"])
        self.assertEqual([item.user_id for item in rolls.items], ["before"])
        self.assertEqual(active.user_ids, ["before"])


if __name__ == "__main__":
    unittest.main()
