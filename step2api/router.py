"""路由核心：账号选择、粘性会话、代理绑定与轮转。

一次请求的解析顺序：

1. **会话粘性** —— 从请求里提取会话键（Claude Code 的 ``metadata.user_id``、
   ``X-Session-Id``、prompt cache key、或者消息内容哈希），查 ``sessions`` 表。
   命中且账号仍然可用（未禁用、未冷却、额度未耗尽）就直接复用。
2. **候选排序** —— 未命中或粘性账号不可用时，按 ``routing_mode`` 对全部可用
   账号排序，逐个作为重试候选。
3. **代理绑定** —— 对每个候选账号解析它该走哪个代理：
   ``direct`` / ``inherit``（全局）/ ``dedicated``（专属固定）/ ``pool``（池 + 轮转）。
   会话命中时优先复用会话里记录的代理，保证同一会话出口 IP 稳定；
   池的 ``affinity=rotate`` 则每次都重新轮转。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from .config import Settings
from .proxy import ProxyPool, ResolvedProxy, is_usable, redact_proxy
from .store import Store

# --------------------------------------------------------------------------
# 会话键提取
# --------------------------------------------------------------------------

_SESSION_HEADERS = (
    "x-session-id",
    "x-conversation-id",
    "session_id",
    "conversation_id",
    "x-request-id",
    "x-trace-id",
)

_AFFINITY_HEADERS = (
    "x-step2api-account",
    "x-account-id",
    "x-sticky-account",
)


def extract_session_key(headers: dict[str, str], body: Any) -> str | None:
    """尽量从请求里挖出一个稳定的会话标识。

    优先级：显式 header > Anthropic ``metadata.user_id`` > OpenAI ``user``
    > prompt cache key > 首条 user 消息的哈希。
    """
    lowered = {k.lower(): v for k, v in (headers or {}).items()}

    for name in _SESSION_HEADERS:
        value = lowered.get(name)
        if value:
            return f"hdr:{value.strip()}"

    if isinstance(body, dict):
        meta = body.get("metadata")
        if isinstance(meta, dict):
            for key in ("user_id", "session_id", "conversation_id"):
                value = meta.get(key)
                if value:
                    return f"meta:{value}"

        user = body.get("user")
        if isinstance(user, str) and user.strip():
            return f"user:{user.strip()}"

        for key in ("prompt_cache_key", "safety_identifier"):
            value = body.get(key)
            if isinstance(value, str) and value.strip():
                return f"{key}:{value.strip()}"

        # 退而求其次：用 system + 首条 user 消息做指纹
        seed_parts: list[str] = []
        system = body.get("system")
        if isinstance(system, str):
            seed_parts.append(system)
        elif isinstance(system, list):
            for block in system:
                if isinstance(block, dict) and isinstance(block.get("text"), str):
                    seed_parts.append(block["text"])

        messages = body.get("messages")
        if isinstance(messages, list):
            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                if msg.get("role") not in ("user", "system"):
                    continue
                content = msg.get("content")
                if isinstance(content, str):
                    seed_parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and isinstance(block.get("text"), str):
                            seed_parts.append(block["text"])
                break

        if seed_parts:
            digest = hashlib.sha256("\x00".join(seed_parts).encode("utf-8")).hexdigest()
            return f"body:{digest[:32]}"

    return None


def extract_model(body: Any) -> str | None:
    if isinstance(body, dict):
        model = body.get("model")
        if isinstance(model, str):
            return model
    return None


def extract_stream(body: Any) -> bool:
    return bool(isinstance(body, dict) and body.get("stream"))


# --------------------------------------------------------------------------
# 路由上下文与目标
# --------------------------------------------------------------------------


@dataclass
class RouteContext:
    """一次上游转发所需的路由条件。"""

    session_key: str | None = None
    model: str | None = None
    group: str | None = None
    channel: str = "plan"  # plan | api
    exclude_account_ids: set[int] = field(default_factory=set)
    requested_account_id: int | None = None
    requested_proxy: str | None = None


@dataclass
class RouteTarget:
    """一个可用的路由目标：账号 + 该账号本次使用的代理。"""

    account_id: int
    account_name: str
    group_name: str
    api_key: str
    proxy: ResolvedProxy
    priority: int = 100
    weight: int = 1
    sticky: bool = False
    percent_remaining: float | None = None
    credits_remaining: float | None = None
    score: float = 0.0

    def describe(self) -> str:
        quota = (
            f"{self.percent_remaining * 100:.1f}%"
            if self.percent_remaining is not None
            else "额度未知"
        )
        return (
            f"account#{self.account_id}({self.account_name}) "
            f"quota={quota} proxy={self.proxy.display_label()}"
        )


class NoAccountAvailable(RuntimeError):
    """没有任何可用账号。"""

    def __init__(self, message: str, *, detail: dict | None = None) -> None:
        super().__init__(message)
        self.detail = detail or {}


# --------------------------------------------------------------------------
# 并发闸门
# --------------------------------------------------------------------------


class _ConcurrencyGate:
    """按账号维度的在途计数 + 信号量。"""

    def __init__(self, default_limit: int) -> None:
        self.default_limit = max(1, default_limit)
        self._sem: dict[int, asyncio.Semaphore] = {}
        self._limit: dict[int, int] = {}
        self._inflight: dict[int, int] = {}
        self._lock = threading.Lock()

    def limit_for(self, account_id: int, override: int) -> int:
        return override if override and override > 0 else self.default_limit

    def semaphore(self, account_id: int, limit: int) -> asyncio.Semaphore:
        if account_id not in self._sem or self._limit.get(account_id) != limit:
            self._sem[account_id] = asyncio.Semaphore(limit)
            self._limit[account_id] = limit
        return self._sem[account_id]

    def inflight(self, account_id: int) -> int:
        return self._inflight.get(account_id, 0)

    def incr(self, account_id: int) -> None:
        with self._lock:
            self._inflight[account_id] = self._inflight.get(account_id, 0) + 1

    def decr(self, account_id: int) -> None:
        with self._lock:
            current = self._inflight.get(account_id, 0)
            self._inflight[account_id] = max(0, current - 1)

    def snapshot(self) -> dict[int, int]:
        with self._lock:
            return dict(self._inflight)


# --------------------------------------------------------------------------
# 路由器
# --------------------------------------------------------------------------


class Router:
    """账号与代理的调度器。"""

    def __init__(self, store: Store, settings: Settings) -> None:
        self.store = store
        self.settings = settings
        self.proxy_pool = ProxyPool(settings.proxy_strategy)
        #: 每个代理池一个常驻轮转器，key = (pool_id, strategy)
        self._pool_pickers: dict[tuple[int, str], ProxyPool] = {}
        self.gate = _ConcurrencyGate(settings.default_max_concurrency)
        self._rr_counter = 0
        self._rr_lock = threading.Lock()

    # -- 内部工具 -------------------------------------------------------
    def _next_rr(self) -> int:
        with self._rr_lock:
            self._rr_counter += 1
            return self._rr_counter

    @staticmethod
    def _cooldown_active(row: dict) -> bool:
        until = row.get("cooldown_until")
        if not until:
            return False
        try:
            deadline = datetime.fromisoformat(str(until))
        except ValueError:
            return False
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return deadline > datetime.now(timezone.utc)

    def _quota_state(self, row: dict) -> tuple[bool, float | None]:
        """返回 ``(是否还有额度, 剩余比例)``。

        额度未知（从未探测成功）时返回 ``(True, None)`` —— 不因为查不到就把
        账号踢掉，只有**明确查到额度为 0 或负**才判定耗尽。
        """
        remaining = row.get("credits_remaining")
        total = row.get("credits_total")
        if remaining is None:
            return True, None
        percent = None
        if total:
            try:
                percent = max(0.0, min(1.0, float(remaining) / float(total)))
            except (TypeError, ZeroDivisionError):
                percent = None
        return float(remaining) > 0, percent

    def is_available(self, row: dict, *, ignore_quota: bool = False) -> tuple[bool, str]:
        """账号当前是否可以承接请求。"""
        if not row.get("enabled"):
            return False, "已禁用"
        if self._cooldown_active(row):
            return False, "冷却中"
        if not ignore_quota:
            has_quota, _ = self._quota_state(row)
            if not has_quota:
                return False, "额度已耗尽"
        return True, "ok"

    # -- 账号排序 -------------------------------------------------------
    def _score(self, row: dict) -> float:
        """综合得分：额度越充足、权重越高、在途越少，得分越高。"""
        _, percent = self._quota_state(row)
        quota_factor = percent if percent is not None else 0.5
        weight = max(1, int(row.get("weight") or 1))
        inflight = self.gate.inflight(int(row["id"]))
        inflight_penalty = 1.0 / (1.0 + inflight)
        # 近期失败过的账号略微降权
        fail_streak = int(row.get("fail_streak") or 0)
        reliability = 1.0 / (1.0 + fail_streak * 0.5)
        return quota_factor * weight * inflight_penalty * reliability

    def _order(self, rows: Sequence[dict], mode: str) -> list[dict]:
        rows = list(rows)
        if mode == "priority":
            return sorted(rows, key=lambda r: (int(r.get("priority") or 100), int(r["id"])))
        if mode == "least_used":
            counts = self.gate.snapshot()
            return sorted(
                rows,
                key=lambda r: (counts.get(int(r["id"]), 0), int(r.get("priority") or 100), int(r["id"])),
            )
        if mode == "round_robin":
            start = self._next_rr() % max(1, len(rows))
            return rows[start:] + rows[:start]
        if mode == "random":
            shuffled = rows[:]
            random.shuffle(shuffled)
            # 权重高的更容易被排到前面
            return sorted(shuffled, key=lambda r: random.random() / max(1, int(r.get("weight") or 1)))
        # sticky / balanced
        return sorted(rows, key=lambda r: (-self._score(r), int(r.get("priority") or 100)))

    # -- 代理解析 -------------------------------------------------------
    def _pool_members(self, pool_id: int) -> list[dict]:
        rows = self.store.list_pool_members(pool_id)
        out: list[dict] = []
        for row in rows:
            r = dict(row)
            r["url"] = self.store.decrypt_proxy_url(row)
            out.append(r)
        return out

    @staticmethod
    def _account_slot(account_id: int, member_count: int) -> int:
        """把一个账号确定性地映射到池内的某个槽位。

        用账号 ID 的散列而不是取模：相邻 ID（1、2、3…）取模容易撞到同一槽位，
        散列能把这些账号摊开打到池内不同代理上，出口 IP 才真正分散。
        """
        if member_count <= 1:
            return 0
        import zlib

        return zlib.crc32(f"step2api-account-{account_id}".encode()) % member_count

    def _pool_picker(self, pool_id: int, strategy: str) -> ProxyPool:
        """取池对应的轮转器。

        必须**按池复用同一个实例**：``round_robin`` 依赖内部计数器递增，
        每次新建实例都会把计数器归零，结果永远选中池里第一个代理 ——
        那就不是轮转了。计数器常驻内存，重启后从池首重新开始，可接受。
        """
        key = (pool_id, strategy)
        picker = self._pool_pickers.get(key)
        if picker is None or picker.strategy != strategy:
            # 共用 proxy_pool 的指标登记处，least_used / lowest_latency 才看得到
            # 全量在途数与延迟；否则每个池只看到自己那份空数据。
            picker = ProxyPool(strategy, registry=self.proxy_pool.registry)
            self._pool_pickers[key] = picker
        return picker

    def resolve_proxy(
        self,
        account: dict,
        *,
        session_key: str | None = None,
        session_proxy: str | None = None,
        force_rotate: bool = False,
    ) -> ResolvedProxy:
        """解析账号本次请求应当使用的代理。"""
        mode = (account.get("proxy_mode") or "inherit").lower()

        # 会话粘住的代理优先复用。
        #
        # 池模式（affinity=sticky）不必在此处理：账号维度已经是确定性选代理，
        # 同一账号在池内的落点是稳定的，会话只要还绑着这个账号，出口自然一致。
        # 若从会话里直接复用，反而会让"池成员被换掉后仍走旧代理"。
        if session_proxy and mode == "inherit" and not force_rotate:
            return ResolvedProxy(
                url=session_proxy, source="session", label=redact_proxy(session_proxy)
            )

        if mode == "direct":
            return ResolvedProxy(url=None, source="direct", label="直连")

        if mode == "dedicated":
            proxy_id = account.get("proxy_id")
            if proxy_id:
                row = self.store.get_proxy(int(proxy_id))
                if row and row["enabled"]:
                    url = self.store.decrypt_proxy_url(row)
                    return ResolvedProxy(
                        url=url,
                        source="dedicated",
                        proxy_id=int(proxy_id),
                        label=row["label"] or f"proxy-{proxy_id}",
                    )
            # 专属代理不可用时按设置回退
            if self.settings.global_proxy:
                return ResolvedProxy(
                    url=self.settings.global_proxy, source="global", label="全局代理"
                )
            return ResolvedProxy(url=None, source="direct", label="直连（专属代理不可用）")

        if mode == "pool":
            pool_id = account.get("pool_id")
            if pool_id:
                pool = self.store.get_pool(int(pool_id))
                if pool and pool["enabled"]:
                    members = [m for m in self._pool_members(int(pool_id)) if is_usable(m)]
                    rotate = bool(account.get("proxy_rotation", 1))
                    affinity = pool["affinity"] or "sticky"

                    if not members:
                        picked = None
                    elif not rotate:
                        # 账号关掉池内轮转：固定用池内第一个可用代理
                        picked = members[0]
                    elif affinity == "sticky":
                        # affinity=sticky：该账号在池内的落点固定，出口 IP 稳定，
                        # 不给上游风控制造抖动。想每个请求都换出口就用 rotate。
                        picked = members[self._account_slot(int(account["id"]), len(members))]
                    else:
                        strategy = pool["strategy"] or self.settings.proxy_strategy
                        picked = self._pool_picker(int(pool_id), strategy).pick(members)
                    if picked:
                        return ResolvedProxy(
                            url=picked["url"],
                            source="pool",
                            pool_id=int(pool_id),
                            proxy_id=int(picked["id"]),
                            label=picked["label"] or f"proxy-{picked['id']}",
                        )
                    if not pool["fallback_direct"]:
                        raise NoAccountAvailable(
                            f"代理池 #{pool_id} 中没有可用代理，且未开启直连回退"
                        )
            # 池不可用 → 落到全局
            if self.settings.global_proxy:
                return ResolvedProxy(
                    url=self.settings.global_proxy, source="global", label="全局代理（池回退）"
                )
            return ResolvedProxy(url=None, source="direct", label="直连（池不可用）")

        # inherit
        if self.settings.global_proxy:
            return ResolvedProxy(
                url=self.settings.global_proxy, source="global", label="全局代理"
            )
        return ResolvedProxy(url=None, source="direct", label="直连")

    # -- 主流程 ---------------------------------------------------------
    def _all_rows(self, ctx: RouteContext) -> list[dict]:
        rows = [dict(r) for r in self.store.list_accounts(enabled_only=True)]
        if ctx.group:
            grouped = [r for r in rows if (r.get("group_name") or "default") == ctx.group]
            if grouped:
                rows = grouped
        return rows

    def targets(self, ctx: RouteContext, *, limit: int | None = None) -> list[RouteTarget]:
        """返回排好序的候选目标列表。"""
        if ctx.requested_account_id:
            row = self.store.get_account(int(ctx.requested_account_id))
            if row is None:
                raise NoAccountAvailable(f"账号 #{ctx.requested_account_id} 不存在")
            rows = [dict(row)]
            ok, reason = self.is_available(rows[0], ignore_quota=False)
            if not ok and reason == "已禁用":
                raise NoAccountAvailable(f"账号 #{ctx.requested_account_id} {reason}")
        else:
            rows = self._all_rows(ctx)

        available: list[dict] = []
        for row in rows:
            if int(row["id"]) in ctx.exclude_account_ids:
                continue
            ok, _reason = self.is_available(row)
            if ok:
                available.append(row)

        if not available:
            # 全部不可用 → 尝试忽略额度限制再筛一次（把耗尽账号作为最后手段）
            for row in rows:
                if int(row["id"]) in ctx.exclude_account_ids:
                    continue
                if row.get("enabled") and not self._cooldown_active(row):
                    available.append(row)

        if not available:
            raise NoAccountAvailable(
                "没有可用账号（全部禁用 / 冷却中 / 额度耗尽）",
                detail={"candidates": len(rows)},
            )

        mode = (self.settings.routing_mode or "sticky").lower()
        ordered = self._order(available, mode)

        # 粘性账号插到最前面
        session_row = None
        session_proxy: str | None = None
        if ctx.session_key:
            session_row = self.store.get_session(ctx.session_key)
            if session_row:
                session_proxy = session_row["proxy_url"]

        out: list[RouteTarget] = []
        seen: set[int] = set()

        def _build(row: dict, *, sticky: bool) -> RouteTarget | None:
            account_id = int(row["id"])
            if account_id in seen:
                return None
            seen.add(account_id)
            _, percent = self._quota_state(row)
            try:
                proxy = self.resolve_proxy(
                    row, session_key=ctx.session_key, session_proxy=session_proxy
                )
            except NoAccountAvailable:
                return None
            return RouteTarget(
                account_id=account_id,
                account_name=row.get("name") or f"account-{account_id}",
                group_name=row.get("group_name") or "default",
                api_key=self.store.decrypt_key(row),
                proxy=proxy,
                priority=int(row.get("priority") or 100),
                weight=int(row.get("weight") or 1),
                sticky=sticky,
                percent_remaining=percent,
                credits_remaining=row.get("credits_remaining"),
                score=self._score(row),
            )

        if session_row:
            for row in ordered:
                if int(row["id"]) == int(session_row["account_id"]):
                    target = _build(row, sticky=True)
                    if target:
                        out.append(target)
                    break

        for row in ordered:
            target = _build(row, sticky=False)
            if target:
                out.append(target)

        if limit and limit > 0:
            out = out[:limit]
        return out

    def plan(self, ctx: RouteContext, *, attempts: int | None = None) -> list[RouteTarget]:
        """返回本次请求的重试计划（按顺序尝试的账号序列）。"""
        max_attempts = attempts or self.settings.max_retries
        return self.targets(ctx, limit=max_attempts)

    # -- 会话写回 -------------------------------------------------------
    def commit(self, ctx: RouteContext, target: RouteTarget) -> None:
        """请求成功后把会话绑定到该账号与代理。"""
        if not ctx.session_key:
            return
        self.store.upsert_session(
            ctx.session_key,
            account_id=target.account_id,
            ttl_seconds=self.settings.affinity_ttl,
            pool_id=target.proxy.pool_id,
            proxy_url=target.proxy.url,
        )

    def bind_proxy_to_session(self, ctx: RouteContext, target: RouteTarget) -> None:
        if ctx.session_key and target.proxy.url:
            self.store.bind_session_proxy(ctx.session_key, target.proxy.url)

    # -- 并发闸门 -------------------------------------------------------
    def limit_for(self, target: RouteTarget, row: dict | None = None) -> int:
        override = 0
        if row is not None:
            override = int(row.get("max_concurrency") or 0)
        return self.gate.limit_for(target.account_id, override)

    def acquire(self, account_id: int) -> None:
        self.gate.incr(account_id)

    def release(self, account_id: int) -> None:
        self.gate.decr(account_id)

    # -- 概览 -----------------------------------------------------------
    def status(self) -> dict:
        counts = self.gate.snapshot()
        return {
            "mode": self.settings.routing_mode,
            "affinity_ttl": self.settings.affinity_ttl,
            "inflight": {str(k): v for k, v in counts.items()},
            "proxy_strategy": self.proxy_pool.strategy,
        }


def select_channel(path: str, settings: Settings) -> str:
    """根据请求路径判断走哪条上游通道。"""
    normalized = path if path.startswith("/") else "/" + path
    plan_prefix = settings.plan_base.split("://", 1)[-1].split("/", 1)
    plan_path = "/" + plan_prefix[1] if len(plan_prefix) > 1 else "/step_plan/v1"
    if normalized.startswith(plan_path):
        return "plan"
    return "api"


def build_upstream_url(settings: Settings, path: str) -> str:
    """把下游路径映射到上游完整 URL。"""
    normalized = path if path.startswith("/") else "/" + path
    channel = select_channel(normalized, settings)

    if channel == "plan":
        base = settings.plan_base
        plan_path = "/" + settings.plan_base.split("://", 1)[-1].split("/", 1)[1]
        remainder = normalized[len(plan_path):] or "/"
        return f"{base}{remainder}"

    # 按量通道：下游 /v1/messages → 上游 https://api.stepfun.ai/v1/messages
    return f"{settings.upstream_base}{normalized}"


@dataclass
class RouteAttempt:
    """一次转发尝试的结果摘要。"""

    target: RouteTarget
    status_code: int = 0
    ok: bool = False
    error: str | None = None
    duration_ms: float = 0.0
    usage: dict = field(default_factory=dict)


def should_failover(status_code: int) -> bool:
    """哪些上游状态码值得换账号重试。"""
    return status_code in (401, 402, 403, 408, 409, 425, 429, 500, 502, 503, 504, 529)


def should_cooldown(status_code: int) -> bool:
    """哪些状态码说明该账号本身有问题，需要冷却。"""
    return status_code in (401, 402, 403, 429, 500, 502, 503, 504, 529)


def parse_usage(payload: Any) -> dict:
    """从响应里提取 token 用量（OpenAI / Anthropic 两种格式）。"""
    if not isinstance(payload, dict):
        return {}
    usage = payload.get("usage")
    if isinstance(usage, dict):
        prompt = usage.get("prompt_tokens") or usage.get("input_tokens") or 0
        completion = usage.get("completion_tokens") or usage.get("output_tokens") or 0
        total = usage.get("total_tokens") or (prompt + completion)
        return {
            "prompt_tokens": int(prompt or 0),
            "completion_tokens": int(completion or 0),
            "total_tokens": int(total or 0),
        }
    return {}
