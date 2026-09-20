/* step2api 控制台 —— 原生单文件前端，无框架无构建步骤 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  accounts: [], proxies: [], pools: [], settings: null, portal: "",
  token: localStorage.getItem("step2api_admin_token") || "",
  view: "overview",
  logChan: "",
  logs: [],
};

const TITLES = {
  overview: "概览", accounts: "账号池", proxies: "代理", pools: "代理池",
  sessions: "粘性会话", logs: "请求日志", config: "配置",
};

/* ------------------------------------------------------------------ */
/* 主题                                                                */
/* ------------------------------------------------------------------ */

const THEME_KEY = "step2api.theme";

function effTheme() {
  const t = localStorage.getItem(THEME_KEY) || "auto";
  if (t !== "auto") return t;
  return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}

function applyTheme() {
  document.documentElement.dataset.theme = effTheme();
}

if ($("#btnTheme")) {
  $("#btnTheme").addEventListener("click", () => {
    const order = ["auto", "dark", "light"];
    const cur = localStorage.getItem(THEME_KEY) || "auto";
    const next = order[(order.indexOf(cur) + 1) % order.length];
    localStorage.setItem(THEME_KEY, next);
    applyTheme();
    toast("主题：" + ({ auto: "跟随系统", dark: "暗色", light: "亮色" }[next]), "info");
  });
}

matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
  if ((localStorage.getItem(THEME_KEY) || "auto") === "auto") applyTheme();
});

/* ------------------------------------------------------------------ */
/* HTTP                                                                */
/* ------------------------------------------------------------------ */

async function api(path, options = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, options.headers || {});
  if (state.token) headers["X-Admin-Token"] = state.token;

  const resp = await fetch("/api" + path, Object.assign({}, options, { headers }));
  const text = await resp.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = { raw: text }; }

  if (!resp.ok) {
    const detail = (data && (data.detail || (data.error && data.error.message))) || resp.statusText;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return data;
}

/* ------------------------------------------------------------------ */
/* 工具                                                                */
/* ------------------------------------------------------------------ */

function toast(message, kind = "info", ms = 3600) {
  const el = document.createElement("div");
  el.className = "tst " + kind;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.style.opacity = "0";
    el.style.transition = "opacity .25s";
    setTimeout(() => el.remove(), 260);
  }, ms);
}

function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function fmtNumber(value, digits = 2) {
  if (value === null || value === undefined || value === "") return "—";
  const n = Number(value);
  if (!isFinite(n)) return "—";
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(2) + "M";
  if (Math.abs(n) >= 1e4) return (n / 1e3).toFixed(1) + "K";
  return n.toFixed(digits).replace(/\.?0+$/, "") || "0";
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const s = Number(seconds);
  if (!isFinite(s) || s <= 0) return s <= 0 ? "已过期" : "—";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}天${h}时`;
  if (h > 0) return `${h}时${m}分`;
  if (m > 0) return `${m}分${Math.floor(s % 60)}秒`;
  return `${Math.floor(s)}秒`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

function barClass(percent) {
  if (percent === null || percent === undefined) return "";
  if (percent <= 0.1) return "bad";
  if (percent <= 0.3) return "warn";
  return "ok";
}

/* 状态徽标带具体原因与倒计时，比单纯颜色信息量大 */
function accountTag(a) {
  if (!a.enabled) return `<span class="tag mute">已禁用</span>`;
  const st = a.status || "unknown";
  if (st === "cooldown") {
    const left = a.cooldown_until ? (new Date(a.cooldown_until) - Date.now()) / 1000 : null;
    return `<span class="tag bad">冷却${left !== null ? " · " + fmtDuration(left) : ""}</span>`;
  }
  if (st === "degraded") return `<span class="tag warn">降级 · 连败${a.fail_streak || 0}</span>`;
  if (st === "healthy") return `<span class="tag ok">健康</span>`;
  return `<span class="tag mute">未知</span>`;
}

function rowClass(a) {
  if (!a.enabled) return "off";
  if (a.status === "cooldown" || a.status === "degraded") return "cool";
  if (a.status === "unknown") return "idle";
  return "";
}

/* ------------------------------------------------------------------ */
/* 路由：hash + hidden 切换；切到哪个视图才拉哪个视图的数据            */
/* ------------------------------------------------------------------ */

function go(view) {
  if (!TITLES[view]) view = "overview";
  state.view = view;
  $$(".view").forEach((s) => { s.hidden = s.id !== "view-" + view; });
  $$(".navlist a").forEach((a) => a.classList.toggle("on", a.dataset.view === view));
  const t = $("#pageTitle");
  if (t) t.textContent = TITLES[view];
  const sub = $("#pageSub");
  if (sub) sub.textContent = "";

  if (view === "accounts") loadAccounts();
  else if (view === "proxies") loadProxies();
  else if (view === "pools") loadPools();
  else if (view === "sessions") loadSessions();
  else if (view === "logs") loadLogs();
  else if (view === "config") loadSettings();
  else loadStats();
}

$$(".navlist a").forEach((a) => {
  a.addEventListener("click", (e) => {
    e.preventDefault();
    history.replaceState(null, "", "#" + a.dataset.view);
    go(a.dataset.view);
  });
});

/* ------------------------------------------------------------------ */
/* 弹窗                                                                */
/* ------------------------------------------------------------------ */

function openModal(title, bodyHtml, footHtml = "", wide = false) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = bodyHtml;
  $("#modalFoot").innerHTML = footHtml;
  $("#modal").classList.toggle("wide", !!wide);
  $("#modalBackdrop").classList.add("on");
}

function closeModal() {
  $("#modalBackdrop").classList.remove("on");
  $("#modalBody").innerHTML = "";
  $("#modalFoot").innerHTML = "";
}

$("#modalClose").addEventListener("click", closeModal);
$("#modalBackdrop").addEventListener("click", (e) => {
  if (e.target === $("#modalBackdrop")) closeModal();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

/* ------------------------------------------------------------------ */
/* 概览                                                                */
/* ------------------------------------------------------------------ */

async function loadStats(quiet = false) {
  try {
    const d = await api("/stats");
    const acc = d.accounts || {}, cr = d.credits || {}, mo = d.money || {};
    const boxes = [
      { k: "账号总数", v: acc.total, s: `${acc.enabled ?? 0} 个已启用`, c: "" },
      { k: "健康", v: acc.healthy ?? 0, s: `冷却 ${acc.cooldown ?? 0} · 降级 ${acc.degraded ?? 0}`, c: "good" },
      { k: "额度告警", v: acc.low_quota ?? 0, s: `已查询 ${acc.quota_ok ?? 0}`, c: (acc.low_quota ? "warn" : "") },
      { k: "剩余额度", v: fmtNumber(cr.remaining), s: cr.total ? `共 ${fmtNumber(cr.total)} · ${(cr.percent * 100).toFixed(1)}%` : "总量未知", c: "acc" },
      { k: "余额合计", v: fmtNumber(mo.balance, 4), s: mo.currency || "", c: "" },
      { k: "路由模式", v: (d.routing && d.routing.mode) || "—", s: `代理 ${(d.routing && d.routing.proxy_strategy) || "—"}`, c: "" },
    ];
    $("#statBar").innerHTML = boxes.map((b) => `
      <div class="stat ${b.c}">
        <div class="k">${esc(b.k)}</div>
        <div class="v">${esc(b.v)}</div>
        <div class="s">${esc(b.s)}</div>
      </div>`).join("");

    const inflight = Object.entries((d.routing && d.routing.inflight) || {}).filter(([, v]) => v > 0);
    $("#routingState").innerHTML = `
      <dt>路由模式</dt><dd>${esc(d.routing && d.routing.mode)}</dd>
      <dt>粘性保留</dt><dd>${fmtDuration(d.routing && d.routing.affinity_ttl)}</dd>
      <dt>代理轮转</dt><dd>${esc(d.routing && d.routing.proxy_strategy)}</dd>
      <dt>在途请求</dt><dd>${inflight.length ? inflight.map(([k, v]) => `#${esc(k)}=${esc(v)}`).join(" · ") : "无"}</dd>
      <dt>额度刷新</dt><dd>${fmtTime(d.last_refresh_at)}${d.last_refresh_error ? ` <span class="tag bad">${esc(d.last_refresh_error)}</span>` : ""}</dd>`;

    renderOverviewAccounts();

    const usage = d.usage_24h || [];
    $("#overviewUsage").innerHTML = usage.length
      ? usage.map((u) => `
          <div class="list-item">
            <div class="grow">
              <div>${esc(u.account_name || "账号 #" + u.account_id)}</div>
              <div class="muted small">成功 ${u.ok} · 失败 ${u.failed} · 均 ${u.avg_ms ?? "—"} ms</div>
            </div>
            <div class="nowrap num">${u.requests} 次<br><span class="muted small">${fmtNumber(u.tokens, 0)} tok</span></div>
          </div>`).join("")
      : `<div class="empty"><b>暂无请求</b>近 24 小时没有流量经过网关</div>`;

    const hint = $("#quotaHint");
    if (hint) hint.textContent = (acc.quota_ok ?? 0) ? `${acc.quota_ok} 个账号已有额度数据` : "尚未取得额度";
  } catch (err) {
    if (!quiet) toast(err.message, "err");
  }
}

function renderOverviewAccounts() {
  const box = $("#overviewAccounts");
  if (!box) return;
  const rows = state.accounts.filter((a) => a.enabled).slice(0, 30);
  box.innerHTML = rows.length
    ? rows.map((a) => {
        const pct = a.percent_remaining;
        return `
        <div class="list-item">
          <div class="grow">
            <div>${esc(a.name)} <span class="muted small">#${a.id}</span></div>
            <div class="bar ${barClass(pct)}"><i style="width:${pct === null ? 0 : Math.max(2, pct * 100).toFixed(1)}%"></i></div>
            <div class="muted small" style="margin-top:3px">
              ${a.credits_remaining === null ? "额度未知" : fmtNumber(a.credits_remaining) + " 剩余"}
              · ${esc(a.plan_name || "套餐未知")}
            </div>
          </div>
          <div class="nowrap" style="text-align:right">
            ${accountTag(a)}
            <div class="muted small" style="margin-top:4px">${fmtDuration(a.seconds_remaining)}</div>
          </div>
        </div>`;
      }).join("")
    : `<div class="empty"><b>还没有账号</b>去「账号池」页导入</div>`;
}

/* ------------------------------------------------------------------ */
/* 账号池                                                              */
/* ------------------------------------------------------------------ */

async function loadAccounts(quiet = false) {
  try {
    const d = await api("/accounts");
    state.accounts = d.accounts || [];
    const c = $("#cntAccounts");
    if (c) c.textContent = state.accounts.length;
    renderAccounts();
    if (state.view === "overview") renderOverviewAccounts();
  } catch (err) {
    if (!quiet) toast(err.message, "err");
  }
}

function accountMatches(a) {
  const kwEl = $("#filterAccounts"), stEl = $("#filterStatus"), lqEl = $("#onlyLowQuota");
  const kw = (kwEl && kwEl.value || "").trim().toLowerCase();
  const st = stEl && stEl.value;
  if (st && (a.status || "") !== st) return false;
  if (lqEl && lqEl.checked && !a.low_quota) return false;
  if (kw) {
    const hay = [a.name, a.group_name, a.key_hint, String(a.id), a.note].join(" ").toLowerCase();
    if (!hay.includes(kw)) return false;
  }
  return true;
}

function proxyLabel(a) {
  const m = a.proxy_mode || "inherit";
  if (m === "direct") return `<span class="tag mute">直连</span>`;
  if (m === "dedicated") return `<span class="tag info">专属 #${a.proxy_id ?? "?"}</span>`;
  if (m === "pool") return `<span class="tag info">池 #${a.pool_id ?? "?"}</span>`;
  return `<span class="tag mute">全局</span>`;
}

