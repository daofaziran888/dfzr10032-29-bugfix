"""运行配置：端口、跨域、运行环境。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    app_name: str = "多源数据采集与RPA自动化平台"
    env: str = "local"
    port: int = 8000
    # 采集登记迁移库：独立于内存样本表的 SQLite 文件，重启后保留已确认版本
    migration_db: str = field(
        default_factory=lambda: os.environ.get(
            "MIGRATION_DB",
            str(Path(__file__).resolve().parent.parent / "data" / "migration.db"),
        )
    )
    allowed_origins: list[str] = field(
        default_factory=lambda: [
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ]
    )
    page_size_default: int = 20
    page_size_max: int = 200


settings = Settings()
