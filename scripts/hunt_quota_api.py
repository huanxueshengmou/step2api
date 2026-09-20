"""从 StepFun 控制台的公开前端 JS 里挖出额度/用量接口。

JS 是静态资源，不需要登录即可下载。控制台用 Next.js + gRPC-Web，
接口形如 ``/api/proto.api.<包>.<Service>/<Method>``。

做法：

1. 抓控制台首页，收集所有 ``/_next/static/chunks/*.js``
2. 优先深挖与账单/额度相关的页面 chunk（plan-usage、account-overview…）
3. 用多组正则抽出路径、gRPC 服务名、方法名，按相关性排序

用法::

    python scripts/hunt_quota_api.py
    python scripts/hunt_quota_api.py --host https://account.stepfun.ai
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import re
from collections import Counter

import httpx

CHUNK_RE = re.compile(r'"(/_next/static/[^"]+?\.js)"')

#: 从浏览器另存下来的页面 HTML 里抽 chunk 路径。dashboard 页面的 chunk 不在
#: 首页 bundle 里，只有拿得到页面 HTML 才能知道它们的文件名。
HTML_CHUNK_RE = re.compile(r'"(/_next/static/chunks/[^"]+?\.js)"')


def chunks_from_html(path: str) -> list[str]:
    text = pathlib.Path(path).read_text(encoding="utf-8", errors="ignore")
    return sorted(set(HTML_CHUNK_RE.findall(text)))

#: 页面 chunk 里优先关注的（额度/账单/用量相关页面）
HOT_PAGES = (
    "plan-usage", "plan-subscribe", "account-overview", "billing",
    "usage", "recharge", "voucher", "payment", "credit",
)

#: 抽 gRPC-Web 风格的服务/方法
GRPC_RE = re.compile(r"(proto\.[A-Za-z0-9_.]+)")
GRPC_CALL_RE = re.compile(r'"((?:/api/)?proto\.[A-Za-z0-9_.]+/[A-Za-z0-9_]+)"')

#: 通用路径字面量
PATH_RE = re.compile(r'"((?:/api|/v\d|/oasis|/billing|/account|/user)[A-Za-z0-9/_\-{}.:]*)"')

#: 额度语义关键词
HOT = (
    "credit", "quota", "usage", "plan", "subscri", "balance", "remain",
    "limit", "voucher", "wallet", "bill", "consume", "package", "order",
)
COLD = ("static", ".css", ".js", ".png", ".svg", ".woff", "image", "font", "/docs/")


def score(path: str) -> int:
    low = path.lower()
    if any(c in low for c in COLD):
        return -99
    s = 0
    for kw in HOT:
        if kw in low:
            s += 10
    if low.startswith(("/api/", "/v1/", "/v2/")):
        s += 5
    if "proto." in low:
        s += 4
    return s


async def fetch(client: httpx.AsyncClient, url: str) -> str:
    try:
        r = await client.get(url)
        if r.status_code == 200:
            return r.text
    except httpx.HTTPError:
        pass
    return ""


async def scan(host: str, limit: int, concurrency: int) -> None:
    print(f"\n{'=' * 74}\n{host}\n{'=' * 74}")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        )
    }

    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True, headers=headers
    ) as client:
        try:
            home = await client.get(host)
        except httpx.HTTPError as exc:
            print(f"  首页抓取失败: {exc}")
            return

        chunks = sorted({c for c in CHUNK_RE.findall(home.text)})
        print(f"  首页发现 {len(chunks)} 个 chunk")

        sem = asyncio.Semaphore(concurrency)

        async def one(rel: str) -> tuple[str, str]:
            async with sem:
                return rel, await fetch(client, "https://" + host.split("://", 1)[-1].strip("/") + rel)

        results = await asyncio.gather(*(one(c) for c in chunks))
        blobs = {rel: txt for rel, txt in results if txt}
        print(f"  已下载 {sum(len(t) for t in blobs.values()):,} 字符")

        # ---- 1. 页面级 chunk 逐个深挖 ----
        page_chunks = {
            rel: txt
            for rel, txt in blobs.items()
            if any(p in rel.lower() for p in HOT_PAGES)
        }
        print(f"\n  --- 额度相关页面 chunk（{len(page_chunks)} 个）---")
        for rel, txt in sorted(page_chunks.items()):
            calls = sorted(set(GRPC_CALL_RE.findall(txt)))
            services = sorted(set(GRPC_RE.findall(txt)))
            paths = sorted({
                m.group(1) for m in PATH_RE.finditer(txt)
            })
            print(f"\n  ▸ {rel.split('/')[-1]}  ({len(txt):,} 字符)")
            if calls:
                print("      gRPC 调用:")
                for c in calls[:12]:
                    print(f"        {c}")
            if services:
                print(f"      服务: {', '.join(services[:10])}")
            interesting = [p for p in paths if score(p) > 0]
            if interesting:
                print("      路径:")
                for p in interesting[:12]:
                    print(f"        [{score(p):>3}] {p}")

        # ---- 2. 全量汇总 ----
        all_text = "\n".join(blobs.values())
        found: Counter[str] = Counter()
        for m in GRPC_CALL_RE.finditer(all_text):
            found[m.group(1)] += 1
        for m in PATH_RE.finditer(all_text):
            found[m.group(1)] += 1

        ranked = sorted(
            ((p, c, score(p)) for p, c in found.items() if "${" not in p),
            key=lambda x: (-x[2], -x[1], x[0]),
        )
        hot = [r for r in ranked if r[2] > 0]

        print(f"\n  --- 全站汇总：{len(found)} 个候选，相关 {len(hot)} 个 ---\n")
        for path, count, sc in hot[:limit]:
            print(f"    [{sc:>3}] {path}   (出现 {count} 次)")

        services = Counter(GRPC_RE.findall(all_text))
        print("\n  --- 出现的 gRPC 服务（前 20）---")
        for svc, cnt in services.most_common(20):
            print(f"    {cnt:>4}  {svc}")


async def scan_chunks(
    origin: str, rels: list[str], limit: int, concurrency: int
) -> None:
    """直接下载给定的 chunk 列表并挖掘（chunk 路径来自页面 HTML）。"""
    print(f"\n{'=' * 74}\n{origin}  —  来自页面 HTML 的 {len(rels)} 个 chunk\n{'=' * 74}")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
        ),
        "Referer": origin + "/",
    }

    async with httpx.AsyncClient(
        timeout=30, follow_redirects=True, headers=headers
    ) as client:
        sem = asyncio.Semaphore(concurrency)

        async def one(rel: str) -> tuple[str, str]:
            async with sem:
                return rel, await fetch(client, origin + rel)

        results = await asyncio.gather(*(one(r) for r in rels))
        blobs = {r: t for r, t in results if t}
        print(f"  成功下载 {len(blobs)}/{len(rels)} 个，{sum(len(t) for t in blobs.values()):,} 字符")

        print("\n  --- 额度相关页面 chunk 深挖 ---")
        shown = 0
        for rel, txt in sorted(blobs.items()):
            name = rel.split("/")[-1]
            interesting = any(p in rel.lower() for p in HOT_PAGES)
            calls = sorted(set(GRPC_CALL_RE.findall(txt)))
            paths = sorted({m.group(1) for m in PATH_RE.finditer(txt) if score(m.group(1)) > 0})
            if not (calls or paths or interesting):
                continue
            shown += 1
            print(f"\n  ▸ {name}  ({len(txt):,} 字符)")
            if calls:
                for c in calls[:14]:
                    print(f"      gRPC  {c}")
            for p in paths[:14]:
                print(f"      [{score(p):>3}] {p}")
        if not shown:
            print("      （无命中）")

        all_text = "\n".join(blobs.values())
        found: Counter[str] = Counter()
        for m in GRPC_CALL_RE.finditer(all_text):
            found[m.group(1)] += 1
        for m in PATH_RE.finditer(all_text):
            found[m.group(1)] += 1

        ranked = sorted(
            ((p, c, score(p)) for p, c in found.items() if "${" not in p),
            key=lambda x: (-x[2], -x[1], x[0]),
        )
        hot = [r for r in ranked if r[2] > 0]
        print(f"\n  --- 汇总：{len(found)} 个候选，相关 {len(hot)} 个 ---\n")
        for path, count, sc in hot[:limit]:
            print(f"    [{sc:>3}] {path}   (出现 {count} 次)")

        services = Counter(GRPC_RE.findall(all_text))
        if services:
            print("\n  --- gRPC 服务 ---")
            for svc, cnt in services.most_common(25):
                print(f"    {cnt:>4}  {svc}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", action="append", default=None)
    ap.add_argument(
        "--html",
        action="append",
        default=None,
        help="浏览器另存的页面 HTML 路径，从中提取 chunk 清单后逐个下载",
    )
    ap.add_argument("--origin", default="https://platform.stepfun.ai")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--concurrency", type=int, default=12)
    args = ap.parse_args()

    if args.html:
        rels: list[str] = []
        for h in args.html:
            try:
                rels.extend(chunks_from_html(h))
            except OSError as exc:
                print(f"读不到 {h}: {exc}")
        rels = sorted(set(rels))
        await scan_chunks(args.origin.rstrip("/"), rels, args.limit, args.concurrency)
        return 0

    hosts = args.host or [
        "https://platform.stepfun.ai/",
        "https://account.stepfun.ai/",
    ]
    for h in hosts:
        await scan(h, args.limit, args.concurrency)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
