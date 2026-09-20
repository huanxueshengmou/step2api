"""端到端冒烟测试。

前提：先起 ``scripts/fake_upstream.py``，再起 step2api（指向假上游）。
运行：``python scripts/smoke_test.py``
"""

from __future__ import annotations

import json
import os
import sys

import httpx

GW = "http://127.0.0.1:8790"
UP = "http://127.0.0.1:8899"

#: 代理健康检查打这个端点（假上游专用，任何方法都回 200）
CHECK_URL = f"{UP}/_debug/ping"

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(f"{name} :: {detail}")
        print(f"  FAIL  {name}  {detail}")


def reset_gateway(c: httpx.Client) -> None:
    """清空网关状态，让脚本可以重复运行。

    不清库的话，第二次运行会因为 Key 重复、粘性会话残留而误判失败。
    """
    for a in c.get(f"{GW}/api/accounts").json()["accounts"]:
        c.delete(f"{GW}/api/accounts/{a['id']}")
    for p in c.get(f"{GW}/api/pools").json()["pools"]:
        c.delete(f"{GW}/api/pools/{p['id']}")
    for x in c.get(f"{GW}/api/proxies").json()["proxies"]:
        c.delete(f"{GW}/api/proxies/{x['id']}")
    for s in c.get(f"{GW}/api/sessions").json()["sessions"]:
        c.delete(f"{GW}/api/sessions/{s['session_key']}")
    c.delete(f"{GW}/api/logs")


