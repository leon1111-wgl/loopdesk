"""Runtime configuration.

Every setting comes from an environment variable prefixed with ``LOOPDESK_`` so the
same image runs in dev, CI and production. Provider presets cover the OpenAI-compatible
endpoints of the Chinese model vendors, so switching from the offline mock to Doubao or
Hunyuan is a two-line change in ``.env``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields
from decimal import Decimal
from urllib.parse import urlsplit

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
@dataclass(frozen=True)
class FeishuApp:
    """One tenant's Feishu app. Each tenant gets its own: with one shared verification token,
    an event signed for one tenant was accepted at another tenant's URL, and every reply came
    from the same bot."""

    app_id: str
    app_secret: str
    verification_token: str


@dataclass(frozen=True)
class CommerceApi:
    """One tenant's own order and payment systems, reached over HTTP (app/integrations/http.py; the
    contract is in docs/DEPLOYMENT.md). A tenant without one uses the demo tables."""

    base_url: str
    token: str = field(repr=False)  # sent as "Authorization: Bearer <token>"
    timeout: float = 5.0  # seconds per phase of an attempt (connecting: at most 3)
    max_attempts: int = 3  # for idempotent calls only: reads, and refunds carrying their idempotency key
    signing_secret: str | None = field(default=None, repr=False)  # when set, requests carry an HMAC-SHA256 signature

    def __post_init__(self) -> None:
        url = urlsplit(self.base_url)
        local = url.hostname in ("localhost", "127.0.0.1", "::1")
        if url.scheme != "https" and not (url.scheme == "http" and local):
            # The token and the customers' order data would cross the network in clear text.
            raise ValueError(f"base_url must use https (plain http only to localhost): {self.base_url!r}")
        if not self.token:
            raise ValueError("token is required")
        if not 0 < self.timeout <= 60:
            raise ValueError("timeout must be between 0 and 60 seconds")
        if not 1 <= self.max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if self.timeout * self.max_attempts > 20:
            # A customer waits for these calls inside a run of 45 s, a refund turn makes two of them,
            # and the stale-run sweep has to allow for them (call_seconds).
            raise ValueError("timeout × max_attempts must be at most 20 seconds")

    @property
    def call_seconds(self) -> float:
        """The longest one call can take: each attempt connecting (3 s at most), sending and waiting
        for the reply (each phase up to the timeout), and a pause of at most 2 s (Retry-After) between
        attempts."""
        return self.max_attempts * (min(self.timeout, 3.0) + 2 * self.timeout) + 2.0 * (self.max_attempts - 1)


DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "mock-fast": (0.3, 0.6),
    "mock-strong": (2.0, 8.0),
}


# JWT secrets published in this repository (code default, .env.example, docker-compose.yml).
# Anyone can sign an admin token with them, so a production configuration refuses them.
PUBLIC_JWT_SECRETS = frozenset({
    "dev-only-insecure-secret-change-me-in-prod",
    "please-replace-with-a-long-random-string-32-bytes-min",
    "change-me-to-a-long-random-string-before-deploying",
})


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


_VERSION_NAME = re.compile(r"[vV]?(\d{1,3}(?:\.\d{1,2})?)")


def prompt_version_name(value: str | None) -> str | None:
    """Prompt versions are written 1.00, 2.00, 1.05: a rewrite takes the next whole number, a
    wording change adds 0.01. The earlier names (v1, v2) and short forms (2, 2.0) still name the
    same version, because existing PROMPT_VERSION settings and stored runs use them."""
    if value is None:
        return None
    match = _VERSION_NAME.fullmatch(value.strip())
    return f"{Decimal(match.group(1)):.2f}" if match else value.strip()


@dataclass
class Settings:
    database_url: str = "sqlite:///./loopdesk.db"
    jwt_secret: str = "dev-only-insecure-secret-change-me-in-prod"
    audit_key: str | None = None  # HMAC key for audit entries; derived from jwt_secret when unset
    jwt_expire_minutes: int = 720
    cors_origins: list[str] = field(default_factory=lambda: ["http://localhost:5173", "http://127.0.0.1:5173"])
    seed_demo: bool = True
    seed_demo_traffic: bool = True
    auto_migrate: bool = True  # run Alembic migrations at startup (tests use create_all)
    # LOOPDESK_AUTO_MIGRATE=0: a release job migrates (python -m app.ops.migrate, a Kubernetes Job);
    # this process leaves the schema alone and is not ready until it is at the latest revision.
    external_migrations: bool = False
    prewarm_index: bool = True  # build retrieval indexes in the background at startup; /api/ready waits for it
    log_format: str = "json"  # json | text
    log_level: str = "INFO"

    # LLM: mock | openai_compat | anthropic
    llm_provider: str = "mock"
    llm_preset: str | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model_fast: str = "mock-fast"
    llm_model_strong: str = "mock-strong"
    llm_timeout: float = 60.0
    # OpenAI-compatible streaming: the longest the model may send nothing (before the first token,
    # or between chunks). A hanging model then fails in seconds, without retries, and the other tier
    # or the quoted-policy answer takes over, instead of every customer waiting out the run deadline
    # (N-077). 0 falls back to llm_timeout.
    llm_stall_timeout: float = 10.0
    otlp_endpoint: str | None = None  # OTLP/HTTP traces endpoint, e.g. http://otel-collector:4318/v1/traces
    otlp_service_name: str = "loopdesk"
    # Whole run (all steps, retries, both model tiers); 0 disables. A customer is handed to a person
    # when it runs out, so it is the longest anyone waits for a hanging model (was 90 s, N-031).
    run_deadline_seconds: float = 45.0
    # Per-model circuit breaker (app/llm/breaker.py): this many consecutive failures open it for the
    # cool-down. 0 disables.
    llm_breaker_failures: int = 3
    llm_breaker_cooldown_seconds: float = 60.0
    llm_stream_usage: bool = True  # send stream_options.include_usage (disable if a vendor rejects it)
    anthropic_effort: str = "low"
    mock_stream_delay: float = 0.0  # seconds between streamed chunks of the mock model
    trace_writes: str = "auto"  # auto (buffered on PostgreSQL, per span on SQLite) | per_span | buffered

    # Embeddings: hashing (offline) | openai_compat (an embeddings API, e.g. bge-m3 behind TEI, vLLM or a
    # vendor) | local (bge-m3 in-process on CPU; needs requirements-semantic.txt)
    embedding_provider: str = "hashing"
    embedding_base_url: str | None = None
    embedding_api_key: str | None = None
    embedding_model: str | None = None
    embedding_dim: int = 512
    # One search waits at most this long for the query vector before going on with BM25 (N-078);
    # ingest keeps the client's 30 s.
    embedding_timeout: float = 5.0
    local_model_threads: int = 0  # torch threads for the local providers; 0: torch's default

    # Cross-encoder reranking of the fused candidates (W5): none (default) | local (bge-reranker-v2-m3
    # in-process) | http (a /rerank API: Jina / Cohere format, or text-embeddings-inference).
    reranker_provider: str = "none"
    reranker_model: str | None = None
    reranker_base_url: str | None = None
    reranker_api_key: str | None = None
    reranker_timeout: float = 3.0  # one search waits this long for the scores, then goes on without them
    rerank_candidates: int = 20  # the top fused candidates the reranker reads; the rest are dropped

    # Agent behaviour
    max_agent_steps: int = 6
    history_turns: int = 6
    retrieval_top_k: int = 4
    min_relevance: float = 0.3
    # The vector path's own gate (N-030). A semantic embedding model gives unrelated sentences a
    # cosine around 0.6, so its cosine opens the gate only once calibrated for that model
    # (python -m app.evals.gate_probe --calibrate). Unset: the offline hashing embedder, whose
    # cosine is lexical, shares min_relevance; any other embedder's cosine never passes alone.
    min_cosine: float | None = None
    # The two-signal gate's rerank side (W5), calibrated per model on the probe's dev split
    # (python -m app.evals.gate_probe --calibrate --split dev). min_rerank: a chunk the reranker scores
    # at least this high passes on its own, whatever its term coverage. rerank_floor: a chunk that
    # passes on coverage is still refused when the reranker scores it below this (a word in common,
    # another question). Unset: the reranker only reorders, it neither lets in nor keeps out.
    min_rerank: float | None = None
    rerank_floor: float | None = None
    index_questions: bool = True  # published colloquial questions count as index entries of their chunk
    evidence_mode: str = "annotate"  # annotate | strict
    evidence_pass_coverage: float = 0.8
    sentence_support_threshold: float = 0.5
    verifier: str = "lexical"  # lexical | llm
    # rules | llm: after the rules, a fast model judges model answers that carry a scam's cues
    # (guardrails/judge.py); answers are then not streamed, so nothing shows before the verdict.
    output_guard: str = "rules"
    pii_masking: bool = True
    rate_limit_per_minute: int = 30
    login_attempts_per_email: int = 10  # per minute, against password guessing on one account
    login_attempts_per_ip: int = 30  # per minute, against spraying many accounts from one client
    rate_limit_backend: str = "auto"  # login limits: database (shared by workers) on PostgreSQL, memory on SQLite
    # Velocity limits for automatic refunds (anti-abuse): a run of small refunds that each
    # stay under the per-refund limit still goes to a human once these are exceeded.
    refund_window_days: int = 30
    refund_window_max_count: int = 3
    refund_window_max_amount: float = 200.0
    answer_cache_ttl_hours: float = 24.0  # 0 disables the answer cache
    prompt_version: str = "1.02"
    prompt_canary_version: str | None = None
    prompt_canary_percent: float = 0.0

    # Channels
    feishu_apps: dict[str, FeishuApp] = field(default_factory=dict)  # tenant slug -> app
    wecom_webhook_key: str | None = None

    # Order and payment systems: tenant slug -> its own systems over HTTP; the rest use the demo tables
    commerce_apis: dict[str, CommerceApi] = field(default_factory=dict)

    # Voice channel (app/voice). Off unless an ASR and a TTS engine are named; the speech packages
    # are optional (requirements-voice.txt), so a server without them just has no voice button.
    voice_asr: str = "off"  # off | paraformer | sensevoice | whisper
    voice_tts: str = "off"  # off | cosyvoice | kokoro | say
    voice_vad: str = "energy"  # energy | silero
    voice_device: str = "cpu"  # where FunASR runs: cpu | cuda | mps
    voice_hotwords: bool = True  # the tenant's plan and product names bias the ASR
    # Punctuation on the final transcript for engines that write none (Paraformer): FunASR ct-punc.
    voice_punc: bool = False
    voice_end_silence_ms: int = 600  # trailing silence that ends an utterance (continuous mode)
    voice_barge_in_ms: int = 240  # speech this long while the agent talks stops it
    voice_readback_threshold: float = 0.6  # an order number or amount below this is read back first
    voice_max_utterance_seconds: float = 20.0
    voice_cosyvoice_url: str = "http://127.0.0.1:50000"  # the CosyVoice sidecar (backend/sidecars)
    voice_tts_voice: str | None = None  # engine voice: a Kokoro voice id, a `say` voice, a CosyVoice speaker
    # Test engines, demo mode only (end-to-end tests of the voice UI without speech models): ASR
    # "scripted" returns these transcripts in turn, "|"-separated; TTS "tone" plays a tone.
    voice_scripted_text: str = ""

    prices: dict[str, tuple[float, float]] = field(default_factory=lambda: dict(DEFAULT_PRICES))

    @classmethod
    def from_env(cls) -> "Settings":
        s = cls()
        s.database_url = _env("DATABASE_URL", s.database_url)
        s.jwt_secret = _env("JWT_SECRET", s.jwt_secret)
        s.audit_key = _env("AUDIT_KEY") or None
        s.jwt_expire_minutes = _env_int("JWT_EXPIRE_MINUTES", s.jwt_expire_minutes)
        origins = _env("CORS_ORIGINS")
        if origins:
            s.cors_origins = [o.strip() for o in origins.split(",") if o.strip()]
        s.seed_demo = _env_bool("SEED_DEMO", s.seed_demo)
        s.auto_migrate = _env_bool("AUTO_MIGRATE", s.auto_migrate)
        s.external_migrations = not s.auto_migrate
        s.prewarm_index = _env_bool("PREWARM_INDEX", s.prewarm_index)
        s.log_format = _env("LOG_FORMAT", s.log_format)
        s.log_level = _env("LOG_LEVEL", s.log_level)

        s.llm_preset = _env("LLM_PRESET")
        s.llm_base_url = _env("LLM_BASE_URL") or PROVIDER_PRESETS.get(s.llm_preset or "")
        s.llm_api_key = _env("LLM_API_KEY")
        # An address and a key are an OpenAI-compatible endpoint: `docker compose up` with a .env of
        # base URL, key and model connects it without naming a provider or tiers (W7).
        inferred = "openai_compat" if s.llm_base_url and s.llm_api_key else s.llm_provider
        s.llm_provider = _env("LLM_PROVIDER", inferred)
        # Demo traffic asks the model a dozen questions on a fresh database: free and instant offline,
        # but with a real model it costs calls and a wrong key or a hanging endpoint used to hold up
        # startup for minutes. Default on only for the offline model (N-032).
        s.seed_demo_traffic = _env_bool("SEED_DEMO_TRAFFIC", s.llm_provider == "mock")
        s.otlp_endpoint = _env("OTLP_ENDPOINT") or None
        s.otlp_service_name = _env("OTLP_SERVICE_NAME", s.otlp_service_name)
        default_fast = s.llm_model_fast
        default_strong = s.llm_model_strong
        if s.llm_provider == "anthropic":
            default_fast = default_strong = "claude-opus-5-5"
        if s.llm_provider != "mock" and _env("LLM_MODEL"):  # one model for both tiers; the tier variables still win
            default_fast = default_strong = _env("LLM_MODEL")
        s.llm_model_strong = _env("LLM_MODEL_STRONG", default_strong)
        s.llm_model_fast = _env("LLM_MODEL_FAST", default_fast if s.llm_provider in ("mock", "anthropic") else s.llm_model_strong)
        s.llm_timeout = _env_float("LLM_TIMEOUT", s.llm_timeout)
        s.llm_stall_timeout = _env_float("LLM_STALL_TIMEOUT", s.llm_stall_timeout)
        s.run_deadline_seconds = _env_float("RUN_DEADLINE_SECONDS", s.run_deadline_seconds)
        s.llm_breaker_failures = _env_int("LLM_BREAKER_FAILURES", s.llm_breaker_failures)
        s.llm_breaker_cooldown_seconds = _env_float("LLM_BREAKER_COOLDOWN_SECONDS", s.llm_breaker_cooldown_seconds)
        s.llm_stream_usage = _env_bool("LLM_STREAM_USAGE", s.llm_stream_usage)
        s.anthropic_effort = _env("ANTHROPIC_EFFORT", s.anthropic_effort)
        s.mock_stream_delay = _env_float("MOCK_STREAM_DELAY", s.mock_stream_delay)
        s.trace_writes = _env("TRACE_WRITES") or s.trace_writes
        if s.trace_writes not in ("auto", "per_span", "buffered"):
            raise ValueError("LOOPDESK_TRACE_WRITES must be auto, per_span or buffered")

        s.embedding_provider = _env("EMBEDDING_PROVIDER", s.embedding_provider)
        s.embedding_base_url = _env("EMBEDDING_BASE_URL", s.llm_base_url)
        s.embedding_api_key = _env("EMBEDDING_API_KEY", s.llm_api_key)
        s.embedding_model = _env("EMBEDDING_MODEL")
        s.embedding_dim = _env_int("EMBEDDING_DIM", s.embedding_dim)
        s.embedding_timeout = _env_float("EMBEDDING_TIMEOUT", s.embedding_timeout)
        s.local_model_threads = _env_int("LOCAL_MODEL_THREADS", s.local_model_threads)
        s.reranker_provider = _env("RERANKER_PROVIDER", s.reranker_provider)
        s.reranker_model = _env("RERANKER_MODEL")
        s.reranker_base_url = _env("RERANKER_BASE_URL")
        s.reranker_api_key = _env("RERANKER_API_KEY")
        s.reranker_timeout = _env_float("RERANKER_TIMEOUT", s.reranker_timeout)
        s.rerank_candidates = _env_int("RERANK_CANDIDATES", s.rerank_candidates)

        s.max_agent_steps = _env_int("MAX_AGENT_STEPS", s.max_agent_steps)
        s.history_turns = _env_int("HISTORY_TURNS", s.history_turns)
        s.retrieval_top_k = _env_int("RETRIEVAL_TOP_K", s.retrieval_top_k)
        s.min_relevance = _env_float("MIN_RELEVANCE", s.min_relevance)
        s.min_cosine = _env_float("MIN_COSINE", -1.0)
        s.min_cosine = None if s.min_cosine < 0 else s.min_cosine
        s.min_rerank = _env_float("MIN_RERANK", -1.0)
        s.min_rerank = None if s.min_rerank < 0 else s.min_rerank
        s.rerank_floor = _env_float("RERANK_FLOOR", -1.0)
        s.rerank_floor = None if s.rerank_floor < 0 else s.rerank_floor
        s.index_questions = _env_bool("INDEX_QUESTIONS", s.index_questions)
        s.evidence_mode = _env("EVIDENCE_MODE", s.evidence_mode)
        s.evidence_pass_coverage = _env_float("EVIDENCE_PASS_COVERAGE", s.evidence_pass_coverage)
        s.sentence_support_threshold = _env_float("SENTENCE_SUPPORT_THRESHOLD", s.sentence_support_threshold)
        s.verifier = _env("VERIFIER", s.verifier)
        s.output_guard = _env("OUTPUT_GUARD", s.output_guard)
        if s.output_guard not in ("rules", "llm"):
            raise ValueError(f"LOOPDESK_OUTPUT_GUARD must be rules or llm, not {s.output_guard!r}")
        s.pii_masking = _env_bool("PII_MASKING", s.pii_masking)
        s.rate_limit_per_minute = _env_int("RATE_LIMIT_PER_MINUTE", s.rate_limit_per_minute)
        s.login_attempts_per_email = _env_int("LOGIN_ATTEMPTS_PER_EMAIL", s.login_attempts_per_email)
        s.login_attempts_per_ip = _env_int("LOGIN_ATTEMPTS_PER_IP", s.login_attempts_per_ip)
        s.rate_limit_backend = _env("RATE_LIMIT_BACKEND") or s.rate_limit_backend
        if s.rate_limit_backend not in ("auto", "memory", "database"):
            raise ValueError("LOOPDESK_RATE_LIMIT_BACKEND must be auto, memory or database")
        s.refund_window_days = _env_int("REFUND_WINDOW_DAYS", s.refund_window_days)
        s.refund_window_max_count = _env_int("REFUND_WINDOW_MAX_COUNT", s.refund_window_max_count)
        s.refund_window_max_amount = _env_float("REFUND_WINDOW_MAX_AMOUNT", s.refund_window_max_amount)
        s.answer_cache_ttl_hours = _env_float("ANSWER_CACHE_TTL_HOURS", s.answer_cache_ttl_hours)
        s.prompt_version = prompt_version_name(_env("PROMPT_VERSION", s.prompt_version))
        s.prompt_canary_version = prompt_version_name(_env("PROMPT_CANARY_VERSION", s.prompt_canary_version))
        s.prompt_canary_percent = _env_float("PROMPT_CANARY_PERCENT", s.prompt_canary_percent)

        s.feishu_apps = _feishu_apps()
        s.wecom_webhook_key = _env("WECOM_WEBHOOK_KEY")
        s.commerce_apis = _commerce_apis()

        s.voice_asr = _env("VOICE_ASR", s.voice_asr)
        s.voice_tts = _env("VOICE_TTS", s.voice_tts)
        s.voice_vad = _env("VOICE_VAD", s.voice_vad)
        s.voice_device = _env("VOICE_DEVICE", s.voice_device)
        s.voice_hotwords = _env_bool("VOICE_HOTWORDS", s.voice_hotwords)
        s.voice_punc = _env_bool("VOICE_PUNC", s.voice_punc)
        s.voice_end_silence_ms = _env_int("VOICE_END_SILENCE_MS", s.voice_end_silence_ms)
        s.voice_barge_in_ms = _env_int("VOICE_BARGE_IN_MS", s.voice_barge_in_ms)
        s.voice_readback_threshold = _env_float("VOICE_READBACK_THRESHOLD", s.voice_readback_threshold)
        s.voice_max_utterance_seconds = _env_float("VOICE_MAX_UTTERANCE_SECONDS", s.voice_max_utterance_seconds)
        s.voice_cosyvoice_url = _env("VOICE_COSYVOICE_URL", s.voice_cosyvoice_url)
        s.voice_tts_voice = _env("VOICE_TTS_VOICE") or None
        s.voice_scripted_text = _env("VOICE_SCRIPTED_TEXT", s.voice_scripted_text)

        extra_prices = _env("PRICES")
        if extra_prices:
            for model, pair in json.loads(extra_prices).items():
                s.prices[model] = (float(pair[0]), float(pair[1]))
        return s

    def jwt_secret_problem(self) -> str | None:
        """Why the JWT signing secret is unsafe, or None. HS256 wants at least 32 bytes."""
        if self.jwt_secret in PUBLIC_JWT_SECRETS:
            return "是仓库中公开的默认值"
        if len(self.jwt_secret.encode()) < 32:
            return "短于 32 字节"
        return None

    def audit_key_problem(self) -> str | None:
        """Why the audit HMAC key is unfit for production, or None. Derived from the JWT secret, it
        would turn rotating that secret (to end every session, or when leaving the demo) into
        every past audit entry failing verification."""
        if not self.audit_key:
            return "未设置（会从 JWT 密钥派生，轮换 JWT 密钥后所有旧审计记录都会校验失败）"
        if len(self.audit_key.encode()) < 32:
            return "短于 32 字节"
        if self.audit_key == self.jwt_secret:
            return "与 JWT 密钥相同"
        return None

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


def _commerce_apis() -> dict[str, CommerceApi]:
    """LOOPDESK_COMMERCE_APIS='{"<tenant slug>": {"base_url", "token", "timeout", "max_attempts",
    "signing_secret"}}': per tenant, like the Feishu apps, since each merchant has its own systems."""
    raw = _env("COMMERCE_APIS")
    if not raw:
        return {}
    known = {f.name for f in fields(CommerceApi)}
    apis: dict[str, CommerceApi] = {}
    for slug, conf in json.loads(raw).items():
        unknown = sorted(set(conf) - known)
        if unknown:
            raise ValueError(f"LOOPDESK_COMMERCE_APIS[{slug!r}] has unknown fields: {', '.join(unknown)}")
        try:
            apis[slug] = CommerceApi(**conf)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"LOOPDESK_COMMERCE_APIS[{slug!r}]: {exc}") from None
    return apis


