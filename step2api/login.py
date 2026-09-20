"""浏览器登录式导入。

一次登录同时拿到三样东西：

* **API Key** —— 调模型用（Dashboard/ListAccessKeys 返回明文）
* **Oasis-Token** —— Cookie，查套餐额度用
* **web_id** —— localStorage，查套餐额度用（**不是** Cookie 里的 Oasis-Webid）

为什么不把登录页嵌进自己的面板：StepFun 登录会走扫码 / 短信 / 图形验证码，
用截图串流做一个假浏览器遇到验证码极易卡住。这里改用**真实浏览器窗口**
（``headless=False``），用户像平时一样登录，服务端只负责轮询会话、
抓取凭据、调 ListAccessKeys 取 Key。

浏览器用独立的持久化 profile（``data/browser_profile``），与用户日常浏览的
Chrome 互不干扰；登录状态在多次导入之间保留。

playwright 是可选依赖：没装时本模块返回明确提示，不影响其它功能。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import CONSOLE_APP_ID, PORTAL_URL, Settings
from .console import ConsoleClient
from .crypto import key_hint

log = logging.getLogger("step2api.login")

#: 登录窗口最长等待时间（秒）
DEFAULT_TIMEOUT = 300.0

#: 轮询间隔（秒）
POLL_INTERVAL = 1.5

#: 浏览器可执行文件的 channel 名，按顺序尝试
BROWSER_CHANNELS = ("chrome", "msedge")

#: 需要尝试的站点。
#:
#: 用户的登录会话通常落在 ``platform.stepfun.ai``（控制台/仪表盘都在这里），
#: 而 ``account.stepfun.ai`` 是账号中心。两者的 Cookie 与 localStorage 是
#: **按域隔离**的，只读其中一个经常拿不到完整凭据，所以两个都试。
CANDIDATE_ORIGINS = (
    "https://platform.stepfun.ai",
    "https://account.stepfun.ai",
)


class PlaywrightMissing(RuntimeError):
    """没装 playwright。"""


def playwright_available() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class CapturedKey:
    """ListAccessKeys 返回的一把 Key。"""

    key_id: str = ""
    access_key: str = ""
    name: str = ""
    is_default: bool = False
    created_at: datetime | None = None

    @property
    def hint(self) -> str:
        return key_hint(self.access_key)

    def as_public(self) -> dict:
        """给人看的形式（不含明文）。"""
        return {
            "key_id": self.key_id,
            "hint": self.hint,
            "name": self.name,
            "is_default": self.is_default,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


@dataclass
class LoginSession:
    """一次浏览器登录会话的状态。"""

    id: str
    status: str = "pending"  # pending | waiting | success | failed | cancelled
    message: str = "正在启动浏览器…"
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    #: 抓到的凭据（仅在内存里存活，commit 后写库）
    token: str = ""
    webid: str = ""
    keys: list[CapturedKey] = field(default_factory=list)

    #: 登录后顺带查到的额度，供用户确认
    plan_name: str | None = None
    plan_status: str | None = None
    credits_remaining: float | None = None
    credits_total: float | None = None
    expires_at: datetime | None = None

    error: str | None = None
    _task: asyncio.Task | None = None

    @property
    def expired(self) -> bool:
        return self.finished_at is not None and (
            datetime.now(timezone.utc) - self.finished_at
        ).total_seconds() > 900

    def as_public(self) -> dict:
        """不含任何密钥原文的对外状态。"""
        return {
            "id": self.id,
            "status": self.status,
            "message": self.message,
            "elapsed": round(
                (
                    (self.finished_at or datetime.now(timezone.utc)) - self.started_at
                ).total_seconds(),
                1,
            ),
            "keys": [k.as_public() for k in self.keys],
            "plan_name": self.plan_name,
            "plan_status": self.plan_status,
            "credits_remaining": self.credits_remaining,
            "credits_total": self.credits_total,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "error": self.error,
        }


class LoginManager:
    """管理登录会话。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sessions: dict[str, LoginSession] = {}
        self._playwright = None

    # ------------------------------------------------------------------
    @property
    def profile_dir(self) -> Path:
        return self.settings.data_dir / "browser_profile"

    def start(self, *, timeout: float = DEFAULT_TIMEOUT, import_all_keys: bool = False) -> LoginSession:
        """开一个新会话（异步任务在后台跑）。"""
        if not playwright_available():
            raise PlaywrightMissing(
                "未安装 playwright。执行 `pip install playwright` 即可启用浏览器登录导入"
                "（无需 `playwright install`：本功能复用系统已安装的 Chrome/Edge）。"
            )
        self.gc()
        session = LoginSession(id=secrets.token_urlsafe(12))
        self.sessions[session.id] = session
        session._task = asyncio.create_task(
            self._run(session, timeout=timeout, import_all_keys=import_all_keys)
        )
        return session

    def get(self, session_id: str) -> LoginSession | None:
        return self.sessions.get(session_id)

    def cancel(self, session_id: str) -> bool:
        session = self.sessions.get(session_id)
        if session is None or session.status in ("success", "failed", "cancelled"):
            return False
        if session._task:
            session._task.cancel()
        session.status = "cancelled"
        session.message = "已取消"
        session.finished_at = datetime.now(timezone.utc)
        return True

    def forget(self, session_id: str) -> None:
        """丢弃会话（连同内存中的凭据）。"""
        session = self.sessions.pop(session_id, None)
        if session and session._task and not session._task.done():
            session._task.cancel()

    def gc(self) -> None:
        for sid in [s.id for s in self.sessions.values() if s.expired]:
            self.sessions.pop(sid, None)

    # ------------------------------------------------------------------
    async def _run(
        self, session: LoginSession, *, timeout: float, import_all_keys: bool
    ) -> None:
        """主流程：开窗口 → 等登录 → 抓凭据 → 取 Key → 查额度。"""
        from playwright.async_api import async_playwright

        session.status = "waiting"
        self.profile_dir.mkdir(parents=True, exist_ok=True)

        context = None
        try:
            self._playwright = await async_playwright().start()

            last_error: str | None = None
            for channel in BROWSER_CHANNELS:
                try:
                    context = await self._playwright.chromium.launch_persistent_context(
                        user_data_dir=str(self.profile_dir),
                        channel=channel,
                        headless=False,
                        args=["--no-first-run", "--no-default-browser-check"],
                        viewport={"width": 1280, "height": 860},
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - 逐个 channel 试
                    last_error = f"{channel}: {type(exc).__name__}: {exc}"
                    continue

            if context is None:
                raise RuntimeError(
                    "无法启动浏览器（已尝试 chrome / msedge）。"
                    f"最后一个错误：{last_error}"
                )

            pages = context.pages
            page = pages[0] if pages else await context.new_page()
            session.message = "已打开浏览器窗口，请完成登录"
            with contextlib.suppress(Exception):
                await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=45000)

            deadline = time.monotonic() + timeout
            last_detail = ""
            while time.monotonic() < deadline:
                if session.status == "cancelled":
                    return

                # 逐个站点试：会话通常落在 platform 上，但凭据按域隔离
                for origin in CANDIDATE_ORIGINS:
                    creds = await self._read_credentials(context, page, origin)
                    if not creds:
                        continue
                    token, webid = creds
                    session.message = f"已登录（{origin.split('//')[1]}），正在读取 API Key…"
                    ok, detail = await self._collect(session, token, webid, origin)
                    if ok:
                        return
                    last_detail = f"{origin}: {detail}"
                    session.message = f"已登录，但读取 Key 失败：{detail}"

                await asyncio.sleep(POLL_INTERVAL)

            if last_detail:
                session.error = f"等待登录超时。最后一次尝试：{last_detail}"

            session.status = "failed"
            session.error = f"等待登录超时（{int(timeout)} 秒）"
            session.message = session.error

        except asyncio.CancelledError:
            session.status = "cancelled"
            session.message = "已取消"
            raise
        except Exception as exc:  # noqa: BLE001 - 任何异常都要回给用户
            session.status = "failed"
            session.error = f"{type(exc).__name__}: {exc}"
            session.message = "登录流程失败"
            log.warning("登录会话失败：%s", exc)
        finally:
            if session.finished_at is None:
                session.finished_at = datetime.now(timezone.utc)
            if context is not None:
                with contextlib.suppress(Exception):
                    await context.close()
            if self._playwright is not None:
                with contextlib.suppress(Exception):
                    await self._playwright.stop()
                self._playwright = None

    # ------------------------------------------------------------------
    @staticmethod
    async def _read_credentials(
        context: Any, page: Any, origin: str
    ) -> tuple[str, str] | None:
        """读取指定站点下的 Oasis-Token（Cookie）与 web_id（localStorage）。

        两者都按域隔离，所以必须有针对性地访问该站点后再各取一份。
        """
        with contextlib.suppress(Exception):
            await page.goto(origin + "/", wait_until="domcontentloaded", timeout=20000)

        token = ""
        with contextlib.suppress(Exception):
            for cookie in await context.cookies(origin + "/"):
                if cookie.get("name") == "Oasis-Token" and cookie.get("value"):
                    token = cookie["value"]
                    break

        webid = ""
        with contextlib.suppress(Exception):
            webid = await page.evaluate("() => localStorage.getItem('web_id')") or ""

        if not token or not webid:
            return None
        return token, webid

    async def _collect(
        self, session: LoginSession, token: str, webid: str, origin: str
    ) -> tuple[bool, str]:
        """用抓到的凭据取 Key，并查一次额度。

        返回 ``(是否成功, 失败说明)``。
        """
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Oasis-Token": token,
            "Oasis-Webid": webid,
            "Oasis-appID": str(CONSOLE_APP_ID),
            "Oasis-Platform": "web",
            "Origin": origin,
            "Referer": origin + "/",
            "User-Agent": "step2api/1.0",
        }
        url = f"{origin}/api/step.openapi.devcenter.Dashboard/ListAccessKeys"

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(20, connect=10), follow_redirects=False
            ) as client:
                resp = await client.post(url, headers=headers, json={})
        except httpx.HTTPError as exc:
            return False, f"{type(exc).__name__}"

        if resp.status_code != 200:
            body = resp.text[:120].replace("\n", " ")
            return False, f"HTTP {resp.status_code} {body}"

        try:
            data = resp.json()
        except ValueError:
            return False, "返回非 JSON"

        keys: list[CapturedKey] = []
        for raw in list(data.get("accessKeys") or []) + list(data.get("projKeys") or []):
            value = (raw.get("accessKey") or "").strip()
            if not value:
                continue
            created = raw.get("createdAt")
            created_at = None
            if created:
                with contextlib.suppress(TypeError, ValueError, OSError):
                    created_at = datetime.fromtimestamp(
                        int(created) / 1000, tz=timezone.utc
                    )
            keys.append(
                CapturedKey(
                    key_id=str(raw.get("keyId") or ""),
                    access_key=value,
                    name=str(raw.get("name") or ""),
                    is_default=str(raw.get("isDefault")) == "1",
                    created_at=created_at,
                )
            )

        if not keys:
            return False, "ListAccessKeys 未返回任何 Key（请在控制台先创建一个）"

        # 默认 Key 排前面
        keys.sort(key=lambda k: (not k.is_default, k.name))
        session.keys = keys
        session.token = token
        session.webid = webid

        # 顺带查一次额度，让用户在确认导入前就能看到套餐
        client = ConsoleClient(
            token, webid, settings=self.settings, base=origin
        )
        quota = await client.fetch()
        if quota.ok:
            session.plan_name = quota.status.plan_name
            session.plan_status = quota.status.status
            session.credits_remaining = quota.remaining_credits
            session.credits_total = quota.total_credits
            session.expires_at = quota.status.expired_at

        session.status = "success"
        session.finished_at = datetime.now(timezone.utc)
        session.message = (
            f"登录成功，抓取到 {len(keys)} 个 API Key"
            + (f"，套餐 {session.plan_name}" if session.plan_name else "")
        )
        return True, ""


__all__ = [
    "CapturedKey",
    "LoginManager",
    "LoginSession",
    "PlaywrightMissing",
    "playwright_available",
]
