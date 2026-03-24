from __future__ import annotations

import os


class Settings:
    def __init__(self):
        self.database_url = os.getenv(
            "ROLLPIG_CLOUD_DATABASE_URL",
            "mysql+pymysql://root:password@127.0.0.1:3306/rollpig_cloud?charset=utf8mb4",
        )
        tokens_raw = os.getenv("ROLLPIG_CLOUD_TOKENS", "")
        self.tokens = {token.strip() for token in tokens_raw.split(",") if token.strip()}
        self.host = os.getenv("ROLLPIG_CLOUD_HOST", "0.0.0.0")
        self.port = int(os.getenv("ROLLPIG_CLOUD_PORT", "8011"))


settings = Settings()