def main() -> int:
    c = httpx.Client(timeout=30.0)
    c.post(f"{GW}/api/settings/routing", json={"proxy_check_url": CHECK_URL})
    reset_gateway(c)
    c.post(f"{UP}/_debug/reset")

    # ------------------------------------------------------------------
    print("\n[1] 批量导入（含国内站拒绝 / 代理 / 重复）")
    import_payload = {
        "content": "\n".join(
            [
                "sk-flaky-000000000001|http://127.0.0.1:8901",
                "sk-gooda-000000000002|http://127.0.0.1:8902",
                "sk-goodb-000000000003",  # 故意不带代理 → 无代理
                "sk-dead-000000000004",
                "# 下面这行应被拒绝",
                "sk-badkey-999999999999|http://platform.stepfun.com:8080",
            ]
        ),
        "group_name": "smoke",
        "name_prefix": "smoke-",
        "verify": True,
        "proxy_assign": "pool",
        "new_pool_name": "smoke-pool",
        "pool_strategy": "round_robin",
    }
    r = c.post(f"{GW}/api/import", json=import_payload)
    check("导入接口返回 200", r.status_code == 200, r.text[:300])
    data = r.json()
    imported = [i for i in data.get("imported", []) if i.get("ok")]
    skipped = data.get("skipped", [])
    check("导入 4 个账号", len(imported) == 4, f"实际 {len(imported)}")
    check("国内站被拒绝", len(skipped) == 1 and "国内站" in (skipped[0].get("reason") or ""),
          json.dumps(skipped, ensure_ascii=False))
    check("自动创建代理池", bool(data.get("pool_id")), json.dumps(data)[:200])

    # ------------------------------------------------------------------
    print("\n[2] 额度解析（Step Plan 订阅通道）")
    accounts = c.get(f"{GW}/api/accounts?group=smoke").json()["accounts"]
    by_name = {a["name"]: a for a in accounts}
    check("账号数 4", len(accounts) == 4, str(len(accounts)))

    a = by_name.get("smoke-2")
    check("flash_plus 套餐名归一化", a and a["plan_name"] == "Flash Plus", str(a and a["plan_name"]))
    check("剩余额度 1.2e9", a and a["credits_remaining"] == 1_200_000_000, str(a and a["credits_remaining"]))
    check("总额度 1.6e9", a and a["credits_total"] == 1_600_000_000, str(a and a["credits_total"]))
    check("剩余比例 75%", a and abs(a["percent_remaining"] - 0.75) < 1e-6, str(a and a["percent_remaining"]))
    check("Plan 时长有值", a and a["seconds_remaining"] and a["seconds_remaining"] > 0,
          str(a and a["seconds_remaining"]))
    check("额度来源为 plan", a and a["quota_source"] == "plan", str(a and a["quota_source"]))
    check("额度端点已缓存", a and a["plan_endpoint"] and a["plan_endpoint"].endswith("/usage"),
          str(a and a["plan_endpoint"]))

    b = by_name.get("smoke-3")
    check("flash_pro 命中", b and b["plan_name"] == "Flash Pro", str(b and b["plan_name"]))

    # 无效 Key 应回落到余额通道（假上游对无效 key 返回 401，这里用 dead 账号验证失败记录）
    dead = by_name.get("smoke-4")
    check("无效 Key 额度查询失败", dead and dead["quota_ok"] is False, str(dead and dead["quota_ok"]))
    check("失败原因已记录", dead and bool(dead["quota_error"]), str(dead and dead["quota_error"]))

    # ------------------------------------------------------------------
    # 账号 ID 是自增的，不硬编码，从导入结果里取
    ids = {i["name"]: i["id"] for i in imported}
    a1, a2, a3, a4 = ids["smoke-1"], ids["smoke-2"], ids["smoke-3"], ids["smoke-4"]

    print("\n[3] 金额余额通道（按量计费，分级体系）")
    c.post(f"{GW}/api/accounts/{a1}/refresh")
    acc = c.get(f"{GW}/api/accounts/{a1}").json()["account"]
    check("余额已解析", acc["balance"] == 12.5, str(acc["balance"]))
    check("现金余额", acc["cash_balance"] == 10.0, str(acc["cash_balance"]))
    check("赠送余额", acc["voucher_balance"] == 2.5, str(acc["voucher_balance"]))
    # 两条通道并存：同一账号既有订阅额度又有账户余额
    check("订阅额度与余额可同时取得",
          acc["credits_remaining"] is not None and acc["balance"] is not None,
          json.dumps({"credits": acc["credits_remaining"], "balance": acc["balance"]}))

    # ------------------------------------------------------------------
    print("\n[4] 故障转移（429 的账号应被跳过）")
    c.post(f"{UP}/_debug/reset")
    # 不指定账号：flaky 额度最充足会被排到首位，它返回 429，网关应换账号
    r = c.post(
        f"{GW}/step_plan/v1/messages",
        json={"model": "step-3.5-flash", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Session-Id": "failover-test"},
    )
    check("转发成功", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
    body = r.json()
    served_by = body.get("content", [{}])[0].get("text", "")
    check("由健康账号服务", "sk-flaky" not in served_by and "sk-dead" not in served_by, served_by)
    check("回落重试次数 >= 2", int(r.headers.get("x-step2api-attempt", "1")) >= 2,
          r.headers.get("x-step2api-attempt", "?"))
    check("响应头回带账号", bool(r.headers.get("x-step2api-account")),
          str(dict(r.headers)))

    # ------------------------------------------------------------------
    print("\n[5] 粘性路由（同会话固定同账号）")
    first = None
    stable = True
    for _ in range(4):
        rr = c.post(
            f"{GW}/step_plan/v1/messages",
            json={"model": "step-3.5-flash", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Session-Id": "sticky-abc"},
        )
        got = rr.headers.get("x-step2api-account")
        if first is None:
            first = got
        elif got != first:
            stable = False
    check("4 次请求命同一账号", stable, f"first={first}")
    sessions = c.get(f"{GW}/api/sessions").json()["sessions"]
    check("会话已入库", any(s["session_key"] == "hdr:sticky-abc" for s in sessions),
          json.dumps(sessions[:3], ensure_ascii=False))

    # ------------------------------------------------------------------
    print("\n[6] 代理池轮转（affinity=sticky：账号间摊开，账号内固定）")
    pool = c.get(f"{GW}/api/pools").json()["pools"][0]
    pool_id = pool["id"]
    check("池内有代理", pool["member_count"] >= 2, str(pool["member_count"]))
    check("池策略 round_robin", pool["strategy"] == "round_robin", pool["strategy"])
    check("池默认 affinity=sticky", pool["affinity"] == "sticky", pool["affinity"])

    # 把另一个账号也绑到同一个池上，观察多账号是否摊开
    c.patch(f"{GW}/api/accounts/{a3}", json={"proxy_mode": "pool", "pool_id": pool_id})

    per_account: dict[str, set] = {}
    for aid in (a2, a3, a4):
        for i in range(4):
            rr = c.post(
                f"{GW}/step_plan/v1/messages",
                json={"model": "step-3.5-flash", "messages": [{"role": "user", "content": "hi"}]},
                headers={"X-Session-Id": f"spread-{aid}-{i}", "X-Step2api-Account": str(aid)},
            )
            per_account.setdefault(str(aid), set()).add(rr.headers.get("x-step2api-proxy"))

    check("同账号出口固定", all(len(v) == 1 for v in per_account.values()),
          json.dumps({k: sorted(map(str, v)) for k, v in per_account.items()}))
    spread = {next(iter(v)) for v in per_account.values() if v}
    check("不同账号摊到不同代理", len(spread) >= 2, json.dumps(sorted(map(str, spread))))

    # 上游确实看到了代理注入头
    dbg = c.get(f"{UP}/_debug/requests").json()
    proxy_hits = {r_["proxy"] for r_ in dbg["requests"] if r_["proxy"]}
    check("上游收到 X-Via-Proxy 注入", len(proxy_hits) >= 2, json.dumps(sorted(proxy_hits)))

    # ------------------------------------------------------------------
    print("\n[7] 池 affinity=rotate（同账号每次请求换出口）")
    c.patch(f"{GW}/api/pools/{pool_id}", json={"affinity": "rotate"})
    rotated = {c.post(
        f"{GW}/step_plan/v1/messages",
        json={"model": "step-3.5-flash", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Session-Id": f"rot-{i}", "X-Step2api-Account": str(a2)},
    ).headers.get("x-step2api-proxy") for i in range(8)}
    check("rotate 模式下出口会变", len(rotated) >= 2, json.dumps(sorted(map(str, rotated))))
    c.patch(f"{GW}/api/pools/{pool_id}", json={"affinity": "sticky"})

    # 账号级轮转开关：关掉后固定池内第一个代理
    c.patch(f"{GW}/api/accounts/{a2}", json={"proxy_rotation": False})
    fixed = {c.post(
        f"{GW}/step_plan/v1/messages",
        json={"model": "step-3.5-flash", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Session-Id": f"fixed-{i}", "X-Step2api-Account": str(a2)},
    ).headers.get("x-step2api-proxy") for i in range(6)}
    check("关闭账号轮转后出口固定", len(fixed) == 1, json.dumps(sorted(map(str, fixed))))
    c.patch(f"{GW}/api/accounts/{a2}", json={"proxy_rotation": True})

    # ------------------------------------------------------------------
    print("\n[8] 直连模式与专属代理模式")
    c.patch(f"{GW}/api/accounts/{a2}", json={"proxy_mode": "direct"})
    rr = c.post(
        f"{GW}/step_plan/v1/messages",
        json={"model": "step-3.5-flash", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Step2api-Account": str(a2)},
    )
    check("direct 模式返回 direct", rr.headers.get("x-step2api-proxy") == "direct",
          rr.headers.get("x-step2api-proxy", "?"))

    # 专属代理：把账号 2 绑到 3 号代理上
    c.post(f"{GW}/api/proxies", json={"url": "http://127.0.0.1:8903", "label": "dedicated-3"})
    ded_id = [p["id"] for p in c.get(f"{GW}/api/proxies").json()["proxies"] if "8903" in p["url"]][0]
    c.patch(f"{GW}/api/accounts/{a2}", json={"proxy_mode": "dedicated", "proxy_id": ded_id})
    rr = c.post(
        f"{GW}/step_plan/v1/messages",
        json={"model": "step-3.5-flash", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Step2api-Account": str(a2)},
    )
    check("dedicated 模式命中专属代理", rr.headers.get("x-step2api-proxy") == f"proxy-{ded_id}",
          rr.headers.get("x-step2api-proxy", "?"))
    c.patch(f"{GW}/api/accounts/{a2}", json={"proxy_mode": "pool", "pool_id": pool_id})

    # ------------------------------------------------------------------
    print("\n[9] 流式 SSE 透传（按量计费通道）")
    with c.stream(
        "POST",
        f"{GW}/v1/chat/completions",
        json={"model": "step-3.5-flash", "messages": [{"role": "user", "content": "hi"}], "stream": True},
    ) as resp:
        check("流式状态 200", resp.status_code == 200, str(resp.status_code))
        check("content-type 为 SSE",
              "text/event-stream" in resp.headers.get("content-type", ""),
              resp.headers.get("content-type", ""))
        chunks = [line for line in resp.iter_lines() if line.startswith("data:")]
    check("收到多个 SSE 数据块", len(chunks) >= 3, str(len(chunks)))
    check("含 [DONE]", any("[DONE]" in ch for ch in chunks), str(chunks[-1:]))

    # ------------------------------------------------------------------
    print("\n[10] 请求日志与用量统计")
    logs = c.get(f"{GW}/api/logs?limit=50").json()["logs"]
    check("日志非空", len(logs) > 0, str(len(logs)))
    with_usage = [l for l in logs if l["total_tokens"]]
    check("记录了 token 用量", len(with_usage) > 0, str(len(with_usage)))
    with_retry = [l for l in logs if (l["attempts"] or 1) > 1]
    check("记录了重试次数", len(with_retry) > 0, str(len(with_retry)))
    streamed = [l for l in logs if l["channel"] == "api"]
    check("按量通道被标记为 api", len(streamed) > 0, str(len(streamed)))

    stats = c.get(f"{GW}/api/stats").json()
    check("统计含账号汇总", stats["accounts"]["total"] == 4, json.dumps(stats["accounts"]))
    check("统计含 Credit 合计", stats["credits"]["remaining"] > 0, str(stats["credits"]))
    check("24h 用量有数据", len(stats["usage_24h"]) > 0, str(len(stats["usage_24h"])))

    # ------------------------------------------------------------------
    print("\n[11] 凭据加密（落库不含明文）")
    import sqlite3

    # 库路径跟着 STEP2API_DATA_DIR 走，别硬编码，否则换目录就断
    data_dir = os.environ.get("STEP2API_DATA_DIR", "./data")
    db = sqlite3.connect(os.path.join(data_dir, "step2api.db"))
    rows = db.execute("SELECT api_key_enc, key_hint FROM accounts").fetchall()
    check("数据库无 Key 明文",
          all("sk-gooda-000000000002" not in (r[0] or "") for r in rows),
          str(rows[:1]))
    check("Key 为密文前缀", all((r[0] or "").startswith("enc:v1:") for r in rows), str(rows[:1]))
    prows = db.execute("SELECT url_enc, url_redacted FROM proxies").fetchall()
    check("代理密码脱敏", all("@" not in (r[1] or "") or "***" in (r[1] or "") for r in prows),
          str(prows[:2]))
    db.close()

    # ------------------------------------------------------------------
    print("\n[12] 账号禁用 / 冷却 / 复位")
    c.post(f"{GW}/api/accounts/{a3}/toggle")
    rr = c.post(
        f"{GW}/step_plan/v1/messages",
        json={"model": "step-3.5-flash", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Step2api-Account": str(a3)},
    )
    check("指定已禁用账号被拒", rr.status_code == 503, f"{rr.status_code} {rr.text[:120]}")
    c.post(f"{GW}/api/accounts/{a3}/toggle")
    c.post(f"{GW}/api/accounts/{a3}/reset")
    acc = c.get(f"{GW}/api/accounts/{a3}").json()["account"]
    check("复位后状态 healthy", acc["status"] == "healthy", acc["status"])

    # ------------------------------------------------------------------
    print("\n[13] 代理健康检查")
    r = c.post(f"{GW}/api/proxies/check", json=None).json()
    ok_count = sum(1 for x in r["results"] if x["ok"])
    check("代理全部可用", ok_count == len(r["results"]), json.dumps(r["results"])[:300])
    check("延迟已记录", all(x["latency_ms"] is not None for x in r["results"]), json.dumps(r["results"])[:200])

    # ------------------------------------------------------------------
    print("\n[14] 控制台静态资源")
    for path, needle in [("/", "step2api"), ("/static/app.js", "loadAccounts"), ("/static/style.css", "--accent")]:
        r = c.get(f"{GW}{path}")
        check(f"GET {path}", r.status_code == 200 and needle in r.text, str(r.status_code))

    # ------------------------------------------------------------------
    print("\n[15] 端点探测命令")
    r = c.post(f"{GW}/api/accounts/{a2}/probe-endpoint").json()
    check("探测命中 /usage", r["hit"] and r["hit"].endswith("/usage"), json.dumps(r["hit"]))
    check("探测返回明细", len(r["detail"]) > 0, str(len(r["detail"])))

    # ------------------------------------------------------------------
    print("\n" + "=" * 62)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        print("\n失败明细：")
        for f in FAILED:
            print("  - " + f)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
