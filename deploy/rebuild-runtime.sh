#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(dirname "$script_dir")

cd "$project_dir"
docker compose config --quiet

# 仅在 requirements.lock、Dockerfile 或 Python 基础镜像变化时执行。
# static 已排除在构建上下文外，不会再重复打包资源图片。
docker compose build --pull felis_rollpig_cloud
docker compose up -d --no-deps --force-recreate felis_rollpig_cloud
sh "$script_dir/healthcheck.sh" felis_rollpig_cloud