function renderAccounts() {
  const tb = $("#accountsTable tbody");
  if (!tb) return;
  const rows = state.accounts.filter(accountMatches);
  if (!rows.length) {
    tb.innerHTML = `<tr><td colspan="13"><div class="empty"><b>没有匹配的账号</b>调整筛选条件，或导入新账号</div></td></tr>`;
    return;
  }

  tb.innerHTML = rows.map((a) => {
    const pct = a.percent_remaining;
    const quota = a.credits_remaining === null
      ? `<span class="muted">未知</span>`
      : `<span class="num">${fmtNumber(a.credits_remaining)}</span> / ${a.credits_total ? fmtNumber(a.credits_total) : "?"}`;
    const plan = a.plan_name ? `<span class="tag info">${esc(a.plan_name)}</span>` : `<span class="tag mute">未知</span>`;
    const expiry = a.plan_expired ? `<span class="tag bad">已过期</span>` : fmtDuration(a.seconds_remaining);
    const consoleBits = a.console_error
      ? `<div class="muted small truncate" title="${esc(a.console_error)}">控制台：${esc(a.console_error.slice(0, 26))}…</div>`
      : (a.console_configured && a.console_seconds_left !== null && a.console_seconds_left !== undefined
          ? `<div class="muted small">凭据剩 ${fmtDuration(a.console_seconds_left)}</div>`
          : "");

    return `<tr class="${rowClass(a)}">
      <td class="mark"><i></i></td>
      <td class="muted num">${a.id}</td>
      <td>
        <div>${esc(a.name)}</div>
        <div class="muted small">${esc(a.group_name || "default")}${a.priority !== 100 ? ` · P${a.priority}` : ""}${a.weight !== 1 ? ` · W${a.weight}` : ""}</div>
      </td>
      <td class="mono">${esc(a.key_hint)}</td>
      <td>
        ${plan}
        ${a.plan_status ? `<div class="muted small">${esc(a.plan_status)}${a.auto_renew ? " · 自动续费" : ""}</div>` : ""}
        ${a.five_hour_left_rate != null ? `<div class="muted small">5h ${(a.five_hour_left_rate * 100).toFixed(0)}%</div>` : ""}
        ${a.weekly_left_rate != null ? `<div class="muted small">周 ${(a.weekly_left_rate * 100).toFixed(0)}%</div>` : ""}
        ${consoleBits}
      </td>
      <td class="w-quota">
        <div>${quota}${a.low_quota ? ' <span class="tag warn">告警</span>' : ""}</div>
        <div class="bar ${barClass(pct)}"><i style="width:${pct === null ? 0 : Math.max(2, pct * 100).toFixed(1)}%"></i></div>
        <div class="muted small num">${pct === null ? "比例未知" : (pct * 100).toFixed(1) + "%"}</div>
      </td>
      <td class="nowrap">${expiry}${a.quota_reset_at ? `<div class="muted small">重置 ${fmtTime(a.quota_reset_at)}</div>` : ""}</td>
      <td class="nowrap num">
        ${a.balance === null ? `<span class="muted">—</span>`
          : `<span title="现金 ${a.cash_balance ?? "?"} / 赠送 ${a.voucher_balance ?? "?"}">${fmtNumber(a.balance, 4)}</span>`}
        ${a.quota_source === "balance" ? `<div class="muted small">回落余额</div>` : ""}
      </td>
      <td>
        ${accountTag(a)}
        ${a.quota_error ? `<div class="muted small truncate" title="${esc(a.quota_error)}">${esc(a.quota_error)}</div>` : ""}
        ${a.last_error && a.status !== "healthy" ? `<div class="muted small truncate" title="${esc(a.last_error)}">${esc(a.last_error)}</div>` : ""}
      </td>
      <td>${proxyLabel(a)}${a.max_concurrency ? `<div class="muted small">并发 ${a.max_concurrency}</div>` : ""}</td>
      <td class="num muted small">${a.inflight ?? 0}</td>
      <td class="num muted small">${a.total_requests || 0}<br>${a.success_count || 0}/${a.failure_count || 0}</td>
      <td class="c-acts">
        <div class="actions">
          <button class="btn btn-sm" data-act="refresh" data-id="${a.id}" title="重新查询额度">刷新</button>
          <button class="btn btn-sm" data-act="edit" data-id="${a.id}">编辑</button>
          <button class="btn btn-sm" data-act="toggle" data-id="${a.id}">${a.enabled ? "禁用" : "启用"}</button>
          <button class="btn btn-sm" data-act="reset" data-id="${a.id}" title="清除冷却与失败计数">复位</button>
          <button class="btn btn-sm btn-danger" data-act="delete" data-id="${a.id}">删除</button>
        </div>
      </td>
    </tr>`;
  }).join("");
}

