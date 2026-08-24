from __future__ import annotations

import datetime as dt
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

os.environ.setdefault("ROLLPIG_CLOUD_DATABASE_URL", "sqlite+pysqlite:///:memory:")

from rollpig_cloud.config import settings
from rollpig_cloud.db import Base
from rollpig_cloud.migrations import (
    _add_column_if_missing,
    _is_duplicate_column_error,
    ensure_runtime_migrations,
)
from rollpig_cloud.models import Collection, DailyRoll, RoastEvent, UserDrawState, UserPigProgress
from rollpig_cloud.routers.daily_rolls import (
    complete_daily_roll_snapshot,
    get_daily_roll_by_date,
    get_or_create_daily_roll,
)
from rollpig_cloud.routers.events import list_events
from rollpig_cloud.schemas import (
    DailyRollGetOrCreateRequest,
    DailyRollSnapshotRequest,
    EventCreateRequest,
)
from rollpig_cloud.services.events import record_roast_event


DATE = dt.date(2026, 8, 23)


class CloudDailyRollSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _create_roll(self, user_id: str = "user", pig_id: str = "pig"):
        return get_or_create_daily_roll(
            DailyRollGetOrCreateRequest(
                user_id=user_id,
                proposed_pig_id=pig_id,
                date_str=DATE,
                group_id="100",
            ),
            self.session,
        )

    def test_created_roll_freezes_growth_and_collection_snapshot(self):
        self.session.add_all([
            Collection(user_id="user", pig_id="old-pig"),
            UserDrawState(
                tenant_id=settings.default_tenant_id,
                user_id="user",
                duplicate_streak=4,
            ),
        ])
        self.session.commit()

        created = self._create_roll()

        self.assertTrue(created.created)
        self.assertTrue(created.is_new_pig)
        self.assertEqual((created.previous_copies, created.copies), (0, 1))
        self.assertEqual((created.previous_duplicate_streak, created.duplicate_streak), (4, 0))
        self.assertEqual(created.outcome_snapshot.collection_size_after_roll, 2)
        self.assertFalse(created.outcome_snapshot.snapshot_available)

        row = self.session.scalar(select(DailyRoll).where(DailyRoll.user_id == "user"))
        self.assertEqual(
            (
                row.is_new_pig,
                row.previous_copies,
                row.copies_after_roll,
                row.collection_size_after_roll,
                row.previous_duplicate_streak,
                row.duplicate_streak_after_roll,
            ),
            (True, 0, 1, 2, 4, 0),
        )

        # 后续进度变化不能污染昨日接口保存的历史结果。
        progress = self.session.scalar(select(UserPigProgress).where(UserPigProgress.user_id == "user"))
        progress.copies = 5
        draw_state = self.session.scalar(select(UserDrawState).where(UserDrawState.user_id == "user"))
        draw_state.duplicate_streak = 9
        self.session.add(Collection(user_id="user", pig_id="later-pig"))
        self.session.commit()

        historical = get_daily_roll_by_date("user", DATE, self.session)
        self.assertEqual((historical.previous_copies, historical.copies), (0, 1))
        self.assertEqual((historical.previous_duplicate_streak, historical.duplicate_streak), (4, 0))
        self.assertEqual(historical.outcome_snapshot.collection_size_after_roll, 2)
        self.assertFalse(historical.outcome_snapshot.snapshot_available)

    def test_duplicate_roll_saves_previous_and_current_copies(self):
        self.session.add_all(
            [
                Collection(user_id="user", pig_id="pig"),
                UserPigProgress(
                    tenant_id=settings.default_tenant_id,
                    user_id="user",
                    pig_id="pig",
                    copies=2,
                ),
                UserDrawState(
                    tenant_id=settings.default_tenant_id,
                    user_id="user",
                    duplicate_streak=1,
                ),
            ]
        )
        self.session.commit()

        created = self._create_roll()

        self.assertFalse(created.is_new_pig)
        self.assertEqual((created.previous_copies, created.copies), (2, 3))
        self.assertEqual((created.previous_duplicate_streak, created.duplicate_streak), (1, 2))
        self.assertEqual(created.outcome_snapshot.collection_size_after_roll, 1)

        draw_state = self.session.scalar(select(UserDrawState).where(UserDrawState.user_id == "user"))
        draw_state.duplicate_streak = 8
        self.session.commit()

        historical = get_daily_roll_by_date("user", DATE, self.session)
        self.assertEqual((historical.previous_duplicate_streak, historical.duplicate_streak), (1, 2))

        row = self.session.scalar(select(DailyRoll).where(DailyRoll.user_id == "user"))
        self.assertEqual((row.previous_duplicate_streak, row.duplicate_streak_after_roll), (1, 2))

    def test_legacy_row_keeps_flat_compatibility_without_claiming_snapshot(self):
        self.session.add_all(
            [
                DailyRoll(date_str=DATE, user_id="legacy", pig_id="pig"),
                Collection(user_id="legacy", pig_id="pig"),
                UserPigProgress(
                    tenant_id=settings.default_tenant_id,
                    user_id="legacy",
                    pig_id="pig",
                    copies=4,
                ),
            ]
        )
        self.session.commit()

        response = get_daily_roll_by_date("legacy", DATE, self.session)

        self.assertIsNone(response.outcome_snapshot)
        self.assertEqual((response.previous_copies, response.copies), (4, 4))

    def test_appearance_snapshot_is_first_writer_wins_and_idempotent(self):
        self._create_roll()
        request = DailyRollSnapshotRequest(
            user_id="user",
            date_str=DATE,
            pig_id="pig",
            resource_version="2026-08-20.1",
            resolved_variant_level=2,
            resolved_image_name="pig_ex2.png",
            unlocked_variant_levels=[2, 2],
            unlocked_variant_fields=["description", "image", "description"],
        )

        first = complete_daily_roll_snapshot(request, self.session)
        second = complete_daily_roll_snapshot(request, self.session)

        self.assertTrue(first.ok and second.ok)
        self.assertTrue(first.outcome_snapshot.snapshot_available)
        self.assertEqual(first.outcome_snapshot.unlocked_variant_levels, [2])
        self.assertEqual(first.outcome_snapshot.unlocked_variant_fields, ["image", "description"])

        historical = get_daily_roll_by_date("user", DATE, self.session)
        self.assertTrue(historical.outcome_snapshot.snapshot_available)
        self.assertEqual(historical.outcome_snapshot.resource_version, "2026-08-20.1")

        changed = request.model_copy(update={"resolved_image_name": "other.png"})
        with self.assertRaises(HTTPException) as raised:
            complete_daily_roll_snapshot(changed, self.session)
        self.assertEqual(raised.exception.status_code, 409)

    def test_old_row_and_unsafe_filename_cannot_be_completed(self):
        self.session.add(DailyRoll(date_str=DATE, user_id="legacy", pig_id="pig"))
        self.session.commit()
        with self.assertRaises(ValidationError):
            DailyRollSnapshotRequest(
                user_id="legacy",
                date_str=DATE,
                pig_id="pig",
                resource_version="version",
                resolved_image_name="../pig.png",
            )

        request = DailyRollSnapshotRequest(
            user_id="legacy",
            date_str=DATE,
            pig_id="pig",
            resource_version="version",
        )
        with self.assertRaises(HTTPException) as raised:
            complete_daily_roll_snapshot(request, self.session)
        self.assertEqual(raised.exception.status_code, 409)

    def test_partial_growth_snapshot_cannot_be_completed(self):
        self.session.add(DailyRoll(
            date_str=DATE,
            user_id="partial",
            pig_id="pig",
            is_new_pig=False,
            previous_copies=1,
            copies_after_roll=2,
            collection_size_after_roll=1,
        ))
        self.session.commit()

        request = DailyRollSnapshotRequest(
            user_id="partial",
            date_str=DATE,
            pig_id="pig",
            resource_version="version",
        )
        with self.assertRaises(HTTPException) as raised:
            complete_daily_roll_snapshot(request, self.session)
        self.assertEqual(raised.exception.status_code, 409)

    def test_runtime_migration_adds_snapshot_columns_idempotently(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "legacy.sqlite3"
            engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}", future=True)
            with engine.begin() as connection:
                connection.execute(text(
                    "CREATE TABLE daily_rolls ("
                    "id INTEGER PRIMARY KEY, date_str DATE NOT NULL, user_id VARCHAR(64) NOT NULL, "
                    "pig_id VARCHAR(128) NOT NULL, created_at DATETIME)"
                ))

            ensure_runtime_migrations(engine, backfill_group_activity=False)
            ensure_runtime_migrations(engine, backfill_group_activity=False)

            columns = {column["name"] for column in inspect(engine).get_columns("daily_rolls")}
            self.assertTrue({
                "is_new_pig",
                "previous_copies",
                "copies_after_roll",
                "collection_size_after_roll",
                "previous_duplicate_streak",
                "duplicate_streak_after_roll",
                "resource_version",
                "appearance_snapshot",
            }.issubset(columns))
            engine.dispose()

    def test_duplicate_column_race_is_accepted_after_schema_reinspection(self):
        engine = MagicMock()
        engine.dialect.name = "sqlite"
        connection = engine.begin.return_value.__enter__.return_value
        connection.execute.side_effect = OperationalError(
            "ALTER TABLE daily_rolls ADD COLUMN snapshot INTEGER",
            {},
            sqlite3.OperationalError("duplicate column name: snapshot"),
        )
        before = MagicMock()
        before.get_columns.return_value = [{"name": "id"}]
        after = MagicMock()
        after.get_columns.return_value = [{"name": "id"}, {"name": "snapshot"}]

        with patch("rollpig_cloud.migrations.inspect", side_effect=[before, after]):
            _add_column_if_missing(engine, "daily_rolls", "snapshot", "INTEGER NULL")

        connection.execute.assert_called_once()

    def test_duplicate_column_error_is_reraised_when_column_is_still_missing(self):
        engine = MagicMock()
        engine.dialect.name = "sqlite"
        connection = engine.begin.return_value.__enter__.return_value
        connection.execute.side_effect = OperationalError(
            "ALTER TABLE daily_rolls ADD COLUMN snapshot INTEGER",
            {},
            sqlite3.OperationalError("duplicate column name: snapshot"),
        )
        missing = MagicMock()
        missing.get_columns.return_value = [{"name": "id"}]

        with patch("rollpig_cloud.migrations.inspect", side_effect=[missing, missing]):
            with self.assertRaises(OperationalError):
                _add_column_if_missing(engine, "daily_rolls", "snapshot", "INTEGER NULL")

    def test_mysql_duplicate_column_error_is_matched_by_error_code(self):
        duplicate = OperationalError("ALTER TABLE", {}, Exception(1060, "Duplicate column name"))
        unrelated = OperationalError("ALTER TABLE", {}, Exception(1054, "Unknown column"))

        self.assertTrue(_is_duplicate_column_error(duplicate, "mysql"))
        self.assertFalse(_is_duplicate_column_error(unrelated, "mysql"))


class CloudEventQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def test_events_are_ordered_and_user_filter_covers_reservation_roles(self):
        later = dt.datetime(2026, 8, 23, 10, 0, 0)
        earlier = dt.datetime(2026, 8, 23, 9, 0, 0)
        self.session.add_all(
            [
                RoastEvent(
                    date_str=DATE,
                    group_id="100",
                    event_type="success",
                    attacker_id="other",
                    target_id="unrelated",
                    created_at=later,
                ),
                RoastEvent(
                    date_str=DATE,
                    group_id="100",
                    event_type="success",
                    attacker_id="owner",
                    target_id="target",
                    reservation_id="reservation-a",
                    participant_snapshot={"ids": ["user"], "names": ["用户"], "count": 1},
                    created_at=earlier,
                ),
                RoastEvent(
                    date_str=DATE,
                    group_id="100",
                    event_type="backfire",
                    attacker_id="owner-2",
                    target_id="target-2",
                    reservation_id="reservation-b",
                    participant_snapshot={
                        "ids": ["helper"],
                        "names": ["帮厨"],
                        "count": 1,
                        "backfire_victim_id": "user",
                        "backfire_victim_name": "用户",
                    },
                    created_at=earlier,
                ),
            ]
        )
        self.session.commit()

        response = list_events(DATE, group_id="100", user_id="user", session=self.session)

        self.assertEqual([item.reservation_id for item in response.items], ["reservation-a", "reservation-b"])
        self.assertEqual([item.event_id for item in response.items], ["2", "3"])
        self.assertTrue(all(item.created_at == earlier for item in response.items))

    def test_non_reservation_special_reason_round_trips_without_fake_participants(self):
        record_roast_event(
            self.session,
            EventCreateRequest(
                event_type="special",
                attacker_id="owner",
                target_id="target",
                group_id="100",
                date_str=DATE,
                special_reason="human",
            ),
        )
        record_roast_event(
            self.session,
            EventCreateRequest(
                event_type="success",
                attacker_id="owner",
                target_id="other",
                group_id="100",
                date_str=DATE,
            ),
        )
        self.session.commit()

        rows = self.session.scalars(select(RoastEvent).order_by(RoastEvent.id)).all()
        self.assertEqual(rows[0].participant_snapshot, {"special_reason": "human"})
        self.assertIsNone(rows[1].participant_snapshot)

        response = list_events(DATE, group_id="100", session=self.session)
        self.assertEqual([item.special_reason for item in response.items], ["human", ""])
        self.assertEqual(response.items[0].participant_ids, [])


if __name__ == "__main__":
    unittest.main()
