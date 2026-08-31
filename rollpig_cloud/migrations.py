from __future__ import annotations

import datetime as dt
import time

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError

from .config import ROLLPIG_TIMEZONE


DEFAULT_ROAST_CHARGE_MAX = 2
DEFAULT_ROAST_CHARGE_RECOVER_SECONDS = 8 * 3600


def _quote_identifier(name: str) -> str:
    """按当前 cloud 只支持 MySQL/SQLite 的使用场景做最小标识符转义。"""
    return "`" + name.replace("`", "``") + "`"


def _add_column_sql(table_name: str, column_name: str, column_type: str) -> str:
    return f"ALTER TABLE {_quote_identifier(table_name)} ADD COLUMN {_quote_identifier(column_name)} {column_type}"


# ================================ 并发安全加列 ================================ #


def _column_exists(engine: Engine, table_name: str, column_name: str) -> bool:
    """重新读取表结构，避免复用并发迁移前的 Inspector 缓存。"""

    return column_name in {
        column["name"]
        for column in inspect(engine).get_columns(table_name)
    }


def _is_duplicate_column_error(error: DBAPIError, dialect_name: str) -> bool:
    """只识别 MySQL 与 SQLite 明确表示“列已存在”的数据库错误。"""

    original = error.orig
    if dialect_name == "mysql":
        original_args = getattr(original, "args", ())
        return bool(original_args) and original_args[0] == 1060
    if dialect_name == "sqlite":
        return "duplicate column name" in str(original).casefold()
    return False


def _add_column_if_missing(
    engine: Engine,
    table_name: str,
    column_name: str,
    column_type: str,
) -> None:
    """幂等新增列，并容忍多实例同时执行同一条 ADD COLUMN。"""

    if _column_exists(engine, table_name, column_name):
        return
    try:
        with engine.begin() as conn:
            conn.execute(text(_add_column_sql(table_name, column_name, column_type)))
    except DBAPIError as error:
        if not _is_duplicate_column_error(error, engine.dialect.name):
            raise
        # 只在竞争实例确实已经完成加列时吞掉重复列错误，其他异常继续暴露。
        if not _column_exists(engine, table_name, column_name):
            raise


def _migrate_existing_user_usage(engine: Engine) -> None:
    """把旧 last_roast_ts 迁移为充能桶；多次执行应保持幂等。"""
    now_ts = int(time.time())
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT id, last_roast_ts, roast_charges, roast_charge_updated_ts "
                "FROM user_usage "
                "WHERE roast_charges IS NULL OR roast_charge_updated_ts IS NULL"
            )
        ).mappings()
        for row in rows:
            last_roast_ts = int(row["last_roast_ts"] or 0)
            if last_roast_ts <= 0:
                charges = DEFAULT_ROAST_CHARGE_MAX
                updated_ts = now_ts
            else:
                elapsed = max(0, now_ts - last_roast_ts)
                recovered = elapsed // DEFAULT_ROAST_CHARGE_RECOVER_SECONDS
                charges = min(DEFAULT_ROAST_CHARGE_MAX, 1 + recovered)
                updated_ts = (
                    now_ts
                    if charges >= DEFAULT_ROAST_CHARGE_MAX
                    else last_roast_ts + recovered * DEFAULT_ROAST_CHARGE_RECOVER_SECONDS
                )
            conn.execute(
                text(
                    "UPDATE user_usage "
                    "SET roast_charges = :charges, roast_charge_updated_ts = :updated_ts "
                    "WHERE id = :row_id"
                ),
                {"charges": int(charges), "updated_ts": int(updated_ts), "row_id": row["id"]},
            )


def _migrate_ambiguous_roast_reservations(engine: Engine) -> None:
    """把旧版可能已发送的 processing 记录转为不可自动重领的 sending。"""

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE roast_reservations SET status = 'sending' "
                "WHERE status = 'processing' AND outcome_snapshot IS NOT NULL"
            )
        )