$("#accountsTable").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = Number(btn.dataset.id), act = btn.dataset.act;
  const a = state.accounts.find((x) => x.id === id);
  btn.disabled = true;
  try {
    if (act === "refresh") {
      const r = await api(`/accounts/${id}/refresh`, { method: "POST" });
      const ok = r.result && r.result.ok;
      toast(ok ? `#${id} 额度已更新` : `#${id} 刷新失败：${(r.result && r.result.error) || "未知"}`, ok ? "ok" : "err");
      await loadAccounts(true);
    } else if (act === "toggle") {
      await api(`/accounts/${id}/toggle`, { method: "POST" });
      await loadAccounts(true);
    } else if (act === "reset") {
      await api(`/accounts/${id}/reset`, { method: "POST" });
      toast(`#${id} 已复位`, "ok");
      await loadAccounts(true);
    } else if (act === "edit") {
      openAccountModal(a);
    } else if (act === "delete") {
      if (!confirm(`确定删除账号 #${id}「${a.name}」？该操作不可恢复。`)) return;
      await api(`/accounts/${id}`, { method: "DELETE" });
      toast(`已删除 #${id}`, "ok");
      await loadAccounts(true);
    }
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
  }
});

["filterAccounts", "filterStatus", "onlyLowQuota"].forEach((id) => {
  const el = document.getElementById(id);
  if (el) el.addEventListener(el.type === "checkbox" ? "change" : "input", renderAccounts);
});

function proxyOptions(sel) {
  return state.proxies.map((p) => `<option value="${p.id}" ${p.id === sel ? "selected" : ""}>#${p.id} ${esc(p.label)} (${esc(p.status)})</option>`).join("");
}
function poolOptions(sel) {
  return state.pools.map((p) => `<option value="${p.id}" ${p.id === sel ? "selected" : ""}>#${p.id} ${esc(p.name)} · ${p.member_count} 个</option>`).join("");
}

function openAccountModal(account = null) {
  const a = account || {};
  const isEdit = !!account;

  openModal(isEdit ? `编辑账号 #${a.id}` : "手动添加账号", `
    <div class="form">
      <div class="row">
        <label>名称 <input id="mName" value="${esc(a.name || "")}" placeholder="主号"></label>
        <label>分组 <input id="mGroup" value="${esc(a.group_name || "default")}"></label>
      </div>
      <label>${isEdit ? "替换 API Key（留空则不修改）" : "API Key（仅支持国外站 account.stepfun.ai）"}
        <input id="mKey" placeholder="sk-...">
      </label>
      <div class="row">
        <label>权重 <input type="number" id="mWeight" min="1" max="100" value="${a.weight ?? 1}"></label>
        <label>优先级（越小越先） <input type="number" id="mPriority" min="0" value="${a.priority ?? 100}"></label>
        <label>并发上限（0=默认） <input type="number" id="mConcurrency" min="0" value="${a.max_concurrency ?? 0}"></label>
      </div>
      <label>代理模式
        <select id="mProxyMode">
          <option value="inherit" ${(a.proxy_mode || "inherit") === "inherit" ? "selected" : ""}>inherit · 使用全局代理</option>
          <option value="direct" ${a.proxy_mode === "direct" ? "selected" : ""}>direct · 直连</option>
          <option value="dedicated" ${a.proxy_mode === "dedicated" ? "selected" : ""}>dedicated · 绑定单个专属代理</option>
          <option value="pool" ${a.proxy_mode === "pool" ? "selected" : ""}>pool · 绑定代理池并轮转</option>
        </select>
      </label>
      <div class="row">
        <label id="wrapDedicated">专属代理 <select id="mProxyId"><option value="">—</option>${proxyOptions(a.proxy_id)}</select></label>
        <label id="wrapPool">代理池 <select id="mPoolId"><option value="">—</option>${poolOptions(a.pool_id)}</select></label>
      </div>
      <label class="inline"><input type="checkbox" id="mRotation" ${a.proxy_rotation !== false ? "checked" : ""}> 池内允许轮转</label>
      <div class="row">
        <label>控制台 Oasis-Token（查真实套餐额度）
          <input id="mConsoleToken" placeholder="${a.console_configured ? "已配置，留空不修改" : "Cookie Oasis-Token"}">
        </label>
        <label>控制台 web_id（localStorage，非 Cookie）
          <input id="mConsoleWebid" placeholder="${a.console_configured ? "已配置，留空不修改" : "localStorage.web_id"}">
        </label>
      </div>
      <div class="row">
        <label>Step Plan 基址（留空用默认） <input id="mPlanBase" value="${esc(a.plan_base || "")}"></label>
        <label>按量计费基址（留空用默认） <input id="mBalanceBase" value="${esc(a.balance_base || "")}"></label>
      </div>
      <label>备注 <input id="mNote" value="${esc(a.note || "")}"></label>
      ${isEdit ? "" : '<label class="inline"><input type="checkbox" id="mVerify" checked> 保存后立即查询额度</label>'}
    </div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn btn-primary" id="mSave">${isEdit ? "保存" : "添加"}</button>`, true);

  const syncMode = () => {
    const m = $("#mProxyMode").value;
    $("#wrapDedicated").style.display = m === "dedicated" ? "" : "none";
    $("#wrapPool").style.display = m === "pool" ? "" : "none";
  };
  $("#mProxyMode").addEventListener("change", syncMode);
  syncMode();

  $("#mSave").addEventListener("click", async () => {
    const b = $("#mSave");
    b.disabled = true;
    try {
      const payload = {
        name: $("#mName").value.trim() || "account",
        group_name: $("#mGroup").value.trim() || "default",
        weight: Number($("#mWeight").value) || 1,
        priority: Number($("#mPriority").value) || 0,
        max_concurrency: Number($("#mConcurrency").value) || 0,
        proxy_mode: $("#mProxyMode").value,
        proxy_rotation: $("#mRotation").checked,
        note: $("#mNote").value,
        plan_base: $("#mPlanBase").value.trim(),
        balance_base: $("#mBalanceBase").value.trim(),
        proxy_id: $("#mProxyId").value ? Number($("#mProxyId").value) : null,
        pool_id: $("#mPoolId").value ? Number($("#mPoolId").value) : null,
      };
      const key = $("#mKey").value.trim();
      const cTok = $("#mConsoleToken").value.trim();
      const cWid = $("#mConsoleWebid").value.trim();

      if (isEdit) {
        if (key) payload.api_key = key;
        await api(`/accounts/${account.id}`, { method: "PATCH", body: JSON.stringify(payload) });
        if (cTok || cWid) {
          const r = await api(`/accounts/${account.id}/console`, {
            method: "POST", body: JSON.stringify({ token: cTok, webid: cWid, verify: true }),
          });
          const acc = r.account || {};
          toast(acc.plan_name ? `控制台额度已同步：${acc.plan_name}` : "控制台凭据已保存",
                acc.plan_name ? "ok" : "warn");
        } else {
          toast("已保存", "ok");
        }
      } else {
        if (!key) { toast("请填写 API Key", "err"); return; }
        payload.api_key = key;
        payload.verify = $("#mVerify").checked;
        if (cTok && cWid) { payload.console_token = cTok; payload.console_webid = cWid; }
        const r = await api("/accounts", { method: "POST", body: JSON.stringify(payload) });
        const v = r.verified;
        toast(v && v.ok ? "账号已添加并查询成功" : `账号已添加（额度：${v ? (v.error || "未知") : "跳过"}）`, v && v.ok ? "ok" : "warn");
      }
      closeModal();
      await loadAccounts(true);
      await loadStats(true);
    } catch (err) {
      toast(err.message, "err");
    } finally {
      b.disabled = false;
    }
  });
}

