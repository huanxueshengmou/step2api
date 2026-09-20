"""REST 管理 API。

全部挂在 ``/api`` 前缀下，由 ``manage_guard`` 做鉴权（配置了
``STEP2API_ADMIN_TOKEN`` 时要求 ``Authorization: Bearer <token>`` 或
``X-Admin-Token``）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, field_validator

from .config import CN_HOST_MARKERS, PORTAL_URL, Settings, get_settings
from .crypto import key_fingerprint, key_hint
from .proxy import ProxyError, normalize_proxy, parse_proxy_list, redact_proxy
from .quota import probe_plan_endpoint
from .router import NoAccountAvailable
from .store import Store, get_store

log = logging.getLogger("step2api.api")

router = APIRouter(prefix="/api")

# --------------------------------------------------------------------------
# 鉴权
# --------------------------------------------------------------------------


async def manage_guard(request: Request) -> None:
    """管理接口鉴权。"""
    settings: Settings = request.app.state.settings
    token = settings.admin_token
    if not token:
        return
    provided = (
        request.headers.get("x-admin-token")
        or (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        or request.query_params.get("token")
    )
    if not secrets.compare_digest(provided or "", token):
        raise HTTPException(status_code=401, detail="管理令牌无效")


guard = [Depends(manage_guard)]


def _store(request: Request) -> Store:
    return request.app.state.store


def _settings(request: Request) -> Settings:
    return request.app.state.settings


# --------------------------------------------------------------------------
# 序列化
# --------------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _seconds_until(value: Any) -> int | None:
    dt = _parse_dt(value)
    if dt is None:
        return None
    return int((dt - datetime.now(timezone.utc)).total_seconds())


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def serialize_account(row: Any, settings: Settings) -> dict:
    data = dict(row)
    remaining = data.get("credits_remaining")
    total = data.get("credits_total")

    percent = None
    if remaining is not None and total:
        try:
            percent = max(0.0, min(1.0, float(remaining) / float(total)))
        except (TypeError, ZeroDivisionError):
            percent = None

    reset_at = data.get("quota_reset_at")
    expires_at = data.get("quota_expires_at")
    seconds = _seconds_until(reset_at) if reset_at else _seconds_until(expires_at)

    low = bool(
        settings.low_quota_ratio > 0
        and percent is not None
        and percent <= settings.low_quota_ratio
    )

    return {
        "id": data["id"],
        "name": data.get("name"),
        "key_hint": data.get("key_hint"),
        "enabled": bool(data.get("enabled")),
        "weight": data.get("weight"),
        "priority": data.get("priority"),
        "group_name": data.get("group_name"),
        "note": data.get("note"),
        "plan_base": data.get("plan_base") or settings.plan_base,
        "balance_base": data.get("balance_base") or settings.upstream_base,
        "plan_endpoint": data.get("plan_endpoint"),
        "max_concurrency": data.get("max_concurrency"),
        "proxy_mode": data.get("proxy_mode"),
        "pool_id": data.get("pool_id"),
        "proxy_id": data.get("proxy_id"),
        "proxy_rotation": bool(data.get("proxy_rotation")),
        # Step Plan 订阅维度
        "plan_name": data.get("plan_name"),
        "plan_status": data.get("plan_status"),
        "credits_remaining": remaining,
        "credits_total": total,
        "credits_used": data.get("credits_used"),
        "percent_remaining": percent,
        "quota_reset_at": _iso(reset_at),
        "quota_expires_at": _iso(expires_at),
        "quota_source": data.get("quota_source"),
        "quota_ok": bool(data.get("quota_ok")),
        "quota_error": data.get("quota_error"),
        "quota_checked_at": _iso(data.get("quota_checked_at")),
        "seconds_remaining": seconds,
        "days_remaining": None if seconds is None else round(seconds / 86400.0, 2),
        "plan_expired": bool(seconds is not None and seconds <= 0),
        "low_quota": low,
        # 按量计费维度
        "balance": data.get("balance"),
        "cash_balance": data.get("cash_balance"),
        "voucher_balance": data.get("voucher_balance"),
        "account_type": data.get("account_type"),
        "balance_checked_at": _iso(data.get("balance_checked_at")),
        "currency": settings.currency,
        # 运行状态
        "status": data.get("status"),
        "last_error": data.get("last_error"),
        "cooldown_until": _iso(data.get("cooldown_until")),
        "fail_streak": data.get("fail_streak"),
        "success_count": data.get("success_count"),
        "failure_count": data.get("failure_count"),
        "total_requests": data.get("total_requests"),
        "last_used_at": _iso(data.get("last_used_at")),
        "created_at": _iso(data.get("created_at")),
        "updated_at": _iso(data.get("updated_at")),
    }


def serialize_pool(row: Any, store: Store) -> dict:
    data = dict(row)
    members = store.list_pool_members(int(data["id"]))
    return {
        "id": data["id"],
        "name": data.get("name"),
        "strategy": data.get("strategy"),
        "affinity": data.get("affinity"),
        "cooldown": data.get("cooldown"),
        "fallback_direct": bool(data.get("fallback_direct")),
        "enabled": bool(data.get("enabled")),
        "note": data.get("note"),
        "member_count": len(members),
        "member_ids": [int(m["id"]) for m in members],
        "members": [store.proxy_public(m) for m in members],
        "created_at": _iso(data.get("created_at")),
        "updated_at": _iso(data.get("updated_at")),
    }


# --------------------------------------------------------------------------
# 请求模型
# --------------------------------------------------------------------------

ProxyMode = Literal["inherit", "direct", "pool", "dedicated"]
RoutingMode = Literal["sticky", "round_robin", "least_used", "priority", "random"]
ProxyStrategy = Literal["round_robin", "random", "least_used", "lowest_latency"]


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    api_key: str = Field(min_length=8)
    group_name: str = "default"
    weight: int = Field(default=1, ge=1, le=100)
    priority: int = Field(default=100, ge=0, le=10000)
    note: str = ""
    plan_base: str = ""
    balance_base: str = ""
    max_concurrency: int = Field(default=0, ge=0, le=1000)
    proxy_mode: ProxyMode = "inherit"
    pool_id: int | None = None
    proxy_id: int | None = None
    proxy_rotation: bool = True
    enabled: bool = True
    verify: bool = True

    @field_validator("api_key")
    @classmethod
    def _strip_key(cls, value: str) -> str:
        return value.strip()


class AccountUpdate(BaseModel):
    name: str | None = None
    group_name: str | None = None
    weight: int | None = Field(default=None, ge=1, le=100)
    priority: int | None = Field(default=None, ge=0, le=10000)
    note: str | None = None
    plan_base: str | None = None
    balance_base: str | None = None
    max_concurrency: int | None = Field(default=None, ge=0, le=1000)
    proxy_mode: ProxyMode | None = None
    pool_id: int | None = None
    proxy_id: int | None = None
    proxy_rotation: bool | None = None
    enabled: bool | None = None
    api_key: str | None = None


class ImportRequest(BaseModel):
    """批量导入国外站账号。

    支持每行 ``API_KEY`` 或 ``API_KEY|代理URL``；``#`` 之后为注释。
    也接受 JSON 数组 / 对象。
    """

    content: str = Field(min_length=1)
    group_name: str = "default"
    weight: int = Field(default=1, ge=1, le=100)
    priority: int = Field(default=100, ge=0, le=10000)
    name_prefix: str = ""
    verify: bool = True
    dedupe: bool = True
    #: 代理分配方式
    proxy_assign: Literal["none", "dedicated", "pool", "global"] = "none"
    pool_id: int | None = None
    new_pool_name: str | None = None
    pool_strategy: ProxyStrategy = "round_robin"
    proxy_rotation: bool = True
    plan_base: str = ""
    balance_base: str = ""
    enabled: bool = True


class ProxyCreate(BaseModel):
    url: str
    label: str = ""
    enabled: bool = True
    check: bool = False


class ProxyBulkCreate(BaseModel):
    urls: str
    prefix: str = ""
    enabled: bool = True
    check: bool = False


class ProxyUpdate(BaseModel):
    label: str | None = None
    enabled: bool | None = None


class PoolCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    strategy: ProxyStrategy = "round_robin"
    affinity: Literal["sticky", "rotate"] = "sticky"
    cooldown: float = Field(default=60.0, ge=0, le=3600)
    fallback_direct: bool = True
    note: str = ""
    enabled: bool = True
    proxy_ids: list[int] = Field(default_factory=list)


class PoolUpdate(BaseModel):
    name: str | None = None
    strategy: ProxyStrategy | None = None
    affinity: Literal["sticky", "rotate"] | None = None
    cooldown: float | None = Field(default=None, ge=0, le=3600)
    fallback_direct: bool | None = None
    note: str | None = None
    enabled: bool | None = None
    proxy_ids: list[int] | None = None


class RoutingUpdate(BaseModel):
    routing_mode: RoutingMode | None = None
    proxy_strategy: ProxyStrategy | None = None
    affinity_ttl: float | None = Field(default=None, ge=60, le=30 * 86400)
    cooldown_seconds: float | None = Field(default=None, ge=0, le=3600)
    max_retries: int | None = Field(default=None, ge=1, le=10)
    default_max_concurrency: int | None = Field(default=None, ge=1, le=1000)
    refresh_interval: float | None = Field(default=None, ge=30, le=86400)
    low_quota_ratio: float | None = Field(default=None, ge=0, le=1)
    global_proxy: str | None = None
    clear_global_proxy: bool = False
    proxy_check_url: str | None = None


# --------------------------------------------------------------------------
# 导入解析
# --------------------------------------------------------------------------

#: StepFun key 形如 ``sk-...``；这里放宽到任意不含空白的 token 以提高兼容性
_KEY_RE = re.compile(r"\bsk-[A-Za-z0-9_\-]{12,}\b")
_GENERIC_RE = re.compile(r"\b[A-Za-z0-9_\-]{24,}\b")

_SPLIT_RE = re.compile(r"\s*(?:\||	|----|,|;)\s*")


class ImportCandidate(BaseModel):
    raw: str
    api_key: str
    label: str = ""
    proxy: str | None = None
    ok: bool = True
    reason: str | None = None
    duplicate: bool = False


def _looks_like_foreign(text: str, allow_cn: bool) -> tuple[bool, str | None]:
    """只允许国外站。命中国内站域名直接拒绝。"""
    lowered = text.lower()
    if any(marker in lowered for marker in CN_HOST_MARKERS):
        if not allow_cn:
            return False, "检测到国内站域名（stepfun.com），本项目只支持国外站 account.stepfun.ai"
        return True, None
    return True, None


def parse_import_content(
    content: str,
    *,
    settings: Settings,
    name_prefix: str = "",
    existing_fps: set[str] | None = None,
    dedupe: bool = True,
) -> list[ImportCandidate]:
    """把粘贴内容解析成候选账号列表。

    支持格式：

    * 每行一个 key
    * ``key|代理URL`` / ``key----代理URL`` （制表符、逗号、分号亦可）
    * JSON 数组 ``["sk-..","sk-.."]``
    * JSON 对象数组 ``[{"api_key":"sk-..","name":"a","proxy":"http://.."}]``
    """
    existing_fps = existing_fps or set()
    out: list[ImportCandidate] = []
    seen: set[str] = set()

    def _push(
        api_key: str,
        *,
        label: str = "",
        proxy: str | None = None,
        raw: str = "",
    ) -> None:
        api_key = (api_key or "").strip().strip('"').strip("'")
        if not api_key:
            return
        ok, reason = _looks_like_foreign(raw or api_key, settings.allow_cn_site)
        fp = key_fingerprint(api_key)
        duplicate = fp in existing_fps or fp in seen
        if not ok:
            out.append(ImportCandidate(raw=raw or api_key, api_key=api_key, label=label,
                                       proxy=proxy, ok=False, reason=reason))
            return
        if dedupe and duplicate:
            out.append(
                ImportCandidate(
                    raw=raw or api_key, api_key=api_key, label=label, proxy=proxy,
                    ok=False, reason="重复的 Key（库中已存在或本次已导入）", duplicate=True,
                )
            )
            return
        seen.add(fp)
        out.append(
            ImportCandidate(raw=raw or api_key, api_key=api_key, label=label, proxy=proxy, ok=True)
        )

    text = (content or "").strip()
    if not text:
        return []

    # ---- JSON 形式 ----
    if text[0] in "[{":
        try:
            payload = json.loads(text)
        except ValueError:
            payload = None
        if payload is not None:
            items = payload if isinstance(payload, list) else [payload]
            if isinstance(payload, dict) and isinstance(payload.get("accounts"), list):
                items = payload["accounts"]
            index = 0
            for item in items:
                index += 1
                if isinstance(item, str):
                    _push(item, raw=item)
                elif isinstance(item, dict):
                    key = (
                        item.get("api_key")
                        or item.get("key")
                        or item.get("token")
                        or item.get("apikey")
                        or ""
                    )
                    _push(
                        str(key),
                        label=str(item.get("name") or item.get("label") or ""),
                        proxy=item.get("proxy") or item.get("proxy_url"),
                        raw=json.dumps(item, ensure_ascii=False),
                    )
            if out:
                return out

    # ---- 文本行形式 ----
    counter = 0
    for line in text.splitlines():
        original = line
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue

        # 去掉行尾注释
        if " #" in line:
            line = line.split(" #", 1)[0].strip()

        ok, reason = _looks_like_foreign(line, settings.allow_cn_site)

        parts = [p for p in _SPLIT_RE.split(line) if p]
        key_part = ""
        proxy_part: str | None = None
        label_part = ""

        if len(parts) >= 2:
            key_idx = None
            for idx, part in enumerate(parts):
                if _KEY_RE.search(part) or _GENERIC_RE.fullmatch(part):
                    key_idx = idx
                    break
            if key_idx is None:
                key_idx = 0
            key_part = parts[key_idx]
            rest = parts[:key_idx] + parts[key_idx + 1:]
            for part in rest:
                if proxy_part is None and ("://" in part or re.fullmatch(r"[\w.\-]+:\d{2,5}", part)):
                    proxy_part = part
                elif not label_part:
                    label_part = part
        else:
            key_part = line

        match = _KEY_RE.search(key_part) or _GENERIC_RE.search(key_part)
        api_key = match.group(0) if match else key_part

        if not api_key:
            continue

        counter += 1
        label = label_part or (f"{name_prefix}{counter}" if name_prefix else "")

        # 站点校验先于代理解析 —— 国内站是硬性拒绝，报错信息要指向真正的原因
        if not ok:
            out.append(
                ImportCandidate(
                    raw=original.strip(), api_key=api_key, label=label,
                    proxy=proxy_part, ok=False, reason=reason,
                )
            )
            continue

        if proxy_part:
            try:
                proxy_part = normalize_proxy(proxy_part)
            except ProxyError as exc:
                out.append(
                    ImportCandidate(
                        raw=original.strip(), api_key=api_key, label=label, ok=False,
                        reason=f"代理格式错误：{exc}",
                    )
                )
                continue

        _push(api_key, label=label, proxy=proxy_part, raw=original.strip())

    return out


# --------------------------------------------------------------------------
# 账号
# --------------------------------------------------------------------------


@router.get("/accounts", dependencies=guard)
async def list_accounts(
    request: Request,
    group: str | None = None,
    enabled_only: bool = False,
) -> dict:
    store = _store(request)
    settings = _settings(request)
    rows = store.list_accounts(enabled_only=enabled_only, group_name=group)
    return {
        "accounts": [serialize_account(r, settings) for r in rows],
        "groups": sorted({(r["group_name"] or "default") for r in store.list_accounts()}),
    }


@router.post("/accounts", dependencies=guard)
async def create_account(request: Request, payload: AccountCreate) -> dict:
    store = _store(request)
    settings = _settings(request)

    existing = store.get_account_by_fp(key_fingerprint(payload.api_key))
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"该 Key 已存在（账号 #{existing['id']} {existing['name']}）",
        )

    proxy_id = payload.proxy_id
    if payload.proxy_mode == "dedicated" and payload.proxy_id is None:
        raise HTTPException(status_code=400, detail="proxy_mode=dedicated 时必须指定 proxy_id")
    if payload.proxy_mode == "pool" and payload.pool_id is None:
        raise HTTPException(status_code=400, detail="proxy_mode=pool 时必须指定 pool_id")

    account_id = store.create_account(
        name=payload.name,
        api_key=payload.api_key,
        group_name=payload.group_name,
        weight=payload.weight,
        priority=payload.priority,
        note=payload.note,
        plan_base=payload.plan_base or settings.plan_base,
        balance_base=payload.balance_base or settings.upstream_base,
        max_concurrency=payload.max_concurrency,
        proxy_mode=payload.proxy_mode,
        pool_id=payload.pool_id,
        proxy_id=proxy_id,
        proxy_rotation=payload.proxy_rotation,
        enabled=payload.enabled,
    )

    result: dict = {"id": account_id, "verified": None}
    if payload.verify:
        scheduler = request.app.state.scheduler
        result["verified"] = await scheduler.refresh_account(account_id)

    row = store.get_account(account_id)
    result["account"] = serialize_account(row, settings)
    return result


@router.get("/accounts/{account_id}", dependencies=guard)
async def get_account(request: Request, account_id: int) -> dict:
    store = _store(request)
    settings = _settings(request)
    row = store.get_account(account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"account": serialize_account(row, settings)}


@router.patch("/accounts/{account_id}", dependencies=guard)
async def update_account(request: Request, account_id: int, payload: AccountUpdate) -> dict:
    store = _store(request)
    settings = _settings(request)
    row = store.get_account(account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    fields = payload.model_dump(exclude_unset=True, exclude_none=True)
    api_key = fields.pop("api_key", None)
    if api_key:
        other = store.get_account_by_fp(key_fingerprint(api_key))
        if other is not None and int(other["id"]) != account_id:
            raise HTTPException(status_code=409, detail=f"该 Key 已被账号 #{other['id']} 使用")
        store.set_account_key(account_id, api_key)

    if fields.get("proxy_mode") == "dedicated" and not (
        fields.get("proxy_id") or row["proxy_id"]
    ):
        raise HTTPException(status_code=400, detail="proxy_mode=dedicated 时必须指定 proxy_id")

    store.update_account(account_id, **fields)
    return {"account": serialize_account(store.get_account(account_id), settings)}


@router.delete("/accounts/{account_id}", dependencies=guard)
async def delete_account(request: Request, account_id: int) -> dict:
    store = _store(request)
    if store.get_account(account_id) is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    store.delete_account(account_id)
    return {"deleted": account_id}


@router.post("/accounts/{account_id}/refresh", dependencies=guard)
async def refresh_account(request: Request, account_id: int) -> dict:
    store = _store(request)
    if store.get_account(account_id) is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    result = await request.app.state.scheduler.refresh_account(account_id)
    return {"result": result}


@router.post("/accounts/{account_id}/probe-endpoint", dependencies=guard)
async def probe_endpoint(request: Request, account_id: int) -> dict:
    """逐个探测 Step Plan 额度候选端点，用于确认上游实际路径。"""
    store = _store(request)
    settings = _settings(request)
    row = store.get_account(account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="账号不存在")

    account = dict(row)
    try:
        proxy = request.app.state.router.resolve_proxy(account)
        proxy_url = proxy.url
    except Exception:  # noqa: BLE001
        proxy_url = None

    hit, lines = await probe_plan_endpoint(
        store.decrypt_key(row), settings=settings, proxy=proxy_url
    )
    if hit:
        store.update_account(account_id, plan_endpoint=hit)
    return {"hit": hit, "detail": lines}


@router.post("/accounts/{account_id}/reset", dependencies=guard)
async def reset_account(request: Request, account_id: int) -> dict:
    """清除冷却与失败计数。"""
    store = _store(request)
    if store.get_account(account_id) is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    store.clear_cooldown(account_id)
    return {"ok": True}


@router.post("/accounts/{account_id}/toggle", dependencies=guard)
async def toggle_account(request: Request, account_id: int) -> dict:
    store = _store(request)
    settings = _settings(request)
    row = store.get_account(account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    store.update_account(account_id, enabled=not bool(row["enabled"]))
    return {"account": serialize_account(store.get_account(account_id), settings)}


# --------------------------------------------------------------------------
# 导入
# --------------------------------------------------------------------------


@router.post("/import/preview", dependencies=guard)
async def import_preview(request: Request, payload: ImportRequest) -> dict:
    """只解析并校验，不落库。"""
    store = _store(request)
    settings = _settings(request)
    existing = {r["key_fp"] for r in store.list_accounts()}
    candidates = parse_import_content(
        payload.content,
        settings=settings,
        name_prefix=payload.name_prefix,
        existing_fps=existing,
        dedupe=payload.dedupe,
    )
    return {
        "total": len(candidates),
        "importable": sum(1 for c in candidates if c.ok),
        "rejected": sum(1 for c in candidates if not c.ok),
        "items": [
            {
                "key_hint": key_hint(c.api_key),
                "label": c.label,
                "proxy": redact_proxy(c.proxy) if c.proxy else None,
                "ok": c.ok,
                "reason": c.reason,
                "duplicate": c.duplicate,
            }
            for c in candidates
        ],
        "portal": PORTAL_URL,
    }


@router.post("/import", dependencies=guard)
async def import_accounts(request: Request, payload: ImportRequest) -> dict:
    """批量导入（只支持国外站）。"""
    store = _store(request)
    settings = _settings(request)
    scheduler = request.app.state.scheduler

    # 国内站拒绝走逐行判定（见 parse_import_content），这样一批里只有违规的
    # 那一行被跳过，而不是整批拒绝 —— 用户粘贴的内容里出现注释或说明文字
    # 提到 stepfun.com 时不应该连带把合法 Key 一起挡掉。

    existing = {r["key_fp"] for r in store.list_accounts()}
    candidates = parse_import_content(
        payload.content,
        settings=settings,
        name_prefix=payload.name_prefix,
        existing_fps=existing,
        dedupe=payload.dedupe,
    )
    importable = [c for c in candidates if c.ok]

    if not importable:
        return {
            "imported": [],
            "skipped": [
                {"key_hint": key_hint(c.api_key), "reason": c.reason} for c in candidates
            ],
            "total": 0,
        }

    # ---- 代理准备 ----
    pool_id = payload.pool_id
    created_proxy_ids: list[int] = []
    if payload.proxy_assign in ("dedicated", "pool"):
        proxy_urls = [c.proxy for c in importable if c.proxy]
        if proxy_urls:
            unique: list[str] = []
            seen_urls: set[str] = set()
            for url in proxy_urls:
                if url not in seen_urls:
                    seen_urls.add(url)
                    unique.append(url)
            for url in unique:
                row = store.get_proxy_by_url(url)
                if row is not None:
                    created_proxy_ids.append(int(row["id"]))
                else:
                    created_proxy_ids.append(store.create_proxy(url, label=redact_proxy(url)))
            if payload.proxy_assign == "pool" and pool_id is None:
                pool_id = store.create_pool(
                    payload.new_pool_name or f"import-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}",
                    strategy=payload.pool_strategy,
                )
            if payload.proxy_assign == "pool" and pool_id:
                store.add_pool_members(pool_id, created_proxy_ids)

    # ---- 落库 ----
    imported: list[dict] = []
    for index, cand in enumerate(importable, start=1):
        name = cand.label or (
            f"{payload.name_prefix}{index}" if payload.name_prefix else key_hint(cand.api_key)
        )

        proxy_mode = "inherit"
        proxy_id = None
        row_pool_id = None
        if payload.proxy_assign == "dedicated" and cand.proxy:
            row = store.get_proxy_by_url(cand.proxy)
            if row is not None:
                proxy_mode = "dedicated"
                proxy_id = int(row["id"])
        elif payload.proxy_assign == "pool" and pool_id:
            proxy_mode = "pool"
            row_pool_id = pool_id

        try:
            account_id = store.create_account(
                name=name,
                api_key=cand.api_key,
                group_name=payload.group_name,
                weight=payload.weight,
                priority=payload.priority,
                plan_base=payload.plan_base or settings.plan_base,
                balance_base=payload.balance_base or settings.upstream_base,
                proxy_mode=proxy_mode,
                pool_id=row_pool_id,
                proxy_id=proxy_id,
                proxy_rotation=payload.proxy_rotation,
                enabled=payload.enabled,
            )
        except Exception as exc:  # noqa: BLE001 - 唯一索引冲突等
            imported.append(
                {"id": None, "name": name, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
            )
            continue

        item: dict = {"id": account_id, "name": name, "ok": True, "verified": None}
        if payload.verify:
            item["verified"] = await scheduler.refresh_account(account_id)
        imported.append(item)

    return {
        "total": len(imported),
        "imported": imported,
        "skipped": [
            {"key_hint": key_hint(c.api_key), "reason": c.reason} for c in candidates if not c.ok
        ],
        "pool_id": pool_id,
    }


# --------------------------------------------------------------------------
# 代理
# --------------------------------------------------------------------------


@router.get("/proxies", dependencies=guard)
async def list_proxies(request: Request) -> dict:
    store = _store(request)
    return {"proxies": [store.proxy_public(r) for r in store.list_proxies()]}


@router.post("/proxies", dependencies=guard)
async def create_proxy(request: Request, payload: ProxyCreate) -> dict:
    store = _store(request)
    try:
        url = normalize_proxy(payload.url)
    except ProxyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not url:
        raise HTTPException(status_code=400, detail="代理地址不能为空")

    if store.get_proxy_by_url(url) is not None:
        raise HTTPException(status_code=409, detail="该代理已存在")

    proxy_id = store.create_proxy(
        url, label=payload.label or redact_proxy(url), enabled=payload.enabled,
        scheme=url.split("://", 1)[0],
    )
    if payload.check:
        await request.app.state.scheduler.check_proxies([proxy_id])
    row = store.get_proxy(proxy_id)
    return {"proxy": store.proxy_public(row)}


@router.post("/proxies/bulk", dependencies=guard)
async def bulk_create_proxies(request: Request, payload: ProxyBulkCreate) -> dict:
    store = _store(request)
    try:
        urls = parse_proxy_list(payload.urls)
    except ProxyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    created: list[dict] = []
    skipped: list[dict] = []
    new_ids: list[int] = []
    for index, url in enumerate(urls, start=1):
        if store.get_proxy_by_url(url) is not None:
            skipped.append({"url": redact_proxy(url), "reason": "已存在"})
            continue
        label = f"{payload.prefix}{index}" if payload.prefix else redact_proxy(url)
        proxy_id = store.create_proxy(
            url, label=label, enabled=payload.enabled, scheme=url.split("://", 1)[0]
        )
        new_ids.append(proxy_id)
        created.append(store.proxy_public(store.get_proxy(proxy_id)))

    if payload.check and new_ids:
        await request.app.state.scheduler.check_proxies(new_ids)
        created = [store.proxy_public(store.get_proxy(i)) for i in new_ids]

    return {"created": created, "skipped": skipped, "total": len(created)}


@router.patch("/proxies/{proxy_id}", dependencies=guard)
async def update_proxy(request: Request, proxy_id: int, payload: ProxyUpdate) -> dict:
    store = _store(request)
    if store.get_proxy(proxy_id) is None:
        raise HTTPException(status_code=404, detail="代理不存在")
    store.update_proxy(proxy_id, **payload.model_dump(exclude_unset=True, exclude_none=True))
    return {"proxy": store.proxy_public(store.get_proxy(proxy_id))}


@router.delete("/proxies/{proxy_id}", dependencies=guard)
async def delete_proxy(request: Request, proxy_id: int) -> dict:
    store = _store(request)
    if store.get_proxy(proxy_id) is None:
        raise HTTPException(status_code=404, detail="代理不存在")
    store.delete_proxy(proxy_id)
    return {"deleted": proxy_id}


@router.post("/proxies/check", dependencies=guard)
async def check_proxies(request: Request, proxy_ids: list[int] | None = None) -> dict:
    results = await request.app.state.scheduler.check_proxies(proxy_ids)
    return {"results": results}


# --------------------------------------------------------------------------
# 代理池
# --------------------------------------------------------------------------


@router.get("/pools", dependencies=guard)
async def list_pools(request: Request) -> dict:
    store = _store(request)
    return {"pools": [serialize_pool(r, store) for r in store.list_pools()]}


@router.post("/pools", dependencies=guard)
async def create_pool(request: Request, payload: PoolCreate) -> dict:
    store = _store(request)
    if any((r["name"] or "").lower() == payload.name.lower() for r in store.list_pools()):
        raise HTTPException(status_code=409, detail="同名代理池已存在")
    pool_id = store.create_pool(
        payload.name,
        strategy=payload.strategy,
        affinity=payload.affinity,
        cooldown=payload.cooldown,
        fallback_direct=payload.fallback_direct,
        note=payload.note,
        enabled=payload.enabled,
    )
    if payload.proxy_ids:
        store.set_pool_members(pool_id, payload.proxy_ids)
    return {"pool": serialize_pool(store.get_pool(pool_id), store)}


@router.patch("/pools/{pool_id}", dependencies=guard)
async def update_pool(request: Request, pool_id: int, payload: PoolUpdate) -> dict:
    store = _store(request)
    if store.get_pool(pool_id) is None:
        raise HTTPException(status_code=404, detail="代理池不存在")
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    proxy_ids = data.pop("proxy_ids", None)
    store.update_pool(pool_id, **data)
    if proxy_ids is not None:
        store.set_pool_members(pool_id, proxy_ids)
    return {"pool": serialize_pool(store.get_pool(pool_id), store)}


@router.delete("/pools/{pool_id}", dependencies=guard)
async def delete_pool(request: Request, pool_id: int) -> dict:
    store = _store(request)
    if store.get_pool(pool_id) is None:
        raise HTTPException(status_code=404, detail="代理池不存在")
    store.delete_pool(pool_id)
    return {"deleted": pool_id}


@router.post("/pools/{pool_id}/rotate", dependencies=guard)
async def rotate_pool(request: Request, pool_id: int) -> dict:
    """清空该池相关的会话代理绑定，让下次请求重新轮转。"""
    store = _store(request)
    if store.get_pool(pool_id) is None:
        raise HTTPException(status_code=404, detail="代理池不存在")
    affected = 0
    for row in store.list_sessions(limit=10000):
        if row["pool_id"] == pool_id:
            store.bind_session_proxy(row["session_key"], None)
            affected += 1
    return {"rotated": affected}


# --------------------------------------------------------------------------
# 会话 / 日志 / 统计
# --------------------------------------------------------------------------


@router.get("/sessions", dependencies=guard)
async def list_sessions(request: Request, limit: int = Query(default=200, ge=1, le=2000)) -> dict:
    store = _store(request)
    rows = store.list_sessions(limit=limit)
    out = []
    for row in rows:
        account = store.get_account(int(row["account_id"]))
        out.append(
            {
                "session_key": row["session_key"],
                "account_id": row["account_id"],
                "account_name": account["name"] if account else None,
                "pool_id": row["pool_id"],
                "proxy": redact_proxy(row["proxy_url"]) if row["proxy_url"] else "直连",
                "hits": row["hits"],
                "created_at": _iso(row["created_at"]),
                "expires_at": _iso(row["expires_at"]),
            }
        )
    return {"sessions": out}


@router.delete("/sessions/{session_key:path}", dependencies=guard)
async def drop_session(request: Request, session_key: str) -> dict:
    store = _store(request)
    store.drop_session(session_key)
    return {"deleted": session_key}


@router.get("/logs", dependencies=guard)
async def list_logs(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    account_id: int | None = None,
) -> dict:
    store = _store(request)
    rows = store.list_logs(limit=limit, offset=offset, account_id=account_id)
    return {
        "logs": [
            {
                "id": r["id"],
                "created_at": _iso(r["created_at"]),
                "account_id": r["account_id"],
                "account_name": r["account_name"],
                "session_key": r["session_key"],
                "method": r["method"],
                "path": r["path"],
                "model": r["model"],
                "channel": r["channel"],
                "status_code": r["status_code"],
                "duration_ms": r["duration_ms"],
                "attempts": r["attempts"],
                "proxy_label": r["proxy_label"],
                "total_tokens": r["total_tokens"],
                "error": r["error"],
            }
            for r in rows
        ]
    }


@router.delete("/logs", dependencies=guard)
async def clear_logs(request: Request) -> dict:
    store = _store(request)
    store.execute("DELETE FROM usage_logs")
    return {"ok": True}


@router.get("/stats", dependencies=guard)
async def stats(request: Request) -> dict:
    store = _store(request)
    settings = _settings(request)

    accounts = [serialize_account(r, settings) for r in store.list_accounts()]
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()

    total_remaining = sum(
        a["credits_remaining"] for a in accounts if a["credits_remaining"] is not None
    )
    total_credits = sum(a["credits_total"] for a in accounts if a["credits_total"] is not None)
    total_balance = sum(a["balance"] for a in accounts if a["balance"] is not None)

    by_status: dict[str, int] = {}
    for a in accounts:
        by_status[a["status"] or "unknown"] = by_status.get(a["status"] or "unknown", 0) + 1

    return {
        "accounts": {
            "total": len(accounts),
            "enabled": sum(1 for a in accounts if a["enabled"]),
            "healthy": by_status.get("healthy", 0),
            "cooldown": by_status.get("cooldown", 0),
            "low_quota": sum(1 for a in accounts if a["low_quota"]),
            "quota_ok": sum(1 for a in accounts if a["quota_ok"]),
            "by_status": by_status,
        },
        "credits": {
            "remaining": round(total_remaining, 2),
            "total": round(total_credits, 2),
            "percent": round(total_remaining / total_credits, 4) if total_credits else None,
            "currency": settings.currency,
        },
        "money": {
            "balance": round(total_balance, 4),
            "currency": settings.currency,
        },
        "routing": request.app.state.router.status(),
        "usage_24h": [dict(r) for r in store.log_summary(since)],
        "last_refresh_at": _iso(request.app.state.scheduler.last_refresh_at),
        "last_refresh_error": request.app.state.scheduler.last_refresh_error,
    }


# --------------------------------------------------------------------------
# 设置 / 导出
# --------------------------------------------------------------------------


@router.get("/settings", dependencies=guard)
async def get_config(request: Request) -> dict:
    settings = _settings(request)
    data = settings.redacted()
    data["routing_mode"] = request.app.state.settings.routing_mode
    data["proxy_strategy"] = request.app.state.router.proxy_pool.strategy
    data["global_proxy_set"] = bool(settings.global_proxy)
    data["global_proxy"] = redact_proxy(settings.global_proxy) if settings.global_proxy else None
    return {"settings": data, "portal": PORTAL_URL}


@router.post("/settings/routing", dependencies=guard)
async def update_routing(request: Request, payload: RoutingUpdate) -> dict:
    settings: Settings = _settings(request)
    router_ = request.app.state.router
    scheduler = request.app.state.scheduler

    data = payload.model_dump(exclude_unset=True)

    if payload.clear_global_proxy:
        settings.global_proxy = None
    elif payload.global_proxy is not None:
        try:
            settings.global_proxy = normalize_proxy(payload.global_proxy)
        except ProxyError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if data.get("routing_mode"):
        settings.routing_mode = data["routing_mode"]
    if data.get("proxy_strategy"):
        settings.proxy_strategy = data["proxy_strategy"]
        router_.proxy_pool.strategy = data["proxy_strategy"]
    if data.get("proxy_check_url"):
        settings.proxy_check_url = data["proxy_check_url"]
    for field in (
        "affinity_ttl",
        "cooldown_seconds",
        "max_retries",
        "default_max_concurrency",
        "refresh_interval",
        "low_quota_ratio",
    ):
        if data.get(field) is not None:
            setattr(settings, field, data[field])

    if data.get("refresh_interval") is not None:
        await scheduler.stop()
        await scheduler.start()

    return {"settings": await get_config(request)}


@router.post("/refresh-all", dependencies=guard)
async def refresh_all(request: Request) -> dict:
    results = await request.app.state.scheduler.refresh_all()
    return {"count": len(results), "results": results}


@router.post("/proxy-check-all", dependencies=guard)
async def proxy_check_all(request: Request) -> dict:
    results = await request.app.state.scheduler.check_proxies()
    return {"results": results}


@router.get("/export/claude-code", dependencies=guard)
async def export_claude_code(request: Request) -> dict:
    """生成一份可直接粘贴的 Claude Code 配置。"""
    settings = _settings(request)
    base = f"http://{settings.host}:{settings.port}"
    token = settings.gateway_tokens[0] if settings.gateway_tokens else "step2api"
    return {
        "env": {
            "ANTHROPIC_BASE_URL": base,
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_MODEL": "step-3.5-flash",
            "ANTHROPIC_SMALL_FAST_MODEL": "step-3.5-flash",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        },
        "openai": {"base_url": f"{base}/v1", "api_key": token},
        "note": (
            "Step Plan 通道请把 BASE_URL 指向网关根路径（网关会自动映射到 "
            f"{settings.plan_base}）；按量通道指向 {base}/v1。"
        ),
    }


@router.get("/health")
async def health(request: Request) -> dict:
    return {
        "ok": True,
        "version": request.app.state.version,
        "accounts": len(_store(request).list_accounts()),
    }


# --------------------------------------------------------------------------
# 下游网关鉴权
# --------------------------------------------------------------------------


async def gateway_guard(request: Request) -> None:
    """校验下游调用方令牌。未配置 ``STEP2API_GATEWAY_TOKENS`` 则不校验。"""
    settings: Settings = request.app.state.settings
    if not settings.gateway_tokens:
        return

    provided = (
        (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        or request.headers.get("x-api-key")
        or request.query_params.get("api_key")
        or ""
    )
    for token in settings.gateway_tokens:
        if secrets.compare_digest(provided, token):
            return
    raise HTTPException(status_code=401, detail="网关令牌无效")


gateway_dep = [Depends(gateway_guard)]


def _noop() -> None:  # pragma: no cover
    return None
