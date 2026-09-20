"""命令行入口。

    step2api serve              启动服务
    step2api probe-endpoint     探测 Step Plan 额度端点（需要先有账号或直接给 key）
    step2api import             从文件批量导入账号
    step2api export             导出账号明细为 JSON
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import __version__


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import build_app
    from .config import get_settings

    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.port

    app = build_app()
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=args.log_level,
        access_log=args.access_log,
        timeout_keep_alive=75,
    )
    return 0


def _cmd_probe_endpoint(args: argparse.Namespace) -> int:
    from .config import get_settings
    from .quota import probe_plan_endpoint
    from .store import get_store

    settings = get_settings()
    api_key = args.key

    if not api_key:
        store = get_store(settings)
        rows = store.list_accounts()
        if not rows:
            print("库中没有账号，请用 --key 直接指定一个 Step API Key。", file=sys.stderr)
            return 2
        target = rows[0]
        api_key = store.decrypt_key(target)
        print(f"使用账号 #{target['id']} {target['name']} 的 Key 进行探测")

    print(f"Step Plan 基址：{settings.plan_base}")
    hit, lines = asyncio.run(
        probe_plan_endpoint(api_key, settings=settings, proxy=args.proxy)
    )
    for line in lines:
        print(line)
    if hit:
        print(f"\n✓ 命中额度端点：{hit}")
        print("  可通过 STEP2API_PLAN_BASE / STEP2API_PLAN_QUOTA_PATHS 固定该端点。")
        return 0
    print("\n✗ 未命中任何候选端点。")
    print("  上游可能未开放订阅额度查询接口，网关会回落到按量通道余额展示。")
    print("  也可以手动指定候选：STEP2API_PLAN_QUOTA_PATHS=/path1,/path2")
    return 1


def _cmd_import(args: argparse.Namespace) -> int:
    from .config import get_settings
    from .quota import fetch_quota
    from .store import get_store

    settings = get_settings()
    store = get_store(settings)

    path = args.file
    try:
        with open(path, "r", encoding="utf-8") as fh:
            content = fh.read()
    except OSError as exc:
        print(f"读取文件失败：{exc}", file=sys.stderr)
        return 2

    from .api import parse_import_content

    existing = {r["key_fp"] for r in store.list_accounts()}
    candidates = parse_import_content(
        content, settings=settings, name_prefix=args.prefix, existing_fps=existing
    )
    ok = [c for c in candidates if c.ok]

    print(f"解析到 {len(candidates)} 条，可导入 {len(ok)} 条。")
    if args.dry_run:
        for cand in candidates:
            mark = "✓" if cand.ok else "✗"
            label = cand.label or ""
            print(f"  {mark} {cand.api_key[:6]}…{cand.api_key[-4:]} {label} {cand.reason or ''}")
        return 0

    imported = 0
    for index, cand in enumerate(ok, start=1):
        name = cand.label or f"{args.prefix}{index}" if args.prefix else (cand.label or f"acct-{index}")
        account_id = store.create_account(
            name=name,
            api_key=cand.api_key,
            group_name=args.group,
            plan_base=settings.plan_base,
            balance_base=settings.upstream_base,
        )
        imported += 1
        if args.verify:
            result = asyncio.run(fetch_quota(store.decrypt_key(cand.api_key), settings=settings))
            snap = result.snapshot.as_dict()
            snap["plan_endpoint"] = result.plan_endpoint
            store.update_account_quota(account_id, snap)
            state = "OK" if snap["ok"] else f"失败：{snap.get('error')}"
            print(f"  #{account_id} {name} → {state}")
        else:
            print(f"  #{account_id} {name}")

    print(f"\n导入完成：{imported} 个账号。")
    if args.proxy:
        print(f"提示：可在控制台为这些账号绑定代理池 {args.proxy}。")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    from .config import get_settings
    from .store import get_store

    settings = get_settings()
    store = get_store(settings)

    rows = store.list_accounts()
    payload = []
    for row in rows:
        data = dict(row)
        data.pop("api_key_enc", None)
        payload.append(data)

    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"已导出 {len(payload)} 个账号到 {args.out}（不含 Key 明文）")
    else:
        print(text)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="step2api",
        description="StepFun Step Plan 多账号聚合网关",
    )
    parser.add_argument("--version", action="version", version=f"step2api {__version__}")
    sub = parser.add_subparsers(dest="command")

    serve = sub.add_parser("serve", help="启动服务")
    serve.add_argument("--host", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--log-level", default="info")
    serve.add_argument("--access-log", action="store_true", help="打印每个请求的访问日志")
    serve.set_defaults(func=_cmd_serve)

    probe = sub.add_parser("probe-endpoint", help="探测 Step Plan 额度查询端点")
    probe.add_argument("--key", default=None, help="直接指定 Step API Key；省略则用库中第一个账号")
    probe.add_argument("--proxy", default=None, help="探测时使用的代理")
    probe.set_defaults(func=_cmd_probe_endpoint)

    imp = sub.add_parser("import", help="从文件批量导入账号")
    imp.add_argument("file", help="每行一个 Key，或 key|代理URL")
    imp.add_argument("--group", default="default")
    imp.add_argument("--prefix", default="", help="自动命名前缀")
    imp.add_argument("--proxy", default=None, help="（提示用）导入后建议绑定的代理池名")
    imp.add_argument("--verify", action="store_true", help="导入后立即查询额度")
    imp.add_argument("--dry-run", action="store_true", help="只解析不落库")
    imp.set_defaults(func=_cmd_import)

    exp = sub.add_parser("export", help="导出账号明细（不含 Key 明文）")
    exp.add_argument("--out", default=None, help="输出文件；省略则打印到 stdout")
    exp.set_defaults(func=_cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 0
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\n已中断。")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