$("#btnAddAccount").addEventListener("click", () => openAccountModal(null));

/* ---------------------------- 粘贴导入 ---------------------------- */

$("#btnImport").addEventListener("click", () => {
  openModal("粘贴导入（仅国外站）", `
    <div class="form">
      <p class="hint">
        每行一个 Key，或 <code>Key|代理URL</code>；也可粘贴 JSON 数组。
        只接受国外站 <a href="${esc(state.portal)}" target="_blank" rel="noreferrer">${esc(state.portal)}</a>
        签发的 Key，含 stepfun.com 的内容会被拒绝。
      </p>
      <label>账号内容
        <textarea id="impContent" rows="8" placeholder="sk-xxxxxxxx
sk-yyyyyyyy|http://user:pass@host:1080
# 井号开头为注释"></textarea>
      </label>
      <div class="row">
        <label>分组 <input id="impGroup" value="default"></label>
        <label>命名前缀 <input id="impPrefix" placeholder="acct-"></label>
        <label>权重 <input type="number" id="impWeight" value="1" min="1"></label>
        <label>优先级 <input type="number" id="impPriority" value="100" min="0"></label>
      </div>
      <div class="row">
        <label>代理分配
          <select id="impProxyAssign">
            <option value="none">不分配（继承全局）</option>
            <option value="dedicated">每行 Key 绑定自己的代理</option>
            <option value="pool">全部放进一个代理池轮转</option>
          </select>
        </label>
        <label>池策略
          <select id="impPoolStrategy">
            <option value="round_robin">round_robin</option>
            <option value="random">random</option>
            <option value="least_used">least_used</option>
            <option value="lowest_latency">lowest_latency</option>
          </select>
        </label>
      </div>
      <label>池名称 <input id="impPoolName" placeholder="留空自动生成"></label>
      <label>控制台凭据（可选，用于查真实套餐额度）
        <textarea id="impConsole" rows="2" placeholder="Oasis-Token=xxx
web_id=yyy
（或直接两行：token 一行、web_id 一行）"></textarea>
      </label>
      <label class="inline"><input type="checkbox" id="impVerify" checked> 导入后立即查询额度</label>
      <label class="inline"><input type="checkbox" id="impDedupe" checked> 跳过重复 Key</label>
    </div>`,
    `<button class="btn" id="impPreview">预览解析</button>
     <button class="btn" onclick="closeModal()">取消</button>
     <button class="btn btn-primary" id="impSubmit">开始导入</button>`, true);

  function parseConsole(text) {
    const out = { token: "", webid: "" };
    const bare = [];
    for (const raw of (text || "").split("\n")) {
      const line = raw.trim();
      if (!line) continue;
      const m = line.match(/^(Oasis-Token|web_id|Oasis-Webid|token|webid)\s*[=:]\s*(.+)$/i);
      if (m) {
        if (m[1].toLowerCase().includes("token")) out.token = m[2].trim();
        else out.webid = m[2].trim();
      } else bare.push(line);
    }
    if (!out.token && bare.length) out.token = bare[0];
    if (!out.webid && bare.length > 1) out.webid = bare[1];
    return out;
  }

  const collect = () => {
    const c = parseConsole($("#impConsole").value);
    return {
      content: $("#impContent").value,
      group_name: $("#impGroup").value.trim() || "default",
      name_prefix: $("#impPrefix").value,
      weight: Number($("#impWeight").value) || 1,
      priority: Number($("#impPriority").value) || 100,
      verify: $("#impVerify").checked,
      dedupe: $("#impDedupe").checked,
      proxy_assign: $("#impProxyAssign").value,
      new_pool_name: $("#impPoolName").value.trim() || null,
      pool_strategy: $("#impPoolStrategy").value,
      pool_id: null,
      console_token: c.token,
      console_webid: c.webid,
    };
  };

  $("#impPreview").addEventListener("click", async () => {
    try {
      const r = await api("/import/preview", { method: "POST", body: JSON.stringify(collect()) });
      const lines = (r.items || []).map((i) =>
        `${i.ok ? "✓" : "✗"} ${i.key_hint}${i.label ? "  " + i.label : ""}${i.proxy ? "  " + i.proxy : ""}${i.reason ? "  ← " + i.reason : ""}`
      ).join("\n");
      openModal("解析预览", `<pre class="code">${esc(lines || "没有解析到任何 Key")}</pre>
        <p class="hint" style="margin-top:12px">共 ${r.total} 条，可导入 ${r.importable} 条，拒绝 ${r.rejected} 条。</p>`,
        `<button class="btn" onclick="closeModal()">关闭</button>`, true);
    } catch (err) {
      toast(err.message, "err");
    }
  });

  $("#impSubmit").addEventListener("click", async () => {
    const b = $("#impSubmit");
    b.disabled = true;
    b.innerHTML = '<span class="spin"></span> 导入中…';
    try {
      const r = await api("/import", { method: "POST", body: JSON.stringify(collect()) });
      const ok = (r.imported || []).filter((i) => i.ok).length;
      toast(`导入完成：${ok} 个成功，${(r.skipped || []).length} 个跳过`, ok ? "ok" : "warn");
      closeModal();
      history.replaceState(null, "", "#accounts");
      go("accounts");
    } catch (err) {
      toast(err.message, "err");
    } finally {
      b.disabled = false;
      b.textContent = "开始导入";
    }
  });
});

/* ------------------------------------------------------------------ */
/* 代理                                                                */
/* ------------------------------------------------------------------ */

async function loadProxies(quiet = false) {
  try {
    const d = await api("/proxies");
    state.proxies = d.proxies || [];
    const c = $("#cntProxies");
    if (c) c.textContent = state.proxies.length;
    renderProxies();
  } catch (err) {
    if (!quiet) toast(err.message, "err");
  }
}

