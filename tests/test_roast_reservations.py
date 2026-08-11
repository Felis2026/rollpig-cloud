from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session

os.environ.setdefault("ROLLPIG_CLOUD_DATABASE_URL", "sqlite+pysqlite:///:memory:")

from rollpig_cloud.db import Base
from rollpig_cloud.main import app
from rollpig_cloud.migrations import ensure_runtime_migrations
from rollpig_cloud.models import (
    DailyRoll,
    GroupProtection,
    RoastEvent,
    RoastReservation,
    RoastReservationParticipant,
    UserUsage,
)
from rollpig_cloud.schemas import ConsumeForceRequest, ConsumeRoastRequest, EventCreateRequest, RoastReservationPrepareRequest
from rollpig_cloud.schemas import (
    RoastReservationClaimRequest,
    RoastReservationCompleteRequest,
    RoastReservationMutationRequest,
    RoastReservationOutcomeRequest,
)
from rollpig_cloud.routers.roast_reservations import (
    claim,
    complete,
    mark_sending,
    prepare_outcome,
    release,
    save_outcome,
)
from rollpig_cloud.routers.cooldowns import consume_force, consume_roast
from rollpig_cloud.routers.events import create_event, list_events
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

    def _claim_request(self, **overrides) -> RoastReservationClaimRequest:
        """测试默认模拟支持 prepared 的新 Plus；旧客户端用原始 schema 构造。"""

        payload = {
            "delivery_bot_id": "bot-1",
            "date_str": dt.date(2026, 8, 7),
            "supports_prepared": True,
        }
        payload.update(overrides)
        return RoastReservationClaimRequest(**payload)

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
        prepare_reservation(self.session, self._request())
        self.session.commit()
        activate_target_reservations(
            self.session,
            date_str=dt.date(2026, 8, 7),
            target_id="target",
            target_pig_id="pig-target",
        )
        self.session.commit()

        claimed = claim(
            self._claim_request(),
            self.session,
        )
        self.assertEqual(len(claimed.items), 1)
        self.assertTrue(claimed.has_owned)
        reservation = claimed.items[0]
        self.assertTrue(reservation.claim_token)
        self.assertFalse(
            claim(
                self._claim_request(),
                self.session,
            ).items
        )

        snapshot = {"event_type": "escape", "plain_text": "fixed"}
        saved = prepare_outcome(
            RoastReservationOutcomeRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
                outcome_snapshot=snapshot,
            ),
            self.session,
        )
        self.assertTrue(saved.ok)
        self.assertEqual(saved.reservation.status, "prepared")
        second_save = prepare_outcome(
            RoastReservationOutcomeRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
                outcome_snapshot=snapshot,
            ),
            self.session,
        )
        self.assertTrue(second_save.ok)
        self.assertEqual(second_save.reservation.outcome_snapshot, snapshot)
        divergent_save = prepare_outcome(
            RoastReservationOutcomeRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
                outcome_snapshot={"event_type": "success"},
            ),
            self.session,
        )
        self.assertFalse(divergent_save.ok)

        released = release(
            RoastReservationMutationRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
            ),
            self.session,
        )
        self.assertTrue(released.ok)
        reclaimed = claim(
            self._claim_request(),
            self.session,
        ).items[0]
        self.assertEqual(reclaimed.outcome_snapshot, snapshot)
        self.assertNotEqual(reclaimed.claim_token, reservation.claim_token)
        self.assertEqual(reclaimed.status, "prepared")

        sending = mark_sending(
            RoastReservationMutationRequest(
                reservation_id=reclaimed.reservation_id,
                claim_token=reclaimed.claim_token,
            ),
            self.session,
        )
        self.assertTrue(sending.ok)
        self.assertEqual(sending.reservation.status, "sending")
        repeated_sending = mark_sending(
            RoastReservationMutationRequest(
                reservation_id=reclaimed.reservation_id,
                claim_token=reclaimed.claim_token,
            ),
            self.session,
        )
        self.assertTrue(repeated_sending.ok)

        late_prepare_retry = prepare_outcome(
            RoastReservationOutcomeRequest(
                reservation_id=reclaimed.reservation_id,
                claim_token=reclaimed.claim_token,
                outcome_snapshot=snapshot,
            ),
            self.session,
        )
        self.assertTrue(late_prepare_retry.ok)

        completed = complete(
            RoastReservationCompleteRequest(
                reservation_id=reclaimed.reservation_id,
                claim_token=reclaimed.claim_token,
            ),
            self.session,
        )
        self.assertTrue(completed.ok)
        repeated = complete(
            RoastReservationCompleteRequest(
                reservation_id=reclaimed.reservation_id,
                claim_token=reclaimed.claim_token,
            ),
            self.session,
        )
        self.assertTrue(repeated.ok)
        final_claim = claim(self._claim_request(), self.session)
        self.assertFalse(final_claim.items)
        self.assertFalse(final_claim.has_owned)

    def test_sending_reservation_is_not_reclaimed_after_claim_timeout(self):
        created = prepare_reservation(self.session, self._request())
        self.session.commit()
        activate_target_reservations(
            self.session,
            date_str=dt.date(2026, 8, 7),
            target_id="target",
            target_pig_id="pig-target",
        )
        self.session.commit()
        reservation = claim(
            self._claim_request(),
            self.session,
        ).items[0]
        saved = prepare_outcome(
            RoastReservationOutcomeRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
                outcome_snapshot={"event_type": "escape"},
            ),
            self.session,
        )
        sending = mark_sending(
            RoastReservationMutationRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
            ),
            self.session,
        )
        refused_release = release(
            RoastReservationMutationRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
            ),
            self.session,
        )
        row = self.session.scalar(
            select(RoastReservation).where(RoastReservation.reservation_id == created.reservation.reservation_id)
        )
        row.claimed_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=1)
        self.session.commit()

        reclaimed = claim(
            self._claim_request(),
            self.session,
        )

        self.assertEqual(saved.reservation.status, "prepared")
        self.assertEqual(sending.reservation.status, "sending")
        self.assertFalse(refused_release.ok)
        self.assertFalse(reclaimed.items)

    def test_stale_prepared_snapshot_is_recoverable(self):
        created = prepare_reservation(self.session, self._request())
        self.session.commit()
        activate_target_reservations(
            self.session,
            date_str=dt.date(2026, 8, 7),
            target_id="target",
            target_pig_id="pig-target",
        )
        self.session.commit()
        first = claim(
            self._claim_request(),
            self.session,
        ).items[0]
        saved = prepare_outcome(
            RoastReservationOutcomeRequest(
                reservation_id=first.reservation_id,
                claim_token=first.claim_token,
                outcome_snapshot={"event_type": "escape", "plain_text": "fixed"},
            ),
            self.session,
        )
        row = self.session.scalar(
            select(RoastReservation).where(
                RoastReservation.reservation_id == created.reservation.reservation_id
            )
        )
        row.claimed_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=1)
        self.session.commit()

        reclaimed = claim(
            self._claim_request(),
            self.session,
        ).items[0]

        self.assertEqual(saved.reservation.status, "prepared")
        self.assertEqual(reclaimed.status, "prepared")
        self.assertEqual(reclaimed.outcome_snapshot["plain_text"], "fixed")
        self.assertNotEqual(reclaimed.claim_token, first.claim_token)

    def test_claim_reports_owner_and_skips_locally_deferred_reservation(self):
        created = prepare_reservation(self.session, self._request())
        self.session.commit()

        pending = claim(self._claim_request(), self.session)
        self.assertFalse(pending.items)
        self.assertTrue(pending.has_owned)

        activate_target_reservations(
            self.session,
            date_str=dt.date(2026, 8, 7),
            target_id="target",
            target_pig_id="pig-target",
        )
        self.session.commit()
        deferred = claim(
            self._claim_request(
                excluded_reservation_ids=[created.reservation.reservation_id]
            ),
            self.session,
        )

        self.assertFalse(deferred.items)
        self.assertTrue(deferred.has_owned)
        row = self.session.scalar(
            select(RoastReservation).where(
                RoastReservation.reservation_id == created.reservation.reservation_id
            )
        )
        self.assertEqual(row.status, "ready")

    def test_legacy_client_can_take_over_stale_prepared_snapshot(self):
        created = prepare_reservation(self.session, self._request())
        self.session.commit()
        activate_target_reservations(
            self.session,
            date_str=dt.date(2026, 8, 7),
            target_id="target",
            target_pig_id="pig-target",
        )
        self.session.commit()
        current = claim(self._claim_request(), self.session).items[0]
        snapshot = {"event_type": "escape", "plain_text": "fixed"}
        prepare_outcome(
            RoastReservationOutcomeRequest(
                reservation_id=current.reservation_id,
                claim_token=current.claim_token,
                outcome_snapshot=snapshot,
            ),
            self.session,
        )
        row = self.session.scalar(
            select(RoastReservation).where(
                RoastReservation.reservation_id == created.reservation.reservation_id
            )
        )
        row.claimed_at = (
            dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
            - dt.timedelta(hours=1)
        )
        self.session.commit()

        # 旧 Plus 没有 supports_prepared 字段；Cloud 必须返回它认识的 sending，
        # 让它直接渲染固定快照，而不是在 prepared 与旧 /outcome 之间循环。
        legacy = claim(
            RoastReservationClaimRequest(
                delivery_bot_id="bot-1",
                date_str=dt.date(2026, 8, 7),
            ),
            self.session,
        ).items[0]

        self.assertEqual(legacy.status, "sending")
        self.assertEqual(legacy.outcome_snapshot, snapshot)
        self.assertNotEqual(legacy.claim_token, current.claim_token)

    def test_legacy_outcome_endpoint_keeps_old_client_sending_semantics(self):
        created = prepare_reservation(self.session, self._request())
        self.session.commit()
        activate_target_reservations(
            self.session,
            date_str=dt.date(2026, 8, 7),
            target_id="target",
            target_pig_id="pig-target",
        )
        self.session.commit()
        reservation = claim(
            RoastReservationClaimRequest(
                delivery_bot_id="bot-1",
                date_str=dt.date(2026, 8, 7),
            ),
            self.session,
        ).items[0]
        released = release(
            RoastReservationMutationRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
            ),
            self.session,
        )
        reservation = claim(
            RoastReservationClaimRequest(
                delivery_bot_id="bot-1",
                date_str=dt.date(2026, 8, 7),
            ),
            self.session,
        ).items[0]

        saved = save_outcome(
            RoastReservationOutcomeRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
                outcome_snapshot={"event_type": "escape"},
            ),
            self.session,
        )

        self.assertTrue(released.ok)
        self.assertTrue(saved.ok)
        self.assertEqual(saved.reservation.status, "sending")

        # 旧 Plus 的完成请求没有 event 字段，随后再通过旧 /events 写日报。
        completed = complete(
            RoastReservationCompleteRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
            ),
            self.session,
        )
        legacy_event = EventCreateRequest(
            event_type="escape",
            attacker_id="wrong-owner",
            target_id="wrong-target",
            group_id="999",
            date_str=dt.date(2026, 8, 8),
            reservation_id=created.reservation.reservation_id,
            participant_ids=["intruder"],
            participant_count=99,
        )
        create_event(legacy_event, self.session)
        create_event(legacy_event, self.session)
        events = self.session.scalars(
            select(RoastEvent).where(
                RoastEvent.reservation_id == created.reservation.reservation_id
            )
        ).all()

        self.assertTrue(completed.ok)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(
            (
                event.date_str,
                event.group_id,
                event.attacker_id,
                event.target_id,
            ),
            (dt.date(2026, 8, 7), "100", "a", "target"),
        )
        self.assertEqual(event.participant_snapshot["ids"], ["a"])

    def test_stale_pre_send_processing_is_still_recoverable(self):
        created = prepare_reservation(self.session, self._request())
        self.session.commit()
        activate_target_reservations(
            self.session,
            date_str=dt.date(2026, 8, 7),
            target_id="target",
            target_pig_id="pig-target",
        )
        self.session.commit()
        first = claim(
            self._claim_request(),
            self.session,
        ).items[0]
        row = self.session.scalar(
            select(RoastReservation).where(RoastReservation.reservation_id == created.reservation.reservation_id)
        )
        row.claimed_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None) - dt.timedelta(hours=1)
        self.session.commit()

        reclaimed = claim(
            self._claim_request(),
            self.session,
        ).items[0]

        self.assertEqual(reclaimed.status, "processing")
        self.assertNotEqual(reclaimed.claim_token, first.claim_token)

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

    def test_runtime_migration_quarantines_ambiguous_processing_outcomes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "legacy.sqlite3"
            engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}", future=True)
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "CREATE TABLE roast_reservations ("
                            "id INTEGER PRIMARY KEY, "
                            "status VARCHAR(16) NOT NULL, "
                            "outcome_snapshot JSON NULL"
                            ")"
                        )
                    )
                    connection.execute(
                        text(
                            "INSERT INTO roast_reservations (id, status, outcome_snapshot) VALUES "
                            "(1, 'processing', '{\"event_type\": \"escape\"}'), "
                            "(2, 'processing', NULL)"
                        )
                    )

                ensure_runtime_migrations(engine)

                with engine.connect() as connection:
                    statuses = connection.execute(
                        text("SELECT id, status FROM roast_reservations ORDER BY id")
                    ).all()
                self.assertEqual(statuses, [(1, "sending"), (2, "processing")])
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
                "/v1/roast-reservations/outcome/prepare",
                "/v1/roast-reservations/sending",
                "/v1/roast-reservations/complete",
                "/v1/roast-reservations/release",
            }.issubset(route_paths)
        )

    def test_reservation_event_snapshot_preserves_actual_backfire_victim(self):
        create_event(
            EventCreateRequest(
                event_type="backfire",
                attacker_id="owner",
                target_id="target",
                group_id="100",
                date_str=dt.date(2026, 8, 7),
                reservation_id="reservation",
                participant_ids=["owner", "helper"],
                participant_names=["主厨", "帮厨"],
                participant_count=2,
                backfire_victim_id="helper",
                backfire_victim_name="帮厨",
            ),
            self.session,
        )

        item = list_events(dt.date(2026, 8, 7), "100", self.session).items[0]

        self.assertEqual((item.attacker, item.target), ("owner", "target"))
        self.assertEqual((item.backfire_victim_id, item.backfire_victim_name), ("helper", "帮厨"))

    def test_complete_atomically_records_reservation_event_and_is_idempotent(self):
        prepare_reservation(self.session, self._request())
        self.session.commit()
        activate_target_reservations(
            self.session,
            date_str=dt.date(2026, 8, 7),
            target_id="target",
            target_pig_id="pig-target",
        )
        self.session.commit()
        reservation = claim(self._claim_request(), self.session).items[0]
        prepare_outcome(
            RoastReservationOutcomeRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
                outcome_snapshot={"event_type": "escape"},
            ),
            self.session,
        )
        mark_sending(
            RoastReservationMutationRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
            ),
            self.session,
        )
        request = RoastReservationCompleteRequest(
            reservation_id=reservation.reservation_id,
            claim_token=reservation.claim_token,
            event=EventCreateRequest(
                event_type="escape",
                attacker_id="wrong-owner",
                target_id="wrong-target",
                attacker_name="错误主厨",
                target_name="错误目标",
                group_id="999",
                date_str=dt.date(2026, 8, 8),
                reservation_id="wrong-reservation",
                participant_ids=["intruder"],
                participant_names=["闯入者"],
                participant_count=99,
            ),
        )

        first = complete(request, self.session)
        repeated = complete(request, self.session)
        events = self.session.scalars(
            select(RoastEvent).where(RoastEvent.reservation_id == reservation.reservation_id)
        ).all()

        self.assertTrue(first.ok and first.event_recorded)
        self.assertTrue(repeated.ok and repeated.event_recorded)
        self.assertEqual(len(events), 1)
        self.assertEqual(
            (
                events[0].reservation_id,
                events[0].date_str,
                events[0].group_id,
                events[0].attacker_id,
                events[0].attacker_name,
                events[0].target_id,
                events[0].target_name,
            ),
            (
                reservation.reservation_id,
                dt.date(2026, 8, 7),
                "100",
                "a",
                "A",
                "target",
                "Target",
            ),
        )
        self.assertEqual(
            events[0].participant_snapshot,
            {
                "ids": ["a"],
                "names": ["A"],
                "count": 1,
                "backfire_victim_id": "",
                "backfire_victim_name": "",
            },
        )

    def test_event_without_date_uses_rollpig_business_date(self):
        with patch("rollpig_cloud.services.events.rollpig_today", return_value=dt.date(2026, 8, 9)):
            create_event(
                EventCreateRequest(
                    event_type="success",
                    attacker_id="a",
                    target_id="b",
                    group_id="100",
                ),
                self.session,
            )

        events = self.session.scalars(select(RoastEvent)).all()
        self.assertEqual([event.date_str for event in events], [dt.date(2026, 8, 9)])


if __name__ == "__main__":
    unittest.main()
