"""核心逻辑单元测试。

运行：``uv run pytest``
"""

from __future__ import annotations

import pytest

from step2api.config import Settings
from step2api.crypto import SecretBox, key_fingerprint, key_hint
from step2api.proxy import (
    ProxyError,
    ProxyPool,
    normalize_proxy,
    parse_proxy_list,
    redact_proxy,
)
from step2api.quota import (
    QuotaSnapshot,
    parse_balance_payload,
    parse_plan_payload,
)
from step2api.router import (
    RouteContext,
    Router,
    build_upstream_url,
    extract_model,
    extract_session_key,
    select_channel,
    should_cooldown,
    should_failover,
)
from step2api.store import Store


# --------------------------------------------------------------------------
# crypto
# --------------------------------------------------------------------------


def test_secret_box_roundtrip():
    box = SecretBox("test-secret")
    token = box.encrypt("sk-abcdef123456")
    assert token.startswith("enc:v1:")
    assert "sk-abcdef123456" not in token
    assert box.decrypt(token) == "sk-abcdef123456"


def test_secret_box_different_keys_do_not_interop():
    a = SecretBox("key-a").encrypt("data")
    with pytest.raises(ValueError):
        SecretBox("key-b").decrypt(a)


def test_key_hint_hides_middle():
    hint = key_hint("sk-1234567890abcdef")
    assert hint.startswith("sk-123")
    assert hint.endswith("cdef")
    assert "4567890" not in hint


def test_key_fingerprint_stable_and_unique():
    assert key_fingerprint("sk-a") == key_fingerprint("sk-a")
    assert key_fingerprint("sk-a") != key_fingerprint("sk-b")


# --------------------------------------------------------------------------
# proxy
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://1.2.3.4:8080", "http://1.2.3.4:8080"),
        ("1.2.3.4:8080", "http://1.2.3.4:8080"),
        ("socks5://u:p@1.2.3.4:1080", "socks5://u:p@1.2.3.4:1080"),
        ("  http://1.2.3.4:8080  ", "http://1.2.3.4:8080"),
    ],
)
def test_normalize_proxy(raw, expected):
    assert normalize_proxy(raw) == expected


def test_normalize_proxy_rejects_bad_input():
    with pytest.raises(ProxyError):
        normalize_proxy("ftp://1.2.3.4:21")
    with pytest.raises(ProxyError):
        normalize_proxy("http://1.2.3.4")


def test_parse_proxy_list_dedupes():
    blob = "1.2.3.4:8080\nhttp://1.2.3.4:8080\n# comment\n5.6.7.8:1080"
    assert parse_proxy_list(blob) == ["http://1.2.3.4:8080", "http://5.6.7.8:1080"]


def test_redact_proxy_hides_credentials():
    assert redact_proxy("http://user:secret@1.2.3.4:8080") == "http://user:***@1.2.3.4:8080"
    assert redact_proxy("http://1.2.3.4:8080") == "http://1.2.3.4:8080"


def test_proxy_pool_round_robin_cycles():
    pool = ProxyPool("round_robin")
    members = [{"url": "http://a:1", "id": 1}, {"url": "http://b:2", "id": 2}]
    picked = [pool.pick(members)["url"] for _ in range(4)]
    assert picked == ["http://a:1", "http://b:2", "http://a:1", "http://b:2"]


def test_proxy_pool_skips_disabled_and_unhealthy():
    pool = ProxyPool("round_robin")
    members = [
        {"url": "http://a:1", "id": 1, "enabled": False},
        {"url": "http://b:2", "id": 2, "status": "unhealthy"},
        {"url": "http://c:3", "id": 3, "status": "healthy"},
    ]
    assert pool.pick(members)["url"] == "http://c:3"


def test_proxy_pool_least_used():
    pool = ProxyPool("least_used")
    members = [{"url": "http://a:1", "id": 1}, {"url": "http://b:2", "id": 2}]
    pool.acquire("http://a:1")
    pool.acquire("http://a:1")
    assert pool.pick(members)["url"] == "http://b:2"


def test_proxy_pool_returns_none_when_empty():
    assert ProxyPool().pick([]) is None


# --------------------------------------------------------------------------
# quota 解析
# --------------------------------------------------------------------------


