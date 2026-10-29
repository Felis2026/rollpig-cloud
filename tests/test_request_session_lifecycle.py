from __future__ import annotations

import asyncio
import concurrent.futures
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

os.environ.setdefault("ROLLPIG_CLOUD_DATABASE_URL", "sqlite+pysqlite:///:memory:")

from rollpig_cloud import db, main
from rollpig_cloud.config import ApiKeyIdentity, settings
from rollpig_cloud.models import DailyRoll, SourceKeyDailyUsage
from rollpig_cloud.services.key_usage import record_key_mutation_outcome, record_key_request


class RequestSessionLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.engine = create_engine(
            f"sqlite:///{(Path(self.directory.name) / 'usage.sqlite3').as_posix()}",
            poolclass=QueuePool,
            pool_size=1,
            max_overflow=0,
            pool_timeout=0.3,
            connect_args={"check_same_thread": False},
        )
        self.addCleanup(self.engine.dispose)
        db.Base.metadata.create_all(self.engine)
        self.factory = sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)
        self.identity = ApiKeyIdentity(key_id="lifecycle-key", name="测试实例")
        self.headers = {"Authorization": "Bearer test-token"}
        self.client = TestClient(main.app)
        self.addCleanup(self.client.close)
        for target, attribute, value in (
            (db, "SessionLocal", self.factory),
            (main, "record_key_request", self._record),
            (settings, "api_keys", {"test-token": self.identity}),
        ):
            item = patch.object(target, attribute, value)
            item.start()
            self.addCleanup(item.stop)

    def _record(self, *args, **kwargs):
        """使用与业务相同的单连接池，重现池耗尽时的依赖清理顺序。"""

        record_key_request(*args, **kwargs, session_factory=self.factory)

    def test_read_endpoints_release_connection_before_usage_transaction(self):
        with self.factory() as session:
            session.add(DailyRoll(date_str=dt.date(2026, 10, 29), user_id="test-user", pig_id="test-pig"))
            session.commit()
        requests = (
            ("/v1/draw-state", {"user_id": "test-user"}),
            ("/v1/daily-rolls/by-date", {"user_id": "test-user", "date_str": "2026-10-29"}),
            ("/v1/daily-rolls/all", {"date_str": "2026-10-29"}),
        )
        for path, params in requests:
            with self.subTest(path=path):
                response = self.client.get(path, params=params, headers=self.headers)
                self.assertEqual(response.status_code, 200)
                if path == "/v1/daily-rolls/by-date":
                    self.assertEqual(response.json()["pig_id"], "test-pig")
                elif path == "/v1/daily-rolls/all":
                    self.assertEqual(response.json()["items"], [{"user_id": "test-user", "pig_id": "test-pig"}])
                with self.factory() as session:
                    usage = session.scalar(select(SourceKeyDailyUsage).where(
                        SourceKeyDailyUsage.operation == f"GET {path}",
                    ))
                    self.assertIsNotNone(usage)
                    self.assertEqual(usage.authenticated_requests, 1)
                    self.assertEqual(usage.successful_requests, 1)
                    self.assertEqual(usage.failed_requests, 0)

    def test_concurrent_reads_preserve_every_counter_with_one_connection(self):
        def request(_index):
            return self.client.get(
                "/v1/draw-state", params={"user_id": "test-user"}, headers=self.headers,
            ).status_code

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            statuses = list(pool.map(request, range(18)))
        self.assertEqual(statuses, [200] * 18)
        with self.factory() as session:
            usage = session.scalar(select(SourceKeyDailyUsage))
            self.assertIsNotNone(usage)
            self.assertEqual(usage.authenticated_requests, 18)
            self.assertEqual(usage.successful_requests, 18)

    def test_rollback_finishes_before_failed_request_is_counted(self):
        app = FastAPI()
        app.middleware("http")(main.track_authenticated_requests)

        @app.post("/rollback-test")
        def fail(request: Request, session: Session = Depends(db.get_session, scope="function")):
            request.state.api_key_identity = self.identity
            record_key_mutation_outcome(
                session, self.identity, operation="POST /rollback-test", created_records=5,
            )
            raise HTTPException(status_code=409, detail="回滚测试")

        with TestClient(app) as client:
            response = client.post("/rollback-test")
        self.assertEqual(response.status_code, 409)
        with self.factory() as session:
            usage = session.scalar(select(SourceKeyDailyUsage))
            self.assertEqual(usage.failed_requests, 1)
            self.assertEqual(usage.created_records, 0)
            self.assertEqual(usage.authenticated_requests, 1)

    def test_committed_business_results_and_request_statistics_are_both_preserved(self):
        app = FastAPI()
        app.middleware("http")(main.track_authenticated_requests)

        @app.post("/commit-test")
        def commit(request: Request, session: Session = Depends(db.get_session, scope="function")):
            request.state.api_key_identity = self.identity
            record_key_mutation_outcome(
                session, self.identity, operation="POST /commit-test", created_records=5,
            )
            session.commit()
            # 写接口提交后还可能查询响应数据，这条查询会再次占用业务连接。
            return {"created": session.scalar(select(SourceKeyDailyUsage.created_records))}

        with TestClient(app) as client:
            response = client.post("/commit-test")
        self.assertEqual(response.json(), {"created": 5})
        with self.factory() as session:
            usage = session.scalar(select(SourceKeyDailyUsage))
            self.assertEqual(usage.created_records, 5)
            self.assertEqual(usage.authenticated_requests, 1)
            self.assertEqual(usage.successful_mutations, 1)

    def test_unhandled_error_rolls_back_before_recording_server_failure(self):
        app = FastAPI()
        app.middleware("http")(main.track_authenticated_requests)

        @app.post("/error-test")
        def fail(request: Request, session: Session = Depends(db.get_session, scope="function")):
            request.state.api_key_identity = self.identity
            record_key_mutation_outcome(
                session, self.identity, operation="POST /error-test", created_records=5,
            )
            raise RuntimeError("业务失败")

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post("/error-test")
        self.assertEqual(response.status_code, 500)
        with self.factory() as session:
            usage = session.scalar(select(SourceKeyDailyUsage))
            self.assertEqual(usage.created_records, 0)
            self.assertEqual(usage.authenticated_requests, 1)
            self.assertEqual(usage.failed_requests, 1)

    def test_every_business_session_uses_function_scope(self):
        checked = 0

        def check(dependant):
            nonlocal checked
            for dependency in dependant.dependencies:
                if dependency.call is db.get_session:
                    self.assertEqual(dependency.scope, "function")
                    checked += 1
                check(dependency)

        def check_routes(routes):
            for route in routes:
                if isinstance(route, APIRoute):
                    check(route.dependant)
                else:
                    # 新版 FastAPI 延迟展开 include_router，检查其原始路由定义。
                    original = getattr(route, "original_router", None)
                    if original is not None:
                        check_routes(original.routes)

        check_routes(main.app.routes)
        self.assertGreater(checked, 20)