function renderProxies() {
  const tb = $("#proxiesTable tbody");
  if (!tb) return;
  if (!state.proxies.length) {
    tb.innerHTML = `<tr><td colspan="10"><div class="empty"><b>还没有代理</b>添加代理后可给账号绑定专属代理或代理池</div></td></tr>`;
    return;
  }
  tb.innerHTML = state.proxies.map((p) => {
    const st = p.status === "healthy" ? "ok" : p.status === "unhealthy" ? "bad" : "mute";
    const cls = !p.enabled ? "off" : p.status === "unhealthy" ? "cool" : "";
    return `<tr class="${cls}">
      <td class="mark"><i></i></td>
      <td class="muted num">${p.id}</td>
      <td>${esc(p.label)}</td>
      <td class="mono truncate" title="${esc(p.url)}">${esc(p.url)}</td>
      <td><span class="tag mute">${esc(p.scheme)}</span></td>
      <td><span class="tag ${st}">${esc(p.status)}</span>
        ${p.last_error ? `<div class="muted small truncate" title="${esc(p.last_error)}">${esc(p.last_error)}</div>` : ""}</td>
      <td class="num">${p.latency_ms == null ? "—" : Math.round(p.latency_ms) + " ms"}</td>
      <td class="num muted small">${p.success_count}/${p.failure_count}</td>
      <td class="muted small nowrap">${fmtTime(p.last_check_at)}</td>
      <td class="c-acts"><div class="actions">
        <button class="btn btn-sm" data-act="check" data-id="${p.id}">检测</button>
        <button class="btn btn-sm" data-act="toggle" data-id="${p.id}">${p.enabled ? "禁用" : "启用"}</button>
        <button class="btn btn-sm btn-danger" data-act="delete" data-id="${p.id}">删除</button>
      </div></td>
    </tr>`;
  }).join("");
}

$("#proxiesTable").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = Number(btn.dataset.id);
  const p = state.proxies.find((x) => x.id === id);
  btn.disabled = true;
  try {
    if (btn.dataset.act === "check") {
      const r = await api("/proxies/check", { method: "POST", body: JSON.stringify([id]) });
      const it = (r.results || [])[0];
      toast(it ? `#${id} ${it.ok ? "可用 " + Math.round(it.latency_ms || 0) + "ms" : "不可用：" + it.message}` : "无结果",
            it && it.ok ? "ok" : "err");
      await loadProxies(true);
    } else if (btn.dataset.act === "toggle") {
      await api(`/proxies/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: !p.enabled }) });
      await loadProxies(true);
    } else if (btn.dataset.act === "delete") {
      if (!confirm(`删除代理 #${id}？`)) return;
      await api(`/proxies/${id}`, { method: "DELETE" });
      toast("已删除", "ok");
      await Promise.all([loadProxies(true), loadPools(true)]);
    }
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
  }
});

$("#btnAddProxy").addEventListener("click", () => {
  openModal("添加代理", `
    <div class="form">
      <label>代理地址 <input id="pxUrl" placeholder="http://user:pass@host:port 或 socks5://host:port"></label>
      <label>标签 <input id="pxLabel" placeholder="us-1"></label>
      <label class="inline"><input type="checkbox" id="pxCheck" checked> 添加后立即检测连通性</label>
    </div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn btn-primary" id="pxSave">添加</button>`);
  $("#pxSave").addEventListener("click", async () => {
    try {
      await api("/proxies", { method: "POST", body: JSON.stringify({
        url: $("#pxUrl").value.trim(), label: $("#pxLabel").value.trim(), check: $("#pxCheck").checked }) });
      toast("代理已添加", "ok");
      closeModal();
      await loadProxies(true);
    } catch (err) { toast(err.message, "err"); }
  });
});

$("#btnBulkProxy").addEventListener("click", () => {
  openModal("批量导入代理", `
    <div class="form">
      <p class="hint">每行一个，支持 http / https / socks5 / socks4；<code>host:port</code> 按 http 处理。</p>
      <label>代理列表
        <textarea id="bulkUrls" rows="10" placeholder="http://user:pass@1.2.3.4:8080
socks5://5.6.7.8:1080
9.9.9.9:3128"></textarea>
      </label>
      <label>标签前缀 <input id="bulkPrefix" placeholder="proxy-"></label>
      <label class="inline"><input type="checkbox" id="bulkCheck" checked> 全部检测连通性</label>
    </div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn btn-primary" id="bulkSave">导入</button>`, true);
  $("#bulkSave").addEventListener("click", async () => {
    const b = $("#bulkSave");
    b.disabled = true; b.innerHTML = '<span class="spin"></span> 处理中…';
    try {
      const r = await api("/proxies/bulk", { method: "POST", body: JSON.stringify({
        urls: $("#bulkUrls").value, prefix: $("#bulkPrefix").value, check: $("#bulkCheck").checked }) });
      toast(`新增 ${r.created.length} 个代理，跳过 ${r.skipped.length} 个`, "ok");
      closeModal();
      await loadProxies(true);
    } catch (err) { toast(err.message, "err"); }
    finally { b.disabled = false; b.textContent = "导入"; }
  });
});

$("#btnCheckProxies").addEventListener("click", async (e) => {
  const b = e.currentTarget;
  b.disabled = true; b.innerHTML = '<span class="spin"></span> 检测中…';
  try {
    const r = await api("/proxies/check", { method: "POST", body: JSON.stringify(null) });
    const ok = (r.results || []).filter((x) => x.ok).length;
    toast(`检测完成：${ok}/${(r.results || []).length} 可用`, ok ? "ok" : "warn");
    await loadProxies(true);
  } catch (err) { toast(err.message, "err"); }
  finally { b.disabled = false; b.textContent = "健康检查"; }
});

/* ------------------------------------------------------------------ */
/* 代理池                                                              */
/* ------------------------------------------------------------------ */

async function loadPools(quiet = false) {
  try {
    const d = await api("/pools");
    state.pools = d.pools || [];
    const c = $("#cntPools");
    if (c) c.textContent = state.pools.length;
    renderPools();
  } catch (err) {
    if (!quiet) toast(err.message, "err");
  }
}

function renderPools() {
  const c = $("#poolsList");
  if (!c) return;
  if (!state.pools.length) {
    c.innerHTML = `<div class="card"><div class="empty"><b>还没有代理池</b>建一个池，把多个代理放进去轮转</div></div>`;
    return;
  }
  c.innerHTML = state.pools.map((p) => `
    <div class="card">
      <div class="pool-head">
        <div>
          <h4>${esc(p.name)} <span class="muted small">#${p.id}</span></h4>
          <span class="hint">${esc(p.note || "")}</span>
        </div>
        <span class="tag ${p.enabled ? "ok" : "mute"}">${p.enabled ? "启用" : "停用"}</span>
      </div>
      <div class="pool-meta">
        <span class="tag info">策略 ${esc(p.strategy)}</span>
        <span class="tag ${p.affinity === "sticky" ? "ok" : "warn"}">${p.affinity === "sticky" ? "账号内固定出口" : "每次轮转"}</span>
        <span class="tag mute">${p.member_count} 个代理</span>
        <span class="tag ${p.fallback_direct ? "mute" : "warn"}">${p.fallback_direct ? "允许直连回退" : "禁止直连回退"}</span>
      </div>
      <div class="member-chips">
        ${p.members.length
          ? p.members.map((m) => `<span class="tag ${m.status === "healthy" ? "ok" : m.status === "unhealthy" ? "bad" : "mute"}"
              title="${esc(m.url)}">#${m.id} ${esc(m.label)}${m.latency_ms ? " · " + Math.round(m.latency_ms) + "ms" : ""}</span>`).join("")
          : `<span class="hint">池内暂无代理</span>`}
      </div>
      <div class="row" style="margin-top:14px">
        <button class="btn btn-sm" data-act="edit" data-id="${p.id}">编辑</button>
        <button class="btn btn-sm" data-act="rotate" data-id="${p.id}" title="清空会话的代理绑定，下次重新轮转">立即轮转</button>
        <button class="btn btn-sm" data-act="check" data-id="${p.id}">检测池内</button>
        <button class="btn btn-sm btn-danger" data-act="delete" data-id="${p.id}">删除</button>
      </div>
    </div>`).join("");
}

$("#poolsList").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-act]");
  if (!btn) return;
  const id = Number(btn.dataset.id);
  const p = state.pools.find((x) => x.id === id);
  btn.disabled = true;
  try {
    if (btn.dataset.act === "edit") {
      openPoolModal(p);
    } else if (btn.dataset.act === "rotate") {
      const r = await api(`/pools/${id}/rotate`, { method: "POST" });
      toast(`已清空 ${r.rotated} 个会话的代理绑定`, "ok");
    } else if (btn.dataset.act === "check") {
      const r = await api("/proxies/check", { method: "POST", body: JSON.stringify(p.member_ids) });
      const ok = (r.results || []).filter((x) => x.ok).length;
      toast(`池 #${id}：${ok}/${(r.results || []).length} 可用`, ok ? "ok" : "warn");
      await Promise.all([loadProxies(true), loadPools(true)]);
    } else if (btn.dataset.act === "delete") {
      if (!confirm(`删除代理池「${p.name}」？绑定该池的账号会回退到全局代理。`)) return;
      await api(`/pools/${id}`, { method: "DELETE" });
      toast("已删除", "ok");
      await Promise.all([loadPools(true), loadAccounts(true)]);
    }
  } catch (err) { toast(err.message, "err"); }
  finally { btn.disabled = false; }
});

