"""StepFun 控制台（Oasis）额度客户端。

**为什么需要它**：`api.stepfun.ai` 只暴露按量计费的余额接口，不提供
Step Plan 订阅额度查询。真正的套餐额度、5 小时窗口与周窗口限额，只有控制台
（``account.stepfun.ai`` / ``platform.stepfun.ai``）的后端提供。

接口是 gRPC-Web 风格，但**实测接受 JSON 请求体**，无需 protobuf 编码：

    POST https://account.stepfun.ai/api/step.openapi.devcenter.Dashboard/<Method>
    Content-Type: application/json
    Oasis-Token:    <控制台会话令牌>
    Oasis-Webid:    <浏览器 localStorage 里的 web_id>
    Oasis-appID:    20700        （Step Plan 应用；10300 是基础平台，会认证失败）
    Oasis-Platform: web          （可选）

可用方法：

``GetStepPlanStatus``      订阅状态：套餐名、档位、开通/到期时间、是否自动续费
``QueryStepPlanRateLimit`` 额度限额：5 小时窗口、周窗口、Credit 桶
``QueryStepPlanUsages``    用量明细：按时间/模型聚合的调用数与消耗 Credit

> 认证明细来自对照平台前端公开 JS chunk
> （``9130-*.js`` 里的 request 拦截器）实测得出：
> ``Oasis-appID`` 必须为 20700，且 ``Oasis-Webid`` 取自 localStorage 而非 Cookie，
> 缺任何一个都会返回 ``auth failed: oasis-token is embezzled``。

凭据结构与自动续期
------------------

控制台 Cookie 里的 ``Oasis-Token`` 其实是**两段 JWT 用三个点拼起来的**：

    <access>...<refresh>            # 各 3 段，共 8 段（中间夹两个空段）

* **access**  寿命仅 **2 小时**（``exp - create_at`` 恒为 7200 秒）
* **refresh** 寿命 **29 天**，payload 里带 ``app_id`` / ``device_id``

只有 access 过期时，用**完整的这两段**调

    POST /passport/proto.api.passport.v1.PassportService/RefreshToken   {}

即可换回一组新的 access + refresh（实测 access 过期后仍可刷新）。
单独拿 refresh 去调会报 ``token is illegal`` —— 必须两段一起。

因此只要每 29 天重新登录一次，额度就能持续自动更新。
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import CONSOLE_BASE, CONSOLE_APP_ID, Settings
from .proxy import normalize_proxy

log = logging.getLogger("step2api.console")

#: gRPC-Web 网关前缀
RPC_PREFIX = "/api/step.openapi.devcenter.Dashboard"

#: 订阅状态枚举（GetStepPlanStatus.subscription.status）
SUB_STATUS = {0: "unknown", 1: "active", 2: "expired", 3: "cancelled", 4: "pending"}

#: Credit 桶类型（plan_credit_rate_limit.credit_buckets[].type）
BUCKET_TYPE = {0: "unspecified", 1: "subscription", 2: "topup"}


def parse_token_expiry(token: str) -> datetime | None:
    """从 Oasis-Token 里解出真实到期时间。

    这个 token 是 ``header.payload.signature`` 结构（后面还跟了几段私有数据），
    payload 里有 ``exp``。**必须看它，不能信 Cookie 的 expires 字段** ——
    实测 Cookie 自称 2027 年过期，而 exp 只给 2 小时。

    只读不验签：这里只用来做"还剩多久"的提示，安全性由服务端判断。
    """
    if not token:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None

    for index in (1, 0):
        segment = parts[index]
        padded = segment + "=" * (-len(segment) % 4)
        try:
            payload = json.loads(base64.urlsafe_b64decode(padded))
        except (ValueError, TypeError, UnicodeDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        exp = payload.get("exp")
        if isinstance(exp, (int, float)) and exp > 0:
            try:
                return datetime.fromtimestamp(float(exp), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
    return None


def split_cookie(token: str) -> tuple[str, str]:
    """把 Cookie 里的 Oasis-Token 拆成 ``(access, refresh)``。

    结构是 ``<access>...<refresh>``（两个 JWT 之间夹两个空段，共 8 段）。
    只给了 access（3 段）时 refresh 返回空串。
    """
    if not token:
        return "", ""
    parts = token.split(".")
    if len(parts) >= 8:
        return ".".join(parts[0:3]), ".".join(parts[5:8])
    if len(parts) == 3:
        return token, ""
    return token, ""


def combine_cookie(access: str, refresh: str) -> str:
    """把 access 与 refresh 拼回 Cookie 形式（两段之间两个空点段）。"""
    if not access:
        return refresh
    if not refresh:
        return access
    # 分隔符是三个点（access 与 refresh 各 3 段，中间夹两个空段）
    return f"{access}...{refresh}"


def _f(value: Any) -> float | None:
    """把字符串/数字统一成 float。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dt(value: Any) -> datetime | None:
    """Unix 秒字符串 → datetime。``0`` 与空值视为"无"。"""
    seconds = _f(value)
    if not seconds or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _rate(value: Any) -> float | None:
    """剩余比例。

    上游用 ``0`` 表示"该窗口不适用"（例如套餐没有 5 小时限额），
    与"剩余 0%"无法区分。这里靠伴随的 reset_time 一起判断，
    由调用方用 :func:`_window` 决定是否保留。
    """
    return _f(value)


