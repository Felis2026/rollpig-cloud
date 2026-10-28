from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ROLLPIG_CLOUD_DATABASE_URL", "sqlite+pysqlite:///:memory:")

from sqlalchemy import create_engine, event, inspect, select, text, update
from sqlalchemy.orm import Session
from sqlalchemy.schema import CreateTable

from rollpig_cloud.db import Base
from rollpig_cloud.migrations import ensure_runtime_migrations
from rollpig_cloud.models import DailyRoll, GroupProtection, RoastReservation, RoastReservationParticipant
from rollpig_cloud.routers.roast_reservations import claim, complete, mark_sending, prepare, prepare_outcome
from rollpig_cloud.schemas import (EventCreateRequest, RoastReservationClaimRequest, RoastReservationCompleteRequest,
                                   RoastReservationMutationRequest, RoastReservationOutcomeRequest,
                                   RoastReservationPrepareRequest)
from rollpig_cloud.services.reservations import activate_target_reservations


DATE = dt.date(2026, 8, 7)


class CloudProtectionGatheringTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.session = Session(self.engine, expire_on_commit=False)
        self.addCleanup(self.session.close)
        self.session.add(GroupProtection(protect_date=DATE, group_id="100", user_id="target", base_level=3))
        self.session.commit()

    def request(self, user="a", **changes):
        """构造声明保护合烤能力的客户端请求。"""

        values = dict(attacker_id=user, attacker_name=user, attacker_pig_id="pig", target_id="target",
                      target_name="目标", group_id="100", delivery_bot_id="bot", date_str=DATE,
                      supports_protection_gathering=True, protection_gathering_version=2, now_ts=1_786_032_000)
        values.update(changes)
        return RoastReservationPrepareRequest(**values)

    def arrive(self):
        """在事务中登记今日抽取并激活满足人数条件的场次。"""

        self.session.add(DailyRoll(date_str=DATE, user_id="target", pig_id="pig"))
        self.session.flush()
        activated = activate_target_reservations(self.session, date_str=DATE, target_id="target", target_pig_id="pig")
        self.session.commit()
        return activated

    def finish(self, event_type="success"):
        """走完整的固定结果与发送完成协议，并验证重试不会重复升级。"""

        claimed = claim(RoastReservationClaimRequest(delivery_bot_id="bot", date_str=DATE,
                                                     supports_prepared=True, supports_protection_gathering=True, protection_gathering_version=2), self.session)
        self.assertEqual(len(claimed.items), 1)
        item = claimed.items[0]
        outcome = RoastReservationOutcomeRequest(reservation_id=item.reservation_id, claim_token=item.claim_token,
                                                  outcome_snapshot={"event_type": event_type})
        first = prepare_outcome(outcome, self.session)
        self.assertTrue(first.ok)
        self.assertTrue(prepare_outcome(outcome, self.session).ok)
        mutation = RoastReservationMutationRequest(reservation_id=item.reservation_id, claim_token=item.claim_token)
        self.assertTrue(mark_sending(mutation, self.session).ok)
        self.assertTrue(complete(RoastReservationCompleteRequest(**mutation.model_dump()), self.session).ok)
        self.assertTrue(complete(RoastReservationCompleteRequest(**mutation.model_dump()), self.session).ok)
        self.session.expire_all()

    def test_accelerated_upgrade_duplicate_and_second_accelerated_activate(self):
        self.arrive()
        created = prepare(self.request(), self.session)
        self.assertEqual(created.reservation.status, "pending")
        upgraded = prepare(self.request(force_mode="normal"), self.session)
        self.assertEqual(upgraded.status, "reservation_upgraded")
        self.assertEqual(upgraded.reservation.participants[0].contribution, 2)
        self.assertEqual(prepare(self.request(force_mode="normal"), self.session).status, "already_joined")
        joined = prepare(self.request("b", force_mode="normal"), self.session)
        self.assertEqual(joined.reservation.status, "ready")
        self.assertEqual(len(joined.reservation.participants), 2)
        self.assertEqual(prepare(self.request("c"), self.session).status, "reservation_busy")

    def test_level_one_acceleration_can_open_alone_and_v1_cannot_claim(self):
        self.session.scalar(select(GroupProtection)).base_level = 1
        self.session.commit()
        self.arrive()
        result = prepare(self.request(force_mode="normal"), self.session)
        self.assertEqual(result.reservation.protection_required, 2)
        self.assertEqual(result.reservation.status, "ready")
        self.assertEqual(len(result.reservation.participants), 1)
        old = claim(RoastReservationClaimRequest(delivery_bot_id="bot", date_str=DATE,
                    supports_prepared=True, supports_protection_gathering=True), self.session)
        self.assertFalse(old.items)

    def test_super_takeover_at_capacity_keeps_feed_and_records_real_operator(self):
        for index in range(12):
            prepare(self.request(f"u{index}", force_mode="normal" if index == 11 else None), self.session)
            self.session.add(DailyRoll(date_str=DATE, user_id=f"u{index}", pig_id="pig"))
        self.session.commit()
        result = prepare(self.request("admin", force_mode="super"), self.session)
        self.assertEqual(result.reservation.owner_id, "u0")
        self.assertEqual(result.reservation.force_operator_id, "admin")
        self.assertEqual(len(result.reservation.participants), 12)
        self.assertEqual(len(result.reservation.force_feed_user_ids), 11)
        self.assertEqual(prepare(self.request("late"), self.session).status, "reservation_closed")
        self.assertEqual(prepare(self.request("admin", force_mode="super"), self.session).status, "already_joined")
        self.arrive()
        item = claim(RoastReservationClaimRequest(delivery_bot_id="bot", date_str=DATE,
                     supports_prepared=True, supports_protection_gathering=True, protection_gathering_version=2), self.session).items[0]
        saved = prepare_outcome(RoastReservationOutcomeRequest(reservation_id=item.reservation_id,
                    claim_token=item.claim_token, outcome_snapshot={"event_type": "success"}, settle_daily_feed=True), self.session)
        self.assertEqual({feed.user_id for feed in saved.reservation.daily_feed_results}, {f"u{index}" for index in range(11)})
        from rollpig_cloud.services.events import bind_reservation_event
        event = bind_reservation_event(saved.reservation, EventCreateRequest(event_type="success", attacker_id="wrong", target_id="wrong"))
        self.assertEqual((event.attacker_id, event.reservation_owner_id), ("admin", "u0"))

    def test_continuous_daily_coupons_and_repeated_writes_keep_levels(self):
        from rollpig_cloud.routers.protections import replace_group
        from rollpig_cloud.schemas import ReplaceGroupProtectionsRequest
        with patch("rollpig_cloud.routers.protections.record_key_mutation_outcome"):
            for offset, level in enumerate((3, 4, 5, 5)):
                date = DATE + dt.timedelta(days=offset)
                req = ReplaceGroupProtectionsRequest(group_id="100", protect_date=date, user_ids=["new"], initial_level=3)
                replace_group(req, self.session, object())
                replace_group(req, self.session, object())
                row = self.session.scalar(select(GroupProtection).where(GroupProtection.user_id == "new", GroupProtection.protect_date == date))
                self.assertEqual((row.base_level, row.consecutive_days), (level, offset + 1))
                row.roast_success_count = 4
                self.session.commit()
            date = DATE + dt.timedelta(days=5)
            replace_group(ReplaceGroupProtectionsRequest(group_id="100", protect_date=date, user_ids=["new"], initial_level=3), self.session, object())
            row = self.session.scalar(select(GroupProtection).where(GroupProtection.user_id == "new", GroupProtection.protect_date == date))
            self.assertEqual((row.base_level, row.consecutive_days), (3, 1))

    def test_executor_already_in_roster_keeps_only_others_feed(self):
        prepare(self.request("admin"), self.session)
        prepare(self.request("b"), self.session)
        result = prepare(self.request("admin", force_mode="super"), self.session)
        self.assertEqual(result.reservation.force_feed_user_ids, ["b"])
        self.assertEqual(len(result.reservation.participants), 2)

    def test_draw_after_points_activates_and_draw_before_points_waits(self):
        for user in "abc":
            prepare(self.request(user), self.session)
        self.assertEqual(self.arrive(), 0)
        joined = prepare(self.request("d"), self.session)
        self.assertEqual(joined.reservation.status, "ready")
        self.assertEqual(joined.reservation.target_pig_id, "pig")

    def test_levels_cap_and_history_survives_new_rounds(self):
        self.arrive()
        for index, required in enumerate((4, 5, 6, 6, 6, 6)):
            created = prepare(self.request(f"r{index}-0"), self.session)
            self.assertEqual(created.reservation.protection_required, required)
            for number in range(1, required):
                prepare(self.request(f"r{index}-{number}"), self.session)
            self.finish()
        self.assertEqual(len(self.session.scalars(select(RoastReservation)).all()), 6)
        self.assertEqual(self.session.scalar(select(GroupProtection)).roast_success_count, 6)

    def test_escape_and_backfire_do_not_increase_level(self):
        self.arrive()
        for index, event_type in enumerate(("escape", "backfire")):
            for number in range(4):
                prepare(self.request(f"r{index}-{number}"), self.session)
            self.finish(event_type)
        self.assertEqual(prepare(self.request("next"), self.session).reservation.protection_required, 4)

    def test_super_force_bypasses_points_and_old_claim_cannot_take_protected_round(self):
        self.arrive()
        created = prepare(self.request(force_mode="super"), self.session)
        self.assertEqual(created.reservation.status, "ready")
        legacy_claim = claim(RoastReservationClaimRequest(delivery_bot_id="bot", date_str=DATE), self.session)
        self.assertFalse(legacy_claim.items)
        self.finish()

    def test_old_accelerated_client_cannot_bypass_protection(self):
        self.arrive()
        result = prepare(self.request(force_mode="normal", supports_protection_gathering=False), self.session)
        self.assertEqual(result.status, "protected")
        self.assertFalse(self.session.scalars(select(RoastReservation)).all())

    def test_mixed_gathering_feed_excludes_only_accelerated_participant(self):
        for accelerated_owner in (False, True):
            target = f"target-{accelerated_owner}"
            self.session.add(GroupProtection(protect_date=DATE, group_id="100", user_id=target, base_level=3))
            self.session.add(DailyRoll(date_str=DATE, user_id=target, pig_id="pig"))
            self.session.commit()
            users = [f"{target}-{index}" for index in range(3)]
            for index, user in enumerate(users):
                mode = "normal" if index == (0 if accelerated_owner else 1) else None
                prepare(self.request(user, target_id=target, force_mode=mode), self.session)
            item = claim(RoastReservationClaimRequest(delivery_bot_id="bot", date_str=DATE,
                         supports_prepared=True, supports_protection_gathering=True, protection_gathering_version=2), self.session).items[0]
            saved = prepare_outcome(RoastReservationOutcomeRequest(reservation_id=item.reservation_id,
                    claim_token=item.claim_token, outcome_snapshot={"event_type": "success"}, settle_daily_feed=True), self.session)
            self.assertEqual({result.user_id for result in saved.reservation.daily_feed_results},
                             set(users) - {users[0 if accelerated_owner else 1]})

    def test_legacy_global_protection_gets_group_scoped_level(self):
        self.session.add(GroupProtection(protect_date=DATE, group_id="__all__", user_id="legacy"))
        self.session.commit()
        result = prepare(self.request(target_id="legacy"), self.session)
        self.assertEqual(result.reservation.protection_required, 2)
        from rollpig_cloud.routers.protections import check_protected
        self.assertTrue(check_protected(DATE, "100", "legacy", self.session).protected)

    def test_group_and_date_levels_are_isolated(self):
        protection = self.session.scalar(select(GroupProtection))
        protection.roast_success_count = 4
        self.session.add(GroupProtection(protect_date=DATE, group_id="200", user_id="target"))
        self.session.add(GroupProtection(protect_date=DATE + dt.timedelta(days=1), group_id="100", user_id="target"))
        self.session.commit()
        self.assertEqual(prepare(self.request(), self.session).reservation.protection_required, 6)
        self.assertEqual(prepare(self.request("b", group_id="200"), self.session).reservation.protection_required, 2)
        self.assertEqual(prepare(self.request("c", date_str=DATE + dt.timedelta(days=1)), self.session).reservation.protection_required, 2)

    def test_replace_protection_preserves_level(self):
        from rollpig_cloud.routers.protections import replace_group
        from rollpig_cloud.schemas import ReplaceGroupProtectionsRequest

        self.session.scalar(select(GroupProtection)).roast_success_count = 3
        self.session.commit()
        with patch("rollpig_cloud.routers.protections.record_key_mutation_outcome"):
            replace_group(ReplaceGroupProtectionsRequest(protect_date=DATE, group_id="100", user_ids=["target"]),
                          self.session, object())
        self.assertEqual(self.session.scalar(select(GroupProtection)).roast_success_count, 3)

    def test_reply_after_target_draw_can_finish_gathering(self):
        from rollpig_cloud.routers.roast_reservations import bind_message, join_by_message
        from rollpig_cloud.schemas import RoastReservationBindMessageRequest, RoastReservationReplyJoinRequest

        self.arrive()
        created = prepare(self.request(), self.session)
        bind_message(RoastReservationBindMessageRequest(reservation_id=created.reservation.reservation_id,
                     bot_id="bot", group_id="100", message_id="42", date_str=DATE), self.session)
        with patch("rollpig_cloud.routers.roast_reservations.rollpig_today", return_value=DATE):
            for user in "bcd":
                self.session.add(DailyRoll(date_str=DATE, user_id=user, pig_id="pig"))
                self.session.commit()
                result = join_by_message(RoastReservationReplyJoinRequest(bot_id="bot", group_id="100", message_id="42",
                                           date_str=DATE, attacker_id=user, attacker_name=user, protection_gathering_version=2), self.session)
        self.assertEqual(result.reservation.status, "ready")

    def test_reply_rechecks_cached_pending_status_under_mutation_lock(self):
        """回复预读后其他请求已冻结并领取，加入事务必须重新读取场次状态。"""
        from rollpig_cloud.routers import roast_reservations as router
        from rollpig_cloud.schemas import RoastReservationBindMessageRequest, RoastReservationReplyJoinRequest
        from rollpig_cloud.services.reservations import prepare_reservation

        self.arrive()
        for user in "abc":
            created = prepare(self.request(user), self.session)
        reservation_id = created.reservation.reservation_id
        router.bind_message(RoastReservationBindMessageRequest(
            reservation_id=reservation_id, bot_id="bot", group_id="100", message_id="42", date_str=DATE,
        ), self.session)
        self.session.add(DailyRoll(date_str=DATE, user_id="late", pig_id="pig"))
        self.session.commit()

        def freeze_before_mutation(session, request, **kwargs):
            """在回复预读之后模拟另一请求提交第四人并领取场次。"""
            session.connection().execute(RoastReservationParticipant.__table__.insert().values(
                reservation_id=reservation_id, user_id="d", pig_id="pig", contribution=1,
            ))
            session.connection().execute(update(RoastReservation).where(
                RoastReservation.reservation_id == reservation_id,
            ).values(status="processing", claim_token="other-claim"))
            return prepare_reservation(session, request, **kwargs)

        def preserve_mysql_identity_map(execution):
            # SQLite 写锁辅助 UPDATE 会刷新对象；关闭该同步以复现 MySQL 的旧对象读取。
            if execution.is_update:
                execution.update_execution_options(synchronize_session=False)

        event.listen(self.session, "do_orm_execute", preserve_mysql_identity_map)
        try:
            with (patch.object(router, "rollpig_today", return_value=DATE),
                  patch.object(router, "prepare_reservation", side_effect=freeze_before_mutation)):
                response = router.join_by_message(RoastReservationReplyJoinRequest(
                    bot_id="bot", group_id="100", message_id="42", date_str=DATE,
                    attacker_id="late", protection_gathering_version=2,
                ), self.session)
        finally:
            event.remove(self.session, "do_orm_execute", preserve_mysql_identity_map)
        self.assertEqual(response.status, "reservation_closed")
        self.session.expire_all()
        row = self.session.scalar(select(RoastReservation))
        self.assertEqual((row.status, row.claim_token), ("processing", "other-claim"))
        members = self.session.scalars(select(RoastReservationParticipant.user_id)).all()
        self.assertEqual(set(members), set("abcd"))

    def test_legacy_takeover_keeps_ordinary_reservation_deliverable(self):
        """未声明接管能力的请求不得改变普通预约，旧 Owner 仍能领取。"""
        self.session.delete(self.session.scalar(select(GroupProtection)))
        self.session.commit()
        created = prepare(self.request(delivery_bot_id="old-bot", supports_protection_gathering=False,
                                       protection_gathering_version=0), self.session)
        result = prepare(self.request("admin", delivery_bot_id="old-bot", force_mode="super",
                                      supports_protection_gathering=False, protection_gathering_version=0), self.session)
        self.assertEqual(result.status, "reservation_closed")
        self.assertEqual(result.reservation.reservation_id, created.reservation.reservation_id)
        self.assertEqual(result.reservation.force_operator_id, "")
        self.assertIsNone(result.reservation.force_mode)
        self.arrive()
        claimed = claim(RoastReservationClaimRequest(delivery_bot_id="old-bot", date_str=DATE), self.session)
        self.assertEqual(len(claimed.items), 1)

    def test_capable_takeover_moves_delivery_to_requesting_bot(self):
        """新 Bot 接管旧 Owner 的场次后，投递归新 Bot，原队伍和加餐资格保留。"""
        self.session.delete(self.session.scalar(select(GroupProtection)))
        self.session.commit()
        prepare(self.request(delivery_bot_id="old-bot", supports_protection_gathering=False,
                             protection_gathering_version=0), self.session)
        prepare(self.request("b", delivery_bot_id="old-bot"), self.session)
        result = prepare(self.request("admin", delivery_bot_id="new-bot", force_mode="super"), self.session)
        self.assertEqual(result.reservation.delivery_bot_id, "new-bot")
        self.assertEqual(result.reservation.owner_id, "a")
        self.assertEqual(result.reservation.force_operator_id, "admin")
        self.assertEqual(result.reservation.force_feed_user_ids, ["a", "b"])
        self.assertEqual([item.user_id for item in result.reservation.participants], ["a", "b"])
        self.arrive()
        old = claim(RoastReservationClaimRequest(delivery_bot_id="old-bot", date_str=DATE), self.session)
        self.assertFalse(old.items)
        new = claim(RoastReservationClaimRequest(delivery_bot_id="new-bot", date_str=DATE,
                    supports_prepared=True, supports_protection_gathering=True, protection_gathering_version=2), self.session)
        self.assertEqual(len(new.items), 1)

    def test_multiple_victim_event_round_trip(self):
        from rollpig_cloud.routers.events import list_events
        from rollpig_cloud.models import RoastEvent
        from rollpig_cloud.services.events import record_roast_event

        prepare(self.request(), self.session)
        prepare(self.request("b"), self.session)
        row = self.session.scalar(select(RoastReservation))
        request = EventCreateRequest(event_type="backfire", reservation_id=row.reservation_id,
                                     attacker_id="a", target_id="target", date_str=DATE,
                                     backfire_victim_id="a", backfire_victim_name="甲",
                                     backfire_victim_ids=["a", "b"], backfire_victim_names=["甲", "乙"])
        record_roast_event(self.session, request)
        self.session.commit()
        self.assertEqual(self.session.scalar(select(RoastEvent)).participant_snapshot["backfire_victim_ids"], ["a", "b"])
        for user in ("a", "b"):
            events = list_events(DATE, session=self.session, user_id=user)
            self.assertEqual(events.items[0].backfire_victim_ids, ["a", "b"])

    def test_catalog_counts_non_operator_victims_and_each_special_kind_once(self):
        from rollpig_cloud.models import RoastEvent
        from rollpig_cloud.routers.catalog import get_catalog_snapshot
        self.session.add(RoastEvent(date_str=DATE, group_id="100", event_type="backfire",
                                   attacker_id="a", target_id="target",
                                   participant_snapshot={"backfire_victim_ids": ["a", "other"]}))
        for _ in range(3):
            self.session.add(RoastEvent(date_str=DATE, group_id="100", event_type="bot_backfire",
                                       attacker_id="other", target_id="bot"))
            self.session.add(RoastEvent(date_str=DATE, group_id="100", event_type="backfire",
                                       attacker_id="other", target_id="target", participant_snapshot={"special_reason": "unrolled"}))
        self.session.commit()
        with patch("rollpig_cloud.routers.catalog.rollpig_today", return_value=DATE):
            self.assertEqual(get_catalog_snapshot("other", session=self.session).roasted_7d, 3)

    def test_catalog_counts_legacy_single_victim_without_plural_snapshot(self):
        """旧单人受害者字段独立参与查询，空多人字段仍按单人记录计数。"""
        from rollpig_cloud.models import RoastEvent
        from rollpig_cloud.routers.catalog import get_catalog_snapshot

        for extra in ({}, {"backfire_victim_ids": []}):
            self.session.add(RoastEvent(
                date_str=DATE, group_id="100", event_type="backfire",
                attacker_id="owner", target_id="target", reservation_id="legacy",
                participant_snapshot={"ids": ["owner", "victim"],
                                      "backfire_victim_id": "victim", **extra},
            ))
        self.session.add(RoastEvent(
            date_str=DATE - dt.timedelta(days=7), group_id="100", event_type="backfire",
            attacker_id="owner", target_id="target",
            participant_snapshot={"backfire_victim_id": "victim"},
        ))
        self.session.add(RoastEvent(
            date_str=DATE, group_id="100", event_type="backfire",
            attacker_id="owner", target_id="target",
            participant_snapshot={"backfire_victim_id": "victim-other"},
        ))
        self.session.commit()
        with patch("rollpig_cloud.routers.catalog.rollpig_today", return_value=DATE):
            snapshot = get_catalog_snapshot("victim", session=self.session)
            self.assertEqual(snapshot.roasted_7d, 2)
            self.assertEqual(snapshot.roast_events_7d, 2)
            self.assertEqual(get_catalog_snapshot("owner", session=self.session).roasted_7d, 0)
            self.assertEqual(get_catalog_snapshot("target", session=self.session).roasted_7d, 0)