function openPoolModal(pool = null) {
  const p = pool || {};
  const isEdit = !!pool;
  openModal(isEdit ? `编辑代理池 #${p.id}` : "新建代理池", `
    <div class="form">
      <div class="row">
        <label>名称 <input id="plName" value="${esc(p.name || "")}" placeholder="us-pool"></label>
        <label>轮转策略
          <select id="plStrategy">
            ${["round_robin", "random", "least_used", "lowest_latency"]
              .map((s) => `<option value="${s}" ${p.strategy === s ? "selected" : ""}>${s}</option>`).join("")}
          </select>
        </label>
      </div>
      <label>出口绑定方式
        <select id="plAffinity">
          <option value="sticky" ${(p.affinity || "sticky") === "sticky" ? "selected" : ""}>sticky · 同一账号固定出口 IP</option>
          <option value="rotate" ${p.affinity === "rotate" ? "selected" : ""}>rotate · 每次请求重新轮转</option>
        </select>
      </label>
      <label class="inline"><input type="checkbox" id="plFallback" ${p.fallback_direct !== false ? "checked" : ""}> 池内无可用代理时允许直连回退</label>
      <label>备注 <input id="plNote" value="${esc(p.note || "")}"></label>
      <label>池成员</label>
      <div class="member-chips" style="max-height:220px;border:1px solid var(--line);border-radius:8px;padding:11px">
        ${state.proxies.length
          ? state.proxies.map((x) => `<label class="inline" style="min-width:210px">
              <input type="checkbox" class="plMember" value="${x.id}" ${(p.member_ids || []).includes(x.id) ? "checked" : ""}>
              #${x.id} ${esc(x.label)} <span class="muted small">${esc(x.scheme)}</span>
            </label>`).join("")
          : `<span class="hint">还没有代理，请先在「代理」页添加</span>`}
      </div>
    </div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn btn-primary" id="plSave">${isEdit ? "保存" : "创建"}</button>`);

  $("#plSave").addEventListener("click", async () => {
    const payload = {
      name: $("#plName").value.trim(),
      strategy: $("#plStrategy").value,
      affinity: $("#plAffinity").value,
      fallback_direct: $("#plFallback").checked,
      note: $("#plNote").value,
      proxy_ids: $$(".plMember:checked").map((el) => Number(el.value)),
    };
    if (!payload.name) { toast("请填写池名称", "err"); return; }
    try {
      if (isEdit) await api(`/pools/${pool.id}`, { method: "PATCH", body: JSON.stringify(payload) });
      else await api("/pools", { method: "POST", body: JSON.stringify(payload) });
      toast(isEdit ? "已保存" : "代理池已创建", "ok");
      closeModal();
      await loadPools(true);
    } catch (err) { toast(err.message, "err"); }
  });
}

$("#btnAddPool").addEventListener("click", () => openPoolModal(null));

/* ------------------------------------------------------------------ */
/* 粘性会话                                                            */
/* ------------------------------------------------------------------ */

async function loadSessions(quiet = false) {
  try {
    const d = await api("/sessions?limit=300");
    const rows = d.sessions || [];
    const c = $("#cntSessions");
    if (c) c.textContent = rows.length;
    const tb = $("#sessionsTable tbody");
    if (!rows.length) {
      tb.innerHTML = `<tr><td colspan="8"><div class="empty"><b>暂无粘性会话</b>带 X-Session-Id 或 metadata.user_id 的请求会在这里留下绑定</div></td></tr>`;
      return;
    }
    tb.innerHTML = rows.map((s) => {
      const left = (new Date(s.expires_at) - Date.now()) / 1000;
      return `<tr>
        <td class="mark"><i></i></td>
        <td class="mono truncate" title="${esc(s.session_key)}">${esc(s.session_key)}</td>
        <td>${esc(s.account_name || "")} <span class="muted small">#${s.account_id}</span></td>
        <td class="mono small">${esc(s.proxy || "直连")}</td>
        <td class="num">${s.hits}</td>
        <td class="muted small nowrap">${fmtTime(s.created_at)}</td>
        <td class="nowrap"><span class="tag ${left > 0 ? "mute" : "bad"}">${fmtDuration(left)}</span></td>
        <td class="c-acts"><button class="btn btn-sm btn-danger" data-key="${esc(s.session_key)}">清除</button></td>
      </tr>`;
    }).join("");
  } catch (err) {
    if (!quiet) toast(err.message, "err");
  }
}

$("#sessionsTable").addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-key]");
  if (!btn) return;
  try {
    await api("/sessions/" + encodeURIComponent(btn.dataset.key), { method: "DELETE" });
    toast("已清除", "ok");
    await loadSessions(true);
  } catch (err) { toast(err.message, "err"); }
});

$("#btnReloadSessions").addEventListener("click", () => loadSessions());

/* ------------------------------------------------------------------ */
/* 日志                                                                */
/* ------------------------------------------------------------------ */

async function loadLogs(quiet = false) {
  try {
    const d = await api("/logs?limit=200");
    state.logs = d.logs || [];
    const all = state.logs;
    const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    set("cntAll", all.length);
    set("cntPlan", all.filter((l) => l.channel === "plan").length);
    set("cntApi", all.filter((l) => l.channel === "api").length);
    set("cntFail", all.filter((l) => l.status_code >= 400 || !l.status_code).length);
    renderLogs();
  } catch (err) {
    if (!quiet) toast(err.message, "err");
  }
}

