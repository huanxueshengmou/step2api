"""上游错误分类。

**为什么需要分类**：不同失败原因要用不同的处置。把「限流」和「余额耗尽」
一视同仁地冷却，会把还能用的账号白扔几小时；把「模型不存在」当成账号故障，
会不断轮换却永远成功不了。

分类结果决定三件事：

* **是否换账号重试**（``retryable``）
* **是否惩罚该账号**（``punish``）
* **冷却多久**（由 :func:`cooldown_for` 按 ``kind`` + 连续次数算）

判定顺序有语义依据，**不能随意调换**：

1. 先看 HTTP 状态码（最权威的信号）
2. 再看响应体里的具体错误码（比关键词精确）
3. 然后是关键词（宽泛，容易误伤）
4. 最后兜底

特别地，``429`` 必须排在「额度不足」类关键词**之前**：429 的响应体里经常
同时出现 "quota exceeded" / "额度不足" 这类跨计费与限流两界的措辞，若先按
关键词判成"余额耗尽"，就会把一个只是被限流的账号硬冷却到次日，白扔半天。
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass


class ErrorKind(str, enum.Enum):
    """失败原因分类。"""

    AUTH = "auth"                  # 401/403 key 无效 —— 账号级故障
    CREDIT = "credit"              # 402 / 余额耗尽 / 套餐用尽
    RATE_LIMIT = "rate_limit"      # 429 / 限流
    MODEL_UNAVAILABLE = "model"    # 该账号无此模型权限
    CONTEXT_TOO_LONG = "context"   # 上下文超长 —— 换账号也没用
    BAD_REQUEST = "bad_request"    # 请求本身有问题 —— 不惩罚账号
    CONTENT_FILTER = "content"     # 内容被拦 —— 不惩罚账号
    SERVER = "server"              # 5xx —— 上游故障
    TIMEOUT = "timeout"            # 连接/读取超时
    NETWORK = "network"            # DNS/连接失败/代理故障
    UNKNOWN = "unknown"


#: 需要换账号重试 —— 换一个账号可能就好
RETRYABLE = frozenset({
    ErrorKind.AUTH, ErrorKind.CREDIT, ErrorKind.RATE_LIMIT, ErrorKind.SERVER,
    ErrorKind.TIMEOUT, ErrorKind.NETWORK, ErrorKind.UNKNOWN,
    ErrorKind.MODEL_UNAVAILABLE,
})

#: 需要惩罚账号（冷却 / 熔断）
PUNISHABLE = frozenset({
    ErrorKind.AUTH, ErrorKind.CREDIT, ErrorKind.RATE_LIMIT,
    ErrorKind.SERVER, ErrorKind.NETWORK,
})

#: 换账号也没用：请求本身的问题，或该模型全池都不可用
POINTLESS_TO_ROTATE = frozenset({
    ErrorKind.BAD_REQUEST, ErrorKind.CONTEXT_TOO_LONG, ErrorKind.CONTENT_FILTER,
})


def _has(body: str, markers: tuple[str, ...]) -> bool:
    """关键词匹配。

    中文按原文比对（避免大小写转换破坏多字节），英文统一小写后比对。
    """
    lowered = body.lower()
    for m in markers:
        if m.isascii():
            if m in lowered:
                return True
        elif m in body:
            return True
    return False


#: 余额/套餐耗尽
_CREDIT_MARKERS = (
    "insufficient balance", "insufficient_quota", "quota exceeded",
    "exceeded your current quota", "no credit", "credit exhausted",
    "payment required", "billing", "out of credits", "balance is not enough",
    "余额不足", "额度不足", "余额已用完", "套餐已用完", "欠费", "账户余额",
)
#: 限流
_RATE_MARKERS = (
    "rate limit", "rate_limit", "too many requests", "requests per",
    "slow down", "concurrency limit", "tpm", "rpm",
    "请求过于频繁", "请求太频繁", "限流", "频率限制", "触发风控",
)
#: 认证
_AUTH_MARKERS = (
    "invalid api key", "invalid_api_key", "unauthorized", "authentication",
    "api key", "token is invalid", "key is disabled", "key not found",
    "无效的", "鉴权失败", "认证失败",
)
#: 模型不可用
_MODEL_MARKERS = (
    "model not found", "no such model", "does not exist", "unsupported model",
    "model_not_found", "no permission to access model", "not support",
    "模型不存在", "无权限访问", "不支持该模型",
)
#: 上下文超长
_CONTEXT_MARKERS = (
    "context length", "context_length", "too long", "maximum context",
    "max_tokens", "string too long", "prompt is too long",
    "上下文长度", "超出最大长度", "输入过长",
)
#: 请求参数问题
_BAD_REQUEST_MARKERS = (
    "invalid request", "invalid_request", "invalid parameter", "bad request",
    "missing required", "malformed", "参数错误", "请求不合法",
)
#: 内容过滤
_CONTENT_MARKERS = (
    "content policy", "content_filter", "content filter", "safety",
    "blocked by", "flagged", "内容审核", "违规", "敏感",
)


@dataclass(frozen=True)
class Classified:
    """一次失败的结构化描述。"""

    kind: ErrorKind
    reason: str

    @property
    def retryable(self) -> bool:
        return self.kind in RETRYABLE

    @property
    def punish(self) -> bool:
        return self.kind in PUNISHABLE

    @property
    def point_rotate(self) -> bool:
        """换账号是否有意义。``False`` 时网关应直接把错误回给客户端。"""
        return self.kind not in POINTLESS_TO_ROTATE


def classify(
    status_code: int | None,
    body: str = "",
    *,
    exception: BaseException | None = None,
) -> Classified:
    """把一次上游失败归类。

    :param status_code: HTTP 状态码；连接层失败传 ``None``
    :param body: 响应体（会截断，不必传全量）
    :param exception: 传输层异常（超时 / 连接失败 / 代理故障）
    """
    text = (body or "")[:4000]

    # ---- 0. 传输层异常：没有状态码 ----
    if exception is not None:
        name = type(exception).__name__
        msg = str(exception)
        if "Timeout" in name or "timeout" in msg.lower():
            return Classified(ErrorKind.TIMEOUT, f"{name}: {msg[:120]}")
        if "Proxy" in name or "proxy" in msg.lower():
            return Classified(ErrorKind.NETWORK, f"代理故障 {name}: {msg[:120]}")
        if "Connect" in name or "Connection" in name:
            return Classified(ErrorKind.NETWORK, f"{name}: {msg[:120]}")
        return Classified(ErrorKind.UNKNOWN, f"{name}: {msg[:120]}")

    # ---- 1. 状态码优先（最权威）----
    if status_code == 401 or status_code == 403:
        return Classified(ErrorKind.AUTH, f"HTTP {status_code}")

    if status_code == 402:
        return Classified(ErrorKind.CREDIT, "HTTP 402 余额/套餐耗尽")

    # 429 必须在「额度不足」关键词之前判：429 响应体常带 quota 类措辞，
    # 若被归成 CREDIT 会硬冷却到次日，白扔可用账号。
    if status_code == 429:
        return Classified(ErrorKind.RATE_LIMIT, "HTTP 429 限流")

    if status_code == 404:
        # 404 可能是模型不存在，也可能是路径错。看 body 再定。
        if _has(text, _MODEL_MARKERS):
            return Classified(ErrorKind.MODEL_UNAVAILABLE, "HTTP 404 模型不可用")
        return Classified(ErrorKind.BAD_REQUEST, "HTTP 404")

    if status_code is not None and 400 <= status_code < 500:
        # 其余 4xx：按 body 细分
        if _has(text, _CONTEXT_MARKERS):
            return Classified(ErrorKind.CONTEXT_TOO_LONG, f"HTTP {status_code} 上下文超长")
        if _has(text, _CONTENT_MARKERS):
            return Classified(ErrorKind.CONTENT_FILTER, f"HTTP {status_code} 内容拦截")
        if _has(text, _MODEL_MARKERS):
            return Classified(ErrorKind.MODEL_UNAVAILABLE, f"HTTP {status_code} 模型不可用")
        if _has(text, _CREDIT_MARKERS):
            return Classified(ErrorKind.CREDIT, f"HTTP {status_code} 余额不足")
        if _has(text, _RATE_MARKERS):
            return Classified(ErrorKind.RATE_LIMIT, f"HTTP {status_code} 限流")
        if _has(text, _AUTH_MARKERS):
            return Classified(ErrorKind.AUTH, f"HTTP {status_code} 认证失败")
        if _has(text, _BAD_REQUEST_MARKERS):
            return Classified(ErrorKind.BAD_REQUEST, f"HTTP {status_code} 请求不合法")
        # 认不出来的 4xx：不算账号问题（可能只是这个请求特殊）
        return Classified(ErrorKind.BAD_REQUEST, f"HTTP {status_code}")

    if status_code is not None and status_code >= 500:
        return Classified(ErrorKind.SERVER, f"HTTP {status_code} 上游故障")

    # ---- 2. 没有状态码时按 body 关键词兜底 ----
    if _has(text, _AUTH_MARKERS):
        return Classified(ErrorKind.AUTH, "响应体含认证失败特征")
    if _has(text, _CONTEXT_MARKERS):
        return Classified(ErrorKind.CONTEXT_TOO_LONG, "响应体含上下文超长特征")
    if _has(text, _MODEL_MARKERS):
        return Classified(ErrorKind.MODEL_UNAVAILABLE, "响应体含模型不可用特征")
    if _has(text, _CONTENT_MARKERS):
        return Classified(ErrorKind.CONTENT_FILTER, "响应体含内容拦截特征")
    if _has(text, _CREDIT_MARKERS):
        return Classified(ErrorKind.CREDIT, "响应体含余额不足特征")
    if _has(text, _RATE_MARKERS):
        return Classified(ErrorKind.RATE_LIMIT, "响应体含限流特征")

    return Classified(ErrorKind.UNKNOWN, (text[:120] or "未知失败"))


# --------------------------------------------------------------------------
# 冷却时长
# --------------------------------------------------------------------------


def cooldown_for(
    kind: ErrorKind,
    streak: int,
    *,
    base: float,
    cap: float,
    hard_until_seconds: float = 6 * 3600,
) -> float:
    """按错误类型与连续次数算冷却时长（秒）。

    * ``CREDIT`` / ``AUTH`` —— 硬冷却，用 ``hard_until_seconds``。
      余额耗尽或 key 失效不会自己好，短冷却只会反复撞墙。
    * ``RATE_LIMIT`` / ``SERVER`` / ``NETWORK`` —— 指数退避 ``base * 2^(streak-1)``，封顶 ``cap``。
    * 其余 —— 不冷却（返回 0），由调用方决定是否降权。
    """
    if kind in (ErrorKind.CREDIT, ErrorKind.AUTH):
        return hard_until_seconds
    if kind in (ErrorKind.RATE_LIMIT, ErrorKind.SERVER, ErrorKind.NETWORK):
        n = max(1, streak)
        # 限制指数规模，避免大 streak 时 2**n 溢出
        shift = min(n - 1, 20)
        return min(base * (2 ** shift), cap)
    return 0.0


#: 命中 Retry-After 时用它覆盖退避
_RETRY_AFTER_RE = re.compile(r"^\s*(\d+)\s*$")


def parse_retry_after(value: str | None) -> float | None:
    """解析 ``Retry-After`` / ``retry-after-ms`` 头。

    只接受纯秒数（HTTP-Date 格式不支持，返回 None 让调用方退回退避）。
    超过 24 小时的视为异常值丢弃 —— 上游偶发返回荒谬数字时，宁可自己退避
    也不要按它把账号锁死一整天。
    """
    if not value:
        return None
    m = _RETRY_AFTER_RE.match(value)
    if not m:
        return None
    try:
        seconds = int(m.group(1))
    except ValueError:
        return None
    if seconds <= 0 or seconds > 24 * 3600:
        return None
    return float(seconds)


__all__ = [
    "POINTLESS_TO_ROTATE",
    "PUNISHABLE",
    "RETRYABLE",
    "Classified",
    "ErrorKind",
    "classify",
    "cooldown_for",
    "parse_retry_after",
]
