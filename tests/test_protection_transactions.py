from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event
from unittest.mock import patch

os.environ.setdefault("ROLLPIG_CLOUD_DATABASE_URL", "sqlite+pysqlite:///:memory:")

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session

from rollpig_cloud.db import Base
from rollpig_cloud.models import DailyRoll, GroupProtection, RoastReservation, RoastReservationParticipant
from rollpig_cloud.routers import protections, roast_reservations
from rollpig_cloud.schemas import (
    ReplaceGroupProtectionsRequest, RoastReservationBindMessageRequest, RoastReservationClaimRequest,
    RoastReservationOutcomeRequest, RoastReservationPrepareRequest, RoastReservationReplyJoinRequest,
)
from rollpig_cloud.services.reservations import activate_target_reservations


DATE = dt.date(2026, 8, 7)
MYSQL_URL = os.environ.get("ROLLPIG_TEST_MYSQL_URL", "")


class SQLiteProtectionTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.engine = create_engine(f"sqlite+pysqlite:///{Path(self.temporary.name) / 'cloud.sqlite3'}")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)

    def parallel_replace(self):
        """同步旧实现在读取空名单后的交错，写锁实现则由 SQLite 自行串行执行。"""
        barrier = Barrier(2)

        def replace(_):
            with Session(self.engine, autoflush=False, expire_on_commit=False) as session:
                has_written = False

                def coordinate_reads(execution):
                    nonlocal has_written
                    if execution.is_update:
                        has_written = True
                    elif (execution.is_select and not has_written and execution.bind_mapper is not None
                          and execution.bind_mapper.class_ is GroupProtection
                          and execution.statement._for_update_arg is None):
                        barrier.wait(timeout=5)

                event.listen(session, "do_orm_execute", coordinate_reads)
                return protections.replace_group(ReplaceGroupProtectionsRequest(
                    group_id="100", protect_date=DATE, user_ids=["target"], initial_level=3,
                ), session, object())

        with patch.object(protections, "record_key_mutation_outcome"):
            with ThreadPoolExecutor(max_workers=2) as pool:
                self.assertEqual(list(pool.map(replace, range(2))), [{"ok": True}, {"ok": True}])

    def test_identical_concurrent_replacements_on_empty_roster_succeed(self):
        self.parallel_replace()
        with Session(self.engine) as session:
            rows = session.scalars(select(GroupProtection)).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual((rows[0].base_level, rows[0].consecutive_days), (3, 1))

    def test_concurrent_replacements_preserve_existing_level_and_breakthroughs(self):
        with Session(self.engine) as session:
            session.add(GroupProtection(protect_date=DATE, group_id="100", user_id="target",
                                        base_level=4, consecutive_days=2, roast_success_count=3))
            session.commit()
        self.parallel_replace()
        with Session(self.engine) as session:
            row = session.scalar(select(GroupProtection))
            self.assertEqual((row.base_level, row.consecutive_days, row.roast_success_count), (4, 2, 3))

    def test_stale_sqlite_read_transaction_retries_whole_replacement(self):
        with self.engine.begin() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        with Session(self.engine) as session:
            session.add(GroupProtection(protect_date=DATE, group_id="100", user_id="target", base_level=4))
            session.commit()
        with Session(self.engine, autoflush=False, expire_on_commit=False) as reader:
            reader.connection().exec_driver_sql("BEGIN")
            cached = reader.scalar(select(GroupProtection))
            with Session(self.engine) as writer:
                row = writer.scalar(select(GroupProtection))
                row.roast_success_count = 3
                writer.commit()
            with patch.object(protections, "record_key_mutation_outcome"):
                result = protections.replace_group(ReplaceGroupProtectionsRequest(
                    group_id="100", protect_date=DATE, user_ids=["target", "new"], initial_level=3,
                ), reader, object())
            self.assertEqual(result, {"ok": True})
            self.assertEqual((cached.base_level, cached.roast_success_count), (4, 3))
        with Session(self.engine) as session:
            self.assertEqual(len(session.scalars(select(GroupProtection)).all()), 2)


