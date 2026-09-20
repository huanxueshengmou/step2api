"""后台任务：额度刷新、会话清理、日志裁剪、代理解除冷却。

全部跑在同一个 asyncio 循环里，单个账号的额度刷新失败不影响其它账号。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .config import Settings
from .proxy import check_many
from .quota import fetch_quota
from .router import Router
from .store import Store

log = logging.getLogger("step2api.scheduler")


class Scheduler:
    """轻量后台调度器。"""

    def __init__(self, store: Store, settings: Settings, router: Router) -> None:
        self.store = store
        self.settings = settings
        self.router = router
        self._tasks: list[asyncio.Task] = []
        self._stopping = asyncio.Event()
        self.last_refresh_at: datetime | None = None
        self.last_refresh_error: str | None = None
        self.refreshing = False

    # ------------------------------------------------------------------
    async def start(self) -> None:
        self._stopping.clear()
        self._tasks = [
            asyncio.create_task(self._quota_loop(), name="quota-loop"),
            asyncio.create_task(self._maintenance_loop(), name="maintenance-loop"),
        ]
        log.info("后台调度器已启动（额度刷新间隔 %.0fs）", self.settings.refresh_interval)

    async def stop(self) -> None:
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()

    # ------------------------------------------------------------------
    async def _quota_loop(self) -> None:
        # 启动后先等一会儿，让服务先把 HTTP 端口起起来
        await self._sleep(self.settings.refresh_interval)
        while not self._stopping.is_set():
            try:
                await self.refresh_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 后台任务不能因单次异常退出
                self.last_refresh_error = f"{type(exc).__name__}: {exc}"
                log.warning("额度刷新失败：%s", exc)
            await self._sleep(self.settings.refresh_interval)

    async def _maintenance_loop(self) -> None:
        while not self._stopping.is_set():
            await self._sleep(60.0)
            try:
                self.store.release_expired_cooldowns()
                self.store.purge_sessions()
                self.store.prune_logs(self.settings.log_retention)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("维护任务失败：%s", exc)

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stopping.wait(), timeout=max(1.0, seconds))
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------------
    async def refresh_account(self, account_id: int) -> dict:
        """刷新单个账号的额度，返回可序列化结果。"""
        row = self.store.get_account(account_id)
        if row is None:
            return {"id": account_id, "ok": False, "error": "账号不存在"}

        account = dict(row)
        api_key = self.store.decrypt_key(row)
        if not api_key:
            return {"id": account_id, "ok": False, "error": "缺少 API Key"}

        try:
            proxy = self.router.resolve_proxy(account)
        except Exception:  # noqa: BLE001 - 代理异常时退化为直连探测
            proxy = None

        result = await fetch_quota(
            api_key,
            settings=self.settings,
            proxy=proxy.url if proxy else None,
            known_plan_endpoint=account.get("plan_endpoint"),
        )
        snapshot = result.snapshot.as_dict()
        snapshot["plan_endpoint"] = result.plan_endpoint
        self.store.update_account_quota(account_id, snapshot)

        # 额度恢复了，顺带解除冷却
        if snapshot.get("ok") and account.get("status") == "cooldown":
            if snapshot.get("credits_remaining") is None or snapshot["credits_remaining"] > 0:
                self.store.clear_cooldown(account_id)

        return {"id": account_id, **snapshot}

    async def refresh_all(self, *, account_ids: list[int] | None = None) -> list[dict]:
        """并发刷新全部（或指定）账号额度。"""
        if self.refreshing:
            return []
        self.refreshing = True
        try:
            if account_ids:
                targets = [i for i in account_ids if self.store.get_account(i)]
            else:
                targets = [int(r["id"]) for r in self.store.list_accounts()]

            if not targets:
                self.last_refresh_at = datetime.now(timezone.utc)
                return []

            sem = asyncio.Semaphore(max(1, self.settings.import_concurrency))

            async def _one(account_id: int) -> dict:
                async with sem:
                    try:
                        return await self.refresh_account(account_id)
                    except Exception as exc:  # noqa: BLE001
                        return {"id": account_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"}

            results = await asyncio.gather(*(_one(i) for i in targets))
            self.last_refresh_at = datetime.now(timezone.utc)
            self.last_refresh_error = None
            return list(results)
        finally:
            self.refreshing = False

    # ------------------------------------------------------------------
    async def check_proxies(self, proxy_ids: list[int] | None = None) -> list[dict]:
        """检查代理健康状态。"""
        rows = self.store.list_proxies()
        if proxy_ids:
            wanted = set(proxy_ids)
            rows = [r for r in rows if int(r["id"]) in wanted]

        entries: list[tuple[int, str]] = []
        for row in rows:
            url = self.store.decrypt_proxy_url(row)
            if url:
                entries.append((int(row["id"]), url))

        if not entries:
            return []

        results = await check_many(
            [u for _, u in entries],
            self.settings.proxy_check_url,
            timeout=min(self.settings.probe_timeout, 20.0),
            concurrency=self.settings.import_concurrency,
        )

        out: list[dict] = []
        for proxy_id, url in entries:
            ok, latency, message = results.get(url, (False, None, "未探测"))
            self.store.record_proxy_result(proxy_id, ok=ok, latency_ms=latency, error=None if ok else message)
            self.router.proxy_pool.record_latency(url, latency)
            out.append(
                {"id": proxy_id, "ok": ok, "latency_ms": latency, "message": message}
            )
        return out
