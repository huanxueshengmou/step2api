"""上游转发网关。

职责：

* 把下游请求映射到 StepFun 国际站的两条通道（``/step_plan/v1/*`` 订阅通道、
  ``/v1/*`` 按量通道）
* 按路由器给出的候选序列逐个尝试，实现跨账号故障转移
* **流式透传** —— SSE 边收边发，不在网关侧缓冲整段响应
* 从响应中抠出 token 用量写入日志
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import Settings
from .proxy import normalize_proxy
from .router import (
    NoAccountAvailable,
    RouteContext,
    RouteTarget,
    Router,
    build_upstream_url,
    extract_model,
    extract_session_key,
    parse_usage,
    select_channel,
    should_cooldown,
    should_failover,
)
from .store import Store

#: 不应透传给上游的逐跳头部
HOP_BY_HOP = {
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "accept-encoding",
}

#: 网关自己消费、不透传的头部
GATEWAY_HEADERS = {
    "x-step2api-account",
    "x-account-id",
    "x-sticky-account",
    "x-step2api-proxy",
    "x-step2api-no-retry",
    "x-step2api-channel",
}

#: 响应中回给客户端、标明本次实际路由结果
ROUTE_ECHO_HEADERS = ("x-step2api-account", "x-step2api-proxy", "x-step2api-attempt")


@dataclass
class GatewayResult:
    """一次网关调用的统计。"""

    account_id: int | None = None
    account_name: str | None = None
    proxy_label: str = ""
    attempts: int = 1
    status_code: int = 0
    channel: str = "plan"
    duration_ms: float = 0.0
    usage: dict | None = None
    error: str | None = None


class UsageTap:
    """从流式/非流式响应里抓取 usage 字段。

    只在尾部积累有限字节，避免为了统计把整个流缓存下来。
    """

    def __init__(self, *, max_bytes: int = 64 * 1024) -> None:
        self.max_bytes = max_bytes
        self._buf = bytearray()
        self.usage: dict = {}
        self.model: str | None = None

    def feed(self, chunk: bytes) -> None:
        if b"usage" not in chunk and b'"model"' not in chunk:
            return
        self._buf.extend(chunk)
        if len(self._buf) > self.max_bytes:
            del self._buf[: len(self._buf) - self.max_bytes]
        self._parse()

    def _parse(self) -> None:
        text = self._buf.decode("utf-8", errors="ignore")
        # SSE：逐个 data: 行尝试
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                data = json.loads(payload)
            except ValueError:
                continue
            self._absorb(data)

    def _absorb(self, data: Any) -> None:
        if not isinstance(data, dict):
            return
        if isinstance(data.get("model"), str):
            self.model = data["model"]
        usage = parse_usage(data)
        if usage:
            self.usage = usage
        # Anthropic 的 message_delta 里 usage 是增量，做累加
        raw = data.get("usage")
        if isinstance(raw, dict) and "output_tokens" in raw and "input_tokens" not in raw:
            prev = self.usage or {}
            self.usage = {
                "prompt_tokens": prev.get("prompt_tokens", 0),
                "completion_tokens": int(raw.get("output_tokens") or 0),
                "total_tokens": prev.get("prompt_tokens", 0) + int(raw.get("output_tokens") or 0),
            }


def _filter_request_headers(headers: dict[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in HOP_BY_HOP or lowered in GATEWAY_HEADERS:
            continue
        if lowered == "authorization":
            continue  # 由账号 key 覆盖
        out[key] = value
    return out


def _filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        lowered = key.lower()
        if lowered in HOP_BY_HOP:
            continue
        if lowered in ("content-encoding",):  # httpx 已解压
            continue
        out[key] = value
    return out


class Gateway:
    """转发执行器。"""

    def __init__(self, store: Store, settings: Settings, router: Router) -> None:
        self.store = store
        self.settings = settings
        self.router = router
        self._clients: dict[str | None, httpx.AsyncClient] = {}

    # -- 客户端池 -------------------------------------------------------
    def _client(self, proxy_url: str | None) -> httpx.AsyncClient:
        """按代理维度复用连接池。"""
        key = proxy_url
        client = self._clients.get(key)
        if client is not None and not client.is_closed:
            return client

        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(
                self.settings.request_timeout,
                connect=self.settings.connect_timeout,
                read=self.settings.request_timeout,
            ),
            "limits": httpx.Limits(max_connections=200, max_keepalive_connections=50),
            "follow_redirects": False,
        }
        if proxy_url:
            kwargs["proxy"] = normalize_proxy(proxy_url)
        client = httpx.AsyncClient(**kwargs)
        self._clients[key] = client
        return client

    async def aclose(self) -> None:
        for client in list(self._clients.values()):
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001 - 关闭失败不影响退出
                pass
        self._clients.clear()

    async def invalidate_proxy(self, proxy_url: str | None) -> None:
        """代理配置变更后丢弃对应的连接池。"""
        client = self._clients.pop(proxy_url, None)
        if client is not None:
            try:
                await client.aclose()
            except Exception:  # noqa: BLE001
                pass

    # -- 主入口 ---------------------------------------------------------
    async def forward(
        self,
        request: Request,
        path: str,
        *,
        log_callback: Callable[[GatewayResult, RouteContext, str, str], None] | None = None,
    ) -> Response:
        raw_body = await request.body()
        body_json: Any = None
        content_type = request.headers.get("content-type", "")
        if raw_body and "json" in content_type.lower():
            try:
                body_json = json.loads(raw_body)
            except ValueError:
                body_json = None

        headers = {k: v for k, v in request.headers.items()}
        session_key = extract_session_key(headers, body_json)
        model = extract_model(body_json)
        channel = select_channel(path, self.settings)

        # 手工指定账号 / 代理 / 禁用重试
        lowered = {k.lower(): v for k, v in headers.items()}
        requested_account_id: int | None = None
        for name in ("x-step2api-account", "x-account-id", "x-sticky-account"):
            if lowered.get(name):
                try:
                    requested_account_id = int(lowered[name])
                except ValueError:
                    requested_account_id = None
                break
        no_retry = str(lowered.get("x-step2api-no-retry", "")).lower() in {"1", "true", "yes"}
        requested_proxy = lowered.get("x-step2api-proxy") or None

        ctx = RouteContext(
            session_key=session_key,
            model=model,
            channel=channel,
            requested_account_id=requested_account_id,
            requested_proxy=requested_proxy,
        )

        max_attempts = 1 if no_retry else max(1, self.settings.max_retries)
        try:
            plan = self.router.plan(ctx, attempts=max_attempts)
        except NoAccountAvailable as exc:
            return JSONResponse(
                status_code=503,
                content={
                    "error": {
                        "type": "no_account_available",
                        "message": str(exc),
                        "detail": exc.detail,
                    }
                },
            )

        strip_prefix = not requested_proxy
        upstream_path = path
        if requested_proxy:
            # 单次请求指定代理：把代理串从 body/header 之外的地方带进来
            ctx.requested_proxy = requested_proxy
            strip_prefix = True

        url = build_upstream_url(self.settings, upstream_path)
        started = time.perf_counter()
        last_error: str | None = None
        attempts = 0

        for target in plan:
            attempts += 1
            attempt_started = time.perf_counter()

            proxy_url = requested_proxy or target.proxy.url
            client = self._client(proxy_url)
            upstream_headers = _filter_request_headers(headers)
            upstream_headers["Authorization"] = f"Bearer {target.api_key}"
            upstream_headers.setdefault("Accept", "application/json")
            if body_json is not None and "stream" in body_json:
                upstream_headers.setdefault(
                    "Accept", "text/event-stream" if body_json.get("stream") else "application/json"
                )

            row = self.store.get_account(target.account_id)
            limit = self.router.limit_for(target, dict(row) if row else None)
            sem = self.router.gate.semaphore(target.account_id, limit)

            self.router.acquire(target.account_id)
            release_needed = True
            try:
                await sem.acquire()
            except BaseException:
                self.router.release(target.account_id)
                raise

            try:
                upstream_request = client.build_request(
                    request.method, url, headers=upstream_headers, content=raw_body
                )
                try:
                    upstream = await client.send(upstream_request, stream=True)
                except httpx.HTTPError as exc:
                    last_error = f"{type(exc).__name__}: {exc}"
                    self.store.mark_account_result(
                        target.account_id,
                        success=False,
                        error=last_error,
                        cooldown_seconds=self.settings.cooldown_seconds,
                        fail_threshold=self.settings.fail_threshold,
                    )
                    duration = (time.perf_counter() - attempt_started) * 1000.0
                    if log_callback:
                        log_callback(
                            GatewayResult(
                                account_id=target.account_id,
                                account_name=target.account_name,
                                proxy_label=target.proxy.display_label(),
                                attempts=attempts,
                                status_code=0,
                                channel=channel,
                                duration_ms=duration,
                                error=last_error,
                            ),
                            ctx,
                            request.method,
                            path,
                        )
                    continue

                status = upstream.status_code

                # 可重试的上游错误 → 换账号（尚未向客户端吐出任何字节）
                if should_failover(status) and attempts < len(plan):
                    body_preview = b""
                    try:
                        body_preview = await upstream.aread()
                    except httpx.HTTPError:
                        pass
                    await upstream.aclose()

                    error_text = body_preview[:400].decode("utf-8", errors="ignore")
                    last_error = f"HTTP {status}: {error_text}"
                    if should_cooldown(status):
                        self.store.mark_account_result(
                            target.account_id,
                            success=False,
                            error=last_error,
                            cooldown_seconds=self.settings.cooldown_seconds,
                            fail_threshold=self.settings.fail_threshold,
                        )
                    # 走了粘性账号但失败了，把它从会话里摘掉
                    if target.sticky and ctx.session_key:
                        self.store.drop_session(ctx.session_key)
                        ctx.session_key = None
                    duration = (time.perf_counter() - attempt_started) * 1000.0
                    if log_callback:
                        log_callback(
                            GatewayResult(
                                account_id=target.account_id,
                                account_name=target.account_name,
                                proxy_label=target.proxy.display_label(),
                                attempts=attempts,
                                status_code=status,
                                channel=channel,
                                duration_ms=duration,
                                error=last_error,
                            ),
                            ctx,
                            request.method,
                            path,
                        )
                    continue

                # 成功（或不可重试的错误）→ 直接回给客户端
                if 200 <= status < 300:
                    self.store.mark_account_result(target.account_id, success=True)
                    self.router.commit(ctx, target)
                else:
                    self.store.mark_account_result(
                        target.account_id,
                        success=False,
                        error=f"HTTP {status}",
                        cooldown_seconds=self.settings.cooldown_seconds,
                        fail_threshold=self.settings.fail_threshold,
                    )

                resp_headers = _filter_response_headers(upstream.headers)
                resp_headers["x-step2api-account"] = str(target.account_id)
                resp_headers["x-step2api-proxy"] = target.proxy.header_value()
                resp_headers["x-step2api-attempt"] = str(attempts)

                is_stream = upstream.headers.get("content-type", "").startswith("text/event-stream")
                tap = UsageTap()

                def _finish(final_status: int, usage: dict, error: str | None) -> None:
                    duration = (time.perf_counter() - started) * 1000.0
                    if usage:
                        total = int(usage.get("total_tokens") or 0)
                        if total:
                            self.store.add_credits_used(target.account_id, 0.0)
                    if log_callback:
                        log_callback(
                            GatewayResult(
                                account_id=target.account_id,
                                account_name=target.account_name,
                                proxy_label=target.proxy.display_label(),
                                attempts=attempts,
                                status_code=final_status,
                                channel=channel,
                                duration_ms=duration,
                                usage=usage or None,
                                error=error,
                            ),
                            ctx,
                            request.method,
                            path,
                        )

                def _release_once() -> None:
                    nonlocal release_needed
                    if release_needed:
                        release_needed = False
                        self.router.release(target.account_id)
                        try:
                            sem.release()
                        except ValueError:
                            pass

                if is_stream:

                    async def _iter() -> AsyncIterator[bytes]:
                        try:
                            async for chunk in upstream.aiter_bytes():
                                tap.feed(chunk)
                                yield chunk
                        except httpx.HTTPError as exc:
                            _finish(status, tap.usage, f"流中断：{type(exc).__name__}: {exc}")
                            return
                        finally:
                            await upstream.aclose()
                            _release_once()
                        _finish(status, tap.usage, None)

                    return StreamingResponse(
                        _iter(),
                        status_code=status,
                        headers=resp_headers,
                        media_type=upstream.headers.get("content-type", "text/event-stream"),
                    )

                try:
                    payload = await upstream.aread()
                finally:
                    await upstream.aclose()
                    _release_once()

                tap.feed(payload)
                usage = tap.usage
                if not usage:
                    try:
                        usage = parse_usage(json.loads(payload))
                    except ValueError:
                        usage = {}
                _finish(status, usage, None)

                return Response(
                    content=payload,
                    status_code=status,
                    headers=resp_headers,
                    media_type=upstream.headers.get("content-type"),
                )

            finally:
                if release_needed:
                    release_needed = False
                    self.router.release(target.account_id)
                    try:
                        sem.release()
                    except ValueError:
                        pass

        # 所有候选都失败
        duration = (time.perf_counter() - started) * 1000.0
        return JSONResponse(
            status_code=502 if last_error else 503,
            content={
                "error": {
                    "type": "all_accounts_failed",
                    "message": "所有候选账号均失败",
                    "attempts": attempts,
                    "last_error": last_error,
                    "duration_ms": round(duration, 1),
                }
            },
        )


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