@unittest.skipUnless(MYSQL_URL, "需用 ROLLPIG_TEST_MYSQL_URL 指定独立 MySQL 测试库")
class MySQLProtectionTransactionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(MYSQL_URL, isolation_level="REPEATABLE READ", pool_size=4)
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.group = uuid.uuid4().hex
        self.bot = f"bot-{self.group}"
        self.target = self.user("target")

    def user(self, label):
        return f"{self.group}-{label}"

    def request(self, label, **changes):
        values = dict(attacker_id=self.user(label), attacker_pig_id="pig", target_id=self.target,
                      group_id=self.group, delivery_bot_id=self.bot, date_str=DATE,
                      supports_protection_gathering=True, protection_gathering_version=2)
        values.update(changes)
        return RoastReservationPrepareRequest(**values)

    def make_round(self, count=2, *, arrived=True, protected=True):
        """建立独立目标与通知，各测试使用唯一群号和用户号隔离数据。"""
        with Session(self.engine, autoflush=False, expire_on_commit=False) as session:
            self.assertEqual(session.scalar(text("SELECT @@transaction_isolation")), "REPEATABLE-READ")
            if protected:
                session.add(GroupProtection(protect_date=DATE, group_id=self.group,
                                            user_id=self.target, base_level=3))
            labels = [*(f"u{index}" for index in range(count)), "late0", "late1", "admin"]
            for label in labels:
                session.add(DailyRoll(date_str=DATE, user_id=self.user(label), pig_id="pig"))
            if arrived:
                session.add(DailyRoll(date_str=DATE, user_id=self.target, pig_id="pig"))
            session.commit()
            for index in range(count):
                created = roast_reservations.prepare(self.request(f"u{index}"), session)
            self.reservation_id = created.reservation.reservation_id
            roast_reservations.bind_message(RoastReservationBindMessageRequest(
                reservation_id=self.reservation_id, bot_id=self.bot, group_id=self.group,
                message_id="42", date_str=DATE,
            ), session)

    def reply(self, session, label):
        return roast_reservations.join_by_message(RoastReservationReplyJoinRequest(
            bot_id=self.bot, group_id=self.group, message_id="42", date_str=DATE,
            attacker_id=self.user(label), protection_gathering_version=2,
        ), session)

    def run_parallel_replies(self):
        """两次回复均建立通知快照后，再竞争实际保护行锁。"""
        barrier = Barrier(2)

        def run(label):
            with Session(self.engine, autoflush=False, expire_on_commit=False) as session:
                synchronized = False

                def before_lock(execution):
                    nonlocal synchronized
                    if (not synchronized and execution.is_select and execution.bind_mapper is not None
                            and execution.bind_mapper.class_ is GroupProtection
                            and execution.statement._for_update_arg is not None):
                        synchronized = True
                        barrier.wait(timeout=10)

                event.listen(session, "do_orm_execute", before_lock)
                return self.reply(session, label)

        with patch.object(roast_reservations, "rollpig_today", return_value=DATE):
            with ThreadPoolExecutor(max_workers=2) as pool:
                return list(pool.map(run, ["late0", "late1"]))

    def members(self, session):
        return session.scalars(select(RoastReservationParticipant).where(
            RoastReservationParticipant.reservation_id == self.reservation_id,
        )).all()

    def claim(self, session):
        return roast_reservations.claim(RoastReservationClaimRequest(
            delivery_bot_id=self.bot, date_str=DATE, supports_prepared=True,
            supports_protection_gathering=True, protection_gathering_version=2,
        ), session)

    def test_parallel_replies_activate_four_points_and_return_full_roster(self):
        self.make_round()
        results = self.run_parallel_replies()
        self.assertEqual([result.status for result in results], ["reservation_joined"] * 2)
        self.assertEqual(sorted(len(result.reservation.participants) for result in results), [3, 4])
        with Session(self.engine) as session:
            row = session.scalar(select(RoastReservation).where(RoastReservation.reservation_id == self.reservation_id))
            self.assertEqual(row.status, "ready")
            self.assertEqual(sum(member.contribution for member in self.members(session)), 4)
            self.assertEqual([item.reservation_id for item in self.claim(session).items], [self.reservation_id])

    def test_parallel_replies_do_not_exceed_twelve_members(self):
        self.make_round(11, arrived=False)
        results = self.run_parallel_replies()
        self.assertEqual(sorted(result.status for result in results), ["reservation_full", "reservation_joined"])
        with Session(self.engine) as session:
            self.assertEqual(len(self.members(session)), 12)

    def run_paused_request(self, request, mapper, concurrent):
        """让请求在锁前建立快照，另一个真实事务提交后再继续。"""
        waiting, resume = Event(), Event()

        def run():
            with Session(self.engine, autoflush=False, expire_on_commit=False) as session:
                paused = False

                def before_lock(execution):
                    nonlocal paused
                    if (not paused and execution.is_select and execution.bind_mapper is not None
                            and execution.bind_mapper.class_ is mapper
                            and execution.statement._for_update_arg is not None):
                        paused = True
                        waiting.set()
                        if not resume.wait(timeout=10):
                            raise AssertionError("并发事务未完成")

                event.listen(session, "do_orm_execute", before_lock)
                return request(session)

        with patch.object(roast_reservations, "rollpig_today", return_value=DATE):
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(run)
                try:
                    self.assertTrue(waiting.wait(timeout=10))
                    concurrent()
                finally:
                    resume.set()
                return future.result(timeout=10)

    def test_takeover_keeps_member_committed_after_its_snapshot(self):
        self.make_round(arrived=False, protected=False)

        def join():
            with Session(self.engine, autoflush=False, expire_on_commit=False) as session:
                self.assertEqual(self.reply(session, "late0").status, "reservation_joined")

        result = self.run_paused_request(
            lambda session: roast_reservations.prepare(self.request("admin", force_mode="super"), session),
            RoastReservation, join,
        )
        expected = {self.user(label) for label in ("u0", "u1", "late0")}
        self.assertEqual(set(result.reservation.force_feed_user_ids), expected)
        self.assertEqual({member.user_id for member in result.reservation.participants}, expected)

    def test_reply_uses_contribution_upgraded_after_its_snapshot(self):
        self.make_round()

        def boost():
            with Session(self.engine, autoflush=False, expire_on_commit=False) as session:
                self.assertEqual(roast_reservations.prepare(self.request("u1", force_mode="normal"), session).status,
                                 "reservation_upgraded")

        result = self.run_paused_request(lambda session: self.reply(session, "late0"), GroupProtection, boost)
        self.assertEqual(result.reservation.status, "ready")
        self.assertEqual({member.user_id: member.contribution for member in result.reservation.participants},
                         {self.user("u0"): 1, self.user("u1"): 2, self.user("late0"): 1})
        with Session(self.engine) as session:
            item = self.claim(session).items[0]
            saved = roast_reservations.prepare_outcome(RoastReservationOutcomeRequest(
                reservation_id=item.reservation_id, claim_token=item.claim_token,
                outcome_snapshot={"event_type": "success"}, settle_daily_feed=True,
            ), session)
            self.assertEqual({feed.user_id for feed in saved.reservation.daily_feed_results},
                             {self.user("u0"), self.user("late0")})

    def test_reply_uses_arrival_saved_after_its_snapshot(self):
        self.make_round(3, arrived=False)

        def arrive():
            with Session(self.engine, autoflush=False, expire_on_commit=False) as session:
                session.add(DailyRoll(date_str=DATE, user_id=self.target, pig_id="pig"))
                session.flush()
                activate_target_reservations(session, date_str=DATE, target_id=self.target, target_pig_id="pig")
                session.commit()

        result = self.run_paused_request(lambda session: self.reply(session, "late0"), GroupProtection, arrive)
        self.assertEqual(result.reservation.status, "ready")
        self.assertEqual(result.reservation.target_pig_id, "pig")
