from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass


ROLLPIG_TIMEZONE = dt.timezone(dt.timedelta(hours=8), "Asia/Shanghai")
KEY_NAME_MAX_LENGTH = 64


def rollpig_today() -> dt.date:
    """返回 RollPig 业务时区中的日期，避免依赖容器主机时区。"""

    return dt.datetime.now(ROLLPIG_TIMEZONE).date()


@dataclass(frozen=True)
class ApiKeyIdentity:
    """鉴权通过后可安全进入日志与统计的数据，不保存原始 Token。"""

    key_id: str
    name: str


def _token_key_id(token: str) -> str:
    """从高熵 Token 生成稳定指纹；更名不会拆分既有统计。"""

    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    return f"key-{digest[:16]}"


def _normalize_key_name(value: object) -> str:
    name = str(value).strip()
    if not name:
        raise ValueError("ROLLPIG_CLOUD_KEYS_JSON 中的 Key 名称不能为空")
    if len(name) > KEY_NAME_MAX_LENGTH:
        raise ValueError(f"ROLLPIG_CLOUD_KEYS_JSON 中的 Key 名称不能超过 {KEY_NAME_MAX_LENGTH} 个字符")
    if any(ord(character) < 32 for character in name):
        raise ValueError("ROLLPIG_CLOUD_KEYS_JSON 中的 Key 名称不能包含控制字符")
    return name


def parse_api_keys(named_keys_raw: str, legacy_tokens_raw: str) -> dict[str, ApiKeyIdentity]:
    """合并具名 Key 与旧 Token 列表；具名配置优先且保持旧部署兼容。"""

    identities: dict[str, ApiKeyIdentity] = {}
    if named_keys_raw.strip():
        try:
            named_keys = json.loads(named_keys_raw)
        except json.JSONDecodeError as exc:
            raise ValueError("ROLLPIG_CLOUD_KEYS_JSON 必须是合法 JSON 对象") from exc
        if not isinstance(named_keys, dict):
            raise ValueError("ROLLPIG_CLOUD_KEYS_JSON 必须是使用 {名称: Token} 格式的 JSON 对象")
        for raw_name, raw_token in named_keys.items():
            name = _normalize_key_name(raw_name)
            if not isinstance(raw_token, str) or not raw_token.strip():
                raise ValueError(f"ROLLPIG_CLOUD_KEYS_JSON 中 {name!r} 对应的 Token 不能为空")
            token = raw_token.strip()
            if token in identities:
                raise ValueError("ROLLPIG_CLOUD_KEYS_JSON 不能为同一个 Token 配置多个名称")
            identities[token] = ApiKeyIdentity(key_id=_token_key_id(token), name=name)

    # 旧变量继续生效；如果同一个 Token 已有名称，不让旧列表覆盖它。
    for raw_token in legacy_tokens_raw.split(","):
        token = raw_token.strip()
        if token and token not in identities:
            key_id = _token_key_id(token)
            identities[token] = ApiKeyIdentity(key_id=key_id, name=key_id)
    return identities


class Settings:
    def __init__(self):
        self.database_url = os.getenv(
            "ROLLPIG_CLOUD_DATABASE_URL",
            "mysql+pymysql://root:password@127.0.0.1:3306/rollpig_cloud?charset=utf8mb4",
        )
        self.api_keys = parse_api_keys(
            os.getenv("ROLLPIG_CLOUD_KEYS_JSON", ""),
            os.getenv("ROLLPIG_CLOUD_TOKENS", ""),
        )
        # 保留 tokens 属性，兼容仍会读取它的部署检查或外部脚本。
        self.tokens = set(self.api_keys)
        self.host = os.getenv("ROLLPIG_CLOUD_HOST", "0.0.0.0")
        self.port = int(os.getenv("ROLLPIG_CLOUD_PORT", "8011"))
        self.default_tenant_id = os.getenv("ROLLPIG_CLOUD_DEFAULT_TENANT_ID", "felis-main")


settings = Settings()