def _window(rate: Any, reset: Any) -> tuple[float | None, datetime | None]:
    """解析一个限额窗口。

    两者皆为 0/空 → 该窗口不适用，返回 ``(None, None)``。
    否则返回 ``(剩余比例 0~1, 重置时间或 None)``。
    """
    r = _rate(rate)
    reset_at = _dt(reset)
    if (r is None or r == 0) and reset_at is None:
        return None, None
    if r is None:
        return None, reset_at
    return max(0.0, min(1.0, r)), reset_at


# --------------------------------------------------------------------------
# 数据结构
# --------------------------------------------------------------------------


@dataclass
class CreditBucket:
    """一桶 Credit（订阅月池或加油包）。"""

    type: str = "unspecified"
    total: float | None = None
    residual: float | None = None
    expire_at: datetime | None = None
    next_reset_at: datetime | None = None

    @property
    def used(self) -> float | None:
        if self.total is None or self.residual is None:
            return None
        return max(0.0, self.total - self.residual)


@dataclass
class PlanStatus:
    """订阅状态。"""

    plan_name: str | None = None
    plan_type: int | None = None
    plan_id: int | None = None
    plan_family: int | None = None
    status: str = "unknown"
    pay_channel: int | None = None
    activated_at: datetime | None = None
    expired_at: datetime | None = None
    auto_renew: bool = False

    @property
    def seconds_remaining(self) -> int | None:
        if self.expired_at is None:
            return None
        return int((self.expired_at - datetime.now(timezone.utc)).total_seconds())

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@dataclass
class ConsoleQuota:
    """控制台返回的完整额度视图。"""

    ok: bool = False
    status: PlanStatus = field(default_factory=PlanStatus)

    #: 5 小时滚动窗口剩余比例（None = 该套餐不适用）
    five_hour_left: float | None = None
    five_hour_reset_at: datetime | None = None
    #: 周窗口剩余比例（None = 该套餐不适用）
    weekly_left: float | None = None
    weekly_reset_at: datetime | None = None

    #: 订阅池剩余比例 / 加油包剩余比例
    subscription_left: float | None = None
    topup_left: float | None = None
    buckets: list[CreditBucket] = field(default_factory=list)

    #: 用量明细（可选，来自 QueryStepPlanUsages）
    usage_records: list[dict] = field(default_factory=list)
    usage_total: int = 0

    #: 凭据自身的到期时间（access 的 exp，通常只有 2 小时）
    credential_expires_at: datetime | None = None
    #: refresh 的到期时间（通常 29 天，决定多久要重新登录一次）
    refresh_expires_at: datetime | None = None
    #: 本次自动续期后的新 Cookie；非 None 时调用方应回写数据库
    renewed_token: str | None = None

    error: str | None = None
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # -- 派生量 ---------------------------------------------------------
    @property
    def total_credits(self) -> float | None:
        vals = [b.total for b in self.buckets if b.total is not None]
        return sum(vals) if vals else None

    @property
    def remaining_credits(self) -> float | None:
        vals = [b.residual for b in self.buckets if b.residual is not None]
        return sum(vals) if vals else None

    @property
    def used_credits(self) -> float | None:
        vals = [b.used for b in self.buckets if b.used is not None]
        return sum(vals) if vals else None

    @property
    def percent_remaining(self) -> float | None:
        total, remaining = self.total_credits, self.remaining_credits
        if total is None or remaining is None or total <= 0:
            return None
        return max(0.0, min(1.0, remaining / total))

    @property
    def reset_at(self) -> datetime | None:
        """最近一次额度失效时间（用于展示"Plan 剩余时长"）。"""
        candidates: list[datetime] = []
        for b in self.buckets:
            for cand in (b.next_reset_at, b.expire_at):
                if cand is not None:
                    candidates.append(cand)
        if self.status.expired_at is not None:
            candidates.append(self.status.expired_at)
        return min(candidates) if candidates else None

    @property
    def seconds_remaining(self) -> int | None:
        if self.reset_at is None:
            return None
        return int((self.reset_at - datetime.now(timezone.utc)).total_seconds())

    @property
    def credential_seconds_left(self) -> int | None:
        """凭据还有多久失效。用于在界面上提示"这个数字的有效期"。"""
        if self.credential_expires_at is None:
            return None
        return int((self.credential_expires_at - datetime.now(timezone.utc)).total_seconds())

    def as_snapshot(self) -> dict:
        """转成 ``store.update_account_quota`` 认识的字段。"""
        return {
            "ok": self.ok,
            "source": "console",
            "plan_name": self.status.plan_name,
            "plan_status": self.status.status,
            "credits_remaining": self.remaining_credits,
            "credits_total": self.total_credits,
            "credits_used": self.used_credits,
            "reset_at": self.reset_at.isoformat() if self.reset_at else None,
            "expires_at": (
                self.status.expired_at.isoformat() if self.status.expired_at else None
            ),
            "five_hour_left_rate": self.five_hour_left,
            "five_hour_reset_at": (
                self.five_hour_reset_at.isoformat() if self.five_hour_reset_at else None
            ),
            "weekly_left_rate": self.weekly_left,
            "weekly_reset_at": (
                self.weekly_reset_at.isoformat() if self.weekly_reset_at else None
            ),
            "auto_renew": 1 if self.status.auto_renew else 0,
            "credential_expires_at": (
                self.credential_expires_at.isoformat() if self.credential_expires_at else None
            ),
            "refresh_expires_at": (
                self.refresh_expires_at.isoformat() if self.refresh_expires_at else None
            ),
            "renewed_token": self.renewed_token,
            "error": self.error,
            "probed_at": self.fetched_at.isoformat(),
        }


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------


