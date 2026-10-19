"""Runtime configuration.

Every setting comes from an environment variable prefixed with ``LOOPDESK_`` so the
same image runs in dev, CI and production. Provider presets cover the OpenAI-compatible
endpoints of the Chinese model vendors, so switching from the offline mock to Doubao or
Hunyuan is a two-line change in ``.env``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

# Base URLs of OpenAI-compatible chat endpoints. Model IDs change often, so they are
# never hard-coded here: copy them from each vendor's console into LOOPDESK_LLM_MODEL_*.
PROVIDER_PRESETS: dict[str, str] = {
    "doubao": "https://ark.cn-beijing.volces.com/api/v3",  # 字节 · 火山方舟
    "hunyuan": "https://api.hunyuan.cloud.tencent.com/v1",  # 腾讯 · 混元
    "deepseek": "https://api.deepseek.com/v1",
    "qwen": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "openai": "https://api.openai.com/v1",
}

# Example prices in CNY per 1M tokens (input, output). They only drive the cost column
# of traces; override with LOOPDESK_PRICES='{"model": [in, out]}' using your contract.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "mock-fast": (0.3, 0.6),
    "mock-strong": (2.0, 8.0),
}


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(f"LOOPDESK_{name}")
    return value if value not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    return float(value) if value is not None else default


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    return int(value) if value is not None else default


@dataclass
class Settings:
    database_url: str = "sqlite:///./loopdesk.db"
    jwt_secret: str = "dev-only-insecure-secret-change-me-in-prod"
    jwt_expire_minutes: int = 720
    cors_origins: list[str] = field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])
    seed_demo: bool = True
    seed_demo_traffic: bool = True

    # LLM: mock | openai_compat | anthropic
    llm_provider: str = "mock"
    llm_preset: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model_fast: str = "mock-fast"
    llm_model_strong: str = "mock-strong"
    llm_timeout: float = 60.0
    llm_stream_usage: bool = True  # send stream_options.include_usage (disable if a vendor rejects it)
    anthropic_effort: str = "low"
    mock_stream_delay: float = 0.0  # seconds between streamed chunks of the mock model

    # Embeddings: hashing (offline) | openai_compat
    embedding_provider: str = "hashing"
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_model: str | None = None
    embedding_dim: int = 512

    # Agent behaviour
    max_agent_steps: int = 6
    history_turns: int = 6
    retrieval_top_k: int = 4
    min_relevance: float = 0.3
    evidence_mode: str = "annotate"  # annotate | strict
    evidence_pass_coverage: float = 0.8
    sentence_support_threshold: float = 0.5
    verifier: str = "lexical"  # lexical | llm
    pii_masking: bool = True
    rate_limit_per_minute: int = 30
    # Velocity limits for automatic refunds (anti-abuse): a run of small refunds that each
    # stay under the per-refund limit still goes to a human once these are exceeded.
    refund_window_days: int = 30
    refund_window_max_count: int = 3
    refund_window_max_amount: float = 200.0
    answer_cache_ttl_hours: float = 24.0  # 0 disables the answer cache

    # Channels
    feishu_app_id: str | None = None
    feishu_app_secret: str | None = None
    feishu_verification_token: str | None = None
    wecom_webhook_key: str | None = None

    prices: dict[str, tuple[float, float]] = field(default_factory=lambda: dict(DEFAULT_PRICES))

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.database_url = _env("DATABASE_URL", s.database_url)
        s.jwt_secret = _env("JWT_SECRET", s.jwt_secret)
        s.jwt_expire_minutes = _env_int("JWT_EXPIRE_MINUTES", s.jwt_expire_minutes)
        origins = _env("CORS_ORIGINS")
        if origins:
            s.cors_origins = [o.strip() for o in origins.split(",") if o.strip()]
        s.seed_demo = _env_bool("SEED_DEMO", s.seed_demo)
        s.seed_demo_traffic = _env_bool("SEED_DEMO_TRAFFIC", s.seed_demo_traffic)

        s.llm_provider = _env("LLM_PROVIDER", s.llm_provider)
        s.llm_preset = _env("LLM_PRESET")
        s.llm_base_url = _env("LLM_BASE_URL") or PROVIDER_PRESETS.get(s.llm_preset or "")
        s.llm_api_key = _env("LLM_API_KEY")
        default_fast = s.llm_model_fast
        default_strong = s.llm_model_strong
        if s.llm_provider == "anthropic":
            default_fast = default_strong = "claude-opus-5-5"
        s.llm_model_strong = _env("LLM_MODEL_STRONG", default_strong)
        s.llm_model_fast = _env("LLM_MODEL_FAST", default_fast if s.llm_provider in ("mock", "anthropic") else s.llm_model_strong)
        s.llm_timeout = _env_float("LLM_TIMEOUT", s.llm_timeout)
        s.llm_stream_usage = _env_bool("LLM_STREAM_USAGE", s.llm_stream_usage)
        s.anthropic_effort = _env("ANTHROPIC_EFFORT", s.anthropic_effort)
        s.mock_stream_delay = _env_float("MOCK_STREAM_DELAY", s.mock_stream_delay)

        s.embedding_provider = _env("EMBEDDING_PROVIDER", s.embedding_provider)
        s.embedding_base_url = _env("EMBEDDING_BASE_URL", s.llm_base_url)
        s.embedding_api_key = _env("EMBEDDING_API_KEY", s.llm_api_key)
        s.embedding_model = _env("EMBEDDING_MODEL")
        s.embedding_dim = _env_int("EMBEDDING_DIM", s.embedding_dim)

        s.max_agent_steps = _env_int("MAX_AGENT_STEPS", s.max_agent_steps)
        s.history_turns = _env_int("HISTORY_TURNS", s.history_turns)
        s.retrieval_top_k = _env_int("RETRIEVAL_TOP_K", s.retrieval_top_k)
        s.min_relevance = _env_float("MIN_RELEVANCE", s.min_relevance)
        s.evidence_mode = _env("EVIDENCE_MODE", s.evidence_mode)
        s.evidence_pass_coverage = _env_float("EVIDENCE_PASS_COVERAGE", s.evidence_pass_coverage)
        s.sentence_support_threshold = _env_float("SENTENCE_SUPPORT_THRESHOLD", s.sentence_support_threshold)
        s.verifier = _env("VERIFIER", s.verifier)
        s.pii_masking = _env_bool("PII_MASKING", s.pii_masking)
        s.rate_limit_per_minute = _env_int("RATE_LIMIT_PER_MINUTE", s.rate_limit_per_minute)
        s.refund_window_days = _env_int("REFUND_WINDOW_DAYS", s.refund_window_days)
        s.refund_window_max_count = _env_int("REFUND_WINDOW_MAX_COUNT", s.refund_window_max_count)
        s.refund_window_max_amount = _env_float("REFUND_WINDOW_MAX_AMOUNT", s.refund_window_max_amount)
        s.answer_cache_ttl_hours = _env_float("ANSWER_CACHE_TTL_HOURS", s.answer_cache_ttl_hours)

        s.feishu_app_id = _env("FEISHU_APP_ID")
        s.feishu_app_secret = _env("FEISHU_APP_SECRET")
        s.feishu_verification_token = _env("FEISHU_VERIFICATION_TOKEN")
        s.wecom_webhook_key = _env("WECOM_WEBHOOK_KEY")

        extra_prices = _env("PRICES")
        if extra_prices:
            for model, pair in json.loads(extra_prices).items():
                s.prices[model] = (float(pair[0]), float(pair[1]))
        return s

    def price_for(self, model: str) -> tuple[float, float]:
        return self.prices.get(model, (0.0, 0.0))


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings.from_env()
    return _settings


def set_settings(settings: Settings) -> None:
    """Swap the process-wide settings (used by the app factory and tests)."""
    global _settings
    _settings = settings
