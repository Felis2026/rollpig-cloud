from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from ..config import ApiKeyIdentity, rollpig_today
from ..db import SessionLocal
from ..models import SourceKeyDailyUsage, SourceKeyIdentity


WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _upsert_identity(session: Session, identity: ApiKeyIdentity) -> None:
    """登记当前名称；Key 改名后沿用稳定指纹，不拆分历史统计。"""

    values = {"key_id": identity.key_id, "key_name": identity.name}
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "mysql":
        statement = mysql_insert(SourceKeyIdentity).values(**values)
        session.execute(
            statement.on_duplicate_key_update(
                key_name=identity.name,
                last_seen_at=func.now(),
            )
        )
        return
    if dialect_name == "sqlite":
        statement = sqlite_insert(SourceKeyIdentity).values(**values)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[SourceKeyIdentity.key_id],
                set_={"key_name": identity.name, "last_seen_at": func.now()},
            )
        )
        return

    row = session.get(SourceKeyIdentity, identity.key_id)
    if row is None:
        session.add(SourceKeyIdentity(**values))
    else:
        row.key_name = identity.name
        row.last_seen_at = func.now()


def _upsert_daily_usage(
    session: Session,
    *,
    identity: ApiKeyIdentity,
    operation: str,
    authenticated_requests: int = 0,
    successful_requests: int = 0,
    successful_mutations: int = 0,
    failed_requests: int = 0,
    created_records: int = 0,
    updated_records: int = 0,
    deleted_records: int = 0,
    idempotent_hits: int = 0,
) -> None:
    """原子累计单日调用与业务结果，避免多 worker 并发时丢失计数。"""

    values = {
        "date_str": rollpig_today(),
        "source_key_id": identity.key_id,
        "operation": operation,
        "authenticated_requests": authenticated_requests,
        "successful_requests": successful_requests,
        "successful_mutations": successful_mutations,
        "failed_requests": failed_requests,
        "created_records": created_records,
        "updated_records": updated_records,
        "deleted_records": deleted_records,
        "idempotent_hits": idempotent_hits,
    }
    increments = {
        "authenticated_requests": authenticated_requests,
        "successful_requests": successful_requests,
        "successful_mutations": successful_mutations,
        "failed_requests": failed_requests,
        "created_records": created_records,
        "updated_records": updated_records,
        "deleted_records": deleted_records,
        "idempotent_hits": idempotent_hits,
    }
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "mysql":
        statement = mysql_insert(SourceKeyDailyUsage).values(**values)
        session.execute(
            statement.on_duplicate_key_update(
                **{
                    field: getattr(SourceKeyDailyUsage, field) + increment
                    for field, increment in increments.items()
                },
                updated_at=func.now(),
            )
        )
        return
    if dialect_name == "sqlite":
        statement = sqlite_insert(SourceKeyDailyUsage).values(**values)
        session.execute(
            statement.on_conflict_do_update(
                index_elements=[
                    SourceKeyDailyUsage.date_str,
                    SourceKeyDailyUsage.source_key_id,
                    SourceKeyDailyUsage.operation,
                ],
                set_={
                    **{
                        field: getattr(SourceKeyDailyUsage, field) + increment
                        for field, increment in increments.items()
                    },
                    "updated_at": func.now(),
                },
            )
        )
        return

    row = session.execute(
        select(SourceKeyDailyUsage).where(
            SourceKeyDailyUsage.date_str == values["date_str"],
            SourceKeyDailyUsage.source_key_id == identity.key_id,
            SourceKeyDailyUsage.operation == operation,
        ).with_for_update()
    ).scalar_one_or_none()
    if row is None:
        session.add(SourceKeyDailyUsage(**values))
    else:
        for field, increment in increments.items():
            setattr(row, field, int(getattr(row, field)) + increment)


def record_key_request(
    identity: ApiKeyIdentity,
    *,
    method: str,
    operation: str,
    status_code: int,
    session_factory: Callable[[], Session] = SessionLocal,
) -> None:
    """独立提交访问统计；统计故障由调用方隔离，不能影响业务 API。"""

    session = session_factory()
    try:
        successful = status_code < 400
        _upsert_identity(session, identity)
        _upsert_daily_usage(
            session,
            identity=identity,
            operation=operation[:192],
            authenticated_requests=1,
            successful_requests=int(successful),
            successful_mutations=int(successful and method.upper() in WRITE_METHODS),
            failed_requests=int(not successful),
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def record_key_mutation_outcome(
    session: Session,
    identity: ApiKeyIdentity | object,
    *,
    operation: str,
    created_records: int = 0,
    updated_records: int = 0,
    deleted_records: int = 0,
    idempotent_hits: int = 0,
) -> None:
    """在业务事务内累计真实结果；业务回滚时统计随之回滚。"""

    # 路由函数也会被单元测试直接调用，此时 FastAPI 的 Depends 尚未解析。
    if not isinstance(identity, ApiKeyIdentity):
        return
    counters = (created_records, updated_records, deleted_records, idempotent_hits)
    if any(counter < 0 for counter in counters):
        raise ValueError("Key 用量统计不能写入负数")
    _upsert_identity(session, identity)
    _upsert_daily_usage(
        session,
        identity=identity,
        operation=operation[:192],
        created_records=created_records,
        updated_records=updated_records,
        deleted_records=deleted_records,
        idempotent_hits=idempotent_hits,
    )
