from __future__ import annotations

import concurrent.futures
import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import Request
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

os.environ.setdefault("ROLLPIG_CLOUD_DATABASE_URL", "sqlite+pysqlite:///:memory:")

from rollpig_cloud.auth import verify_token
from rollpig_cloud.config import ApiKeyIdentity, parse_api_keys, settings
from rollpig_cloud.models import SourceKeyDailyUsage, SourceKeyIdentity
from rollpig_cloud.services.key_usage import record_key_mutation_outcome, record_key_request


class ApiKeyConfigTests(unittest.TestCase):
    def test_named_and_legacy_keys_are_merged_without_exposing_tokens(self):
        identities = parse_api_keys('{"nekobot-v2":"secret-v2"}', "secret-v2,secret-legacy")

        self.assertEqual(identities["secret-v2"].name, "nekobot-v2")
        self.assertTrue(identities["secret-legacy"].name.startswith("key-"))
        self.assertNotIn("secret-v2", repr(identities["secret-v2"]))

    def test_renaming_key_preserves_stable_identity(self):
        before = parse_api_keys('{"old-name":"same-token"}', "")["same-token"]
        after = parse_api_keys('{"new-name":"same-token"}', "")["same-token"]

        self.assertEqual(before.key_id, after.key_id)
        self.assertNotEqual(before.name, after.name)

    def test_invalid_named_key_json_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "合法 JSON"):
            parse_api_keys("not-json", "")
        with self.assertRaisesRegex(ValueError, "JSON 对象"):
            parse_api_keys('["token"]', "")

    def test_verify_token_stores_only_safe_identity_on_request(self):
        identity = ApiKeyIdentity(key_id="key-test", name="test-bot")
        request = Request({"type": "http", "method": "GET", "path": "/v1/test", "headers": []})
        credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="raw-secret")

        with patch.object(settings, "api_keys", {"raw-secret": identity}):
            result = verify_token(request, credentials)

        self.assertEqual(result, identity)
        self.assertEqual(request.state.api_key_identity, identity)
        self.assertFalse(hasattr(request.state.api_key_identity, "token"))


class SourceKeyUsageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        database_path = Path(self.temp_dir.name) / "usage.sqlite3"
        self.engine = create_engine(
            f"sqlite:///{database_path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 30},
        )
        SourceKeyIdentity.__table__.create(self.engine)
        SourceKeyDailyUsage.__table__.create(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.identity = ApiKeyIdentity(key_id="key-stable", name="NekoBot V2")
        self.business_date = dt.date(2026, 8, 25)

    def tearDown(self):
        self.engine.dispose()
        self.temp_dir.cleanup()

    def _record(self, *, method: str, operation: str, status_code: int, identity=None):
        with patch("rollpig_cloud.services.key_usage.rollpig_today", return_value=self.business_date):
            record_key_request(
                identity or self.identity,
                method=method,
                operation=operation,
                status_code=status_code,
                session_factory=self.session_factory,
            )

    def test_usage_distinguishes_reads_mutations_and_failures(self):
        self._record(method="GET", operation="GET /v1/daily-rolls/all", status_code=200)
        self._record(method="POST", operation="POST /v1/events", status_code=200)
        self._record(method="POST", operation="POST /v1/events", status_code=409)

        with self.session_factory() as session:
            rows = {
                row.operation: row
                for row in session.scalars(select(SourceKeyDailyUsage)).all()
            }

        read_row = rows["GET /v1/daily-rolls/all"]
        self.assertEqual(read_row.authenticated_requests, 1)
        self.assertEqual(read_row.successful_requests, 1)
        self.assertEqual(read_row.successful_mutations, 0)
        self.assertEqual(read_row.failed_requests, 0)

        write_row = rows["POST /v1/events"]
        self.assertEqual(write_row.authenticated_requests, 2)
        self.assertEqual(write_row.successful_requests, 1)
        self.assertEqual(write_row.successful_mutations, 1)
        self.assertEqual(write_row.failed_requests, 1)
        self.assertEqual(write_row.created_records, 0)
        self.assertEqual(write_row.idempotent_hits, 0)

    def test_current_key_name_is_updated_without_splitting_usage(self):
        self._record(method="POST", operation="POST /v1/events", status_code=200)
        renamed = ApiKeyIdentity(key_id=self.identity.key_id, name="NekoBot V2 主实例")
        self._record(method="POST", operation="POST /v1/events", status_code=200, identity=renamed)

        with self.session_factory() as session:
            stored_identity = session.get(SourceKeyIdentity, self.identity.key_id)
            usage_rows = session.scalars(select(SourceKeyDailyUsage)).all()

        self.assertEqual(stored_identity.key_name, renamed.name)
        self.assertEqual(len(usage_rows), 1)
        self.assertEqual(usage_rows[0].successful_mutations, 2)

    def test_concurrent_requests_use_atomic_counters(self):
        def record_once(_index: int) -> None:
            self._record(method="POST", operation="POST /v1/events", status_code=200)

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(record_once, range(24)))

        with self.session_factory() as session:
            usage = session.scalar(select(SourceKeyDailyUsage))

        self.assertEqual(usage.authenticated_requests, 24)
        self.assertEqual(usage.successful_requests, 24)
        self.assertEqual(usage.successful_mutations, 24)
        self.assertEqual(usage.failed_requests, 0)

    def test_business_outcomes_follow_the_caller_transaction(self):
        with patch("rollpig_cloud.services.key_usage.rollpig_today", return_value=self.business_date):
            with self.session_factory() as session:
                record_key_mutation_outcome(
                    session,
                    self.identity,
                    operation="POST /v1/daily-rolls/get-or-create",
                    created_records=1,
                )
                session.commit()

            with self.session_factory() as session:
                record_key_mutation_outcome(
                    session,
                    self.identity,
                    operation="POST /v1/daily-rolls/get-or-create",
                    created_records=5,
                    idempotent_hits=2,
                )
                session.rollback()

        with self.session_factory() as session:
            usage = session.scalar(select(SourceKeyDailyUsage))

        self.assertEqual(usage.created_records, 1)
        self.assertEqual(usage.idempotent_hits, 0)


if __name__ == "__main__":
    unittest.main()
