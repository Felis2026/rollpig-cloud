from __future__ import annotations

import sys
from pathlib import Path

# ================================ Direct Script Bootstrap ================================ #
# Running `python tools/backfill_p1a_progress.py` only adds `tools/` to sys.path.
# Insert the project root explicitly so `rollpig_cloud` can be imported in the container.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import select

from rollpig_cloud.config import settings
from rollpig_cloud.db import SessionLocal, init_db
from rollpig_cloud.models import Collection, UserDrawState, UserPigProgress


def main():
    init_db()
    created_progress = 0
    created_draw_state = 0
    touched_users: set[str] = set()

    with SessionLocal() as session:
        # ================================ 图鉴进度回填 ================================ #
        # 旧 collections 只记录“拥有过”，没有重复次数。P1A 上线时先把每个已拥有猪
        # 初始化为 copies=1，后续真正重复抽到时再从 1 往上累加。
        collection_rows = session.execute(select(Collection)).scalars().all()
        for row in collection_rows:
            touched_users.add(row.user_id)
            existing = session.execute(
                select(UserPigProgress).where(
                    UserPigProgress.tenant_id == settings.default_tenant_id,
                    UserPigProgress.user_id == row.user_id,
                    UserPigProgress.pig_id == row.pig_id,
                )
            ).scalar_one_or_none()
            if existing:
                continue
            session.add(
                UserPigProgress(
                    tenant_id=settings.default_tenant_id,
                    user_id=row.user_id,
                    pig_id=row.pig_id,
                    copies=1,
                    first_obtained_at=row.first_seen_at,
                )
            )
            created_progress += 1

        # ================================ 抽卡状态回填 ================================ #
        # 历史数据无法可靠还原“连续重复次数”，因此统一从 0 开始，避免错误放大伪保底。
        for user_id in sorted(touched_users):
            existing = session.execute(
                select(UserDrawState).where(
                    UserDrawState.tenant_id == settings.default_tenant_id,
                    UserDrawState.user_id == user_id,
                )
            ).scalar_one_or_none()
            if existing:
                continue
            session.add(
                UserDrawState(
                    tenant_id=settings.default_tenant_id,
                    user_id=user_id,
                    duplicate_streak=0,
                )
            )
            created_draw_state += 1

        session.commit()

    print(
        "backfill p1a progress done: "
        f"tenant={settings.default_tenant_id} "
        f"progress_created={created_progress} "
        f"draw_state_created={created_draw_state}"
    )


if __name__ == "__main__":
    main()