function renderLogs() {
  const tb = $("#logsTable tbody");
  if (!tb) return;
  const ch = state.logChan;
  let rows = state.logs;
  if (ch === "plan" || ch === "api") rows = rows.filter((l) => l.channel === ch);
  else if (ch === "fail") rows = rows.filter((l) => l.status_code >= 400 || !l.status_code);

  if (!rows.length) {
    tb.innerHTML = `<tr><td colspan="13"><div class="empty"><b>暂无日志</b>经过网关的请求会记录在这里</div></td></tr>`;
    return;
  }
  tb.innerHTML = rows.map((l) => {
    const okStatus = l.status_code >= 200 && l.status_code < 300;
    const st = okStatus ? "ok" : (l.status_code >= 400 ? "bad" : "warn");
    return `<tr class="${okStatus ? "" : "cool"}">
      <td class="mark"><i></i></td>
      <td class="muted small nowrap num">${fmtTime(l.created_at)}</td>
      <td class="nowrap">${esc(l.account_name || "")} <span class="muted small">#${l.account_id ?? "—"}</span></td>
      <td class="muted small">${esc(l.method || "")}</td>
      <td class="mono small truncate" title="${esc(l.path || "")}">${esc(l.path || "")}</td>
      <td class="small">${esc(l.model || "—")}</td>
      <td>${l.channel === "plan" ? '<span class="tag info">plan</span>' : '<span class="tag mute">api</span>'}</td>
      <td><span class="tag ${st}">${l.status_code ?? "—"}</span></td>
      <td class="muted small nowrap num">${l.duration_ms ? Math.round(l.duration_ms) + "ms" : "—"}</td>
      <td class="muted small num">${l.attempts || 1}</td>
      <td class="muted small truncate" title="${esc(l.proxy_label || "")}">${esc(l.proxy_label || "—")}</td>
      <td class="muted small num">${l.total_tokens ? fmtNumber(l.total_tokens, 0) : "—"}</td>
      <td class="muted small truncate" title="${esc(l.error || "")}">${esc(l.error || "")}</td>
    </tr>`;
  }).join("");
}

$("#logChips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  state.logChan = chip.dataset.chan;
  $$("#logChips .chip").forEach((c) => c.classList.toggle("on", c === chip));
  renderLogs();
});

$("#btnReloadLogs").addEventListener("click", () => loadLogs());
$("#btnClearLogs").addEventListener("click", async () => {
  if (!confirm("清空全部请求日志？")) return;
  await api("/logs", { method: "DELETE" });
  toast("日志已清空", "ok");
  await loadLogs(true);
});

/* ------------------------------------------------------------------ */
/* 配置                                                                */
/* ------------------------------------------------------------------ */

async function loadSettings(quiet = false) {
  try {
    const d = await api("/settings");
    state.settings = d.settings;
    state.portal = d.portal;
    const s = d.settings;

    $("#setRoutingMode").value = s.routing_mode;
    $("#setAffinityTtl").value = Math.round(s.affinity_ttl);
    $("#setCooldown").value = Math.round(s.cooldown_seconds);
    const cMax = $("#setCooldownMax");
    if (cMax) cMax.value = Math.round(s.cooldown_max_seconds ?? s.cooldown_seconds * 8);
    $("#setMaxRetries").value = s.max_retries;
    $("#setMaxConcurrency").value = s.default_max_concurrency;
    $("#setLowQuota").value = s.low_quota_ratio;
    $("#setRefreshInterval").value = Math.round(s.refresh_interval);
    $("#setProxyStrategy").value = s.proxy_strategy;
    $("#setGlobalProxy").value = "";
    $("#setGlobalProxy").placeholder = s.global_proxy || "http://user:pass@host:port";
    $("#globalProxyState").textContent = s.global_proxy
      ? `当前全局代理：${s.global_proxy}` : "当前未设置全局代理，inherit 的账号将直连。";

    $("#siteInfo").innerHTML = `
      <dt>控制台</dt><dd><a href="${esc(state.portal)}" target="_blank" rel="noreferrer">${esc(state.portal)}</a></dd>
      <dt>Step Plan 通道</dt><dd class="mono small">${esc(s.plan_base)}</dd>
      <dt>按量计费通道</dt><dd class="mono small">${esc(s.upstream_base)}</dd>
      <dt>余额接口</dt><dd class="mono small">${esc(s.upstream_base + s.balance_path)}</dd>
      <dt>数据目录</dt><dd class="mono small">${esc(s.data_dir)}</dd>
      <dt>站点限制</dt><dd>${s.foreign_site_only ? '<span class="tag ok">仅国外站</span>' : '<span class="tag warn">已放开国内站</span>'}</dd>
      <dt>管理鉴权</dt><dd>${s.admin_token_required ? '<span class="tag ok">已开启</span>' : '<span class="tag warn">未设置令牌</span>'}</dd>
      <dt>网关鉴权</dt><dd>${s.gateway_tokens_required ? '<span class="tag ok">已开启</span>' : '<span class="tag mute">未设置</span>'}</dd>`;

    const exp = await api("/export/claude-code");
    $("#clientConfig").textContent =
      "# Claude Code (Step Plan 通道)\n" +
      Object.entries(exp.env).map(([k, v]) => `${k}=${v}`).join("\n") +
      "\n\n# OpenAI SDK\n" + `base_url = ${exp.openai.base_url}\napi_key  = ${exp.openai.api_key}`;
  } catch (err) {
    if (!quiet) toast(err.message, "err");
  }
}

$("#btnSaveRouting").addEventListener("click", async () => {
  try {
    const body = {
      routing_mode: $("#setRoutingMode").value,
      proxy_strategy: $("#setProxyStrategy").value,
      affinity_ttl: Number($("#setAffinityTtl").value),
      cooldown_seconds: Number($("#setCooldown").value),
      max_retries: Number($("#setMaxRetries").value),
      default_max_concurrency: Number($("#setMaxConcurrency").value),
      low_quota_ratio: Number($("#setLowQuota").value),
      refresh_interval: Number($("#setRefreshInterval").value),
      global_proxy: $("#setGlobalProxy").value.trim() || null,
    };
    const cMax = $("#setCooldownMax");
    if (cMax) body.cooldown_max_seconds = Number(cMax.value);
    await api("/settings/routing", { method: "POST", body: JSON.stringify(body) });
    toast("设置已保存", "ok");
    await loadSettings(true);
  } catch (err) { toast(err.message, "err"); }
});

$("#btnClearGlobalProxy").addEventListener("click", async () => {
  try {
    await api("/settings/routing", { method: "POST", body: JSON.stringify({ clear_global_proxy: true }) });
    toast("已清除全局代理", "ok");
    await loadSettings(true);
  } catch (err) { toast(err.message, "err"); }
});

$("#btnSaveToken").addEventListener("click", () => {
  state.token = $("#setAdminToken").value.trim();
  localStorage.setItem("step2api_admin_token", state.token);
  toast("令牌已保存到本地", "ok");
  refreshAll().catch((err) => toast(err.message, "err"));
});

$("#btnClearToken").addEventListener("click", () => {
  state.token = "";
  localStorage.removeItem("step2api_admin_token");
  $("#setAdminToken").value = "";
  toast("令牌已清除", "ok");
});

$("#btnToken").addEventListener("click", () => {
  openModal("管理令牌", `
    <div class="form">
      <p class="hint">若服务端设置了 <code>STEP2API_ADMIN_TOKEN</code>，在这里填入同样的值。
        令牌只保存在浏览器 localStorage。</p>
      <label>令牌 <input type="password" id="tkValue" value="${esc(state.token)}" placeholder="STEP2API_ADMIN_TOKEN"></label>
    </div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn btn-primary" id="tkSave">保存</button>`);
  $("#tkSave").addEventListener("click", () => {
    state.token = $("#tkValue").value.trim();
    localStorage.setItem("step2api_admin_token", state.token);
    closeModal();
    toast("已保存，正在重新加载", "ok");
    refreshAll().catch((err) => toast(err.message, "err"));
  });
});

$("#btnRefreshAll").addEventListener("click", async (e) => {
  const b = e.currentTarget;
  b.disabled = true; b.innerHTML = '<span class="spin"></span> 刷新中…';
  try {
    const r = await api("/refresh-all", { method: "POST" });
    const ok = (r.results || []).filter((x) => x.ok).length;
    toast(`已刷新 ${r.count} 个账号，${ok} 个成功`, ok ? "ok" : "warn");
    await Promise.all([loadAccounts(true), loadStats(true)]);
  } catch (err) { toast(err.message, "err"); }
  finally { b.disabled = false; b.textContent = "刷新额度"; }
});

