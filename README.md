# rollpig-cloud

`rollpig-cloud` 是 [nonebot-plugin-rollpig-plus](https://github.com/Felis2026/nonebot-plugin-rollpig-plus) 的云端存储、状态同步与静态资源托管服务。

它负责把多个 Bot 实例的抽猪记录、图鉴成长、烤群友充能、群内日报与资源包访问统一到同一个后端。

## 功能概述

- **多 Bot 状态同步**：保存 `daily_rolls`、`draw_state`、`collections` 等用户成长数据。
- **群维度数据**：保存 `group_rolls`、群保护状态、活跃群列表与日报所需聚合数据。
- **烤群友充能**：为普通烤群友提供服务端次数存储与冷却恢复。
- **图鉴快照接口**：为 rollpig-plus 图片版图鉴聚合收藏、近 14 天抽猪与近 7 天被烤数据。
- **静态资源托管**：通过 `/resources/...` 暴露来自 `rollpig-resources` 的远端资源包。

## 环境变量

| 变量名 | 默认值 | 说明 |
| --- | --- | --- |
| `ROLLPIG_CLOUD_DATABASE_URL` | `mysql+pymysql://root:password@127.0.0.1:3306/rollpig_cloud?charset=utf8mb4` | MySQL 连接串 |
| `ROLLPIG_CLOUD_TOKENS` | 空 | Bearer Token 列表，多个 Token 用英文逗号分隔；未配置时 `/v1` 接口会拒绝服务 |
| `ROLLPIG_CLOUD_HOST` | `0.0.0.0` | Uvicorn 监听地址 |
| `ROLLPIG_CLOUD_PORT` | `8011` | Uvicorn 监听端口 |
| `ROLLPIG_CLOUD_DEFAULT_TENANT_ID` | `felis-main` | 默认租户 ID，用于成长状态与聚合接口；自建服务建议显式改成自己的稳定 ID |

所有 `/v1/...` API 都需要请求头：

```http
Authorization: Bearer <ROLLPIG_CLOUD_TOKENS 中的某个 Token>
```

`GET /healthz` 不需要鉴权，方便反代或容器健康检查。

`ROLLPIG_CLOUD_DEFAULT_TENANT_ID` 是数据租户命名空间，不是资源包名称。当前代码默认值是 `felis-main`，用于兼容 Felis 现有部署；自建服务可以改成自己的 ID。已有数据上线后不要随意更换，否则客户端会像切到新租户一样看不到旧成长数据。

## 快速部署

### Docker Compose（推荐）

首次部署先复制模板：

```bash
cp docker-compose.yml.example docker-compose.yml
```

然后编辑 `docker-compose.yml`，至少填写：

```yaml
environment:
  ROLLPIG_CLOUD_DATABASE_URL: "mysql+pymysql://user:password@mysql:3306/rollpig_cloud?charset=utf8mb4"
  ROLLPIG_CLOUD_TOKENS: "replace-with-token"
  ROLLPIG_CLOUD_DEFAULT_TENANT_ID: "felis-main"
```

首次部署或 Python 依赖发生变化时，构建运行时镜像并启动：

```bash
sh deploy/rebuild-runtime.sh
```

普通 `rollpig_cloud/` Python 代码更新后，不需要重新 build：

```bash
sh deploy/restart-code.sh
```

当前 Compose 会把宿主机的 `rollpig_cloud/` 和 `tools/` 只读挂载进容器。这样依赖镜像保持稳定，代码更新只需重启进程；只有修改 `requirements.lock`、基础镜像或 Dockerfile 时才需要重新构建。

`static/resources/` 同样使用只读挂载，资源包更新后立即对外可见，不需要 build，也不需要重启 Cloud。

### GitHub 自动部署

仓库内的 `.github/workflows/deploy-cloud.yml` 会在 `main` 的 Cloud 代码变化后执行语法检查，并把代码包上传到服务器。服务器由 `deploy/apply-github-package.sh` 完成预检、依赖变化检测、源码原子切换、健康检查和失败回滚。

GitHub 仓库需要创建名为 `production` 的 Environment，并配置：

```text
ROLLPIG_DEPLOY_HOST
ROLLPIG_DEPLOY_PORT
ROLLPIG_DEPLOY_USER
ROLLPIG_DEPLOY_SSH_KEY
ROLLPIG_DEPLOY_KNOWN_HOSTS
```

工作流不会上传 `docker-compose.yml`、Token、数据库地址或资源目录。服务器上的生产配置和资源由宿主机继续保管，GitHub 仅同步可公开的 Cloud 代码与依赖描述。

如果需要调整端口、容器名、内存限制或外部 Docker 网络，在 `docker-compose.yml` 中修改后执行：

```bash
docker compose up -d --no-build --force-recreate felis_rollpig_cloud
sh deploy/healthcheck.sh
```

### 手动运行

项目元数据继续使用 Poetry 管理，`requirements.txt` 记录直接依赖范围；生产 Docker 镜像读取经过启动验证的 `requirements.lock`，避免每次冷构建拉到不同的间接依赖版本。

```bash
poetry install
poetry run uvicorn rollpig_cloud.main:app --host 0.0.0.0 --port 8011
```

或：

```bash
pip install -r requirements.txt
uvicorn rollpig_cloud.main:app --host 0.0.0.0 --port 8011
```

### Docker 单容器

```bash
docker build -t rollpig-cloud .
docker run -d \
  --name rollpig-cloud \
  -v "$PWD/static/resources:/app/static/resources:ro" \
  -e ROLLPIG_CLOUD_DATABASE_URL='mysql+pymysql://user:pass@mysql:3306/rollpig_cloud?charset=utf8mb4' \
  -e ROLLPIG_CLOUD_TOKENS='replace-with-token' \
  -p 8011:8011 \
  rollpig-cloud
```

## API 路由表

### 健康检查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/healthz` | 服务健康检查 |

### 每日抽猪与抽取状态

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/daily-rolls/get-or-create` | 获取或创建用户某天的今日小猪 |
| `GET` | `/v1/daily-rolls/by-date` | 按用户与日期查询今日小猪 |
| `GET` | `/v1/daily-rolls/all` | 查询用户全部每日抽猪记录 |
| `GET` | `/v1/draw-state` | 查询用户抽取状态与重复计数 |

### 图鉴与收藏

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/v1/collections` | 查询用户收藏、EX 等级与图鉴进度 |
| `GET` | `/v1/catalog-snapshot` | 聚合图片版图鉴所需的用户快照数据 |

### 群记录、事件与日报

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/group-rolls/mark-seen` | 标记群内已见过某只小猪 |
| `GET` | `/v1/group-rolls` | 查询群内小猪记录 |
| `POST` | `/v1/events` | 写入抽猪、烤猪等事件 |
| `GET` | `/v1/events` | 查询事件列表 |
| `GET` | `/v1/groups/active` | 查询有活动记录的群列表 |

### 冷却、保护与强制次数

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/cooldowns/consume-roast` | 消耗普通烤群友充能，返回剩余次数与恢复时间 |
| `POST` | `/v1/cooldowns/consume-force` | 消耗强制类操作次数 |
| `POST` | `/v1/protections/replace-group` | 替换群保护名单 |
| `GET` | `/v1/protections/check` | 检查用户是否在群保护名单中 |

## 数据迁移

从旧本地 JSON 导入云端：

```bash
poetry run python tools/import_legacy_json.py --file /path/to/pig_data.json
```

补齐成长状态与普通烤群友充能：

```bash
poetry run python tools/backfill_p1a_progress.py
poetry run python tools/migrate_roast_charges.py
```

服务启动时也会执行轻量运行期迁移，自动为旧 `user_usage` 表补齐 `roast_charges` 与 `roast_charge_updated_ts` 列。脚本保留是为了上线前可手动确认与重复执行。

## 静态资源包

小猪资源包源文件不放在本仓库，统一维护在 [rollpig-resources](https://github.com/Felis2026/rollpig-resources)。

`rollpig-cloud` 只负责在部署环境中挂载并暴露 `/resources`，例如将外部资源目录挂载到容器内 `/app/static/resources` 后，可以访问：

```text
https://pig.felislab.cc/resources/rollpig/manifest.json
https://pig.felislab.cc/resources/rollpig-pjsk/manifest.json
```

这样可以避免 cloud 服务代码仓库和资源仓库重复存储图片、manifest 与构建工具。

## 相关项目

- PLUS 拓展版插件：[Felis2026/nonebot-plugin-rollpig-plus](https://github.com/Felis2026/nonebot-plugin-rollpig-plus)
- 云端资源包：[Felis2026/rollpig-resources](https://github.com/Felis2026/rollpig-resources)
- 原作插件：[Bearlele/nonebot-plugin-rollpig](https://github.com/Bearlele/nonebot-plugin-rollpig)
- PigHub：[pighub.top](https://pighub.top/)

## 许可证

本项目使用 MIT License，详见 [LICENSE](./LICENSE)。
