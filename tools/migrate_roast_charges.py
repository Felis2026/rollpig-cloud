from __future__ import annotations

import sys
from pathlib import Path

# ================================ Direct Script Bootstrap ================================ #
# 容器里直接执行 `python tools/migrate_roast_charges.py` 时，先把仓库根目录加入 import path。
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rollpig_cloud.config import settings
from rollpig_cloud.db import init_db


def main() -> None:
    """补齐并回填普通烤群友充能列；脚本可重复执行。"""
    # init_db 内部会先 create_all，再执行轻量运行期迁移；这里不重复调迁移函数。
    init_db()
    print(f"migrate roast charges done: database={settings.database_url.split('@')[-1]}")


if __name__ == "__main__":
    main()