/* ------------------------------------------------------------------ */
/* 浏览器登录导入                                                       */
/* ------------------------------------------------------------------ */

let loginPoll = null;

async function refreshLoginAvailability() {
  try {
    const r = await api("/login/available");
    const b = $("#btnLoginImport");
    if (b) { b.disabled = !r.available; b.title = r.available ? "打开浏览器窗口登录，自动抓取凭据" : (r.hint || "不可用"); }
    return r;
  } catch { return { available: false }; }
}

function openLoginModal() {
  openModal("浏览器登录导入", `
    <div class="form">
      <p class="hint">会打开一个独立浏览器窗口，请在窗口里正常登录
        <a href="${esc(state.portal)}" target="_blank" rel="noreferrer">${esc(state.portal)}</a>。
        登录完成后自动抓取 API Key 与控制台凭据。</p>
      <p class="hint"><b>注意</b>：此功能依赖控制台未公开的私有接口，且实测未跑通
        （返回 not a logined oasis account）。建议改用「粘贴导入」并手动填写凭据。</p>
      <div id="loginBody" class="form"></div>
    </div>`,
    `<button class="btn" id="loginClose">关闭</button>
     <button class="btn btn-primary" id="loginGo">打开登录窗口</button>`);
  $("#loginClose").addEventListener("click", () => {
    if (loginPoll) { clearInterval(loginPoll); loginPoll = null; }
    closeModal();
  });
  $("#loginGo").addEventListener("click", startLogin);
}

async function startLogin() {
  const b = $("#loginGo");
  if (!b) return;
  b.disabled = true; b.innerHTML = '<span class="spin"></span> 启动中…';
  try {
    const r = await api("/login/start", { method: "POST", body: JSON.stringify({ timeout: 300 }) });
    pollLogin(r.session.id);
  } catch (err) { toast(err.message, "err"); b.disabled = false; b.textContent = "重试"; }
}

function pollLogin(sid) {
  if (loginPoll) clearInterval(loginPoll);
  const tick = async () => {
    try {
      const s = (await api(`/login/${sid}`)).session;
      const box = $("#loginBody");
      if (!box) return;
      const st = s.status === "success" ? "ok" : (s.status === "failed" || s.status === "cancelled") ? "bad" : "info";
      let html = `<div><span class="tag ${st}">${esc(s.status)}</span>
        <span class="hint"> ${esc(s.message)} · ${s.elapsed}s</span></div>`;
      if (s.plan_name) {
        html += `<div class="list-item"><div class="grow">
          <div>套餐 ${esc(s.plan_name)} <span class="muted small">${esc(s.plan_status || "")}</span></div>
          <div class="muted small">${s.credits_remaining != null ? fmtNumber(s.credits_remaining) + " / " + fmtNumber(s.credits_total) + " Credit" : "额度未知"}</div>
        </div></div>`;
      }
      if (s.keys && s.keys.length) {
        html += `<label>选择要导入的 Key</label><div class="member-chips">`;
        html += s.keys.map((k, i) => `<label class="inline" style="min-width:250px">
            <input type="checkbox" class="loginKey" value="${esc(k.key_id)}" ${i === 0 || k.is_default ? "checked" : ""}>
            <span class="mono">${esc(k.hint)}</span>
            <span class="muted small">${esc(k.name || "")}</span>
          </label>`).join("");
        html += `</div>
          <label class="inline"><input type="checkbox" id="loginWithConsole" checked> 同时写入控制台凭据</label>
          <div class="row">
            <label>分组 <input id="loginGroup" value="default"></label>
            <label>命名前缀 <input id="loginPrefix" placeholder="留空用 Key 名称"></label>
          </div>`;
      }
      if (s.error) html += `<div class="tag bad">${esc(s.error)}</div>`;
      box.innerHTML = html;

      if (s.status === "success" || s.status === "failed" || s.status === "cancelled") {
        clearInterval(loginPoll); loginPoll = null;
        const foot = $("#modalFoot");
        if (!foot) return;
        if (s.status === "success") {
          foot.innerHTML = `<button class="btn" id="loginClose2">关闭</button>
            <button class="btn btn-primary" id="loginCommit">导入选中的 Key</button>`;
          $("#loginClose2").addEventListener("click", closeModal);
          $("#loginCommit").addEventListener("click", () => commitLogin(sid));
        } else {
          foot.innerHTML = `<button class="btn" onclick="closeModal()">关闭</button>
            <button class="btn" id="loginRetry">重试</button>`;
          $("#loginRetry").addEventListener("click", () => { foot.innerHTML = ""; startLogin(); });
        }
      }
    } catch (err) {
      clearInterval(loginPoll); loginPoll = null;
      toast(err.message, "err");
    }
  };
  tick();
  loginPoll = setInterval(tick, 1500);
}

async function commitLogin(sid) {
  const b = $("#loginCommit");
  if (!b) return;
  b.disabled = true; b.innerHTML = '<span class="spin"></span> 导入中…';
  try {
    const r = await api(`/login/${sid}/commit`, { method: "POST", body: JSON.stringify({
      key_ids: $$(".loginKey:checked").map((el) => el.value),
      with_console: $("#loginWithConsole") ? $("#loginWithConsole").checked : true,
      group_name: ($("#loginGroup") || {}).value || "default",
      name_prefix: ($("#loginPrefix") || {}).value || "",
      verify: true,
    })});
    const ok = r.imported.filter((i) => i.verified && i.verified.ok).length;
    toast(`导入完成：${r.total} 个账号，${ok} 个额度查询成功`, "ok");
    closeModal();
    history.replaceState(null, "", "#accounts");
    go("accounts");
  } catch (err) {
    toast(err.message, "err");
    b.disabled = false; b.textContent = "导入选中的 Key";
  }
}

$("#btnLoginImport").addEventListener("click", async () => {
  const av = await refreshLoginAvailability();
  if (!av.available) { toast(av.hint || "浏览器登录导入不可用", "warn"); return; }
  openLoginModal();
});

/* ------------------------------------------------------------------ */
/* 健康信号 + 轮询                                                     */
/* ------------------------------------------------------------------ */

async function checkHealth() {
  const pulse = $("#pulse");
  try {
    const r = await fetch("/api/health").then((x) => x.json());
    if (pulse) pulse.className = "pulse ok";
    const t = $("#sigText"); if (t) t.textContent = `${r.accounts} 个账号`;
    const v = $("#sigVersion"); if (v) v.textContent = "v" + r.version;
  } catch {
    if (pulse) pulse.className = "pulse bad";
    const t = $("#sigText"); if (t) t.textContent = "服务不可达";
  }
}

async function refreshAll() {
  await Promise.all([loadAccounts(true), loadProxies(true), loadPools(true), loadStats(true), checkHealth()]);
  const m = $("#sigMode");
  if (m && state.settings) m.textContent = state.settings.routing_mode;
  if (state.view === "config") await loadSettings(true);
}

/* 只轮询当前可见视图，避免后台请求浪费 */
setInterval(() => {
  if (state.view === "overview") loadStats(true);
  else if (state.view === "accounts") loadAccounts(true);
  else if (state.view === "logs") {
    const el = document.getElementById("autoScroll");
    if (el && el.checked) loadLogs(true);
  }
}, 5000);

setInterval(checkHealth, 30000);

(function boot() {
  applyTheme();
  $("#setAdminToken").value = state.token;
  const initial = (location.hash || "#overview").slice(1);
  go(TITLES[initial] ? initial : "overview");
  refreshAll().catch((err) => toast("加载失败：" + err.message, "err"));
  refreshLoginAvailability().catch(() => {});
})();

window.closeModal = closeModal;
