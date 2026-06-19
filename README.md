# rollpig-cloud

`nonebot-plugin-rollpig felis-dev` 的云端存储服务。

## 目标

- 保存全局 `daily_roll`
- 保存全局 `roast_cd / force_usage`
- 保存按群 `group_rolls / daily_summary / protection`
- 为多 Bot 提供统一数据底座

## 环境变量

- `ROLLPIG_CLOUD_DATABASE_URL`：MySQL 连接串，例如 `mysql+pymysql://user:pass@127.0.0.1:3306/rollpig_cloud?charset=utf8mb4`
- `ROLLPIG_CLOUD_TOKENS`：Bearer Token，多个用逗号分隔
- `ROLLPIG_CLOUD_HOST`：默认 `0.0.0.0`
- `ROLLPIG_CLOUD_PORT`：默认 `8011`
- `ROLLPIG_CLOUD_DEFAULT_TENANT_ID`：默认 `felis-main`，P1A 成长状态使用的默认租户

## 运行依赖

- Python `>=3.10`
- `fastapi>=0.110.0`
- `uvicorn[standard]>=0.29.0`
- `sqlalchemy>=2.0.29`
- `pymysql>=1.1.0`
- `cryptography>=42.0.0`

项目当前以 Poetry 管理依赖，定义文件为 `pyproject.toml`。  
同时仓库也提供了 `requirements.txt`，方便不使用 Poetry 的环境直接安装。

## 准备

如果你是首次部署，先执行：

```bash
cp docker-compose.yml.example docker-compose.yml
```

然后编辑 `docker-compose.yml`，填写你自己的：

- MySQL 连接串
- Bearer Token
- Docker 绑定端口 / 网络名（如有需要）

## 启动

```bash
poetry install
poetry run uvicorn rollpig_cloud.main:app --host 0.0.0.0 --port 8011
```

如果你不使用 Poetry，也可以这样安装：

```bash
pip install -r requirements.txt
uvicorn rollpig_cloud.main:app --host 0.0.0.0 --port 8011
```

## Docker Compose

1. 复制配置模板：

```bash
cp docker-compose.yml.example docker-compose.yml
```

2. 按实际环境填写 `docker-compose.yml`

至少需要改这两项：

```yaml
environment:
  ROLLPIG_CLOUD_DATABASE_URL: "mysql+pymysql://user:password@127.0.0.1:3306/rollpig_cloud?charset=utf8mb4"
  ROLLPIG_CLOUD_TOKENS: "replace-with-token"
  ROLLPIG_CLOUD_DEFAULT_TENANT_ID: "felis-main"
```

3. 启动服务：

```bash
docker compose up -d --build
```

如果你还需要调整端口、容器名或外部 Docker 网络，也一并在 `docker-compose.yml` 里修改。

## Docker（单容器）

```bash
docker build -t rollpig-cloud .
docker run -d \
  --name rollpig-cloud \
  -e ROLLPIG_CLOUD_DATABASE_URL='mysql+pymysql://user:pass@mysql:3306/rollpig_cloud?charset=utf8mb4' \
  -e ROLLPIG_CLOUD_TOKENS='replace-with-token' \
  -p 8011:8011 \
  rollpig-cloud
```

## Docker Compose 文件说明

仓库中的 `docker-compose.yml.example` 需要自行设定密码和 Token。  

## RollPig 静态资源包

小猪资源包源文件不放在本仓库，统一维护在独立仓库 `rollpig-resources`。

`rollpig-cloud` 只负责在部署环境中挂载并暴露 `/resources`，例如将外部资源目录挂载到容器内 `/app/static/resources` 后访问：

```text
https://pig.felislab.cc/resources/rollpig/manifest.json
```

这样可以避免 cloud 服务代码仓库和资源仓库重复存储图片、manifest 与构建工具。

## 迁移旧数据

```bash
poetry run python tools/import_legacy_json.py --file /path/to/pig_data.json
```
