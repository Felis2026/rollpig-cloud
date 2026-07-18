#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(dirname "$script_dir")

cd "$project_dir"
docker compose config --quiet

# 强制重建容器，确保自动部署通过目录原子替换源码后，Docker 会重新绑定新目录。
# 该操作复用现有依赖镜像，不会触发 build。
docker compose up -d --no-build --no-deps --force-recreate felis_rollpig_cloud
sh "$script_dir/healthcheck.sh" felis_rollpig_cloud
