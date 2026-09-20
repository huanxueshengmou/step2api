"""冒烟测试用的假上游 + 最小 HTTP 正向代理。

启动一个进程同时提供：

* ``http://127.0.0.1:8899`` —— 冒充 StepFun 国际站的两条通道
* ``http://127.0.0.1:8901/8902/8903`` —— 三个最小 HTTP 正向代理，
  会在转发时注入 ``X-Via-Proxy`` 头，用来验证代理轮转真的生效了

仅供测试使用。
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

UPSTREAM_PORT = 8899
PROXY_PORTS = (8901, 8902, 8903)

DEFAULT_BEHAVIOR = {"remaining": 400_000_000, "total": 400_000_000, "plan": "flash_mini"}

#: 按 Key 定制上游行为。
#:
#: 注意 flaky 账号必须带着**充足额度**：额度为 0 的账号会被网关的额度感知
#: 调度直接摘除，那样就测不到"额度充足但上游限流"这条故障转移路径了。
ACCOUNTS = {
    "sk-flaky-000000000001": {
        # 额度 100% 剩余 → 排序时排第一，且调用时返回 429 触发故障转移
        "remaining": 400_000_000,
        "total": 400_000_000,
        "plan": "flash_mini",
        "behavior": "429",
    },
    "sk-gooda-000000000002": {
        "remaining": 1_200_000_000,
        "total": 1_600_000_000,
        "plan": "flash_plus",
    },
    "sk-goodb-000000000003": {
        "remaining": 300_000_000,
        "total": 800_000_000,
        "plan": "flash_pro",
    },
    "sk-dead-000000000004": {
        # 无效 Key：额度查询 401，调用也 401
        "behavior": "401",
    },
    # ---- 以下仅用于演示控制台的额度展示 ----
    "sk-max-000000000005": {
        # flash_max，剩余 4% → 触发低额度告警
        "remaining": 1_600_000_000,
        "total": 40_000_000_000,
        "plan": "flash_max",
    },
    "sk-empty-000000000006": {
        # 额度耗尽 → 被额度感知调度摘出候选
        "remaining": 0,
        "total": 1_600_000_000,
        "plan": "flash_plus",
    },
    "sk-soon-000000000007": {
        # 剩余额度充足但 2 天后到期 → Plan 时长告警
        "remaining": 1_500_000_000,
        "total": 1_600_000_000,
        "plan": "flash_plus",
        "reset_in_days": 2,
    },
}

SEEN: list[dict] = []

app = FastAPI()


def _key_of(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return auth.removeprefix("Bearer ").strip()


def _behavior(key: str) -> dict:
    return ACCOUNTS.get(key, DEFAULT_BEHAVIOR)


def _record(request: Request, key: str) -> None:
    SEEN.append(
        {
            "key": key,
            "path": request.url.path,
            "proxy": request.headers.get("x-via-proxy"),
        }
    )


@app.api_route("/_debug/requests", methods=["GET", "HEAD"])
async def debug_requests() -> dict:
    return {"count": len(SEEN), "requests": SEEN}


@app.api_route("/_debug/reset", methods=["POST", "GET"])
async def debug_reset() -> dict:
    SEEN.clear()
    return {"ok": True}


@app.api_route("/_debug/ping", methods=["GET", "HEAD", "POST"])
async def debug_ping() -> dict:
    """代理健康检查目标：任何方法都回 200。"""
    return {"ok": True}


# --------------------------------------------------------------------------
# Step Plan 订阅通道
# --------------------------------------------------------------------------


@app.get("/step_plan/v1/usage")
async def plan_usage(request: Request):
    key = _key_of(request)
    _record(request, key)
    if key == "sk-dead-000000000004":
        return JSONResponse({"error": "invalid api key"}, status_code=401)

    behavior = _behavior(key)
    reset_at = datetime.now(timezone.utc) + timedelta(
        days=behavior.get("reset_in_days", 12), hours=4
    )
    return {
        "data": {
            "plan": behavior.get("plan", "flash_mini"),
            "remaining_credits": behavior.get("remaining", 0),
            "total_credits": behavior.get("total", 0),
            "reset_at": reset_at.isoformat(),
            "status": "active",
        }
    }


@app.post("/step_plan/v1/messages")
async def plan_messages(request: Request):
    key = _key_of(request)
    _record(request, key)
    behavior = _behavior(key)

    if behavior.get("behavior") == "429":
        return JSONResponse(
            {"error": {"type": "rate_limit_error", "message": "quota exhausted"}},
            status_code=429,
        )
    if behavior.get("behavior") == "401":
        return JSONResponse({"error": {"type": "authentication_error"}}, status_code=401)

    body = await request.json()
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": body.get("model", "step-3.5-flash"),
        "content": [{"type": "text", "text": f"served-by:{key}"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 11, "output_tokens": 7},
        "upstream_saw_proxy": request.headers.get("x-via-proxy"),
    }


# --------------------------------------------------------------------------
# 按量计费通道
# --------------------------------------------------------------------------


@app.get("/v1/accounts")
async def account_balance(request: Request):
    key = _key_of(request)
    _record(request, key)
    if key == "sk-dead-000000000004":
        return JSONResponse({"error": "invalid api key"}, status_code=401)
    return {
        "object": "account",
        "type": "prepaid",
        "balance": 12.5,
        "total_cash_balance": 10.0,
        "total_voucher_balance": 2.5,
    }


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    key = _key_of(request)
    _record(request, key)
    behavior = _behavior(key)

    if behavior.get("behavior") == "429":
        return JSONResponse({"error": {"message": "rate limited"}}, status_code=429)
    if behavior.get("behavior") == "401":
        return JSONResponse({"error": {"message": "bad key"}}, status_code=401)

    body = await request.json()
    model = body.get("model", "step-3.5-flash")
    proxy_seen = request.headers.get("x-via-proxy")

    if body.get("stream"):
        async def gen():
            for token in ("Hello", " from", " step2api"):
                chunk = {
                    "id": "chatcmpl-test",
                    "object": "chat.completion.chunk",
                    "model": model,
                    "choices": [{"index": 0, "delta": {"content": token}}],
                }
                yield f"data: {json.dumps(chunk)}\n\n".encode()
                await asyncio.sleep(0.01)
            final = {
                "id": "chatcmpl-test",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
            }
            yield f"data: {json.dumps(final)}\n\n".encode()
            yield b"data: [DONE]\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"X-Upstream-Saw-Proxy": proxy_seen or "direct"},
        )

    return JSONResponse(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": f"served-by:{key}"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 9, "completion_tokens": 3, "total_tokens": 12},
            "upstream_saw_proxy": proxy_seen,
        },
        headers={"X-Upstream-Saw-Proxy": proxy_seen or "direct"},
    )


# --------------------------------------------------------------------------
# 最小 HTTP 正向代理
# --------------------------------------------------------------------------


async def _tunnel(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, host: str, dest_port: int
) -> None:
    """CONNECT 隧道：双向透传原始字节（HTTPS 走这条路径）。"""
    try:
        origin_reader, origin_writer = await asyncio.open_connection(host, dest_port)
    except OSError as exc:
        writer.write(f"HTTP/1.1 502 Bad Gateway\r\n\r\n{exc}".encode())
        await writer.drain()
        writer.close()
        return

    writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
    await writer.drain()

    async def pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
        try:
            while True:
                chunk = await src.read(65536)
                if not chunk:
                    break
                dst.write(chunk)
                await dst.drain()
        except (ConnectionError, asyncio.IncompleteReadError, OSError):
            pass
        finally:
            try:
                dst.close()
            except Exception:  # noqa: BLE001
                pass

    await asyncio.gather(
        pump(reader, origin_writer),
        pump(origin_reader, writer),
        return_exceptions=True,
    )


async def proxy_session(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, port: int) -> None:
    """把一个 HTTP 请求原样转发到目标，并注入 X-Via-Proxy。"""
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
    except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
        writer.close()
        return

    lines = head.split(b"\r\n")
    try:
        method, target, _version = lines[0].decode("latin-1").split(" ", 2)
    except ValueError:
        writer.close()
        return

    # CONNECT 用于 HTTPS：建隧道，不再注入头（内容已加密）
    if method.upper() == "CONNECT":
        host, _, port_text = target.partition(":")
        try:
            dest_port = int(port_text or 443)
        except ValueError:
            dest_port = 443
        await _tunnel(reader, writer, host, dest_port)
        return

    headers: list[tuple[str, str]] = []
    for raw in lines[1:]:
        if not raw:
            continue
        name, _, value = raw.decode("latin-1").partition(":")
        if name.strip():
            headers.append((name.strip(), value.strip()))
    hdict = {n.lower(): v for n, v in headers}

    body = b""
    length = int(hdict.get("content-length") or 0)
    if length:
        try:
            body = await reader.readexactly(length)
        except asyncio.IncompleteReadError:
            body = b""

    if "://" in target:
        parts = urlsplit(target)
        host = parts.hostname or ""
        dest_port = parts.port or 80
        path = parts.path + (f"?{parts.query}" if parts.query else "")
    else:
        host = (hdict.get("host") or "").split(":")[0]
        dest_port = 80
        path = target

    if not host:
        writer.write(b"HTTP/1.1 400 Bad Request\r\nContent-Length: 0\r\n\r\n")
        await writer.drain()
        writer.close()
        return

    out = [(n, v) for n, v in headers if n.lower() not in ("proxy-connection", "connection", "host")]
    out.append(("Host", f"{host}:{dest_port}"))
    out.append(("Connection", "close"))
    out.append(("X-Via-Proxy", str(port)))

    request_bytes = f"{method} {path} HTTP/1.1\r\n".encode("latin-1")
    for name, value in out:
        request_bytes += f"{name}: {value}\r\n".encode("latin-1")
    request_bytes += b"\r\n" + body

    try:
        origin_reader, origin_writer = await asyncio.open_connection(host, dest_port)
    except OSError as exc:
        writer.write(
            f"HTTP/1.1 502 Bad Gateway\r\nContent-Length: {len(str(exc))}\r\n\r\n{exc}".encode()
        )
        await writer.drain()
        writer.close()
        return

    try:
        origin_writer.write(request_bytes)
        await origin_writer.drain()
        while True:
            chunk = await origin_reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            origin_writer.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


async def main() -> None:
    config = uvicorn.Config(app, host="127.0.0.1", port=UPSTREAM_PORT, log_level="warning")
    server = uvicorn.Server(config)

    servers = []
    for port in PROXY_PORTS:
        def _make(p: int):
            return lambda r, w: proxy_session(r, w, p)

        servers.append(await asyncio.start_server(_make(port), "127.0.0.1", port))

    print(f"fake upstream on {UPSTREAM_PORT}, proxies on {PROXY_PORTS}", flush=True)
    try:
        await server.serve()
    finally:
        for s in servers:
            s.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
