from __future__ import annotations

import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("ROLLPIG_CLOUD_DATABASE_URL", "sqlite+pysqlite:///:memory:")

from rollpig_cloud.migrations import _migrate_protection_gatherings


OLD_INDEX = "uq_roast_reservation_date_group_target"
NEW_INDEX = "uq_roast_reservation_active_target"


class MySQLProtectionMigrationTests(unittest.TestCase):
    def run_migration(self, *, concurrent=False, fail_add=False, acquired=1):
        """模拟 MySQL 命名锁与索引 DDL，验证并发交错和失败后的锁释放。"""
        constraints = {OLD_INDEX}
        migration_lock = Lock()
        barrier = Barrier(2) if concurrent else None
        calls = []
        engine = Mock()
        engine.dialect.name = "mysql"

        @contextmanager
        def begin():
            connection = Mock()
            connection.has_lock = False

            def execute(statement, parameters=None):
                sql = str(statement)
                if "GET_LOCK(" in sql:
                    if acquired == 1:
                        if not migration_lock.acquire(timeout=3):
                            raise AssertionError("迁移未释放命名锁")
                        connection.has_lock = True
                    calls.append("lock")
                    return SimpleNamespace(scalar=lambda: acquired)
                if "RELEASE_LOCK(" in sql:
                    self.assertTrue(connection.has_lock)
                    connection.has_lock = False
                    migration_lock.release()
                    calls.append("release")
                    return SimpleNamespace(scalar=lambda: 1)
                if "ADD CONSTRAINT" in sql:
                    calls.append("add")
                    if fail_add:
                        raise RuntimeError("index creation failed")
                    self.assertNotIn(NEW_INDEX, constraints)
                    constraints.add(NEW_INDEX)
                elif "DROP INDEX" in sql:
                    calls.append("drop")
                    self.assertIn(NEW_INDEX, constraints)
                    self.assertIn(OLD_INDEX, constraints)
                    constraints.remove(OLD_INDEX)
                return SimpleNamespace(scalar=lambda: None)

            connection.execute.side_effect = execute
            try:
                yield connection
            finally:
                self.assertFalse(connection.has_lock, "连接返回池前必须释放命名锁")

        def inspector(source):
            result = Mock()
            result.get_table_names.return_value = ["roast_reservations"]

            def unique_constraints(table):
                snapshot = [{"name": name} for name in constraints]
                if source is engine and barrier is not None:
                    barrier.wait(timeout=3)
                elif source is not engine:
                    self.assertTrue(source.has_lock)
                return snapshot

            result.get_unique_constraints.side_effect = unique_constraints
            return result

        engine.begin.side_effect = begin
        with (patch("rollpig_cloud.migrations.inspect", side_effect=inspector),
              patch("rollpig_cloud.migrations._add_column_if_missing")):
            if concurrent:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    list(pool.map(lambda _: _migrate_protection_gatherings(engine), range(2)))
            elif fail_add:
                with self.assertRaisesRegex(RuntimeError, "index creation failed"):
                    _migrate_protection_gatherings(engine)
            elif acquired != 1:
                with self.assertRaisesRegex(RuntimeError, "迁移锁"):
                    _migrate_protection_gatherings(engine)
            else:
                _migrate_protection_gatherings(engine)
        self.assertFalse(migration_lock.locked())
        return calls, constraints

    def test_concurrent_startup_rechecks_indexes_after_acquiring_lock(self):
        calls, constraints = self.run_migration(concurrent=True)
        self.assertEqual(calls.count("add"), 1)
        self.assertEqual(calls.count("drop"), 1)
        self.assertEqual(calls.count("release"), 2)
        self.assertEqual(constraints, {NEW_INDEX})

    def test_failed_index_creation_releases_lock_and_preserves_old_index(self):
        calls, constraints = self.run_migration(fail_add=True)
        self.assertEqual(calls.count("release"), 1)
        self.assertNotIn("drop", calls)
        self.assertEqual(constraints, {OLD_INDEX})

    def test_lock_timeout_or_error_does_not_modify_indexes(self):
        for acquired in (0, None):
            with self.subTest(acquired=acquired):
                calls, constraints = self.run_migration(acquired=acquired)
                self.assertEqual(calls, ["lock"])
                self.assertEqual(constraints, {OLD_INDEX})