def _feishu_apps() -> dict[str, FeishuApp]:
    """LOOPDESK_FEISHU_APPS='{"<tenant slug>": {"app_id", "app_secret", "verification_token"}}'.
    The single-app variables of earlier versions still work, but only with LOOPDESK_FEISHU_TENANT
    saying which tenant they belong to: unbound, they used to authenticate every tenant's URL."""
    apps: dict[str, FeishuApp] = {}
    raw = _env("FEISHU_APPS")
    if raw:
        for slug, conf in json.loads(raw).items():
            missing = [k for k in ("app_id", "app_secret", "verification_token") if not conf.get(k)]
            if missing:
                raise ValueError(f"LOOPDESK_FEISHU_APPS[{slug!r}] is missing {', '.join(missing)}")
            apps[slug] = FeishuApp(conf["app_id"], conf["app_secret"], conf["verification_token"])
    legacy = [_env(n) for n in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_VERIFICATION_TOKEN")]
    if any(legacy):
        tenant = _env("FEISHU_TENANT")
        if not tenant or not all(legacy):
            raise ValueError("LOOPDESK_FEISHU_APP_ID / _APP_SECRET / _VERIFICATION_TOKEN need all three values and "
                             "LOOPDESK_FEISHU_TENANT (the tenant slug they belong to); for several tenants use "
                             "LOOPDESK_FEISHU_APPS")
        if tenant in apps:
            raise ValueError(f"tenant {tenant!r} is configured in both LOOPDESK_FEISHU_APPS and LOOPDESK_FEISHU_*")
        apps[tenant] = FeishuApp(*legacy)
    app_ids = [app.app_id for app in apps.values()]
    if len(set(app_ids)) != len(app_ids):
        # Events carry the app ID, and that is what ties an event to one tenant.
        raise ValueError("each tenant needs its own Feishu app: an app_id appears under two tenants")
    return apps