class ConsoleAuthError(RuntimeError):
    """会话凭据无效或已失效。"""


class ConsoleClient:
    """StepFun 控制台额度客户端。"""

    def __init__(
        self,
        token: str,
        webid: str,
        *,
        settings: Settings,
        app_id: int | None = None,
        base: str | None = None,
        proxy: str | None = None,
    ) -> None:
        self.token = (token or "").strip()
        self.webid = (webid or "").strip()
        self.app_id = app_id or CONSOLE_APP_ID
        self.base = (base or CONSOLE_BASE).rstrip("/")
        self.proxy = normalize_proxy(proxy) if proxy else None
        self.settings = settings
        self.token_expires_at = parse_token_expiry(self.token)

    # -- 内部 -----------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Oasis-Token": self.token,
            "Oasis-Webid": self.webid,
            "Oasis-appID": str(self.app_id),
            "Oasis-Platform": "web",
            "Origin": self.base,
            "Referer": self.base + "/",
            "User-Agent": "step2api/1.0 (+https://github.com/huanxueshengmou/step2api)",
        }

    async def _call(self, method: str, payload: dict | None = None) -> dict:
        url = f"{self.base}{RPC_PREFIX}/{method}"
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(self.settings.probe_timeout, connect=self.settings.connect_timeout),
            "follow_redirects": False,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy

        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.post(url, headers=self._headers(), json=payload or {})

        if resp.status_code in (401, 403):
            detail = ""
            try:
                detail = resp.json().get("message", "")
            except ValueError:
                detail = resp.text[:120]
            if "expired" in detail.lower():
                raise ConsoleAuthError(
                    "控制台凭据已过期（Oasis-Token 实测寿命约 2 小时，"
                    "Cookie 自带的过期时间不可信）。请重新从浏览器复制一次。"
                )
            if "embezzled" in detail:
                raise ConsoleAuthError(
                    "控制台凭据被拒绝：Oasis-appID 必须为 20700，且 Oasis-Webid 需取自"
                    f"浏览器的 localStorage.web_id（不是 Cookie）。（{detail}）"
                )
            raise ConsoleAuthError(f"控制台凭据无效或已过期：{detail or resp.status_code}")
        if resp.status_code == 307:
            raise ConsoleAuthError("控制台会话已失效（被重定向到登录页），请重新登录后更新令牌")
        if resp.status_code >= 400:
            raise RuntimeError(f"{method} 返回 HTTP {resp.status_code}: {resp.text[:160]}")

        try:
            return resp.json()
        except ValueError as exc:
            raise RuntimeError(f"{method} 返回非 JSON：{resp.text[:160]}") from exc

    # -- 续期 -----------------------------------------------------------
    async def refresh_session(self) -> str | None:
        """用 refresh 换一组新的 access + refresh，返回新的完整 Cookie。

        实测 **access 过期后依然可以刷新**（这是能持续监控的关键）。
        必须带完整的两段凭据去调；只给 refresh 会返回 ``token is illegal``。
        """
        url = (
            f"{self.base}/passport/proto.api.passport.v1"
            f".PassportService/RefreshToken"
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": self.base,
            "Referer": self.base + "/",
            "User-Agent": "step2api/1.0",
            "oasis-appid": str(self.app_id),
            "oasis-platform": "web",
            "oasis-webid": self.webid,
            "Oasis-Webid": self.webid,
            "Oasis-Token": self.token,
        }
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(self.settings.probe_timeout, connect=self.settings.connect_timeout),
            "follow_redirects": False,
        }
        if self.proxy:
            kwargs["proxy"] = self.proxy

        async with httpx.AsyncClient(**kwargs) as client:
            resp = await client.post(url, headers=headers, json={})

        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:
            return None

        access = ((data.get("accessToken") or {}).get("raw") or "").strip()
        refresh = ((data.get("refreshToken") or {}).get("raw") or "").strip()
        if not access:
            return None
        if not refresh:
            _, refresh = split_cookie(self.token)
        return combine_cookie(access, refresh)

    # -- 公开方法 -------------------------------------------------------
    async def get_status(self) -> PlanStatus:
        data = await self._call("GetStepPlanStatus")
        sub = data.get("subscription") or {}
        return PlanStatus(
            plan_name=sub.get("name"),
            plan_type=sub.get("plan_type"),
            plan_id=sub.get("plan_id"),
            plan_family=sub.get("plan_family"),
            status=SUB_STATUS.get(sub.get("status"), f"code:{sub.get('status')}"),
            pay_channel=sub.get("pay_channel"),
            activated_at=_dt(sub.get("activated_at")),
            expired_at=_dt(sub.get("expired_at")),
            auto_renew=bool(sub.get("auto_renew")),
        )

    async def get_rate_limit(self) -> tuple[dict, list[CreditBucket]]:
        data = await self._call("QueryStepPlanRateLimit")
        buckets: list[CreditBucket] = []
        for raw in ((data.get("plan_credit_rate_limit") or {}).get("credit_buckets") or []):
            buckets.append(
                CreditBucket(
                    type=BUCKET_TYPE.get(raw.get("type"), f"code:{raw.get('type')}"),
                    total=_f(raw.get("credit_total")),
                    residual=_f(raw.get("credit_residual")),
                    expire_at=_dt(raw.get("expire_at")),
                    next_reset_at=_dt(raw.get("next_reset_at")),
                )
            )
        return data, buckets

    async def query_usages(
        self, *, page: int = 1, page_size: int = 20,
        start_ms: int | None = None, end_ms: int | None = None,
    ) -> tuple[list[dict], int]:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        payload = {
            "page": page,
            "pageSize": page_size,
            "startTime": str(start_ms if start_ms is not None else now_ms - 30 * 86400 * 1000),
            "toTime": str(end_ms if end_ms is not None else now_ms),
        }
        data = await self._call("QueryStepPlanUsages", payload)
        return list(data.get("records") or []), int(data.get("total") or 0)

    async def fetch(self, *, with_usage: bool = False, usage_page_size: int = 20) -> ConsoleQuota:
        """一次性拉取完整额度视图。

        access 过期时自动用 refresh 续期一次再重试；续期成功后会把新 Cookie
        放在 ``quota.renewed_token`` 里，由调用方回写数据库。
        """
        # 先试一次。access 通常只有 2 小时，过期后必须靠 refresh 换新的。
        quota = await self._fetch_once(with_usage=with_usage, usage_page_size=usage_page_size)
        if quota.ok:
            return quota

        # 失败就尝试续期一次：不预先判断 exp —— 时钟偏差、服务端提前失效
        # 都会让"看起来没过期"的凭据实际不可用，直接以请求结果为准更可靠。
        renewed = await self.refresh_session()
        if not renewed or renewed == self.token:
            return quota

        previous_token = self.token
        self.token = renewed
        self.token_expires_at = parse_token_expiry(renewed)
        retry = await self._fetch_once(with_usage=with_usage, usage_page_size=usage_page_size)

        if retry.ok:
            retry.renewed_token = renewed
            return retry

        # 续期后的凭据也不好用 —— 回滚，把原始错误报给用户
        self.token = previous_token
        self.token_expires_at = parse_token_expiry(previous_token)
        return quota

    async def _fetch_once(
        self, *, with_usage: bool = False, usage_page_size: int = 20
    ) -> ConsoleQuota:
        """拉取一次额度（不做续期）。"""
        _, refresh_part = split_cookie(self.token)
        quota = ConsoleQuota(
            credential_expires_at=self.token_expires_at,
            refresh_expires_at=parse_token_expiry(refresh_part),
        )
        try:
            quota.status = await self.get_status()
            rate, buckets = await self.get_rate_limit()
            quota.buckets = buckets
            quota.five_hour_left, quota.five_hour_reset_at = _window(
                rate.get("five_hour_usage_left_rate"), rate.get("five_hour_usage_reset_time")
            )
            quota.weekly_left, quota.weekly_reset_at = _window(
                rate.get("weekly_usage_left_rate"), rate.get("weekly_usage_reset_time")
            )
            limit = rate.get("plan_credit_rate_limit") or {}
            quota.subscription_left = _rate(limit.get("subscription_credit_left_rate"))
            quota.topup_left = _rate(limit.get("topup_credit_left_rate"))
            if with_usage:
                quota.usage_records, quota.usage_total = await self.query_usages(
                    page_size=usage_page_size
                )
            quota.ok = True
        except ConsoleAuthError as exc:
            quota.ok = False
            quota.error = str(exc)
        except (httpx.HTTPError, RuntimeError) as exc:
            quota.ok = False
            quota.error = f"{type(exc).__name__}: {exc}"
        return quota

    async def test(self) -> ConsoleQuota:
        """仅校验凭据是否可用。"""
        return await self.fetch(with_usage=False)


__all__ = [
    "BUCKET_TYPE",
    "combine_cookie",
    "parse_token_expiry",
    "split_cookie",
    "SUB_STATUS",
    "ConsoleAuthError",
    "ConsoleClient",
    "ConsoleQuota",
    "CreditBucket",
    "PlanStatus",
]