class RequestTimingTests(unittest.TestCase):
    def _request(self):
        request = Request({"type": "http", "method": "GET", "path": "/v1/test", "headers": []})
        request.state.api_key_identity = ApiKeyIdentity(key_id="test-key", name="test")
        return request

    def test_duration_includes_usage_write_without_exposing_query_parameters(self):
        with (
            patch.object(main, "record_key_request"),
            patch.object(main.time, "perf_counter", side_effect=[10.1, 10.6]),
            self.assertLogs(main.logger, level="INFO") as logs,
        ):
            asyncio.run(main._record_authenticated_request(self._request(), 200, 10.0))
        payload = json.loads(logs.records[-1].getMessage().split(" ", 1)[1])
        self.assertEqual(payload["duration_ms"], 600.0)
        self.assertEqual(payload["business_duration_ms"], 100.0)
        self.assertEqual(payload["usage_duration_ms"], 500.0)
        self.assertNotIn("token", payload)

    def test_slow_statistics_are_visible_as_warning(self):
        with (
            patch.object(main, "record_key_request"),
            patch.object(main.time, "perf_counter", side_effect=[10.01, 12.0]),
            self.assertLogs(main.logger, level="WARNING") as logs,
        ):
            asyncio.run(main._record_authenticated_request(self._request(), 200, 10.0))
        self.assertEqual(logs.records[-1].levelname, "WARNING")
        self.assertIn('"usage_duration_ms":1990.0', logs.records[-1].getMessage())

    def test_statistics_failure_keeps_business_status_and_records_elapsed_time(self):
        with (
            patch.object(main, "record_key_request", side_effect=RuntimeError("database unavailable")),
            patch.object(main.time, "perf_counter", side_effect=[10.01, 12.0]),
            self.assertLogs(main.logger, level="WARNING") as logs,
        ):
            asyncio.run(main._record_authenticated_request(self._request(), 409, 10.0))
        payload = json.loads(logs.records[-1].getMessage().split(" ", 1)[1])
        self.assertEqual(payload["status"], 409)
        self.assertEqual(payload["duration_ms"], 2000.0)


if __name__ == "__main__":
    unittest.main()
