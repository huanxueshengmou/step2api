"""代理解析、轮转与健康检查。

支持协议：http / https / socks5 / socks5h / socks4（httpx[socks] 提供 SOCKS 支持）。

轮转策略（``STEP2API_PROXY_STRATEGY``）：

``round_robin``    池内顺序轮转，保证每个代理被均摊使用
``random``         随机取一个可用代理
``least_used``     取当前在途请求数最少的代理
``lowest_latency`` 取最近一次健康检查延迟最低的代理
"""

from __future__ import annotations

import itertools
import random
import threading
from dataclasses import dataclass
from typing import Iterable, Sequence

import httpx

SUPPORTED_SCHEMES = ("http://", "https://", "socks5://", "socks5h://", "socks4://", "socks4a://")


class ProxyError(ValueError):
    """代理串格式不合法。"""


def normalize_proxy(raw: str | None) -> str | None:
    """把各种写法归一成 httpx 可用的 URL。

    接受 ``host:port``、``user:pass@host:port``、``scheme://...``。
    裸露的 host:port 默认补 ``http://``。
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if "://" not in text:
        text = "http://" + text
    scheme = text.split("://", 1)[0].lower() + "://"
    if scheme not in SUPPORTED_SCHEMES:
        raise ProxyError(
            f"不支持的代理协议 {scheme[:-3]!r}，可用："
            + ", ".join(s[:-3] for s in SUPPORTED_SCHEMES)
        )
    rest = text.split("://", 1)[1]
    if not rest or ":" not in rest:
        raise ProxyError(f"代理地址缺少端口：{raw!r}")
    return text


def parse_proxy_list(blob: str | None, *, sep: str = "\n") -> list[str]:
    """把多行/逗号分隔的代理串解析成规范化列表（去重保序）。"""
    if not blob:
        return []
    raw_items: list[str] = []
    for line in blob.replace(",", "\n").replace(";", "\n").split(sep):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        raw_items.append(line)

    seen: set[str] = set()
    out: list[str] = []
    for item in raw_items:
        norm = normalize_proxy(item)
        if norm and norm not in seen:
            seen.add(norm)
            out.append(norm)
    return out


def redact_proxy(proxy_url: str | None) -> str:
    """隐藏代理 URL 中的账号密码，用于日志与前端展示。"""
    if not proxy_url:
        return ""
    if "@" not in proxy_url:
        return proxy_url
    scheme, rest = proxy_url.split("://", 1)
    creds, host = rest.rsplit("@", 1)
    user = creds.split(":", 1)[0]
    return f"{scheme}://{user}:***@{host}"


def is_usable(proxy: dict | None) -> bool:
    """代理是否处于可调度状态。"""
    if not proxy:
        return False
    if not proxy.get("enabled", True):
        return False
    status = proxy.get("status") or "unknown"
    return status in {"unknown", "healthy"}


@dataclass
class _ProxyRuntime:
    """单个代理的运行期计数。"""

    index: int = 0
    inflight: int = 0
    latency_ms: float | None = None


class ProxyRuntimeRegistry:
    """代理运行期指标的共享登记处。

    在途数与延迟都按**代理 URL** 记录（URL 在库内唯一）。多个轮转器共用同一
    份登记处是必要的：``least_used`` / ``lowest_latency`` 依赖全局在途数，
    如果每个池各自持有一份，策略就只能看到自己那份空数据，退化成永远选第一个。
    """

    def __init__(self) -> None:
        self._runtime: dict[str, _ProxyRuntime] = {}
        self._lock = threading.Lock()

    def acquire(self, proxy_url: str) -> None:
        with self._lock:
            self._runtime.setdefault(proxy_url, _ProxyRuntime()).inflight += 1

    def release(self, proxy_url: str) -> None:
        with self._lock:
            rt = self._runtime.get(proxy_url)
            if rt and rt.inflight > 0:
                rt.inflight -= 1

    def record_latency(self, proxy_url: str, latency_ms: float | None) -> None:
        with self._lock:
            self._runtime.setdefault(proxy_url, _ProxyRuntime()).latency_ms = latency_ms

    def get(self, proxy_url: str) -> _ProxyRuntime:
        with self._lock:
            return self._runtime.get(proxy_url) or _ProxyRuntime()

    def stats(self) -> dict[str, dict]:
        with self._lock:
            return {
                url: {"inflight": rt.inflight, "latency_ms": rt.latency_ms}
                for url, rt in self._runtime.items()
            }

    def clear(self) -> None:
        with self._lock:
            self._runtime.clear()


class ProxyPool:
    """代理轮转器。线程安全（网关是单事件循环，但健康检查跑在线程里）。"""

    def __init__(
        self,
        strategy: str = "round_robin",
        registry: ProxyRuntimeRegistry | None = None,
    ) -> None:
        self.strategy = strategy if strategy in {
            "round_robin",
            "random",
            "least_used",
            "lowest_latency",
        } else "round_robin"
        self._counter = itertools.count()
        self.registry = registry or ProxyRuntimeRegistry()
        self._lock = threading.Lock()

    # -- 选择 -----------------------------------------------------------
    def pick(self, candidates: Sequence[dict]) -> dict | None:
        """按策略选出一个代理条目（dict 需含 ``url`` 字段）。"""
        pool = [c for c in candidates if is_usable(c)]
        if not pool:
            return None

        if self.strategy == "random":
            return random.choice(pool)

        if self.strategy == "lowest_latency":
            scored = [
                c for c in pool if self.registry.get(c["url"]).latency_ms is not None
            ]
            if scored:
                return min(
                    scored,
                    key=lambda c: (
                        self.registry.get(c["url"]).latency_ms or 0.0,
                        self.registry.get(c["url"]).inflight,
                    ),
                )

        if self.strategy == "least_used":
            return min(pool, key=lambda c: self.registry.get(c["url"]).inflight)

        # round_robin：计数器按池常驻，每次调用递增
        with self._lock:
            return pool[next(self._counter) % len(pool)]

    # -- 计数（委派给共享登记处）------------------------------------------
    def acquire(self, proxy_url: str) -> None:
        self.registry.acquire(proxy_url)

    def release(self, proxy_url: str) -> None:
        self.registry.release(proxy_url)

    def record_latency(self, proxy_url: str, latency_ms: float | None) -> None:
        self.registry.record_latency(proxy_url, latency_ms)

    def stats(self) -> dict[str, dict]:
        return self.registry.stats()

    def reset(self) -> None:
        self.registry.clear()


async def check_proxy(
    proxy_url: str,
    check_url: str,
    *,
    timeout: float = 15.0,
    method: str = "GET",
) -> tuple[bool, float | None, str]:
    """对代理做一次连通性探测。

    返回 ``(ok, latency_ms, message)``。探测目标默认是上游余额接口，
    因此 401/403 也算"通"——说明链路已经到达上游。
    """
    from time import perf_counter

    try:
        norm = normalize_proxy(proxy_url)
    except ProxyError as exc:
        return False, None, str(exc)

    transport = httpx.AsyncHTTPTransport(proxy=norm, retries=0)
    started = perf_counter()
    try:
        async with httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout, connect=timeout),
            follow_redirects=False,
        ) as client:
            resp = await client.request(method, check_url)
        latency = (perf_counter() - started) * 1000.0
        if resp.status_code < 500:
            return True, latency, f"HTTP {resp.status_code}"
        return False, latency, f"上游返回 HTTP {resp.status_code}"
    except httpx.ProxyError as exc:
        return False, None, f"代理不可用：{exc}"
    except httpx.ConnectError as exc:
        return False, None, f"连接失败：{exc}"
    except httpx.TimeoutException:
        return False, None, "连接超时"
    except Exception as exc:  # noqa: BLE001 - 探测失败原因需要透传给前端
        return False, None, f"{type(exc).__name__}: {exc}"


async def check_many(
    proxy_urls: Iterable[str],
    check_url: str,
    *,
    timeout: float = 15.0,
    concurrency: int = 8,
) -> dict[str, tuple[bool, float | None, str]]:
    """并发探测多个代理。"""
    import asyncio

    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(url: str) -> tuple[str, tuple[bool, float | None, str]]:
        async with sem:
            return url, await check_proxy(url, check_url, timeout=timeout)

    results = await asyncio.gather(*(_one(u) for u in proxy_urls))
    return dict(results)


@dataclass
class ResolvedProxy:
    """一次请求最终使用的代理。"""

    url: str | None = None
    source: str = "none"  # none | global | pool | direct | session
    pool_id: int | None = None
    proxy_id: int | None = None
    label: str = ""

    @property
    def is_direct(self) -> bool:
        return self.url is None

    def display_label(self) -> str:
        """给人看的标签（可以含中文，用于界面与日志）。"""
        return self.label or ("直连" if self.is_direct else redact_proxy(self.url))

    def header_value(self) -> str:
        """回给客户端的 ``X-Step2api-Proxy`` 头值。

        HTTP 头必须是 latin-1 可编码的，中文标签（如"直连"）会让
        Starlette 在构造响应时抛 UnicodeEncodeError，所以这里单独给出
        ASCII 安全的标识。
        """
        if self.is_direct:
            return "direct"
        if self.proxy_id:
            return f"proxy-{self.proxy_id}"
        if self.pool_id:
            return f"pool-{self.pool_id}"
        return redact_proxy(self.url) or "unknown"

    def as_dict(self) -> dict:
        return {
            "url": redact_proxy(self.url),
            "raw_url": self.url,
            "source": self.source,
            "pool_id": self.pool_id,
            "proxy_id": self.proxy_id,
            "label": self.display_label(),
            "header_value": self.header_value(),
        }

