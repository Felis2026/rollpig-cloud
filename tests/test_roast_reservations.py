from __future__ import annotations

import datetime as dt
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

from sqlalchemy import create_engine, event as sqlalchemy_event, inspect, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

os.environ.setdefault("ROLLPIG_CLOUD_DATABASE_URL", "sqlite+pysqlite:///:memory:")

from rollpig_cloud.db import Base, run_transaction_with_sqlite_lock_retry
from rollpig_cloud.main import app
from rollpig_cloud.migrations import ensure_runtime_migrations
from rollpig_cloud.models import (
    Collection,
    DailyRoll,
    GroupProtection,
    RoastEvent,
    RoastReservation,
    RoastReservationParticipant,
    UserDailyFeed,
    UserPigProgress,
    UserUsage,
)
from rollpig_cloud.config import settings
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
from rollpig_cloud.services.events import record_roast_event_with_status
from rollpig_cloud.services.progress import apply_daily_feed
from rollpig_cloud.services.reservations import activate_target_reservations, prepare_reservation


class CloudRoastReservationTests(unittest.TestCase):
    def test_concurrent_reply_join_respects_last_slot_and_duplicate_user(self):
        from rollpig_cloud.routers.roast_reservations import bind_message, join_by_message
        from rollpig_cloud.schemas import RoastReservationBindMessageRequest, RoastReservationReplyJoinRequest

        for same_user in (False, True):
            with self.subTest(same_user=same_user), tempfile.TemporaryDirectory() as directory:
                engine = create_engine(f"sqlite+pysqlite:///{Path(directory) / 'reply.sqlite3'}", connect_args={"check_same_thread": False})
                Base.metadata.create_all(engine)
                scope = dict(bot_id="bot-1", group_id="100", message_id="900", date_str=self._request().date_str)
                with Session(engine) as session:
                    created = prepare_reservation(session, self._request())
                    for index in range(10):
                        prepare_reservation(session, self._request(attacker_id=f"member-{index}"))
                    session.add_all([DailyRoll(user_id=user_id, date_str=scope["date_str"], pig_id="pig") for user_id in ("b", "c")])
                    session.commit()
                    bind_message(RoastReservationBindMessageRequest(reservation_id=created.reservation.reservation_id, **scope), session)
                barrier = Barrier(2)

                def join(user_id):
                    with Session(engine) as session:
                        barrier.wait(timeout=5)
                        return join_by_message(RoastReservationReplyJoinRequest(attacker_id=user_id, **scope), session).status

                with ThreadPoolExecutor(max_workers=2) as executor:
                    statuses = sorted(executor.map(join, ("b", "b" if same_user else "c")))
                self.assertEqual(statuses, sorted(["reservation_joined", "already_joined" if same_user else "reservation_full"]))
                with Session(engine) as session:
                    self.assertEqual(len(session.scalars(select(RoastReservationParticipant)).all()), 12)
                engine.dispose()

    def test_reply_join_persists_and_is_idempotent_without_consuming_charges(self):
        from rollpig_cloud.routers.roast_reservations import bind_message, join_by_message
        from rollpig_cloud.schemas import RoastReservationBindMessageRequest, RoastReservationReplyJoinRequest

        created = prepare_reservation(self.session, self._request())
        self.session.commit()
        scope = dict(bot_id="bot-1", group_id="100", message_id="900", date_str=self._request().date_str)
        bind = RoastReservationBindMessageRequest(reservation_id=created.reservation.reservation_id, **scope)
        self.assertTrue(bind_message(bind, self.session)["ok"])
        self.assertTrue(bind_message(bind, self.session)["ok"])
        self.session.add(DailyRoll(user_id="b", date_str=scope["date_str"], pig_id="pig-b"))
        self.session.add(DailyRoll(user_id="c", date_str=scope["date_str"], pig_id="pig-c"))
        self.session.commit()
        self.session.close()
        self.session = Session(self.engine, expire_on_commit=False)
        request = RoastReservationReplyJoinRequest(attacker_id="b", attacker_name="B", **scope)
        joined = join_by_message(request, self.session)
        self.assertEqual(joined.status, "reservation_joined")
        self.assertEqual(len(joined.reservation.participants), 2)
        self.assertEqual(join_by_message(request, self.session).status, "already_joined")
        self.assertTrue(bind_message(bind.model_copy(update={"message_id": "901"}), self.session)["ok"])
        chained = join_by_message(request.model_copy(update={"message_id": "901", "attacker_id": "c"}), self.session)
        self.assertEqual(len(chained.reservation.participants), 3)
        self.assertEqual(chained.reservation.reservation_id, created.reservation.reservation_id)
        self.assertIsNone(self.session.scalar(select(UserUsage).where(UserUsage.user_id == "b")))

    def test_reply_join_rejects_wrong_scope_expiry_makeup_full_and_closed(self):
        from rollpig_cloud.routers.roast_reservations import bind_message, join_by_message
        from rollpig_cloud.schemas import RoastReservationBindMessageRequest, RoastReservationReplyJoinRequest

        created = prepare_reservation(self.session, self._request())
        self.session.commit()
        scope = dict(bot_id="bot-1", group_id="100", message_id="900", date_str=self._request().date_str)
        bind_message(RoastReservationBindMessageRequest(reservation_id=created.reservation.reservation_id, **scope), self.session)
        request = RoastReservationReplyJoinRequest(attacker_id="b", attacker_name="B", **scope)
        for changes, status in (
            ({"bot_id": "bot-2"}, "message_not_found"),
            ({"group_id": "200"}, "message_not_found"),
            ({"date_str": dt.date(2026, 8, 8)}, "reservation_closed"),
            ({}, "attacker_unrolled"),
            ({"attacker_id": "target"}, "self_target"),
        ):
            self.assertEqual(join_by_message(request.model_copy(update=changes), self.session).status, status)
        self.session.add(DailyRoll(user_id="b", date_str=scope["date_str"], pig_id="pig-b", appearance_snapshot={"is_makeup": True}))
        self.session.commit()
        self.assertEqual(join_by_message(request, self.session).status, "attacker_unrolled")
        roll = self.session.scalar(select(DailyRoll).where(DailyRoll.user_id == "b"))
        roll.appearance_snapshot = None
        for index in range(11):
            prepare_reservation(self.session, self._request(attacker_id=f"user-{index}"))
        self.session.commit()
        self.assertEqual(join_by_message(request, self.session).status, "reservation_full")
        row = self.session.scalar(select(RoastReservation))
        for status in ("ready", "prepared", "sending", "completed"):
            row.status = status
            self.session.commit()
            self.assertEqual(join_by_message(request, self.session).status, "reservation_closed")
        row.status = "pending"
        self.session.add(DailyRoll(user_id="target", date_str=scope["date_str"], pig_id="pig-target"))
        self.session.commit()
        self.assertEqual(join_by_message(request, self.session).status, "reservation_closed")
        self.assertEqual(len(self.session.scalars(select(RoastReservation)).all()), 1)

    def test_makeup_before_or_after_prepare_never_activates_reservation(self):
        from rollpig_cloud.routers.daily_rolls import makeup_daily_roll
        from rollpig_cloud.routers.roast_reservations import prepare
        from rollpig_cloud.schemas import DailyRollGetOrCreateRequest
        from rollpig_cloud.services.reservations import activate_if_target_already_rolled

        yesterday = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).date() - dt.timedelta(days=1)
        for makeup_first in (True, False):
            with self.subTest(makeup_first=makeup_first):
                target = f"target-{makeup_first}"
                request = self._request(attacker_id=f"owner-{makeup_first}", target_id=target, date_str=yesterday)
                makeup_request = DailyRollGetOrCreateRequest(user_id=target, proposed_pig_id="pig", date_str=yesterday)
                if makeup_first:
                    makeup_daily_roll(makeup_request, self.session)
                created = prepare(request, self.session)
                self.assertEqual(created.status, "reservation_created")
                if not makeup_first:
                    makeup_daily_roll(makeup_request, self.session)
                repeated = prepare(request, self.session)
                self.assertEqual(repeated.status, "already_joined")
                joined = prepare(request.model_copy(update={"attacker_id": f"joiner-{makeup_first}"}), self.session)
                self.assertEqual(joined.status, "reservation_joined")
                self.assertEqual(activate_if_target_already_rolled(self.session, date_str=yesterday, target_id=target), 0)
                self.assertEqual(activate_target_reservations(self.session, date_str=yesterday, target_id=target, target_pig_id="pig"), 0)
                self.session.commit()
                self.session.expire_all()
                row = self.session.scalar(select(RoastReservation).where(RoastReservation.target_id == target))
                self.assertEqual(row.status, "pending")
                self.assertIsNone(row.ready_at)
                self.assertFalse(row.target_pig_id)
        claimed = claim(self._claim_request(date_str=yesterday), self.session)
        self.assertFalse(claimed.items)

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

    def _add_daily_pig(self, user_id: str, pig_id: str, *, copies: int = 1) -> None:
        self.session.add_all([
            DailyRoll(
                date_str=dt.date(2026, 8, 7),
                user_id=user_id,
                pig_id=pig_id,
                previous_copies=max(0, copies - 1),
                copies_after_roll=copies,
                previous_expert_level=max(0, copies - 2),
                expert_level_after_roll=max(0, copies - 1),
            ),
            Collection(user_id=user_id, pig_id=pig_id),
            UserPigProgress(
                tenant_id=settings.default_tenant_id,
                user_id=user_id,
                pig_id=pig_id,
                copies=copies,
                growth_bonus=0,
            ),
        ])
        self.session.commit()

    def test_normal_success_feeds_once_across_sources(self):
        self._add_daily_pig("a", "pig-a")

        first = create_event(
            EventCreateRequest(
                event_type="success",
                attacker_id="a",
                target_id="target",
                group_id="100",
                date_str=dt.date(2026, 8, 7),
                settle_daily_feed=True,
                source_id="event-1",
            ),
            self.session,
        )
        second = create_event(
            EventCreateRequest(
                event_type="success",
                attacker_id="a",
                target_id="target-2",
                group_id="100",
                date_str=dt.date(2026, 8, 7),
                settle_daily_feed=True,
                source_id="event-2",
            ),
            self.session,
        )

        progress = self.session.scalar(select(UserPigProgress).where(UserPigProgress.user_id == "a"))
        feeds = self.session.scalars(select(UserDailyFeed).where(UserDailyFeed.user_id == "a")).all()
        self.assertEqual((first.daily_feed_result.status, first.daily_feed_result.new_level), ("fed", 1))
        self.assertEqual(second.daily_feed_result.status, "already_fed")
        self.assertEqual((progress.copies, progress.growth_bonus, len(feeds)), (1, 1, 1))

    def test_event_retries_whole_transaction_after_sqlite_lock(self):
        self._add_daily_pig("a", "pig-a")
        attempts = 0

        def apply_after_one_lock(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OperationalError(
                    "INSERT INTO user_daily_feeds",
                    {},
                    sqlite3.OperationalError("database is locked"),
                )
            return apply_daily_feed(*args, **kwargs)

        with patch(
            "rollpig_cloud.routers.events.apply_daily_feed",
            side_effect=apply_after_one_lock,
        ):
            result = create_event(
                EventCreateRequest(
                    event_type="success",
                    attacker_id="a",
                    target_id="target",
                    group_id="100",
                    date_str=dt.date(2026, 8, 7),
                    settle_daily_feed=True,
                    source_id="event-lock-retry",
                ),
                self.session,
            )

        progress = self.session.scalar(select(UserPigProgress).where(UserPigProgress.user_id == "a"))
        events = self.session.scalars(select(RoastEvent)).all()
        feeds = self.session.scalars(select(UserDailyFeed)).all()
        self.assertEqual((attempts, result.daily_feed_result.status), (2, "fed"))
        self.assertEqual((len(events), len(feeds), progress.growth_bonus), (1, 1, 1))

    def test_old_or_incomplete_event_request_does_not_trigger_feed(self):
        self._add_daily_pig("a", "pig-a")

        legacy = create_event(
            EventCreateRequest(
                event_type="success",
                attacker_id="a",
                target_id="target",
                group_id="100",
                date_str=dt.date(2026, 8, 7),
            ),
            self.session,
        )
        missing_source = create_event(
            EventCreateRequest(
                event_type="success",
                attacker_id="a",
                target_id="target-2",
                group_id="100",
                date_str=dt.date(2026, 8, 7),
                settle_daily_feed=True,
            ),
            self.session,
        )

        progress = self.session.scalar(select(UserPigProgress).where(UserPigProgress.user_id == "a"))
        self.assertIsNone(legacy.daily_feed_result)
        self.assertIsNone(missing_source.daily_feed_result)
        self.assertEqual(progress.growth_bonus, 0)

    def test_max_level_does_not_consume_daily_feed(self):
        self._add_daily_pig("a", "pig-a", copies=6)

        result = create_event(
            EventCreateRequest(
                event_type="success",
                attacker_id="a",
                target_id="target",
                group_id="100",
                date_str=dt.date(2026, 8, 7),
                settle_daily_feed=True,
                source_id="event-1",
            ),
            self.session,
        )

        self.assertEqual(result.daily_feed_result.status, "max_level")
        self.assertIsNone(self.session.scalar(select(UserDailyFeed)))

    def test_successful_reservation_feeds_all_participants_and_reuses_results(self):
        self._add_daily_pig("a", "pig-a")
        self._add_daily_pig("b", "pig-b")
        prepare_reservation(self.session, self._request())
        self.session.commit()
        prepare_reservation(self.session, self._request("b"))
        self.session.commit()
        activate_target_reservations(
            self.session,
            date_str=dt.date(2026, 8, 7),
            target_id="target",
            target_pig_id="pig-target",
        )
        self.session.commit()
        reservation = claim(self._claim_request(), self.session).items[0]
        request = RoastReservationOutcomeRequest(
            reservation_id=reservation.reservation_id,
            claim_token=reservation.claim_token,
            outcome_snapshot={"event_type": "success"},
            settle_daily_feed=True,
        )

        saved = prepare_outcome(request, self.session)
        repeated = prepare_outcome(request, self.session)

        self.assertEqual([item.user_id for item in saved.reservation.daily_feed_results], ["a", "b"])
        self.assertTrue(all(item.status == "fed" for item in saved.reservation.daily_feed_results))
        self.assertEqual(repeated.reservation.daily_feed_results, saved.reservation.daily_feed_results)
        progresses = self.session.scalars(select(UserPigProgress).order_by(UserPigProgress.user_id)).all()
        self.assertEqual([(row.user_id, row.growth_bonus) for row in progresses], [("a", 1), ("b", 1)])

    def test_forced_reservation_never_feeds_even_when_client_requests_it(self):
        self._add_daily_pig("a", "pig-a")
        prepare_reservation(self.session, self._request(force_mode="normal"))
        self.session.commit()
        activate_target_reservations(
            self.session,
            date_str=dt.date(2026, 8, 7),
            target_id="target",
            target_pig_id="pig-target",
        )
        self.session.commit()
        reservation = claim(self._claim_request(), self.session).items[0]

        saved = prepare_outcome(
            RoastReservationOutcomeRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
                outcome_snapshot={"event_type": "success"},
                settle_daily_feed=True,
            ),
            self.session,
        )

        progress = self.session.scalar(select(UserPigProgress).where(UserPigProgress.user_id == "a"))
        self.assertEqual(saved.reservation.daily_feed_results, [])
        self.assertEqual(progress.growth_bonus, 0)

    def test_blank_force_mode_keeps_ordinary_reservation_feed_semantics(self):
        self._add_daily_pig("a", "pig-a")
        prepare_reservation(self.session, self._request(force_mode=""))
        self.session.commit()
        activate_target_reservations(
            self.session,
            date_str=dt.date(2026, 8, 7),
            target_id="target",
            target_pig_id="pig-target",
        )
        self.session.commit()
        reservation = claim(self._claim_request(), self.session).items[0]

        saved = prepare_outcome(
            RoastReservationOutcomeRequest(
                reservation_id=reservation.reservation_id,
                claim_token=reservation.claim_token,
                outcome_snapshot={"event_type": "success"},
                settle_daily_feed=True,
            ),
            self.session,
        )

        progress = self.session.scalar(select(UserPigProgress).where(UserPigProgress.user_id == "a"))
        usage = self.session.scalar(select(UserUsage).where(UserUsage.user_id == "a"))
        self.assertEqual([item.status for item in saved.reservation.daily_feed_results], ["fed"])
        self.assertEqual((usage.roast_charges, progress.growth_bonus), (1, 1))

    def test_reservation_outcome_retries_whole_transaction_after_sqlite_lock(self):
        self._add_daily_pig("a", "pig-a")
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
        request = RoastReservationOutcomeRequest(
            reservation_id=reservation.reservation_id,
            claim_token=reservation.claim_token,
            outcome_snapshot={"event_type": "success"},
            settle_daily_feed=True,
        )
        attempts = 0

        def apply_after_one_lock(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OperationalError(
                    "INSERT INTO user_daily_feeds",
                    {},
                    sqlite3.OperationalError("database is locked"),
                )
            return apply_daily_feed(*args, **kwargs)

        with patch(
            "rollpig_cloud.routers.roast_reservations.apply_daily_feed",
            side_effect=apply_after_one_lock,
        ):
            saved = prepare_outcome(request, self.session)

        progress = self.session.scalar(select(UserPigProgress).where(UserPigProgress.user_id == "a"))
        events = self.session.scalars(select(UserDailyFeed)).all()
        self.assertEqual((attempts, len(saved.reservation.daily_feed_results)), (2, 1))
        self.assertEqual((len(events), progress.growth_bonus), (1, 1))

    def test_daily_feed_is_unique_under_concurrent_sqlite_writers(self):
        self.session.close()
        self.engine.dispose()
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "daily-feed.sqlite3"
            engine = create_engine(
                f"sqlite+pysqlite:///{database_path.as_posix()}",
                future=True,
                connect_args={"check_same_thread": False, "timeout": 10},
            )
            Base.metadata.create_all(engine)
            with Session(engine) as session:
                session.add_all([
                    DailyRoll(
                        date_str=dt.date(2026, 8, 7),
                        user_id="a",
                        pig_id="pig-a",
                        previous_copies=0,
                        copies_after_roll=1,
                    ),
                    UserPigProgress(
                        tenant_id=settings.default_tenant_id,
                        user_id="a",
                        pig_id="pig-a",
                        copies=1,
                        growth_bonus=0,
                    ),
                ])
                session.commit()

            barrier = Barrier(2)

            def synchronize_after_progress_read(conn, _cursor, statement, _parameters, _context, _executemany):
                # 强制两个事务都持有旧读取快照后再写入，稳定复现 SQLite 锁升级冲突。
                if (
                    statement.lstrip().upper().startswith("SELECT")
                    and "user_pig_progress" in statement.casefold()
                    and not conn.info.get("daily_feed_progress_read")
                ):
                    conn.info["daily_feed_progress_read"] = True
                    barrier.wait(timeout=5)

            sqlalchemy_event.listen(engine, "after_cursor_execute", synchronize_after_progress_read)

            def settle(source_id: str) -> str:
                with Session(engine, expire_on_commit=False) as session:
                    def settle_once() -> str:
                        result = apply_daily_feed(
                            session,
                            date_str=dt.date(2026, 8, 7),
                            user_id="a",
                            source_type="roast",
                            source_id=source_id,
                        )
                        session.commit()
                        return result.status

                    return run_transaction_with_sqlite_lock_retry(session, settle_once)

            with ThreadPoolExecutor(max_workers=2) as executor:
                statuses = sorted(executor.map(settle, ("event-1", "event-2")))

            with Session(engine) as session:
                progress = session.scalar(select(UserPigProgress).where(UserPigProgress.user_id == "a"))
                feed_count = len(session.scalars(select(UserDailyFeed)).all())
            engine.dispose()

        self.assertEqual(statuses, ["already_fed", "fed"])
        self.assertEqual((progress.growth_bonus, feed_count), (1, 1))

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
                columns = {
                    column["name"]
                    for column in inspect(engine).get_columns("roast_reservations")
                }
                self.assertIn("daily_feed_results", columns)
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
        self._add_daily_pig("a", "pig-a")
        with (
            patch("rollpig_cloud.routers.events.rollpig_today", return_value=dt.date(2026, 8, 7)),
            patch(
                "rollpig_cloud.services.events.rollpig_today",
                side_effect=AssertionError("事件服务不应再次解析业务日期"),
            ),
        ):
            result = create_event(
                EventCreateRequest(
                    event_type="success",
                    attacker_id="a",
                    target_id="b",
                    group_id="100",
                    settle_daily_feed=True,
                    source_id="event-business-date",
                ),
                self.session,
            )

        events = self.session.scalars(select(RoastEvent)).all()
        feeds = self.session.scalars(select(UserDailyFeed)).all()
        self.assertEqual([event.date_str for event in events], [dt.date(2026, 8, 7)])
        self.assertEqual([feed.date_str for feed in feeds], [dt.date(2026, 8, 7)])
        self.assertEqual(result.daily_feed_result.status, "fed")

    def test_event_status_distinguishes_creation_from_idempotent_retry(self):
        request = EventCreateRequest(
            event_type="escape",
            attacker_id="a",
            target_id="target",
            reservation_id="reservation-idempotent",
            date_str=dt.date(2026, 8, 9),
        )

        first = record_roast_event_with_status(self.session, request)
        self.session.commit()
        second = record_roast_event_with_status(self.session, request)

        self.assertEqual(first, (True, True))
        self.assertEqual(second, (True, False))


if __name__ == "__main__":
    unittest.main()
