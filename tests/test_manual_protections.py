from __future__ import annotations

import datetime as dt
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("ROLLPIG_CLOUD_DATABASE_URL", "sqlite+pysqlite:///:memory:")

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from rollpig_cloud.auth import settings
from rollpig_cloud.config import ApiKeyIdentity
from rollpig_cloud.db import Base, get_session
from rollpig_cloud.migrations import ensure_runtime_migrations
from rollpig_cloud.models import DailyRoll, GroupProtection, RoastReservation, RoastReservationParticipant
from rollpig_cloud.routers import protections
from rollpig_cloud.routers.roast_reservations import claim, prepare, prepare_outcome
from rollpig_cloud.schemas import (GrantProtectionRequest, ReplaceGroupProtectionsRequest, RoastReservationClaimRequest,
                                   RoastReservationOutcomeRequest, RoastReservationPrepareRequest)
from rollpig_cloud.services.reservations import activate_target_reservations


DATE = dt.date(2026, 10, 27)


class ManualProtectionTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.session = Session(self.engine, autoflush=False, expire_on_commit=False)
        self.addCleanup(self.session.close)
        today = patch.object(protections, "rollpig_today", return_value=DATE)
        today.start()
        self.addCleanup(today.stop)
        usage = patch.object(protections, "record_key_mutation_outcome")
        usage.start()
        self.addCleanup(usage.stop)

    def grant(self, level=5, **changes):
        """调用实际发放事务，可调整群、用户与日期。"""
        values = dict(group_id="100", user_id="2", level=level, protect_date=DATE)
        values.update(changes)
        return protections.grant_protection(GrantProtectionRequest(**values), self.session, object())

    def replace_daily(self, users, date=DATE):
        """调用日报名单替换，核对它与手动来源的隔离。"""
        protections.replace_group(ReplaceGroupProtectionsRequest(
            group_id="100", protect_date=date, user_ids=users, initial_level=3,
        ), self.session, object())

    def row(self):
        return self.session.scalar(select(GroupProtection).where(
            GroupProtection.group_id == "100", GroupProtection.user_id == "2", GroupProtection.protect_date == DATE,
        ).execution_options(populate_existing=True))

    def prepare(self, user="3", **changes):
        """通过真实预约入口创建或加入场次。"""
        values = dict(attacker_id=user, attacker_pig_id="pig", target_id="2", group_id="100",
                      delivery_bot_id="bot", date_str=DATE, supports_protection_gathering=True,
                      protection_gathering_version=2)
        values.update(changes)
        return prepare(RoastReservationPrepareRequest(**values), self.session)

    def test_grant_is_group_day_scoped_and_duplicate_never_lowers(self):
        first = self.grant()
        self.assertEqual((first.level, first.changed), (5, True))
        for level in (5, 1, 3):
            result = self.grant(level)
            self.assertEqual((result.level, result.changed), (5, False))
        for group, date in (("200", DATE), ("100", DATE + dt.timedelta(days=1))):
            self.assertFalse(protections.check_protected(date, group, "2", self.session).protected)
        self.assertEqual(self.row().source_levels, {"manual": 5})
        self.assertEqual(self.row().consecutive_days, 0)

    def test_daily_replacement_preserves_manual_and_does_not_use_manual_streak(self):
        self.grant(5)
        self.replace_daily(["2", "4"])
        state = protections.check_protected(DATE, "100", "2", self.session)
        self.assertEqual((state.level, state.source, state.daily_base_level), (5, "manual", 3))
        self.replace_daily([])
        self.assertEqual(self.row().source_levels, {"manual": 5})
        self.assertEqual(self.row().consecutive_days, 0)
        self.replace_daily(["2"], DATE + dt.timedelta(days=1))
        tomorrow = self.session.scalar(select(GroupProtection).where(GroupProtection.protect_date == DATE + dt.timedelta(days=1)))
        self.assertEqual((tomorrow.base_level, tomorrow.consecutive_days), (3, 1))

    def test_equal_sources_choose_daily_and_lower_manual_survives_daily_removal(self):
        self.replace_daily(["2"])
        self.assertFalse(self.grant(3).changed)
        self.assertEqual(protections.check_protected(DATE, "100", "2", self.session).source, "daily")
        self.replace_daily([])
        self.assertEqual((self.row().base_level, self.row().source_levels), (3, {"manual": 3}))

    def test_reserved_pig_source_survives_daily_replacement(self):
        self.replace_daily(["2"])
        row = self.row()
        row.source_levels = {"daily": 3, "pig": 4}
        row.base_level = 4
        self.session.commit()
        self.replace_daily([])
        state = protections.check_protected(DATE, "100", "2", self.session)
        self.assertEqual((state.protected, state.level, state.source, state.daily_base_level), (True, 4, "pig", 0))
        self.assertEqual(self.row().source_levels, {"pig": 4})

    def test_grant_uses_actual_level_without_double_adding_breakthroughs(self):
        self.replace_daily(["2"])
        row = self.row()
        row.roast_success_count = 1
        self.session.commit()
        self.assertEqual(self.grant(5).level, 5)
        self.assertEqual((row.base_level, row.roast_success_count), (5, 1))
        self.assertEqual(row.source_levels, {"daily": 3, "manual": 5})
        self.assertEqual(row.source_baselines, {"daily": 0, "manual": 1})
        self.assertFalse(self.grant(5).changed)
        row.roast_success_count = 2
        self.session.commit()
        self.assertEqual(protections.check_protected(DATE, "100", "2", self.session).source, "daily")

    def test_removing_daily_source_recomputes_waiting_round_from_remaining_manual_level(self):
        self.replace_daily(["2"])
        row = self.row()
        row.base_level = 5
        row.source_levels = {"daily": 5}
        self.session.commit()
        self.grant(3)
        for user in ("3", "4", "5", "6"):
            self.prepare(user)
        self.session.add(DailyRoll(date_str=DATE, user_id="2", pig_id="pig"))
        self.session.flush()
        self.assertEqual(activate_target_reservations(self.session, date_str=DATE, target_id="2", target_pig_id="pig"), 0)
        self.session.commit()
        self.replace_daily([])
        reservation = self.session.scalar(select(RoastReservation))
        self.assertEqual((reservation.protection_required, reservation.protection_source, reservation.status), (4, "manual", "ready"))

    def test_legacy_protection_uses_generic_source_and_is_not_downgraded(self):
        self.session.add(GroupProtection(group_id="__all__", user_id="2", protect_date=DATE, base_level=4))
        self.session.commit()
        self.assertEqual(protections.check_protected(DATE, "100", "2", self.session).source, "legacy")
        result = self.grant(2)
        self.assertEqual((result.level, result.changed), (4, False))
        self.assertEqual(self.row().base_level, 4)

    def test_grant_updates_pending_only_and_preserves_participants(self):
        original = self.prepare().reservation
        result = self.grant()
        row = self.session.scalar(select(RoastReservation))
        self.assertEqual((result.level, row.protection_required, row.protection_source, row.status), (5, 6, "manual", "pending"))
        members = self.session.scalars(select(RoastReservationParticipant)).all()
        self.assertEqual([(member.user_id, member.contribution) for member in members], [("3", 1)])
        self.assertEqual(row.reservation_id, original.reservation_id)
        self.session.add(DailyRoll(date_str=DATE, user_id="2", pig_id="pig"))
        self.session.flush()
        self.assertEqual(activate_target_reservations(self.session, date_str=DATE, target_id="2", target_pig_id="pig"), 0)
        self.session.commit()
        self.assertEqual(self.prepare("4").reservation.protection_required, 6)

    def test_pending_accelerated_round_keeps_paid_double_fire(self):
        self.prepare(force_mode="normal")
        self.grant()
        member = self.session.scalar(select(RoastReservationParticipant))
        self.assertEqual(member.contribution, 2)

    def test_ready_or_super_round_is_not_rewritten(self):
        for index, (status, mode) in enumerate((("ready", None), ("processing", None), ("prepared", None),
                                              ("sending", None), ("completed", None), ("pending", "super"))):
            with self.subTest(status=status, mode=mode):
                self.session.query(RoastReservationParticipant).delete()
                self.session.query(RoastReservation).delete()
                self.session.query(GroupProtection).delete()
                self.session.commit()
                self.prepare(user=str(30 + index), force_mode=mode)
                row = self.session.scalar(select(RoastReservation))
                row.status = status
                self.session.commit()
                self.grant()
                self.assertEqual((row.status, row.protection_required), (status, 0))

    def test_invalid_dates_ids_and_levels_do_not_write(self):
        for date in (DATE - dt.timedelta(days=1), DATE + dt.timedelta(days=1)):
            with self.assertRaises(HTTPException):
                self.grant(protect_date=date)
        for changes in ({"level": 0}, {"level": 6}, {"level": True}, {"group_id": "__all__"}, {"user_id": "0"}):
            with self.assertRaises(ValidationError):
                GrantProtectionRequest(**{**dict(group_id="100", user_id="2", level=3, protect_date=DATE), **changes})
        self.assertIsNone(self.row())

    def test_old_client_is_blocked_after_grant(self):
        self.grant()
        self.assertEqual(self.prepare(supports_protection_gathering=False, protection_gathering_version=0).status, "protected")

    def arrive_target(self):
        """登记目标到场，并通过实际激活入口更新本场状态。"""
        self.session.add(DailyRoll(date_str=DATE, user_id="2", pig_id="pig"))
        self.session.flush()
        activate_target_reservations(self.session, date_str=DATE, target_id="2", target_pig_id="pig")
        self.session.commit()

    def claim_for(self, bot="bot", supported=True):
        return claim(RoastReservationClaimRequest(delivery_bot_id=bot, date_str=DATE, supports_prepared=True,
                     supports_protection_gathering=supported, protection_gathering_version=2 if supported else 0), self.session)

    def test_new_manual_source_ignores_old_breakthroughs_and_keeps_later_upgrades(self):
        self.replace_daily(["2"])
        row = self.row()
        row.roast_success_count = 2
        self.session.commit()
        self.assertEqual(self.grant(1).level, 5)
        self.replace_daily([])
        self.assertEqual(protections.check_protected(DATE, "100", "2", self.session).level, 1)
        self.assertEqual(row.source_baselines, {"manual": 2})
        row.roast_success_count += 1
        self.session.commit()
        result = self.grant(1)
        self.assertEqual((result.level, result.changed), (2, False))
        self.assertEqual(row.source_baselines, {"manual": 2})
        self.assertEqual(self.grant(3).level, 3)
        self.assertEqual(row.source_baselines, {"manual": 3})

    def test_new_source_can_still_upgrade_after_four_previous_breakthroughs(self):
        self.replace_daily(["2"])
        row = self.row()
        row.roast_success_count = 4
        self.session.commit()
        self.grant(1)
        self.replace_daily([])
        self.arrive_target()
        self.prepare()
        self.prepare("4")
        item = self.claim_for().items[0]
        request = RoastReservationOutcomeRequest(reservation_id=item.reservation_id, claim_token=item.claim_token,
                                                outcome_snapshot={"event_type": "success"})
        self.assertTrue(prepare_outcome(request, self.session).ok)
        self.assertTrue(prepare_outcome(request, self.session).ok)
        self.assertEqual(row.roast_success_count, 5)
        self.assertEqual(protections.check_protected(DATE, "100", "2", self.session).level, 2)

    def test_new_daily_source_does_not_inherit_manual_breakthroughs(self):
        self.grant(1)
        row = self.row()
        row.roast_success_count = 2
        self.session.commit()
        self.replace_daily(["2"])
        state = protections.check_protected(DATE, "100", "2", self.session)
        self.assertEqual((state.level, state.daily_base_level, state.source), (3, 3, "daily"))
        self.assertEqual(row.source_baselines, {"daily": 2})

    def test_removing_last_source_restores_ordinary_reservation_before_or_after_arrival(self):
        for index, arrived in enumerate((False, True)):
            with self.subTest(arrived=arrived):
                self.session.query(DailyRoll).delete()
                self.session.query(RoastReservationParticipant).delete()
                self.session.query(RoastReservation).delete()
                self.session.commit()
                self.prepare(user=str(30 + index))
                self.replace_daily(["2"])
                if arrived:
                    self.arrive_target()
                self.replace_daily([])
                row = self.session.scalar(select(RoastReservation))
                self.assertFalse(protections.check_protected(DATE, "100", "2", self.session).protected)
                self.assertEqual(row.protection_required, 0)
                self.assertEqual(row.status, "ready" if arrived else "pending")
                if not arrived:
                    self.arrive_target()
                self.assertEqual(row.status, "ready")
                self.assertEqual(len(self.claim_for(supported=False).items), 1)

    def test_removing_group_source_keeps_legacy_global_protection(self):
        self.prepare()
        self.replace_daily(["2"])
        self.session.add(GroupProtection(group_id="__all__", user_id="2", protect_date=DATE, base_level=1))
        self.session.commit()
        self.replace_daily([])
        row = self.session.scalar(select(RoastReservation))
        self.assertTrue(protections.check_protected(DATE, "100", "2", self.session).protected)
        self.assertEqual(row.protection_required, 2)

    def test_new_bot_takes_pending_legacy_delivery_without_changing_paid_roster(self):
        self.prepare(delivery_bot_id="old", supports_protection_gathering=False, protection_gathering_version=0)
        self.grant(3)
        result = self.prepare("4", delivery_bot_id="new")
        self.assertEqual(result.reservation.delivery_bot_id, "new")
        self.prepare("5", delivery_bot_id="new")
        self.prepare("6", delivery_bot_id="new")
        self.arrive_target()
        self.assertFalse(self.claim_for("old", supported=False).items)
        first = self.claim_for("new").items
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].owner_id, "3")
        self.assertEqual([item.user_id for item in first[0].participants], ["3", "4", "5", "6"])
        self.assertFalse(self.claim_for("new").items)

    def test_new_bot_can_take_unclaimed_ready_legacy_delivery_without_joining(self):
        self.prepare(delivery_bot_id="old", supports_protection_gathering=False, protection_gathering_version=0)
        self.prepare("4", delivery_bot_id="old", supports_protection_gathering=False, protection_gathering_version=0)
        self.grant(1)
        self.arrive_target()
        result = self.prepare("5", delivery_bot_id="new")
        self.assertEqual((result.status, result.reservation.status, result.reservation.delivery_bot_id), ("reservation_busy", "ready", "new"))
        self.assertEqual([item.user_id for item in result.reservation.participants], ["3", "4"])
        self.assertFalse(self.claim_for("old", supported=False).items)
        self.assertEqual(len(self.claim_for("new").items), 1)

    def test_capable_owner_is_preserved(self):
        self.prepare(delivery_bot_id="capable")
        self.grant(3)
        self.assertEqual(self.prepare("4", delivery_bot_id="other").reservation.delivery_bot_id, "capable")

    def test_full_or_old_protocol_join_cannot_take_delivery_but_existing_member_can(self):
        for user in ("3", *(str(value) for value in range(10, 21))):
            self.prepare(user, delivery_bot_id="old", supports_protection_gathering=False, protection_gathering_version=0)
        self.grant(5)
        rejected = self.prepare("99", delivery_bot_id="new")
        self.assertEqual((rejected.status, rejected.reservation.delivery_bot_id), ("reservation_full", "old"))
        rejected = self.prepare("3", delivery_bot_id="new", protection_gathering_version=1)
        self.assertEqual(rejected.status, "protected")
        self.assertEqual(self.session.scalar(select(RoastReservation)).delivery_bot_id, "old")
        accepted = self.prepare("3", delivery_bot_id="new")
        self.assertEqual((accepted.status, accepted.reservation.delivery_bot_id), ("already_joined", "new"))
        self.assertEqual(len(accepted.reservation.participants), 12)

    def test_current_owner_can_confirm_capability_through_existing_claim_poll(self):
        self.prepare(delivery_bot_id="upgraded", supports_protection_gathering=False, protection_gathering_version=0)
        self.assertFalse(self.claim_for("upgraded").items)
        self.grant(3)
        result = self.prepare("4", delivery_bot_id="other")
        self.assertEqual(result.reservation.delivery_bot_id, "upgraded")

    def test_fixed_or_claimed_legacy_round_cannot_change_delivery_owner(self):
        for index, status in enumerate(("processing", "prepared", "sending", "ready")):
            with self.subTest(status=status):
                self.session.query(RoastReservationParticipant).delete()
                self.session.query(RoastReservation).delete()
                self.session.query(GroupProtection).delete()
                self.session.commit()
                self.prepare(user=str(50 + index), delivery_bot_id="old", supports_protection_gathering=False, protection_gathering_version=0)
                self.grant(1)
                row = self.session.scalar(select(RoastReservation))
                row.status = status
                row.outcome_snapshot = {"event_type": "success"}
                row.claim_token = "token" if status != "ready" else None
                self.session.commit()
                result = self.prepare("99", delivery_bot_id="new")
                self.assertEqual((result.status, result.reservation.delivery_bot_id), ("reservation_busy", "old"))
                self.assertEqual(row.outcome_snapshot, {"event_type": "success"})


