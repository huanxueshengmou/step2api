"""全局配置。

所有配置项都可以通过 STEP2API_ 前缀的环境变量覆盖，例如：

    STEP2API_PORT=9000
    STEP2API_UPSTREAM_BASE=https://api.stepfun.ai

本项目只对接 **国际站**（account.stepfun.ai / api.stepfun.ai）。
国内站（*.stepfun.com）在导入阶段会被硬性拒绝，见 import 校验逻辑。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(f"STEP2API_{name}")
    if value is None or value == "":
        return default
    return value


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


# --------------------------------------------------------------------------
# 上游站点常量
# --------------------------------------------------------------------------

#: 国际站主域（唯一允许的上游）
INTL_HOST = "stepfun.ai"

#: 国际站控制台，导入说明与手工取 key 的地方
PORTAL_URL = "https://account.stepfun.ai/"

#: 按量计费（金额/余额）通道基址
UPSTREAM_BASE = "https://api.stepfun.ai"

#: Step Plan（订阅额度）通道基址 —— 分级体系，与按量通道相互独立
PLAN_BASE = "https://api.stepfun.ai/step_plan/v1"

#: 国内站域名特征，命中即拒绝导入
CN_HOST_MARKERS = ("stepfun.com", "platform.stepfun.com")

#: 余额查询路径（相对于 UPSTREAM_BASE）
BALANCE_PATH = "/v1/accounts"

#: Step Plan 额度查询候选路径（相对于 PLAN_BASE）。
#: 官方未公开订阅额度查询端点，启动后按顺序探测，命中一次即缓存。
DEFAULT_PLAN_QUOTA_PATHS: tuple[str, ...] = (
    "/usage",
    "/credits",
    "/quota",
    "/subscription",
    "/plan",
    "/me",
    "/account",
    "/accounts",
    "/balance",
)

#: 候选路径全部失败时，仍会退回到按量通道的余额接口做兜底展示。
DEFAULT_MODELS: tuple[str, ...] = (
    "step-5-preview",
    "step-3.7-flash",
    "step-3.5-flash",
    "step-3.5-flash-2603",
    "step-router-v1",
    "stepaudio-2.5-chat",
    "stepaudio-2.5-tts",
    "stepaudio-2.5-asr",
    "stepaudio-2.5-realtime",
)


@dataclass(slots=True)
class Settings:
    """运行期配置。"""

    # -- 服务 ------------------------------------------------------------
    host: str = field(default_factory=lambda: _env("HOST", "127.0.0.1") or "127.0.0.1")
    port: int = field(default_factory=lambda: _env_int("PORT", 8787))

    #: 数据目录（SQLite、密钥文件）
    data_dir: Path = field(
        default_factory=lambda: Path(_env("DATA_DIR", "./data") or "./data").expanduser()
    )

    # -- 安全 ------------------------------------------------------------
    #: 用来看护 Web 控制台与 /api/* 的令牌。留空则仅本机可访问、不校验。
    admin_token: str | None = field(default_factory=lambda: _env("ADMIN_TOKEN"))

    #: 主密钥（用于加密落库的 API Key）。留空则自动生成并写入 data_dir/.secret_key
    secret: str | None = field(default_factory=lambda: _env("SECRET"))

    # -- 网关 ------------------------------------------------------------
    #: 下游客户端访问本网关所需的令牌。留空表示不校验。
    gateway_tokens: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            t.strip() for t in (_env("GATEWAY_TOKENS", "") or "").split(",") if t.strip()
        )
    )

    upstream_base: str = field(
        default_factory=lambda: (_env("UPSTREAM_BASE", UPSTREAM_BASE) or UPSTREAM_BASE).rstrip("/")
    )
    plan_base: str = field(
        default_factory=lambda: (_env("PLAN_BASE", PLAN_BASE) or PLAN_BASE).rstrip("/")
    )
    balance_path: str = field(
        default_factory=lambda: _env("BALANCE_PATH", BALANCE_PATH) or BALANCE_PATH
    )
    plan_quota_paths: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            p.strip()
            for p in (
                _env("PLAN_QUOTA_PATHS", ",".join(DEFAULT_PLAN_QUOTA_PATHS))
                or ",".join(DEFAULT_PLAN_QUOTA_PATHS)
            ).split(",")
            if p.strip()
        )
    )

    #: 上游请求超时（秒）
    request_timeout: float = field(default_factory=lambda: _env_float("REQUEST_TIMEOUT", 300.0))
    #: 建立连接超时（秒）
    connect_timeout: float = field(default_factory=lambda: _env_float("CONNECT_TIMEOUT", 15.0))
    #: 额度/余额探测超时（秒）
    probe_timeout: float = field(default_factory=lambda: _env_float("PROBE_TIMEOUT", 20.0))

    # -- 路由 ------------------------------------------------------------
    #: sticky | round_robin | least_used | priority | random
    routing_mode: str = field(default_factory=lambda: _env("ROUTING_MODE", "sticky") or "sticky")
    #: 会话粘性保留时长（秒），默认 6 小时
    affinity_ttl: float = field(default_factory=lambda: _env_float("AFFINITY_TTL", 6 * 3600))
    #: 失败账号冷却时长（秒）
    cooldown_seconds: float = field(default_factory=lambda: _env_float("COOLDOWN", 60.0))
    #: 单次请求最多尝试的账号数
    max_retries: int = field(default_factory=lambda: _env_int("MAX_RETRIES", 3))
    #: 账号并发上限（账号自身 max_concurrency 为 0 时使用）
    default_max_concurrency: int = field(
        default_factory=lambda: _env_int("DEFAULT_MAX_CONCURRENCY", 8)
    )
    #: 连续失败多少次后标记为故障
    fail_threshold: int = field(default_factory=lambda: _env_int("FAIL_THRESHOLD", 5))

    # -- 额度刷新 --------------------------------------------------------
    refresh_interval: float = field(default_factory=lambda: _env_float("REFRESH_INTERVAL", 300.0))
    #: 额度低于该比例（0~1）时标记为即将耗尽
    low_quota_ratio: float = field(default_factory=lambda: _env_float("LOW_QUOTA_RATIO", 0.1))

    # -- 代理 ------------------------------------------------------------
    #: 全局默认代理（账号 proxy_mode=inherit 且未绑定代理池时使用）
    global_proxy: str | None = field(default_factory=lambda: _env("GLOBAL_PROXY"))
    #: round_robin | random | least_used | lowest_latency
    proxy_strategy: str = field(
        default_factory=lambda: _env("PROXY_STRATEGY", "round_robin") or "round_robin"
    )
    #: 代理健康检查地址
    proxy_check_url: str = field(
        default_factory=lambda: _env("PROXY_CHECK_URL", f"{UPSTREAM_BASE}{BALANCE_PATH}")
        or f"{UPSTREAM_BASE}{BALANCE_PATH}"
    )

    # -- 导入 ------------------------------------------------------------
    #: 导入并发度
    import_concurrency: int = field(default_factory=lambda: _env_int("IMPORT_CONCURRENCY", 6))
    #: 是否允许导入国内站 key。默认 False —— 只支持国外站。
    allow_cn_site: bool = field(default_factory=lambda: _env_bool("ALLOW_CN_SITE", False))

    # -- 其它 ------------------------------------------------------------
    #: 金额维度展示用的币种符号（国际站按量计费为美元）
    currency: str = field(default_factory=lambda: _env("CURRENCY", "USD") or "USD")
    log_requests: bool = field(default_factory=lambda: _env_bool("LOG_REQUESTS", True))
    log_retention: int = field(default_factory=lambda: _env_int("LOG_RETENTION", 5000))

    def __post_init__(self) -> None:
        if isinstance(self.data_dir, str):
            self.data_dir = Path(self.data_dir).expanduser()
        if not isinstance(self.data_dir, Path):
            self.data_dir = Path(self.data_dir).expanduser()

    @property
    def db_path(self) -> Path:
        return self.data_dir / "step2api.db"

    @property
    def secret_path(self) -> Path:
        return self.data_dir / ".secret_key"

    @property
    def static_dir(self) -> Path:
        return Path(__file__).resolve().parent / "static"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    def is_foreign_host(self, url_or_host: str) -> bool:
        """判断给定 URL/主机是否属于国际站（非国内站）。"""
        text = (url_or_host or "").lower()
        if any(marker in text for marker in CN_HOST_MARKERS):
            return False
        return INTL_HOST in text

    def redacted(self) -> dict:
        """用于 /api/settings 返回的可公开配置。"""
        return {
            "host": self.host,
            "port": self.port,
            "data_dir": str(self.data_dir),
            "upstream_base": self.upstream_base,
            "plan_base": self.plan_base,
            "balance_path": self.balance_path,
            "plan_quota_paths": list(self.plan_quota_paths),
            "portal_url": PORTAL_URL,
            "routing_mode": self.routing_mode,
            "affinity_ttl": self.affinity_ttl,
            "cooldown_seconds": self.cooldown_seconds,
            "max_retries": self.max_retries,
            "default_max_concurrency": self.default_max_concurrency,
            "refresh_interval": self.refresh_interval,
            "low_quota_ratio": self.low_quota_ratio,
            "proxy_strategy": self.proxy_strategy,
            "global_proxy": bool(self.global_proxy),
            "import_concurrency": self.import_concurrency,
            "allow_cn_site": self.allow_cn_site,
            "foreign_site_only": not self.allow_cn_site,
            "admin_token_required": bool(self.admin_token),
            "gateway_tokens_required": bool(self.gateway_tokens),
            "models": list(DEFAULT_MODELS),
        }


_settings: Settings | None = None


def get_settings(reload: bool = False) -> Settings:
    """返回进程级单例配置。"""
    global _settings
    if _settings is None or reload:
        _settings = Settings()
        _settings.ensure_dirs()
    return _settings
