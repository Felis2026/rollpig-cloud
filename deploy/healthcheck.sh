#!/bin/sh
set -eu

container_name="${1:-felis_rollpig_cloud}"
max_attempts=20
attempt=1

# 容器启动和数据库轻量迁移都可能需要几秒；在服务器侧等待健康接口，
# 比只看 docker compose 的 started 状态更能确认新版本确实可用。
while [ "$attempt" -le "$max_attempts" ]; do
    if docker exec "$container_name" python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8011/healthz', timeout=3)" >/dev/null 2>&1; then
        echo "rollpig-cloud healthcheck passed: container=$container_name attempt=$attempt"
        exit 0
    fi
    sleep 2
    attempt=$((attempt + 1))
done

echo "rollpig-cloud healthcheck failed: container=$container_name" >&2
docker logs --tail 100 "$container_name" >&2 || true
exit 1