class ProtectionMigrationAndConcurrencyTests(unittest.TestCase):
    def test_old_unique_constraint_migrates_idempotently_and_preserves_data(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        # 模拟旧版本表级唯一约束，其余表仍由真实模型创建。
        for table in Base.metadata.sorted_tables:
            if table.name != "roast_reservations":
                table.create(engine)
        ddl = str(CreateTable(RoastReservation.__table__).compile(engine))
        ddl = ddl.replace("uq_roast_reservation_active_target UNIQUE (date_str, group_id, target_id, active_slot)",
                          "uq_roast_reservation_date_group_target UNIQUE (date_str, group_id, target_id)")
        ddl = "\n".join(line for line in ddl.splitlines() if not line.strip().startswith(("active_slot ", "protection_required ")))
        with engine.begin() as connection:
            connection.execute(text(ddl))
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO roast_reservations (reservation_id, date_str, group_id, target_id, "
                "target_name, target_pig_id, owner_id, owner_name, owner_pig_id, delivery_bot_id, status, outcome_snapshot) "
                "VALUES ('old', '2026-08-07', '100', 'target', '', '', 'a', '', 'pig', 'bot', 'completed', "
                "'{\"event_type\":\"escape\"}')"))

        def fail_copy(connection, cursor, statement, parameters, context, executemany):
            """在真实复制阶段注入失败，核对 SQLite DDL 会随事务回滚。"""

            if statement.startswith("INSERT INTO roast_reservations_migrating"):
                raise RuntimeError("copy failed")

        event.listen(engine, "before_cursor_execute", fail_copy)
        with self.assertRaisesRegex(RuntimeError, "copy failed"):
            ensure_runtime_migrations(engine)
        event.remove(engine, "before_cursor_execute", fail_copy)
        self.assertIn("roast_reservations", inspect(engine).get_table_names())
        self.assertNotIn("roast_reservations_migrating", inspect(engine).get_table_names())
        ensure_runtime_migrations(engine)
        ensure_runtime_migrations(engine)
        constraints = {item["name"] for item in inspect(engine).get_unique_constraints("roast_reservations")}
        self.assertIn("uq_roast_reservation_active_target", constraints)
        self.assertNotIn("uq_roast_reservation_date_group_target", constraints)
        with Session(engine) as session:
            old = session.scalar(select(RoastReservation))
            self.assertEqual(old.outcome_snapshot, {"event_type": "escape"})
            self.assertIsNone(old.active_slot)
            session.add(RoastReservation(reservation_id="new", date_str=DATE, group_id="100", target_id="target",
                                         owner_id="b", owner_pig_id="pig", delivery_bot_id="bot"))
            session.commit()

    def test_concurrent_last_joins_only_activate_one_round(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine = create_engine(f"sqlite+pysqlite:///{Path(temporary) / 'cloud.sqlite3'}",
                                   connect_args={"check_same_thread": False})
            Base.metadata.create_all(engine)
            try:
                with Session(engine) as session:
                    session.add(GroupProtection(protect_date=DATE, group_id="100", user_id="target", base_level=3))
                    session.add(DailyRoll(date_str=DATE, user_id="target", pig_id="pig"))
                    session.commit()

                def join(number):
                    """使用独立会话模拟多个 Bot 同时追加参与者。"""

                    with Session(engine) as session:
                        request = RoastReservationPrepareRequest(attacker_id=str(number), attacker_pig_id="pig",
                            target_id="target", group_id="100", delivery_bot_id="bot", date_str=DATE,
                            supports_protection_gathering=True, protection_gathering_version=2)
                        return prepare(request, session).status

                with ThreadPoolExecutor(max_workers=8) as pool:
                    results = list(pool.map(join, range(8)))
                self.assertEqual(results.count("reservation_created"), 1)
                self.assertEqual(results.count("reservation_joined"), 3)
                with Session(engine) as session:
                    self.assertEqual(len(session.scalars(select(RoastReservation)).all()), 1)
                    self.assertEqual(len(session.scalars(select(RoastReservationParticipant)).all()), 4)
                    item = claim(RoastReservationClaimRequest(delivery_bot_id="bot", date_str=DATE,
                                 supports_prepared=True, supports_protection_gathering=True, protection_gathering_version=2), session).items[0]

                def save_result(number):
                    """并发提交不同结果文本，同一令牌只能固化其中一份。"""

                    with Session(engine) as session:
                        return prepare_outcome(RoastReservationOutcomeRequest(reservation_id=item.reservation_id,
                            claim_token=item.claim_token, outcome_snapshot={"event_type": "success", "text": str(number)}), session).ok

                with ThreadPoolExecutor(max_workers=8) as pool:
                    saved = list(pool.map(save_result, range(8)))
                self.assertEqual(saved.count(True), 1)
                with Session(engine) as session:
                    self.assertEqual(session.scalar(select(GroupProtection)).roast_success_count, 1)
            finally:
                engine.dispose()
