"""上游额度 / 余额探测。

StepFun 国际站是**分级体系**，两条通道互相独立：

1. **Step Plan 订阅通道** —— ``https://api.stepfun.ai/step_plan/v1``
   以 Credit 计量，按月发放到"月池"，月末清零不结转；额度用尽可加购
   "加油包"（独立 30 天周期）。这里关心的是 *plan 时长* 与 *剩余额度*。

2. **按量计费通道** —— ``https://api.stepfun.ai``
   ``GET /v1/accounts`` 返回账户余额（预付费/后付费），是"金额"维度。

官方没有公开订阅额度查询端点，因此本模块的策略是：

* 按 ``settings.plan_quota_paths`` 顺序**探测**候选路径，第一个返回
  2xx 且能被解析出额度语义的路径会被缓存复用；
* 候选全部失败时退回到按量通道的余额接口，至少保证账号可用性可见；
* 任何一步失败都不会阻塞导入 —— 账号照样入库，错误信息留在 ``last_error``。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

from .config import Settings
from .proxy import normalize_proxy

# --------------------------------------------------------------------------
# 数值解析
# --------------------------------------------------------------------------

_CREDIT_KEYS = (
    "remaining_credits",
    "remaining_credit",
    "credits_remaining",
    "credit_remaining",
    "remaining",
    "left",
    "available",
    "available_credits",
    "balance",
    "quota_remaining",
    "remain",
)

_TOTAL_KEYS = (
    "total_credits",
    "total_credit",
    "credits_total",
    "total",
    "quota",
    "limit",
    "allowance",
    "monthly_credits",
    "granted",
)

_USED_KEYS = (
    "used_credits",
    "used_credit",
    "credits_used",
    "used",
    "consumed",
    "usage",
    "spent",
)

_RESET_KEYS = (
    "expires_at",
    "expire_at",
    "reset_at",
    "renew_at",
    "renewal_at",
    "period_end",
    "current_period_end",
    "cycle_end",
    "end_time",
    "end_at",
    "expired_at",
    "expire_time",
    "due_at",
    "next_reset_at",
    "reset_time",
)

_PLAN_KEYS = ("plan", "plan_name", "plan_type", "tier", "package", "subscription_plan", "level", "sku")

_STATUS_KEYS = ("status", "state", "subscription_status", "plan_status")

#: 归一化后的档位名（官方四个档位）
KNOWN_PLANS = {
    "flashmini": "Flash Mini",
    "flashplus": "Flash Plus",
    "flashpro": "Flash Pro",
    "flashmax": "Flash Max",
    "flash_mini": "Flash Mini",
    "flash_plus": "Flash Plus",
    "flash_pro": "Flash Pro",
    "flash_max": "Flash Max",
}

_MONEY_KEYS = ("balance", "total_cash_balance", "total_voucher_balance", "cash_balance")


def _first_present(payload: Any, keys: Iterable[str], *, depth: int = 0) -> Any:
    """在嵌套 dict 中广度优先找第一个命中的 key（忽略大小写）。"""
    if depth > 4 or not isinstance(payload, dict):
        return None
    lowered = {str(k).lower(): v for k, v in payload.items()}
    for key in keys:
        if key in lowered and lowered[key] is not None:
            return lowered[key]
    # 递归进一层子 dict（很多接口把数据包在 data / result / subscription 里）
    for value in payload.values():
        if isinstance(value, dict):
            found = _first_present(value, keys, depth=depth + 1)
            if found is not None:
                return found
    return None


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        text = value.strip().replace(",", "")
        if not text:
            return None
        match = re.search(r"-?\d+(\.\d+)?", text)
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return None
    return None


def _to_datetime(value: Any) -> datetime | None:
    """把时间戳/ISO 字符串解析成带时区的 datetime。"""
    if value is None:
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value)
        if seconds > 1e11:  # 毫秒
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if re.fullmatch(r"\d{10,13}", text):
            return _to_datetime(int(text))
        normalized = text.replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed

    return None


def _normalize_plan_name(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    key = raw.lower().replace("-", "").replace(" ", "")
    if key in KNOWN_PLANS:
        return KNOWN_PLANS[key]
    key2 = raw.lower().replace("-", "_").replace(" ", "_")
    if key2 in KNOWN_PLANS:
        return KNOWN_PLANS[key2]
    return raw


# --------------------------------------------------------------------------
# 结果结构
# --------------------------------------------------------------------------


@dataclass
class QuotaSnapshot:
    """一次额度探测的完整结果。"""

    ok: bool = False
    source: str = "none"  # plan | balance | none
    endpoint: str | None = None

    # -- Step Plan 订阅维度 --
    plan_name: str | None = None
    plan_status: str | None = None
    credits_remaining: float | None = None
    credits_total: float | None = None
    credits_used: float | None = None
    reset_at: datetime | None = None
    expires_at: datetime | None = None

    # -- 按量通道金额维度 --
    balance: float | None = None
    cash_balance: float | None = None
    voucher_balance: float | None = None
    account_type: str | None = None

    raw: dict = field(default_factory=dict)
    error: str | None = None
    probed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # ------------------------------------------------------------------
    @property
    def percent_remaining(self) -> float | None:
        if self.credits_remaining is None or not self.credits_total:
            return None
        if self.credits_total <= 0:
            return None
        return max(0.0, min(1.0, self.credits_remaining / self.credits_total))

    @property
    def seconds_remaining(self) -> int | None:
        """plan 剩余时长（秒）。优先按额度重置时间，其次按订阅到期时间。"""
        target = self.reset_at or self.expires_at
        if target is None:
            return None
        return int((target - datetime.now(timezone.utc)).total_seconds())

    @property
    def days_remaining(self) -> float | None:
        secs = self.seconds_remaining
        return None if secs is None else round(secs / 86400.0, 2)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "source": self.source,
            "endpoint": self.endpoint,
            "plan_name": self.plan_name,
            "plan_status": self.plan_status,
            "credits_remaining": self.credits_remaining,
            "credits_total": self.credits_total,
            "credits_used": self.credits_used,
            "percent_remaining": self.percent_remaining,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "seconds_remaining": self.seconds_remaining,
            "days_remaining": self.days_remaining,
            "balance": self.balance,
            "cash_balance": self.cash_balance,
            "voucher_balance": self.voucher_balance,
            "account_type": self.account_type,
            "error": self.error,
            "probed_at": self.probed_at.isoformat(),
        }


# --------------------------------------------------------------------------
# 解析器
# --------------------------------------------------------------------------


def parse_plan_payload(payload: Any) -> QuotaSnapshot | None:
    """尝试把任意 json 解析成订阅额度快照。解析不出额度则返回 None。"""
    if not isinstance(payload, (dict, list)):
        return None

    total = _to_float(_first_present(payload, _TOTAL_KEYS))
    remaining = _to_float(_first_present(payload, _CREDIT_KEYS))
    used = _to_float(_first_present(payload, _USED_KEYS))

    # 只拿到 used + total 时反推剩余
    if remaining is None and total is not None and used is not None:
        remaining = max(0.0, total - used)
    if used is None and total is not None and remaining is not None:
        used = max(0.0, total - remaining)
    if total is None and remaining is not None and used is not None:
        total = remaining + used

    plan_name = _normalize_plan_name(_first_present(payload, _PLAN_KEYS))
    status = _first_present(payload, _STATUS_KEYS)
    reset_at = _to_datetime(_first_present(payload, _RESET_KEYS))

    if remaining is None and total is None and plan_name is None and reset_at is None:
        return None

    return QuotaSnapshot(
        ok=True,
        source="plan",
        plan_name=plan_name,
        plan_status=str(status) if status is not None else None,
        credits_remaining=remaining,
        credits_total=total,
        credits_used=used,
        reset_at=reset_at,
        expires_at=reset_at,
    )


def parse_balance_payload(payload: Any) -> QuotaSnapshot | None:
    """解析按量计费通道的余额响应（``GET /v1/accounts``）。"""
    if not isinstance(payload, dict):
        return None
    balance = _to_float(_first_present(payload, _MONEY_KEYS))
    if balance is None:
        return None
    cash = _to_float(payload.get("total_cash_balance"))
    voucher = _to_float(payload.get("total_voucher_balance"))
    return QuotaSnapshot(
        ok=True,
        source="balance",
        balance=balance,
        cash_balance=cash,
        voucher_balance=voucher,
        account_type=payload.get("type") or payload.get("account_type"),
        raw=payload if isinstance(payload, dict) else {},
    )


# --------------------------------------------------------------------------
# 探测器
# --------------------------------------------------------------------------


def _auth_headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "User-Agent": "step2api/1.0 (+https://github.com/huanxueshengmou/step2api)",
    }


def _build_client(proxy: str | None, settings: Settings, timeout: float) -> httpx.AsyncClient:
    kwargs: dict[str, Any] = {
        "timeout": httpx.Timeout(timeout, connect=min(timeout, settings.connect_timeout)),
        "follow_redirects": True,
        "headers": {"User-Agent": "step2api/1.0"},
    }
    if proxy:
        kwargs["proxy"] = normalize_proxy(proxy)
    return httpx.AsyncClient(**kwargs)


@dataclass
class QuotaResult:
    """探测的最终返回：快照 + 建议缓存哪个 plan 端点。"""

    snapshot: QuotaSnapshot
    plan_endpoint: str | None = None


async def fetch_quota(
    api_key: str,
    *,
    settings: Settings,
    proxy: str | None = None,
    plan_endpoint: str | None = None,
    known_plan_endpoint: str | None = None,
) -> QuotaResult:
    """查询一个账号的额度。

    :param plan_endpoint: 指定要探测的 plan 端点（单路径模式）
    :param known_plan_endpoint: 该账号上次探测命中的端点，会优先重试
    """
    if not api_key:
        return QuotaResult(QuotaSnapshot(ok=False, error="缺少 API Key"))

    candidates: list[str] = []
    if plan_endpoint:
        candidates.append(plan_endpoint)
    if known_plan_endpoint and known_plan_endpoint not in candidates:
        candidates.append(known_plan_endpoint)
    if not plan_endpoint:
        for path in settings.plan_quota_paths:
            if path not in candidates:
                candidates.append(path)

    last_error: str | None = None
    plan_base = settings.plan_base

    async with _build_client(proxy, settings, settings.probe_timeout) as client:
        # ---- 1. Step Plan 订阅通道（额度维度）----
        plan_result = await _fetch_plan_quota(
            client, api_key, candidates, plan_base
        )
        plan_snapshot, plan_endpoint, plan_error = plan_result
        if plan_error:
            last_error = plan_error

        # ---- 2. 按量计费通道（金额维度）----
        # 关键：即使订阅额度查到了，余额也要独立查一次 —— 两条通道是分级体系，
        # 用户需要同时看到"套餐还剩多少 Credit"和"账户还压着多少钱"。
        balance_snapshot, balance_error = await _fetch_balance(client, api_key, settings)

        if plan_snapshot is not None:
            # 把金额维度合并进同一个快照返回
            snapshot = plan_snapshot
            if balance_snapshot is not None:
                snapshot.balance = balance_snapshot.balance
                snapshot.cash_balance = balance_snapshot.cash_balance
                snapshot.voucher_balance = balance_snapshot.voucher_balance
                snapshot.account_type = balance_snapshot.account_type
            return QuotaResult(snapshot, plan_endpoint=plan_endpoint)

        # 订阅额度没拿到，但有余额 —— 用余额兜底，保证账号可用性可见
        if balance_snapshot is not None:
            balance_snapshot.error = (
                f"Step Plan 额度未命中，已回落余额通道（{last_error}）" if last_error else None
            )
            return QuotaResult(balance_snapshot, plan_endpoint=None)

        # 两条通道都失败
        error = last_error or balance_error or "额度查询失败"
        return QuotaResult(QuotaSnapshot(ok=False, endpoint=plan_base, error=error))


async def _fetch_plan_quota(
    client: httpx.AsyncClient,
    api_key: str,
    candidates: list[str],
    plan_base: str,
) -> tuple[QuotaSnapshot | None, str | None, str | None]:
    """探测 Step Plan 订阅额度。

    返回 ``(快照或 None, 命中端点, 最后一条错误)``。
    """
    last_error: str | None = None

    for path in candidates:
        url = path if path.startswith("http") else f"{plan_base}{path}"
        try:
            resp = await client.get(url, headers=_auth_headers(api_key))
        except httpx.TimeoutException:
            last_error = f"{url} 超时"
            continue
        except httpx.HTTPError as exc:
            last_error = f"{url} 请求失败：{type(exc).__name__}"
            continue

        if resp.status_code in (401, 403):
            return (
                None,
                None,
                f"HTTP {resp.status_code}：Key 无效或无权访问 Step Plan 通道（{url}）",
            )
        if resp.status_code == 404:
            last_error = f"{url} 不存在（404）"
            continue
        if resp.status_code >= 400:
            last_error = f"{url} 返回 HTTP {resp.status_code}"
            continue

        try:
            payload = resp.json()
        except (json.JSONDecodeError, ValueError):
            last_error = f"{url} 返回非 JSON"
            continue

        snapshot = parse_plan_payload(payload)
        if snapshot is None:
            last_error = f"{url} 响应中未找到额度字段"
            continue

        snapshot.endpoint = url
        snapshot.raw = payload if isinstance(payload, dict) else {"data": payload}
        return snapshot, url, None

    return None, None, last_error


async def _fetch_balance(
    client: httpx.AsyncClient,
    api_key: str,
    settings: Settings,
) -> tuple[QuotaSnapshot | None, str | None]:
    """查询按量计费通道余额。返回 ``(快照或 None, 错误)``。"""
    url = f"{settings.upstream_base}{settings.balance_path}"
    try:
        resp = await client.get(url, headers=_auth_headers(api_key))
    except httpx.HTTPError as exc:
        return None, f"{url} 请求失败：{type(exc).__name__}: {exc}"

    if resp.status_code in (401, 403):
        return None, f"HTTP {resp.status_code}：API Key 无效（{url}）"
    if resp.status_code != 200:
        return None, f"{url} 返回 HTTP {resp.status_code}"

    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None, f"{url} 返回非 JSON"

    snapshot = parse_balance_payload(payload)
    if snapshot is None:
        return None, f"{url} 响应中未找到余额字段"

    snapshot.endpoint = url
    return snapshot, None


async def verify_key(
    api_key: str,
    *,
    settings: Settings,
    proxy: str | None = None,
) -> QuotaSnapshot:
    """导入时的轻量校验：能查到额度或者余额即视为有效 key。"""
    result = await fetch_quota(api_key, settings=settings, proxy=proxy)
    return result.snapshot


async def probe_plan_endpoint(
    api_key: str,
    *,
    settings: Settings,
    proxy: str | None = None,
) -> tuple[str | None, list[str]]:
    """逐个探测候选额度端点，供 ``step2api probe-endpoint`` 命令使用。

    返回 ``(命中端点或 None, 每个候选的探测明细)``。
    """
    lines: list[str] = []
    hit: str | None = None

    async with _build_client(proxy, settings, settings.probe_timeout) as client:
        for path in settings.plan_quota_paths:
            url = path if path.startswith("http") else f"{settings.plan_base}{path}"
            try:
                resp = await client.get(url, headers=_auth_headers(api_key))
            except httpx.HTTPError as exc:
                lines.append(f"  {url} -> {type(exc).__name__}")
                continue

            body = resp.text[:160].replace("\n", " ").replace("\r", " ")
            lines.append(f"  {url} -> HTTP {resp.status_code} {body}")

            if resp.status_code != 200:
                continue
            try:
                payload = resp.json()
            except ValueError:
                continue
            if parse_plan_payload(payload) is not None:
                hit = url
                break

    return hit, lines


def estimate_reset_at(now: datetime | None = None, *, days: int = 30) -> datetime:
    """在没有重置时间信息时，按 30 天周期估算。"""
    base = now or datetime.now(timezone.utc)
    return base + timedelta(days=days)
