"""集中管理配置：读取环境变量 / .env，统一使用 DATAPULSE_ 前缀。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DATAPULSE_", extra="ignore")

    # 数据库
    db_path: str = "data/tms_demo.db"
    max_rows: int = 200  # 单次查询最多返回的行数（SQL 守卫会自动注入 / 收紧 LIMIT）
    query_timeout_s: float = 5.0

    # 大模型（OpenAI 兼容接口：DeepSeek / Qwen / GPT 等）
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-chat"
    llm_temperature: float = 0.0

    # Embedding：hash = 离线字符 n-gram 哈希；openai = OpenAI 兼容 Embedding 接口
    embedding_provider: Literal["hash", "openai"] = "hash"
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "BAAI/bge-m3"

    # Schema Linking 与 Few-Shot 检索
    top_k_tables: int = 4
    top_k_examples: int = 3

    # 执行反馈自修复：首次生成之外最多再重写几轮
    max_repair_rounds: int = 3


@lru_cache
def get_settings() -> Settings:
    return Settings()
