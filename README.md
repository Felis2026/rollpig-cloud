<div align="center">
  <img src="https://raw.githubusercontent.com/Felis2026/nonebot-plugin-rollpig-plus/refs/heads/main/docs/assets/logo.jpeg" width="180" alt="RollPig Logo">

  <h1>🐖 RollPig Cloud 🐖</h1>

  <p><strong>RollPig Plus 的可选云端服务</strong></p>
  <p>统一存储多 Bot 的成长状态与群数据，并托管上游原版和 RollPig Plus 使用的静态资源。</p>

  <p>
    <img src="https://img.shields.io/badge/Python-3.10%2B-blue" alt="Python >= 3.10">
    <img src="https://img.shields.io/badge/FastAPI-0.110%2B-009688" alt="FastAPI >= 0.110">
    <img src="https://img.shields.io/badge/License-MIT-green" alt="MIT License">
  </p>
</div>

<p align="center">
  <a href="https://github.com/Bearlele/nonebot-plugin-rollpig">上游原作</a> ·
  <a href="https://github.com/Felis2026/nonebot-plugin-rollpig-plus">Plus</a> ·
  <a href="https://github.com/Felis2026/rollpig-cloud">Cloud</a> ·
  <a href="https://github.com/Felis2026/rollpig-resources">Resources</a>
</p>

> **什么时候需要部署？** 单 Bot 使用本地存储时不需要 RollPig Cloud。只有多个 Bot 需要共享抽猪记录、图鉴成长、烤群友充能、预约烤猪、烤箱补货或群日报状态时，才需要启用云端存储。

## ✨ 功能概述

- **多 Bot 状态同步**：保存 `daily_rolls`、`draw_state`、`collections` 等用户成长数据。
- **群维度数据**：保存 `group_rolls`、群保护状态、活跃群列表与日报所需聚合数据。
- **烤群友充能**：为普通烤群友提供服务端次数存储与冷却恢复。
- **预约烤猪**：处理预约创建、多人加入、首次抽猪激活、跨 Bot 领取与固定结果重试。
- **烤箱补货**：保存群日活与投票申请，并在单个事务中验票、批量恢复普通烧烤配额。
- **昨日抽取快照**：冻结每日抽取时的成长结果、资源版本与 EX 外观，供新版“昨日小猪”准确回放。
- **图鉴快照接口**：为 RollPig Plus 图片版图鉴聚合收藏、近 14 天抽猪与近 7 天被烤数据。
- **静态资源托管**：通过 `/resources/...` 暴露来自 `rollpig-resources` 的远端资源包。
- **共享文案托管**：通过 `/resources/rollpig-roasts/...` 提供审核后的只读烤猪文案，不进入数据库。

### 组件关系

| 访问方 | 接口或路径 | 用途 |
| --- | --- | --- |
| RollPig Plus | `/v1/...` | 多 Bot 用户状态、群数据与充能同步 |
| 上游原版 / RollPig Plus | `/resources/...` | 公有小猪资源与官方 Overlay 下载 |
| RollPig Plus 0.10.0+ | `/resources/rollpig-roasts/...` | 共享烤猪文案快照下载 |
| RollPig Cloud | MySQL | 持久化用户状态、群事件与聚合数据 |

## 🔗 客户端兼容

| 客户端 | 推荐组合 | 说明 |
| --- | --- | --- |
| 支持新版“昨日小猪”的 RollPig Plus | Cloud `0.5.0+` | 完整保存每日抽取成长、资源版本与 EX 外观快照，并支持按用户查询昨日相关事件。 |
| RollPig Plus `0.11.x` | Cloud `0.4.x` 或 `0.5.x` | 多 Bot 状态同步、预约烤猪与烤箱补货保持兼容；升级 Cloud 不要求旧客户端同步升级。 |
| RollPig Plus 旧版本 | Cloud `0.5.x` | 既有请求与响应字段继续保留；客户端不使用的新接口不会影响旧玩法。 |
| 上游原版 RollPig | 不需要 Cloud | 原版可直接读取静态资源，不使用 Cloud 的 `/v1` 状态接口。 |

Cloud 只保存服务端数据；替换 Cloud 容器或代码不会删除 MySQL 数据，但删除数据库、切换租户 ID 或清理数据库卷都会影响已有成长记录。正式升级前请先备份数据库。

## ⚙️ 环境变量

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

## 🚀 快速部署

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
ROLLPIG_DEPLOY_ROOT
ROLLPIG_DEPLOY_SSH_KEY
ROLLPIG_DEPLOY_KNOWN_HOSTS
```

可选在 `production` Environment 中配置变量 `ROLLPIG_DEPLOY_PUBLIC_URL`，例如 `https://pig.felislab.cc/api/healthz`，用于部署完成后验证公网反向代理链路。工作流会同时校验响应 JSON 中的 `ok=true`，避免前端页面返回 HTTP 200 时被误判为 Cloud 健康。

工作流不会上传 `docker-compose.yml`、Token、数据库地址或资源目录。服务器上的生产配置和资源由宿主机继续保管，GitHub 仅同步可公开的 Cloud 代码、部署脚本与依赖描述。

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

## 🧭 API 路由表

### 健康检查

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/healthz` | 服务健康检查 |

### 每日抽猪与抽取状态

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/daily-rolls/get-or-create` | 获取或创建用户某天的今日小猪 |
| `GET` | `/v1/daily-rolls/by-date` | 按用户与日期查询今日小猪及可用的历史结果快照 |
| `PUT` | `/v1/daily-rolls/snapshot` | 幂等补全客户端实际解析的资源版本与 EX 外观快照 |
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
| `GET` | `/v1/events` | 按日期、群或用户查询有序事件列表 |
| `GET` | `/v1/groups/active` | 查询有活动记录的群列表 |