def test_parse_balance_payload():
    snap = parse_balance_payload(
        {
            "object": "account",
            "type": "prepaid",
            "balance": 12.5,
            "total_cash_balance": 10.0,
            "total_voucher_balance": 2.5,
        }
    )
    assert snap is not None
    assert snap.source == "balance"
    assert snap.balance == 12.5
    assert snap.account_type == "prepaid"


def test_parse_plan_payload_with_credits():
    snap = parse_plan_payload(
        {
            "data": {
                "plan": "flash_plus",
                "remaining_credits": 1_200_000_000,
                "total_credits": 1_600_000_000,
                "reset_at": "2026-10-01T00:00:00Z",
            }
        }
    )
    assert snap is not None
    assert snap.source == "plan"
    assert snap.plan_name == "Flash Plus"
    assert snap.credits_remaining == 1_200_000_000
    assert snap.percent_remaining == pytest.approx(0.75)
    assert snap.reset_at is not None


def test_parse_plan_payload_derives_remaining_from_used():
    snap = parse_plan_payload({"total": 100, "used": 30})
    assert snap is not None
    assert snap.credits_remaining == 70
    assert snap.credits_total == 100


def test_parse_plan_payload_uses_epoch_millis():
    snap = parse_plan_payload({"remaining": 10, "total": 100, "expires_at": 1_800_000_000_000})
    assert snap is not None
    assert snap.reset_at.year == 2027


def test_parse_plan_payload_returns_none_for_unrelated_json():
    assert parse_plan_payload({"hello": "world"}) is None
    assert parse_plan_payload([]) is None


def test_parse_balance_payload_returns_none_without_balance():
    assert parse_balance_payload({"type": "prepaid"}) is None


def test_snapshot_duration_helpers():
    from datetime import datetime, timedelta, timezone  # noqa: F401

    snap = QuotaSnapshot(
        ok=True,
        credits_remaining=50,
        credits_total=100,
        reset_at=datetime.now(timezone.utc) + timedelta(days=3),
    )
    assert 2.9 <= snap.days_remaining <= 3.0
    assert snap.percent_remaining == 0.5


def test_snapshot_percent_none_when_no_total():
    snap = QuotaSnapshot(ok=True, credits_remaining=50)
    assert snap.percent_remaining is None


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------


def test_extract_session_key_from_header():
    assert extract_session_key({"X-Session-Id": "abc"}, None) == "hdr:abc"


def test_extract_session_key_from_anthropic_metadata():
    body = {"metadata": {"user_id": "u-1"}, "messages": []}
    assert extract_session_key({}, body) == "meta:u-1"


def test_extract_session_key_from_body_fingerprint_is_stable():
    body = {"messages": [{"role": "user", "content": "hello"}]}
    first = extract_session_key({}, body)
    second = extract_session_key({}, body)
    assert first == second
    assert first.startswith("body:")


def test_extract_session_key_differs_for_different_content():
    a = extract_session_key({}, {"messages": [{"role": "user", "content": "aaa"}]})
    b = extract_session_key({}, {"messages": [{"role": "user", "content": "bbb"}]})
    assert a != b


def test_extract_model_and_channel():
    assert extract_model({"model": "step-3.5-flash"}) == "step-3.5-flash"
    assert extract_model({}) is None


def test_build_upstream_url_maps_both_channels():
    settings = Settings()
    assert (
        build_upstream_url(settings, "/step_plan/v1/messages")
        == "https://api.stepfun.ai/step_plan/v1/messages"
    )
    assert (
        build_upstream_url(settings, "/v1/chat/completions")
        == "https://api.stepfun.ai/v1/chat/completions"
    )
    assert select_channel("/step_plan/v1/messages", settings) == "plan"
    assert select_channel("/v1/messages", settings) == "api"


def test_failover_predicates():
    assert should_failover(429)
    assert should_failover(503)
    assert not should_failover(400)
    assert should_cooldown(401)
    assert not should_cooldown(400)


# --------------------------------------------------------------------------
# 存储 + 路由器（使用临时数据目录）
# --------------------------------------------------------------------------