class ManualProtectionMigrationTests(unittest.TestCase):
    def test_old_records_keep_levels_and_gain_nullable_sources_idempotently(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(engine.dispose)
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE group_protections (id INTEGER PRIMARY KEY, protect_date DATE NOT NULL, group_id VARCHAR(64) NOT NULL, user_id VARCHAR(64) NOT NULL, base_level INTEGER NOT NULL DEFAULT 1, consecutive_days INTEGER NOT NULL DEFAULT 1, roast_success_count INTEGER NOT NULL DEFAULT 0, created_at DATETIME DEFAULT CURRENT_TIMESTAMP)"))
            connection.execute(text("INSERT INTO group_protections (id, protect_date, group_id, user_id, base_level) VALUES (1, '2026-10-27', '100', '2', 4)"))
        Base.metadata.create_all(engine)
        for _ in range(2):
            ensure_runtime_migrations(engine)
        self.assertIn("source_levels", {column["name"] for column in inspect(engine).get_columns("group_protections")})
        with Session(engine) as session:
            row = session.scalar(select(GroupProtection))
            self.assertEqual((row.base_level, row.source_levels, row.source_baselines), (4, None, None))


class ConcurrentManualProtectionTests(unittest.TestCase):
    def test_grant_and_daily_replacement_preserve_manual_protection(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine = create_engine(f"sqlite+pysqlite:///{Path(temporary) / 'cloud.sqlite3'}")
            Base.metadata.create_all(engine)
            try:
                def mutate(operation):
                    """让真实独立事务竞争同一群日保护记录。"""
                    with Session(engine, autoflush=False, expire_on_commit=False) as session:
                        if operation == "grant":
                            return protections.grant_protection(GrantProtectionRequest(
                                group_id="100", user_id="2", level=5, protect_date=DATE,
                            ), session, object())
                        return protections.replace_group(ReplaceGroupProtectionsRequest(
                            group_id="100", user_ids=["2", "4"], protect_date=DATE, initial_level=3,
                        ), session, object())
                with patch.object(protections, "rollpig_today", return_value=DATE), patch.object(protections, "record_key_mutation_outcome"):
                    with ThreadPoolExecutor(max_workers=3) as pool:
                        list(pool.map(mutate, ["grant", "daily", "grant"]))
                with Session(engine) as session:
                    row = session.scalar(select(GroupProtection).where(GroupProtection.user_id == "2"))
                    self.assertEqual((row.base_level, row.source_levels), (5, {"daily": 3, "manual": 5}))
                    self.assertEqual(len(session.scalars(select(GroupProtection)).all()), 2)
            finally:
                engine.dispose()


class ManualProtectionApiTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        app = FastAPI()
        app.include_router(protections.router, prefix="/api")

        def session():
            """每次 HTTP 请求使用独立会话，事务提交和回滚走真实依赖生命周期。"""
            with Session(self.engine, autoflush=False, expire_on_commit=False) as value:
                yield value

        app.dependency_overrides[get_session] = session
        self.client = TestClient(app)
        self.addCleanup(self.client.close)
        today = patch.object(protections, "rollpig_today", return_value=DATE)
        today.start()
        self.addCleanup(today.stop)
        keys = patch.object(settings, "api_keys", {"test-token": ApiKeyIdentity(key_id="test-key", name="test-bot")})
        keys.start()
        self.addCleanup(keys.stop)
        self.headers = {"Authorization": "Bearer test-token"}
        self.body = dict(group_id="100", user_id="2", level=5, protect_date=DATE.isoformat())

    def test_http_grant_repeat_and_old_check_fields_remain_usable(self):
        path = "/api/v1/protections/grant"
        first = self.client.post(path, json=self.body, headers=self.headers)
        self.assertEqual((first.status_code, first.json()), (200, {"level": 5, "changed": True}))
        repeat = self.client.post(path, json=self.body, headers=self.headers)
        self.assertEqual(repeat.json(), {"level": 5, "changed": False})
        check = self.client.get("/api/v1/protections/check", params={key: self.body[key] for key in ("group_id", "user_id", "protect_date")}, headers=self.headers)
        self.assertEqual(check.status_code, 200)
        self.assertEqual(check.json(), dict(protected=True, base_level=5, level=5, consecutive_days=0, source="manual", daily_base_level=0))

    def test_authentication_and_invalid_level_cannot_write(self):
        self.assertEqual(self.client.post("/api/v1/protections/grant", json=self.body).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/protections/grant", json={**self.body, "level": 6}, headers=self.headers).status_code, 422)
        with Session(self.engine) as session:
            self.assertIsNone(session.scalar(select(GroupProtection)))

    def test_old_daily_replace_request_keeps_manual_protection_and_original_response_fields(self):
        self.client.post("/api/v1/protections/grant", json=self.body, headers=self.headers).raise_for_status()
        response = self.client.post("/api/v1/protections/replace-group", json={
            "group_id": "100", "user_ids": [], "protect_date": DATE.isoformat(),
        }, headers=self.headers)
        self.assertEqual(response.json(), {"ok": True})
        response = self.client.get("/api/v1/protections/check", params={
            "group_id": "100", "user_id": "2", "protect_date": DATE.isoformat(),
        }, headers=self.headers)
        self.assertTrue(response.json()["protected"])
        self.assertEqual((response.json()["base_level"], response.json()["level"]), (5, 5))

    def test_manual_endpoint_does_not_accept_pig_source_override(self):
        response = self.client.post("/api/v1/protections/grant", json={**self.body, "source": "pig"}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        with Session(self.engine) as session:
            self.assertEqual(session.scalar(select(GroupProtection)).source_levels, {"manual": 5})