### 冷却、保护与强制次数

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/cooldowns/consume-roast` | 消耗普通烤群友充能，返回剩余次数与恢复时间 |
| `POST` | `/v1/cooldowns/consume-force` | 消耗强制类操作次数 |
| `POST` | `/v1/protections/replace-group` | 替换群保护名单 |
| `GET` | `/v1/protections/check` | 检查用户是否在群保护名单中 |

### 预约烤猪

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/roast-reservations/unrolled-attempt` | 记录用户当天未抽猪先烤的违规次数 |
| `POST` | `/v1/roast-reservations/prepare` | 原子检查目标、群保护、预约状态并消费创建资源或免费加入 |
| `GET` | `/v1/roast-reservations/owned` | 查询指定 Bot 当天是否持有未完成预约 |
| `POST` | `/v1/roast-reservations/claim` | 原子领取可投递预约，并在同一响应返回当前 Bot 是否仍持有未完成预约 |
| `POST` | `/v1/roast-reservations/outcome/prepare` | 幂等保存固定结果并进入可安全重领的 `prepared` |
| `POST` | `/v1/roast-reservations/sending` | 幂等提交发送意图；进入后不再自动释放或重领 |
| `POST` | `/v1/roast-reservations/outcome` | 兼容旧 Plus：保存固定结果并直接进入 `sending` |
| `POST` | `/v1/roast-reservations/complete` | 幂等完成已发送预约 |
| `POST` | `/v1/roast-reservations/release` | 外部发送前释放 `processing/prepared`，保留固定结果等待重试 |

### 烤箱补货

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/v1/group-roast-refills/active-users/mark` | 幂等登记本群当日 RollPig 活跃玩家 |
| `GET` | `/v1/group-roast-refills/active-users` | 查询本群当日活跃玩家集合 |
| `POST` | `/v1/group-roast-refills/prepare` | 冻结活跃人数门槛并原子创建或返回当前申请 |
| `POST` | `/v1/group-roast-refills/bind-message` | 绑定 QQ 投票消息 ID |
| `GET` | `/v1/group-roast-refills/active` | 查询并懒过期当前群申请 |
| `POST` | `/v1/group-roast-refills/fail` | 将不可继续验票的申请标记失败 |
| `POST` | `/v1/group-roast-refills/complete` | 按最新日活原子验票、按插件配置上限批量恢复配额并完成申请 |

新版 Plus 会在 `/prepare` 中声明 `capped-v1` 门槛能力：以发起瞬间的活跃人数为固定分母，四档依次使用 25% / 35% / 45% / 55%，并分别封顶为 8 / 12 / 16 / 20 票。未声明该能力的旧版 Plus 继续使用原五档算法，因此可以先升级 Cloud，再滚动升级各 Bot。

## 🔄 数据迁移

从旧本地 JSON 导入云端：

```bash
poetry run python tools/import_legacy_json.py --file /path/to/pig_data.json
```

补齐成长状态与普通烤群友充能：

```bash
poetry run python tools/backfill_p1a_progress.py
poetry run python tools/migrate_roast_charges.py
```

服务启动时也会执行轻量运行期迁移：自动为旧 `user_usage` 表补齐充能列，并通过 SQLAlchemy `create_all` 新建预约、群日活与烤箱补货相关表；群日活表首次创建时只回填上海业务日期的今天与昨天，避免后续启动重复扫描全部历史记录。Cloud `0.5.0` 会为旧 `daily_rolls` 表幂等增加抽取结果与外观快照列；历史行保持为空，不会使用当前成长状态反向伪造过去的抽取结果。本次预约可靠性更新还会执行一次幂等、非破坏性的数据状态修复：旧版 `processing + outcome_snapshot` 记录统一冻结为 `sending`，避免升级后把一条可能已经发出的群消息自动重发。

预约结果由负责群聊的 Owner Bot 领取。只要本机仍持有未完成预约，就会在已有用户请求之外每 60 秒最多发起一次合并领取请求；该请求同时返回可投递项目和是否仍需继续轮询，没有预约时自动停止。领取后的状态依次为 `processing → prepared → sending → completed`：`processing/prepared` 可在租约过期后重领，`sending` 表示“已经提交外部发送意图、结果可能不确定”，永不自动重领。`prepare`、`sending` 与 `complete` 均允许同一领取凭证幂等重试。

## 📦 静态资源包

小猪资源包源文件不放在本仓库，统一维护在 [rollpig-resources](https://github.com/Felis2026/rollpig-resources)。

`rollpig-cloud` 只负责在部署环境中挂载并暴露 `/resources`，例如将外部资源目录挂载到容器内 `/app/static/resources` 后，可以访问：

```text
https://pig.felislab.cc/resources/rollpig/manifest.json
https://pig.felislab.cc/resources/rollpig-gif/manifest.json
https://pig.felislab.cc/resources/rollpig-pjsk/manifest.json
https://pig.felislab.cc/resources/rollpig-roasts/manifest.json
```

这样可以避免 cloud 服务代码仓库和资源仓库重复存储图片、manifest 与构建工具。

## 🔗 相关项目

- 上游原作：[Bearlele/nonebot-plugin-rollpig](https://github.com/Bearlele/nonebot-plugin-rollpig)
- RollPig Plus：[Felis2026/nonebot-plugin-rollpig-plus](https://github.com/Felis2026/nonebot-plugin-rollpig-plus)
- 小猪资源包：[Felis2026/rollpig-resources](https://github.com/Felis2026/rollpig-resources)
- PigHub：[pighub.top](https://pighub.top/)

## 📄 许可证

本项目使用 MIT License，详见 [LICENSE](./LICENSE)。
