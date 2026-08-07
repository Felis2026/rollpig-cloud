from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

os.environ.setdefault("ROLLPIG_CLOUD_DATABASE_URL", "sqlite+pysqlite:///:memory:")

from rollpig_cloud.db import Base
from rollpig_cloud.main import app
from rollpig_cloud.migrations import ensure_runtime_migrations
from rollpig_cloud.models import DailyRoll, GroupProtection, RoastReservation, RoastReservationParticipant, UserUsage
from rollpig_cloud.schemas import ConsumeForceRequest, ConsumeRoastRequest, RoastReservationPrepareRequest
from rollpig_cloud.schemas import (
    RoastReservationClaimRequest,
    RoastReservationMutationRequest,
    RoastReservationOutcomeRequest,
)
from rollpig_cloud.routers.roast_reservations import claim, complete, release, save_outcome
from rollpig_cloud.routers.cooldowns import consume_force, consume_roast
from rollpig_cloud.services.reservations import activate_target_reservations, prepare_reservation


class CloudRoastReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        Base.metadata.create_all(self.engine)
        self.session = Session(self.engine, expire_on_commit=False)

    def tearDown(self) -> None:
        self.session.close()
        self.engine.dispose()

    def _request(self, attacker_id: str = "a", **overrides) -> RoastReservationPrepareRequest:
        payload = {
            "attacker_id": attacker_id,
            "attacker_name": attacker_id.upper(),
            "attacker_pig_id": f"pig-{attacker_id}",
            "target_id": "target",
            "target_name": "Target",
            "group_id": "100",
            "delivery_bot_id": "bot-1",
            "date_str": dt.date(2026, 8, 7),
            "now_ts": 1_786_032_000,
            "cooldown_seconds": 3600,
            "max_charges": 2,
        }
        payload.update(overrides)
        return RoastReservationPrepareRequest(**payload)

    def test_prepare_create_join_and_duplicate_is_atomic(self):
        created = prepare_reservation(self.session, self._request())
        self.session.commit()
        self.assertEqual(created.status, "reservation_created")
        usage = self.session.scalar(select(UserUsage).where(UserUsage.user_id == "a"))
        self.assertEqual(usage.roast_charges, 1)

        duplicate = prepare_reservation(self.session, self._request())
        self.session.commit()
        self.assertEqual(duplicate.status, "already_joined")
        self.assertEqual(usage.roast_charges, 1)

        joined = prepare_reservation(self.session, self._request("b"))
        self.session.commit()
        self.assertEqual(joined.status, "reservation_joined")
        self.assertIsNone(self.session.scalar(select(UserUsage).where(UserUsage.user_id == "b")))

    def test_existing_cooldown_endpoints_keep_charge_and_force_semantics(self):
        first = consume_roast(
            ConsumeRoastRequest(
                user_id="legacy-user",
                now_ts=1_786_032_000,
                cooldown_seconds=3600,
                max_charges=2,
            ),
            self.session,
        )
        second = consume_roast(
            ConsumeRoastRequest(
                user_id="legacy-user",
                now_ts=1_786_032_001,
                cooldown_seconds=3600,
                max_charges=2,
            ),
            self.session,
        )
        denied = consume_roast(
            ConsumeRoastRequest(
                user_id="legacy-user",
                now_ts=1_786_032_002,
                cooldown_seconds=3600,
                max_charges=2,
            ),
            self.session,
        )
        self.assertEqual((first.allowed, first.charges_left), (True, 1))
        self.assertEqual((second.allowed, second.charges_left), (True, 0))
        self.assertFalse(denied.allowed)

        force_request = ConsumeForceRequest(user_id="force-user", date_str=dt.date(2026, 8, 7))
        self.assertTrue(consume_force(force_request, self.session).allowed)
        self.assertFalse(consume_force(force_request, self.session).allowed)

    def test_target_ready_never_consumes_usage(self):
        self.session.add(DailyRoll(date_str=dt.date(2026, 8, 7), user_id="target", pig_id="pig-target"))
        self.session.commit()
        result = prepare_reservation(self.session, self._request())
        self.session.commit()
        self.assertEqual(result.status, "target_ready")
        self.assertEqual(result.target_pig_id, "pig-target")
        self.assertIsNone(self.session.scalar(select(UserUsage).where(UserUsage.user_id == "a")))

    def test_activation_is_idempotent(self):
        created = prepare_reservation(self.session, self._request())
        self.session.commit()
        first = activate_target_reservations(
            self.session,
            date_str=dt.date(2026, 8, 7),
            target_id="target",
            target_pig_id="pig-target",
        )
        second = activate_target_reservations(
            self.session,
            date_str=dt.date(2026, 8, 7),
            target_id="target",
            target_pig_id="pig-target",
        )
        self.session.commit()
        row = self.session.scalar(
            select(RoastReservation).where(RoastReservation.reservation_id == created.reservation.reservation_id)
        )
        self.assertEqual((first, second), (1, 0))
        self.assertEqual((row.status, row.target_pig_id), ("ready", "pig-target"))

    def test_multiple_groups_activate_as_independent_reservations(self):
        first = prepare_reservation(self.session, self._request(group_id="100", delivery_bot_id="bot-1"))
        second = prepare_reservation(self.session, self._request(group_id="200", delivery_bot_id="bot-2"))
        self.session.commit()
        self.assertNotEqual(first.reservation.reservation_id, second.reservation.reservation_id)

        activated = activate_target_reservations(
            self.session,
            date_str=dt.date(2026, 8, 7),
            target_id="target",
            target_pig_id="pig-target",
        )
        self.session.commit()
        self.assertEqual(activated, 2)
        rows = self.session.scalars(select(RoastReservation).order_by(RoastReservation.group_id)).all()
        self.assertEqual([(row.group_id, row.status) for row in rows], [("100", "ready"), ("200", "ready")])

    def test_participant_limit_is_twelve(self):
        prepare_reservation(self.session, self._request())
        self.session.commit()
        for index in range(2, 13):
            result = prepare_reservation(self.session, self._request(f"u{index}"))
            self.session.commit()
            self.assertEqual(result.status, "reservation_joined")
        full = prepare_reservation(self.session, self._request("u13"))
        self.session.commit()
        self.assertEqual(full.status, "reservation_full")
        count = len(self.session.scalars(select(RoastReservationParticipant)).all())
        self.assertEqual(count, 12)

    def test_protection_blocks_creation_but_existing_reservation_remains_joinable(self):
        self.session.add(
            GroupProtection(
                protect_date=dt.date(2026, 8, 7),
                group_id="100",
                user_id="target",
            )
        )
        self.session.commit()
        blocked = prepare_reservation(self.session, self._request())
        self.session.commit()
        self.assertEqual(blocked.status, "protected")

        forced = prepare_reservation(self.session, self._request(force_mode="normal"))
        self.session.commit()
        self.assertEqual(forced.status, "reservation_created")
        self.assertTrue(forced.protection_broken)

        joined = prepare_reservation(self.session, self._request("b"))
        self.session.commit()
        self.assertEqual(joined.status, "reservation_joined")

    def test_claim_outcome_release_and_complete_are_idempotent(self):
        created = prepare_reservation(self.session, self._request())
        self.session.commit()
        activate_target_reservations(
            self.session,
            date_str=dt.date(2026, 8, 7),
            target_id="target",
            target_pig_id="pig-target",
        )
        self.session.commit()

        claimed = claim(
            RoastReservationClaimRequest(delivery_bot_id="bot-1", date_str=dt.date(2026, 8, 7)),
            self.session,
        )
        self.assertEqual(len(claimed.items), 1)
        reservation = claimed.items[0]
        self.assertTrue(reservation.claim_token)
        self.assertFalse(
            claim(
                RoastReservationClaimRequest(delivery_bot_id="bot-1", date_str=dt.date(2026, 8, 7)),
                self.session,
            ).items
        )

        snapshot = {"event_type": "escape", "plain_text": "fixed"}
        saved = save_outcome(
            RoastReservationOutcomeRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
                outcome_snapshot=snapshot,
            ),
            self.session,
        )
        self.assertTrue(saved.ok)
        second_save = save_outcome(
            RoastReservationOutcomeRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
                outcome_snapshot={"event_type": "success"},
            ),
            self.session,
        )
        self.assertEqual(second_save.reservation.outcome_snapshot, snapshot)

        released = release(
            RoastReservationMutationRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
            ),
            self.session,
        )
        self.assertTrue(released.ok)
        reclaimed = claim(
            RoastReservationClaimRequest(delivery_bot_id="bot-1", date_str=dt.date(2026, 8, 7)),
            self.session,
        ).items[0]
        self.assertEqual(reclaimed.outcome_snapshot, snapshot)
        self.assertNotEqual(reclaimed.claim_token, reservation.claim_token)

        completed = complete(
            RoastReservationMutationRequest(
                reservation_id=reclaimed.reservation_id,
                claim_token=reclaimed.claim_token,
            ),
            self.session,
        )
        self.assertTrue(completed.ok)
        repeated = complete(
            RoastReservationMutationRequest(
                reservation_id=reclaimed.reservation_id,
                claim_token=reclaimed.claim_token,
            ),
            self.session,
        )
        self.assertTrue(repeated.ok)
        self.assertFalse(
            claim(
                RoastReservationClaimRequest(delivery_bot_id="bot-1", date_str=dt.date(2026, 8, 7)),
                self.session,
            ).items
        )

    def test_runtime_migration_adds_optional_event_columns(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "legacy.sqlite3"
            engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}", future=True)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "CREATE TABLE roast_events ("
                            "id INTEGER PRIMARY KEY, "
                            "date_str DATE NOT NULL, "
                            "event_type VARCHAR(32) NOT NULL, "
                            "attacker_id VARCHAR(64) NOT NULL, "
                            "target_id VARCHAR(64) NOT NULL, "
                            "group_id VARCHAR(64) NOT NULL, "
                            "attacker_name VARCHAR(128) NOT NULL, "
                            "target_name VARCHAR(128) NOT NULL, "
                            "food_name VARCHAR(128) NOT NULL, "
                            "created_at DATETIME NOT NULL"
                            ")"
                        )
                    )
                ensure_runtime_migrations(engine)
                columns = {column["name"] for column in inspect(engine).get_columns("roast_events")}
                self.assertIn("reservation_id", columns)
                self.assertIn("participant_snapshot", columns)
            finally:
                engine.dispose()

    def test_application_exposes_all_reservation_routes(self):
        # FastAPI 0.141+ 会在 app.routes 中保留 _IncludedRouter；OpenAPI 是跨版本
        # 稳定的最终路由视图，也与实际对外文档和请求分发保持一致。
        route_paths = set(app.openapi().get("paths", {}))
        self.assertTrue(
            {
                "/v1/roast-reservations/unrolled-attempt",
                "/v1/roast-reservations/prepare",
                "/v1/roast-reservations/owned",
                "/v1/roast-reservations/claim",
                "/v1/roast-reservations/outcome",
                "/v1/roast-reservations/complete",
                "/v1/roast-reservations/release",
            }.issubset(route_paths)
        )


if __name__ == "__main__":
    unittest.main()
