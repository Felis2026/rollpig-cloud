from __future__ import annotations

import time

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


DEFAULT_ROAST_CHARGE_MAX = 2
DEFAULT_ROAST_CHARGE_RECOVER_SECONDS = 8 * 3600


def _quote_identifier(name: str) -> str:
    """按当前 cloud 只支持 MySQL/SQLite 的使用场景做最小标识符转义。"""
    return "`" + name.replace("`", "``") + "`"


def _add_column_sql(table_name: str, column_name: str, column_type: str) -> str:
    return f"ALTER TABLE {_quote_identifier(table_name)} ADD COLUMN {_quote_identifier(column_name)} {column_type}"


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


def ensure_runtime_migrations(engine: Engine) -> None:
    """执行轻量运行期迁移，专门兜底 SQLAlchemy create_all 不会补旧表列的问题。"""
    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    if "user_usage" not in table_names:
        return

    existing_columns = {column["name"] for column in inspector.get_columns("user_usage")}
    with engine.begin() as conn:
        if "roast_charges" not in existing_columns:
            conn.execute(text(_add_column_sql("user_usage", "roast_charges", "INTEGER NULL")))
        if "roast_charge_updated_ts" not in existing_columns:
            conn.execute(text(_add_column_sql("user_usage", "roast_charge_updated_ts", "BIGINT NULL")))

    _migrate_existing_user_usage(engine)