def ensure_runtime_migrations(
    engine: Engine,
    *,
    backfill_group_activity: bool = True,
    activity_start: dt.date | None = None,
    activity_end: dt.date | None = None,
) -> None:
    """执行轻量运行期迁移，并按需回填最近群日活数据。"""
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "daily_rolls" in table_names:
        additions = {
            "is_new_pig": "BOOLEAN NULL",
            "previous_copies": "INTEGER NULL",
            "copies_after_roll": "INTEGER NULL",
            "collection_size_after_roll": "INTEGER NULL",
            "previous_duplicate_streak": "INTEGER NULL",
            "duplicate_streak_after_roll": "INTEGER NULL",
            "resource_version": "VARCHAR(192) NULL",
            "appearance_snapshot": "JSON NULL",
        }
        for column_name, column_type in additions.items():
            _add_column_if_missing(engine, "daily_rolls", column_name, column_type)

    if "user_usage" in table_names:
        _add_column_if_missing(engine, "user_usage", "roast_charges", "INTEGER NULL")
        _add_column_if_missing(engine, "user_usage", "roast_charge_updated_ts", "BIGINT NULL")
        _migrate_existing_user_usage(engine)

    if "roast_events" in table_names:
        _add_column_if_missing(
            engine,
            "roast_events",
            "reservation_id",
            "VARCHAR(64) NOT NULL DEFAULT ''",
        )
        _add_column_if_missing(engine, "roast_events", "participant_snapshot", "JSON NULL")

    if "roast_reservations" in table_names:
        _migrate_ambiguous_roast_reservations(engine)

    if "daily_report_deliveries" in table_names:
        # 领取次数与下次可领取时间是安全重试的持久化依据；已有记录从零次开始，
        # 不能因为升级迁移被误判为已经耗尽重试额度。
        _add_column_if_missing(
            engine,
            "daily_report_deliveries",
            "attempt_count",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _add_column_if_missing(
            engine,
            "daily_report_deliveries",
            "next_attempt_at",
            "DATETIME NULL",
        )

    # 新增群日活表时只回填上海业务日期的今天与昨天：既覆盖跨日部署，
    # 又避免 Cloud 每次启动扫描全部历史记录。调用方负责仅在首次建表时开启回填。
    table_names = set(inspect(engine).get_table_names())
    if backfill_group_activity and "group_daily_active_users" in table_names:
        business_today = dt.datetime.now(ROLLPIG_TIMEZONE).date()
        start_date = activity_start or business_today - dt.timedelta(days=1)
        end_date = activity_end or business_today
        date_params = {"activity_start": start_date, "activity_end": end_date}
        # 多实例首次启动可能同时判断为需要回填；数据库原生冲突忽略负责兜底唯一键竞争。
        insert_clause = {
            "mysql": "INSERT IGNORE INTO",
            "sqlite": "INSERT OR IGNORE INTO",
        }.get(engine.dialect.name, "INSERT INTO")
        with engine.begin() as conn:
            if "group_rolls" in table_names:
                conn.execute(text(
                    f"{insert_clause} group_daily_active_users (date_str, group_id, user_id, active_at) "
                    "SELECT DISTINCT source.date_str, source.group_id, source.user_id, CURRENT_TIMESTAMP "
                    "FROM group_rolls AS source "
                    "WHERE source.date_str BETWEEN :activity_start AND :activity_end "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM group_daily_active_users AS active "
                    "WHERE active.date_str = source.date_str "
                    "AND active.group_id = source.group_id "
                    "AND active.user_id = source.user_id)"
                ), date_params)
            if "roast_events" in table_names:
                conn.execute(text(
                    f"{insert_clause} group_daily_active_users (date_str, group_id, user_id, active_at) "
                    "SELECT DISTINCT source.date_str, source.group_id, source.user_id, CURRENT_TIMESTAMP "
                    "FROM ("
                    "SELECT date_str, group_id, attacker_id AS user_id FROM roast_events "
                    "WHERE group_id <> '' AND attacker_id <> '' "
                    "UNION "
                    "SELECT date_str, group_id, target_id AS user_id FROM roast_events "
                    "WHERE group_id <> '' AND target_id <> '' AND event_type <> 'bot_backfire'"
                    ") AS source "
                    "WHERE source.date_str BETWEEN :activity_start AND :activity_end "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM group_daily_active_users AS active "
                    "WHERE active.date_str = source.date_str "
                    "AND active.group_id = source.group_id "
                    "AND active.user_id = source.user_id)"
                ), date_params)
            if {"roast_reservations", "roast_reservation_participants"}.issubset(table_names):
                conn.execute(text(
                    f"{insert_clause} group_daily_active_users (date_str, group_id, user_id, active_at) "
                    "SELECT DISTINCT reservation.date_str, reservation.group_id, participant.user_id, CURRENT_TIMESTAMP "
                    "FROM roast_reservations AS reservation "
                    "JOIN roast_reservation_participants AS participant "
                    "ON participant.reservation_id = reservation.reservation_id "
                    "WHERE reservation.date_str BETWEEN :activity_start AND :activity_end "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM group_daily_active_users AS active "
                    "WHERE active.date_str = reservation.date_str "
                    "AND active.group_id = reservation.group_id "
                    "AND active.user_id = participant.user_id)"
                ), date_params)