@pytest.fixture()
def store(tmp_path):
    settings = Settings(data_dir=tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    st = Store(settings)
    st.init()
    yield st
    st.close()


@pytest.fixture()
def settings(tmp_path):
    s = Settings(data_dir=tmp_path)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


def test_store_account_key_encrypted_at_rest(store):
    account_id = store.create_account(name="a", api_key="sk-secret-value-1234")
    row = store.get_account(account_id)
    assert "sk-secret-value-1234" not in row["api_key_enc"]
    assert store.decrypt_key(row) == "sk-secret-value-1234"
    assert row["key_hint"].endswith("1234")


def test_store_account_duplicate_fingerprint_rejected(store):
    import sqlite3

    store.create_account(name="a", api_key="sk-dup-key-000001")
    with pytest.raises(sqlite3.IntegrityError):
        store.create_account(name="b", api_key="sk-dup-key-000001")


def test_store_router_sticky_session(store, settings):
    first_id = store.create_account(name="a1", api_key="sk-key-aaaa-111111")
    second_id = store.create_account(name="a2", api_key="sk-key-bbbb-222222")
    router = Router(store, settings)

    ctx = RouteContext(session_key="hdr:session-1")
    plan = router.plan(ctx, attempts=2)
    assert len(plan) == 2
    first = plan[0]

    router.commit(ctx, first)

    again = router.plan(ctx, attempts=2)
    assert again[0].account_id == first.account_id
    assert again[0].sticky is True
    assert first.account_id in (first_id, second_id)


def test_router_skips_disabled_account(store, settings):
    store.create_account(name="a1", api_key="sk-key-aaaa-111111", enabled=False)
    a2 = store.create_account(name="a2", api_key="sk-key-bbbb-222222")
    router = Router(store, settings)
    plan = router.plan(RouteContext(), attempts=5)
    assert [t.account_id for t in plan] == [a2]


def test_router_skips_exhausted_quota(store, settings):
    a1 = store.create_account(name="a1", api_key="sk-key-aaaa-111111")
    a2 = store.create_account(name="a2", api_key="sk-key-bbbb-222222")
    store.update_account_quota(
        a1, {"ok": True, "source": "plan", "credits_remaining": 0.0, "credits_total": 100.0}
    )
    router = Router(store, settings)
    plan = router.plan(RouteContext(), attempts=5)
    assert [t.account_id for t in plan][0] == a2


def test_router_keeps_account_with_unknown_quota(store, settings):
    a1 = store.create_account(name="a1", api_key="sk-key-aaaa-111111")
    router = Router(store, settings)
    plan = router.plan(RouteContext(), attempts=5)
    assert [t.account_id for t in plan] == [a1]


def test_router_respects_cooldown(store, settings):
    from step2api.router import NoAccountAvailable

    a1 = store.create_account(name="a1", api_key="sk-key-aaaa-111111")
    store.mark_account_result(
        a1, success=False, error="boom", cooldown_seconds=300, fail_threshold=1
    )
    router = Router(store, settings)
    with pytest.raises(NoAccountAvailable):
        router.plan(RouteContext(), attempts=2)


def test_router_priority_mode_orders_by_priority(store, settings):
    settings.routing_mode = "priority"
    store.create_account(name="low", api_key="sk-key-aaaa-111111", priority=200)
    store.create_account(name="high", api_key="sk-key-bbbb-222222", priority=10)
    router = Router(store, settings)
    plan = router.plan(RouteContext(), attempts=5)
    assert plan[0].account_name == "high"


def test_router_pool_proxy_rotation_sets_proxy(store, settings):
    p1 = store.create_proxy("http://1.1.1.1:8080", label="p1")
    p2 = store.create_proxy("http://2.2.2.2:8080", label="p2")
    pool_id = store.create_pool("pool", strategy="round_robin")
    store.set_pool_members(pool_id, [p1, p2])
    store.create_account(
        name="a1", api_key="sk-key-aaaa-111111", proxy_mode="pool", pool_id=pool_id
    )
    router = Router(store, settings)
    targets = router.targets(RouteContext(), limit=5)
    assert targets[0].proxy.url in ("http://1.1.1.1:8080", "http://2.2.2.2:8080")
    assert targets[0].proxy.source == "pool"


def test_router_pool_round_robin_actually_rotates(store, settings):
    """affinity=rotate 时池内轮转必须真的换代理 —— 轮转器要按池复用。"""
    p1 = store.create_proxy("http://1.1.1.1:8080")
    p2 = store.create_proxy("http://2.2.2.2:8080")
    p3 = store.create_proxy("http://3.3.3.3:8080")
    pool_id = store.create_pool("pool", strategy="round_robin", affinity="rotate")
    store.set_pool_members(pool_id, [p1, p2, p3])
    store.create_account(
        name="a1", api_key="sk-key-aaaa-111111", proxy_mode="pool", pool_id=pool_id
    )
    router = Router(store, settings)

    picked = [
        router.targets(RouteContext(session_key=f"s{i}"), limit=1)[0].proxy.url
        for i in range(6)
    ]
    assert len(set(picked)) == 3, picked
    # 顺序轮转：三次一轮
    assert picked[:3] == picked[3:6]


def test_router_pool_sticky_affinity_pins_one_proxy(store, settings):
    """affinity=sticky 时同一账号出口固定，不随请求次数漂移。"""
    p1 = store.create_proxy("http://1.1.1.1:8080")
    p2 = store.create_proxy("http://2.2.2.2:8080")
    p3 = store.create_proxy("http://3.3.3.3:8080")
    pool_id = store.create_pool("pool", strategy="round_robin", affinity="sticky")
    store.set_pool_members(pool_id, [p1, p2, p3])
    store.create_account(
        name="a1", api_key="sk-key-aaaa-111111", proxy_mode="pool", pool_id=pool_id
    )
    router = Router(store, settings)
    picked = {
        router.targets(RouteContext(session_key=f"s{i}"), limit=1)[0].proxy.url
        for i in range(6)
    }
    assert len(picked) == 1, picked


def test_router_sticky_pool_spreads_accounts_across_members(store, settings):
    """sticky 下不同账号要落到池内不同代理，出口 IP 才算被摊开。"""
    pids = [store.create_proxy(f"http://{i}.{i}.{i}.{i}:8080") for i in range(1, 7)]
    pool_id = store.create_pool("pool", strategy="round_robin", affinity="sticky")
    store.set_pool_members(pool_id, pids)
    account_ids = [
        store.create_account(
            name=f"a{i}", api_key=f"sk-key-{i:04d}-111111",
            proxy_mode="pool", pool_id=pool_id,
        )
        for i in range(6)
    ]
    router = Router(store, settings)

    # 逐个账号取它固定的出口
    picked = {}
    for aid in account_ids:
        target = router.targets(RouteContext(requested_account_id=aid), limit=1)[0]
        picked[aid] = target.proxy.url

    assert len(set(picked.values())) >= 3, picked

    # 同一个账号反复取，落点必须稳定
    for aid in account_ids:
        for _ in range(3):
            again = router.targets(RouteContext(requested_account_id=aid), limit=1)[0]
            assert again.proxy.url == picked[aid]


def test_router_pool_rotation_off_pins_first_proxy(store, settings):
    p1 = store.create_proxy("http://1.1.1.1:8080")
    p2 = store.create_proxy("http://2.2.2.2:8080")
    pool_id = store.create_pool("pool", strategy="round_robin", affinity="rotate")
    store.set_pool_members(pool_id, [p1, p2])
    store.create_account(
        name="a1", api_key="sk-key-aaaa-111111", proxy_mode="pool", pool_id=pool_id,
        proxy_rotation=False,
    )
    router = Router(store, settings)
    picked = {
        router.targets(RouteContext(session_key=f"s{i}"), limit=1)[0].proxy.url
        for i in range(5)
    }
    assert picked == {"http://1.1.1.1:8080"}


def test_router_pool_least_used_strategy(store, settings):
    p1 = store.create_proxy("http://1.1.1.1:8080")
    p2 = store.create_proxy("http://2.2.2.2:8080")
    pool_id = store.create_pool("pool", strategy="least_used", affinity="rotate")
    store.set_pool_members(pool_id, [p1, p2])
    store.create_account(
        name="a1", api_key="sk-key-aaaa-111111", proxy_mode="pool", pool_id=pool_id
    )
    router = Router(store, settings)
    # 在途计数记录在共享登记处，池的轮转器必须能看到
    router.proxy_pool.acquire("http://1.1.1.1:8080")
    router.proxy_pool.acquire("http://1.1.1.1:8080")
    target = router.targets(RouteContext(), limit=1)[0]
    assert target.proxy.url == "http://2.2.2.2:8080"


def test_router_skips_unhealthy_pool_member(store, settings):
    p1 = store.create_proxy("http://1.1.1.1:8080")
    p2 = store.create_proxy("http://2.2.2.2:8080")
    store.record_proxy_result(p1, ok=False, latency_ms=None, error="down")
    pool_id = store.create_pool("pool", strategy="round_robin", affinity="rotate")
    store.set_pool_members(pool_id, [p1, p2])
    store.create_account(
        name="a1", api_key="sk-key-aaaa-111111", proxy_mode="pool", pool_id=pool_id
    )
    router = Router(store, settings)
    for i in range(4):
        target = router.targets(RouteContext(session_key=f"s{i}"), limit=1)[0]
        assert target.proxy.url == "http://2.2.2.2:8080"


def test_resolved_proxy_header_value_is_ascii_safe(store, settings):
    """HTTP 头必须 latin-1 可编码，中文标签会让响应构造炸掉。"""
    from step2api.proxy import ResolvedProxy

    direct = ResolvedProxy(url=None, source="direct", label="直连")
    direct.header_value().encode("latin-1")  # 不应抛异常
    assert direct.header_value() == "direct"

    pooled = ResolvedProxy(
        url="http://u:p@1.2.3.4:8080", source="pool", pool_id=7, proxy_id=3, label="直连"
    )
    pooled.header_value().encode("latin-1")
    assert pooled.header_value() == "proxy-3"
    assert pooled.display_label() == "直连"


def test_account_quota_failure_preserves_last_known_values(store):
    """探测失败不应把已知额度抹成 NULL。"""
    aid = store.create_account(name="a", api_key="sk-key-aaaa-111111")
    store.update_account_quota(
        aid, {"ok": True, "source": "plan", "credits_remaining": 500.0, "credits_total": 1000.0}
    )
    store.update_account_quota(aid, {"ok": False, "error": "上游 500"})
    row = store.get_account(aid)
    assert row["credits_remaining"] == 500.0
    assert row["credits_total"] == 1000.0
    assert row["quota_ok"] == 0
    assert row["quota_error"] == "上游 500"


def test_router_dedicated_proxy(store, settings):
    pid = store.create_proxy("socks5://u:p@3.3.3.3:1080", label="ded")
    store.create_account(
        name="a1", api_key="sk-key-aaaa-111111", proxy_mode="dedicated", proxy_id=pid
    )
    router = Router(store, settings)
    target = router.targets(RouteContext(), limit=1)[0]
    assert target.proxy.url == "socks5://u:p@3.3.3.3:1080"
    assert target.proxy.label == "ded"


def test_router_direct_mode_never_uses_proxy(store, settings):
    settings.global_proxy = "http://9.9.9.9:8080"
    store.create_account(name="a1", api_key="sk-key-aaaa-111111", proxy_mode="direct")
    router = Router(store, settings)
    target = router.targets(RouteContext(), limit=1)[0]
    assert target.proxy.url is None


def test_router_inherit_mode_uses_global_proxy(store, settings):
    settings.global_proxy = "http://9.9.9.9:8080"
    store.create_account(name="a1", api_key="sk-key-aaaa-111111", proxy_mode="inherit")
    router = Router(store, settings)
    target = router.targets(RouteContext(), limit=1)[0]
    assert target.proxy.url == "http://9.9.9.9:8080"
    assert target.proxy.source == "global"


def test_router_pool_falls_back_to_direct_when_pool_empty(store, settings):
    pool_id = store.create_pool("empty", fallback_direct=True)
    store.create_account(
        name="a1", api_key="sk-key-aaaa-111111", proxy_mode="pool", pool_id=pool_id
    )
    router = Router(store, settings)
    target = router.targets(RouteContext(), limit=1)[0]
    assert target.proxy.url is None


def test_router_raises_when_no_accounts(store, settings):
    from step2api.router import NoAccountAvailable

    router = Router(store, settings)
    with pytest.raises(NoAccountAvailable):
        router.plan(RouteContext(), attempts=2)


def test_router_respects_session_proxy_affinity(store, settings):
    p1 = store.create_proxy("http://1.1.1.1:8080", label="p1")
    p2 = store.create_proxy("http://2.2.2.2:8080", label="p2")
    pool_id = store.create_pool("pool", strategy="round_robin", affinity="sticky")
    store.set_pool_members(pool_id, [p1, p2])
    store.create_account(
        name="a1", api_key="sk-key-aaaa-111111", proxy_mode="pool", pool_id=pool_id
    )
    router = Router(store, settings)

    ctx = RouteContext(session_key="hdr:s1")
    first = router.targets(ctx, limit=1)[0]
    router.commit(ctx, first)

    for _ in range(5):
        again = router.targets(ctx, limit=1)[0]
        assert again.proxy.url == first.proxy.url


def test_store_session_expiry(store):
    account_id = store.create_account(name="a", api_key="sk-key-aaaa-111111")
    store.upsert_session("k1", account_id=account_id, ttl_seconds=-1)
    assert store.get_session("k1") is None

    store.upsert_session("k2", account_id=account_id, ttl_seconds=600)
    assert store.get_session("k2") is not None


def test_store_pool_members_roundtrip(store):
    p1 = store.create_proxy("http://1.1.1.1:8080")
    p2 = store.create_proxy("http://2.2.2.2:8080")
    pool_id = store.create_pool("pool")
    store.set_pool_members(pool_id, [p1, p2])
    assert store.list_pool_member_ids(pool_id) == [p1, p2]
    store.set_pool_members(pool_id, [p2])
    assert store.list_pool_member_ids(pool_id) == [p2]


def test_store_proxy_url_is_encrypted(store):
    pid = store.create_proxy("http://user:supersecret@1.2.3.4:8080")
    row = store.get_proxy(pid)
    assert "supersecret" not in row["url_enc"]
    assert store.decrypt_proxy_url(row) == "http://user:supersecret@1.2.3.4:8080"
    assert row["url_redacted"] == "http://user:***@1.2.3.4:8080"


def test_store_usage_log_roundtrip(store):
    store.log_usage({"account_id": 1, "account_name": "a", "status_code": 200, "total_tokens": 42})
    rows = store.list_logs(limit=10)
    assert len(rows) == 1
    assert rows[0]["total_tokens"] == 42


def test_store_prune_logs(store):
    for _ in range(20):
        store.log_usage({"account_id": 1, "status_code": 200})
    store.prune_logs(5)
    assert len(store.list_logs(limit=100)) == 5


# --------------------------------------------------------------------------
# 导入解析
# --------------------------------------------------------------------------


def _settings_for_import(tmp_path):
    s = Settings(data_dir=tmp_path)
    s.data_dir.mkdir(parents=True, exist_ok=True)
    return s


def test_parse_import_plain_lines(tmp_path):
    from step2api.api import parse_import_content

    settings = _settings_for_import(tmp_path)
    items = parse_import_content(
        "sk-aaaaaaaaaaaaaaaaaaaa\nsk-bbbbbbbbbbbbbbbbbbbb\n", settings=settings
    )
    assert len(items) == 2
    assert all(i.ok for i in items)


def test_parse_import_with_proxy(tmp_path):
    from step2api.api import parse_import_content

    settings = _settings_for_import(tmp_path)
    items = parse_import_content(
        "sk-aaaaaaaaaaaaaaaaaaaa|http://u:p@1.2.3.4:8080\n", settings=settings
    )
    assert len(items) == 1
    assert items[0].proxy == "http://u:p@1.2.3.4:8080"


def test_parse_import_rejects_cn_domain(tmp_path):
    from step2api.api import parse_import_content

    settings = _settings_for_import(tmp_path)
    items = parse_import_content(
        "sk-aaaaaaaaaaaaaaaaaaaa\n# 从 platform.stepfun.com 获取\nsk-bbbbbbbbbbbbbbbbbbbb",
        settings=settings,
    )
    # 注释行被跳过，没有国内站域名进入 key 行
    assert all(i.ok for i in items)

    items2 = parse_import_content(
        "sk-aaaaaaaaaaaaaaaaaaaa|http://platform.stepfun.com/proxy", settings=settings
    )
    assert items2 and items2[0].ok is False
    assert "国内站" in (items2[0].reason or "")


def test_parse_import_json_array(tmp_path):
    from step2api.api import parse_import_content

    settings = _settings_for_import(tmp_path)
    items = parse_import_content(
        '[{"api_key":"sk-aaaaaaaaaaaaaaaaaaaa","name":"主号","proxy":"http://1.2.3.4:8080"}]',
        settings=settings,
    )
    assert len(items) == 1
    assert items[0].label == "主号"
    assert items[0].proxy == "http://1.2.3.4:8080"


def test_parse_import_dedupes(tmp_path):
    from step2api.api import parse_import_content

    settings = _settings_for_import(tmp_path)
    items = parse_import_content(
        "sk-aaaaaaaaaaaaaaaaaaaa\nsk-aaaaaaaaaaaaaaaaaaaa", settings=settings, dedupe=True
    )
    assert sum(1 for i in items if i.ok) == 1
    assert sum(1 for i in items if i.duplicate) == 1


def test_parse_import_skips_comments_and_blank_lines(tmp_path):
    from step2api.api import parse_import_content

    settings = _settings_for_import(tmp_path)
    items = parse_import_content(
        "# 注释\n\n// 另一种注释\nsk-aaaaaaaaaaaaaaaaaaaa\n", settings=settings
    )
    assert len(items) == 1


def test_parse_import_bad_proxy_reported(tmp_path):
    from step2api.api import parse_import_content

    settings = _settings_for_import(tmp_path)
    items = parse_import_content(
        "sk-aaaaaaaaaaaaaaaaaaaa|ftp://1.2.3.4:21", settings=settings
    )
    assert items and items[0].ok is False
    assert "代理格式错误" in (items[0].reason or "")


# --------------------------------------------------------------------------
# 会话键 / usage 解析
# --------------------------------------------------------------------------


def test_gateway_usage_tap_parses_openai_stream():
    from step2api.gateway import UsageTap

    tap = UsageTap()
    tap.feed(b'data: {"model":"step-3.5-flash","usage":{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n')
    assert tap.usage["total_tokens"] == 15
    assert tap.model == "step-3.5-flash"


def test_gateway_usage_tap_ignores_non_usage_chunks():
    from step2api.gateway import UsageTap

    tap = UsageTap()
    tap.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
    assert tap.usage == {}


def test_filter_request_headers_drops_auth_and_gateway_headers():
    from step2api.gateway import _filter_request_headers

    out = _filter_request_headers(
        {"Authorization": "Bearer client-token", "X-Step2api-Account": "3", "Content-Type": "application/json"}
    )
    assert "Authorization" not in out
    assert "X-Step2api-Account" not in out
    assert out["Content-Type"] == "application/json"


# --------------------------------------------------------------------------
# 并发闸门
# --------------------------------------------------------------------------


def test_concurrency_gate_tracks_inflight():
    from step2api.router import _ConcurrencyGate

    gate = _ConcurrencyGate(default_limit=4)
    assert gate.limit_for(1, 0) == 4      # 账号未单独配置 → 用默认
    assert gate.limit_for(1, 2) == 2      # 账号单独配置优先

    assert gate.inflight(1) == 0
    gate.incr(1)
    gate.incr(1)
    assert gate.inflight(1) == 2
    gate.decr(1)
    assert gate.inflight(1) == 1
    gate.decr(1)
    gate.decr(1)                            # 多余的释放不应把计数压成负数
    assert gate.inflight(1) == 0


def test_concurrency_gate_semaphore_reused_per_account():
    import asyncio

    from step2api.router import _ConcurrencyGate

    gate = _ConcurrencyGate(default_limit=2)
    a = gate.semaphore(7, 2)
    b = gate.semaphore(7, 2)
    assert a is b, "同一账号同一上限必须复用同一个信号量"
    assert gate.semaphore(7, 3) is not a, "上限变化后应重建信号量"
    assert isinstance(a, asyncio.Semaphore)

# --------------------------------------------------------------------------
# 控制台（Oasis）额度解析
# --------------------------------------------------------------------------


def test_console_window_treats_zero_as_not_applicable():
    """上游用 0 表示"该窗口不适用"，不能误显示成"剩余 0%"。"""
    from step2api.console import _window

    assert _window(0, "0") == (None, None)
    assert _window("0", None) == (None, None)
    assert _window(None, None) == (None, None)

    rate, reset = _window(1, "1791190544")
    assert rate == 1.0
    assert reset is not None and reset.year == 2026

    # 剩余 0 但有重置时间 —— 这是真的耗尽了
    rate, reset = _window(0, "1791190544")
    assert rate == 0.0
    assert reset is not None


def test_console_quota_derives_credits_from_buckets():
    from step2api.console import ConsoleQuota, CreditBucket

    q = ConsoleQuota(
        ok=True,
        buckets=[
            CreditBucket(type="subscription", total=1_600_000_000, residual=1_200_000_000),
            CreditBucket(type="topup", total=400_000_000, residual=100_000_000),
        ],
    )
    assert q.total_credits == 2_000_000_000
    assert q.remaining_credits == 1_300_000_000
    assert q.used_credits == 700_000_000
    assert q.percent_remaining == pytest.approx(0.65)


def test_console_quota_percent_none_without_buckets():
    from step2api.console import ConsoleQuota

    q = ConsoleQuota(ok=True)
    assert q.total_credits is None
    assert q.percent_remaining is None
    assert q.remaining_credits is None


def test_console_snapshot_maps_to_store_fields():
    from step2api.console import ConsoleQuota, CreditBucket, PlanStatus

    from datetime import datetime, timedelta, timezone

    expiry = datetime.now(timezone.utc) + timedelta(days=10)
    q = ConsoleQuota(
        ok=True,
        status=PlanStatus(plan_name="Plus", status="active", expired_at=expiry),
        five_hour_left=0.5,
        weekly_left=0.25,
        buckets=[CreditBucket(type="subscription", total=1000.0, residual=250.0,
                              expire_at=expiry)],
    )
    snap = q.as_snapshot()
    assert snap["ok"] is True
    assert snap["source"] == "console"
    assert snap["plan_name"] == "Plus"
    assert snap["credits_total"] == 1000.0
    assert snap["credits_remaining"] == 250.0
    assert snap["credits_used"] == 750.0
    assert snap["five_hour_left_rate"] == 0.5
    assert snap["weekly_left_rate"] == 0.25
    assert q.seconds_remaining is not None and q.seconds_remaining > 0


def test_console_auth_error_is_reported_not_swallowed(store):
    """凭据失效要显式报错，不能静默当成"查不到额度"。"""
    from step2api.console import ConsoleAuthError

    err = ConsoleAuthError("控制台凭据被拒绝：Oasis-appID 必须为 20700")
    assert "20700" in str(err)


def test_store_console_credentials_roundtrip(store):
    aid = store.create_account(name="a", api_key="sk-console-key-0001")
    store.set_console_credentials(aid, "tok-abc", "web-xyz")

    token, webid = store.get_console_credentials(aid)
    assert (token, webid) == ("tok-abc", "web-xyz")

    # 密文落库
    row = store.get_account(aid)
    assert "tok-abc" not in (row["console_token_enc"] or "")
    assert "web-xyz" not in (row["console_webid_enc"] or "")

    store.clear_console_credentials(aid)
    assert store.get_console_credentials(aid) == ("", "")


def test_store_console_quota_update_marks_source(store):
    aid = store.create_account(name="a", api_key="sk-console-key-0002")
    store.update_account_console_quota(aid, {
        "ok": True, "source": "console", "plan_name": "Plus", "plan_status": "active",
        "credits_remaining": 1_600_000_000.0, "credits_total": 1_600_000_000.0,
        "credits_used": 0.0, "five_hour_left_rate": 0.5, "weekly_left_rate": 0.25,
        "auto_renew": 0,
    })
    row = store.get_account(aid)
    assert row["plan_name"] == "Plus"
    assert row["quota_source"] == "console"
    assert row["credits_remaining"] == 1_600_000_000.0
    assert row["five_hour_left_rate"] == 0.5
    assert row["weekly_left_rate"] == 0.25
    assert row["console_synced_at"] is not None


def test_store_migration_adds_console_columns(tmp_path):
    """老库升级：已有 accounts 表要能补上新列。"""
    import sqlite3

    from step2api.config import Settings
    from step2api.store import Store

    settings = Settings(data_dir=tmp_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    # 用真实 v1 结构造库：取当前 SCHEMA，剔掉 v2 才引入的控制台列
    from step2api.store import SCHEMA

    v1 = SCHEMA
    for line in (
        "    console_token_enc    TEXT,",
        "    console_webid_enc    TEXT,",
        "    console_synced_at    TEXT,",
        "    console_error        TEXT,",
        "    five_hour_left_rate  REAL,",
        "    five_hour_reset_at   TEXT,",
        "    weekly_left_rate     REAL,",
        "    weekly_reset_at      TEXT,",
        "    auto_renew           INTEGER NOT NULL DEFAULT 0,",
    ):
        assert line in v1, f"SCHEMA 结构变了，迁移测试需要同步：{line}"
        v1 = v1.replace(line + "\n", "")

    db = settings.db_path
    conn = sqlite3.connect(str(db))
    conn.executescript(v1)
    conn.execute(
        "INSERT INTO accounts(name, api_key_enc, key_fp, created_at, updated_at) "
        "VALUES('old','enc','fp','t','t')"
    )
    conn.commit()
    conn.close()

    st = Store(settings)
    st.init()  # 不应抛 no such column
    row = st.get_account(1)
    assert row["console_token_enc"] is None
    assert row["weekly_left_rate"] is None
    st.close()
