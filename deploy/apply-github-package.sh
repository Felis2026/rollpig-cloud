#!/bin/sh
set -eu

archive_path="${1:-}"
revision="${2:-}"
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
service_name="felis_rollpig_cloud"
image_name="rollpig-cloud:0.2.0-runtime"

# ================================ 输入与路径校验 ================================ #

case "$revision" in
    *[!0-9a-f]*|'')
        echo "invalid deployment revision: $revision" >&2
        exit 2
        ;;
esac

if [ "${#revision}" -ne 40 ] || [ ! -f "$archive_path" ]; then
    echo "deployment package missing or revision is not a full commit SHA" >&2
    exit 2
fi

if tar -tzf "$archive_path" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
    echo "deployment package contains unsafe path" >&2
    exit 2
fi

deploy_root="$project_dir/.deploy"
staging_dir="$deploy_root/staging/$revision"
backup_dir="$deploy_root/backup/$revision"

rm -rf "$staging_dir" "$backup_dir"
mkdir -p "$staging_dir" "$backup_dir"
tar -xzf "$archive_path" -C "$staging_dir"

for required_path in rollpig_cloud tools deploy/apply-github-package.sh deploy/healthcheck.sh Dockerfile requirements.lock; do
    if [ ! -e "$staging_dir/$required_path" ]; then
        echo "deployment package missing: $required_path" >&2
        exit 2
    fi
done

# ================================ 新代码预检与依赖构建 ================================ #

docker run --rm \
    -e PYTHONPYCACHEPREFIX=/tmp/rollpig-pycache \
    -v "$staging_dir/rollpig_cloud:/src/rollpig_cloud:ro" \
    -v "$staging_dir/tools:/src/tools:ro" \
    --entrypoint python \
    "$image_name" \
    -m compileall -q /src/rollpig_cloud /src/tools

old_image_id=$(docker inspect "$service_name" --format '{{.Image}}')
runtime_changed=false
if ! cmp -s "$staging_dir/Dockerfile" "$project_dir/Dockerfile" || \
   ! cmp -s "$staging_dir/requirements.lock" "$project_dir/requirements.lock"; then
    runtime_changed=true
fi

cp "$project_dir/Dockerfile" "$backup_dir/Dockerfile"
cp "$project_dir/requirements.lock" "$backup_dir/requirements.lock"
cp "$staging_dir/Dockerfile" "$project_dir/Dockerfile"
cp "$staging_dir/requirements.lock" "$project_dir/requirements.lock"

if [ "$runtime_changed" = true ]; then
    # 当前容器继续运行旧镜像；只有新依赖镜像完整构建成功后才进入切换阶段。
    if ! docker compose -f "$project_dir/docker-compose.yml" build --pull "$service_name"; then
        cp "$backup_dir/Dockerfile" "$project_dir/Dockerfile"
        cp "$backup_dir/requirements.lock" "$project_dir/requirements.lock"
        exit 1
    fi
fi

# ================================ 原子切换与失败回滚 ================================ #

for directory in rollpig_cloud tools deploy; do
    mv "$project_dir/$directory" "$backup_dir/$directory"
    mv "$staging_dir/$directory" "$project_dir/$directory"
done

if docker compose -f "$project_dir/docker-compose.yml" up -d --no-build --no-deps --force-recreate "$service_name" && \
   sh "$project_dir/deploy/healthcheck.sh" "$service_name"; then
    rm -rf "$staging_dir"
    rm -f "$archive_path"
    echo "rollpig-cloud deployed: revision=$revision runtime_changed=$runtime_changed"
    exit 0
fi

echo "rollpig-cloud deployment failed, rolling back: revision=$revision" >&2
for directory in rollpig_cloud tools deploy; do
    rm -rf "$project_dir/$directory"
    mv "$backup_dir/$directory" "$project_dir/$directory"
done
cp "$backup_dir/Dockerfile" "$project_dir/Dockerfile"
cp "$backup_dir/requirements.lock" "$project_dir/requirements.lock"
docker tag "$old_image_id" "$image_name"
docker compose -f "$project_dir/docker-compose.yml" up -d --no-build --no-deps --force-recreate "$service_name"
sh "$project_dir/deploy/healthcheck.sh" "$service_name"
exit 1
