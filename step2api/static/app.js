/* step2api 控制台 —— 无依赖单文件前端 */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  accounts: [],
  proxies: [],
  pools: [],
  settings: null,
  portal: "",
  token: localStorage.getItem("step2api_admin_token") || "",
  tab: "overview",
};

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
/* UI 工具                                                             */
/* ------------------------------------------------------------------ */

function toast(message, kind = "info", ms = 4200) {
  const el = document.createElement("div");
  el.className = "toast " + kind;
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
  const num = Number(value);
  if (!isFinite(num)) return "—";
  if (Math.abs(num) >= 1e9) return (num / 1e9).toFixed(2) + "B";
  if (Math.abs(num) >= 1e6) return (num / 1e6).toFixed(2) + "M";
  if (Math.abs(num) >= 1e4) return (num / 1e3).toFixed(1) + "K";
  return num.toFixed(digits).replace(/\.?0+$/, "") || "0";
}

function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const secs = Number(seconds);
  if (secs <= 0) return "已过期";
  const d = Math.floor(secs / 86400);
  const h = Math.floor((secs % 86400) / 3600);
  const m = Math.floor((secs % 3600) / 60);
  if (d > 0) return `${d}天${h}小时`;
  if (h > 0) return `${h}小时${m}分`;
  return `${m}分`;
}

function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return "—";
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function quotaBarClass(percent) {
  if (percent === null || percent === undefined) return "plain";
  if (percent <= 0.1) return "danger";
  if (percent <= 0.3) return "warn";
  return "ok";
}

function statusPill(status) {
  const map = {
    healthy: ["ok", "healthy"],
    degraded: ["warn", "degraded"],
    cooldown: ["danger", "cooldown"],
    disabled: ["plain", "disabled"],
    unknown: ["plain", "unknown"],
  };
  const [cls, label] = map[status] || ["plain", status || "unknown"];
  return `<span class="pill ${cls}">${esc(label)}</span>`;
}

/* ------------------------------------------------------------------ */
/* 弹窗                                                                */
/* ------------------------------------------------------------------ */

function openModal(title, bodyHtml, footHtml = "", wide = false) {
  $("#modalTitle").textContent = title;
  $("#modalBody").innerHTML = bodyHtml;
  $("#modalFoot").innerHTML = footHtml;
  $("#modal").classList.toggle("wide", !!wide);
  $("#modalBackdrop").classList.add("open");
}

function closeModal() {
  $("#modalBackdrop").classList.remove("open");
  $("#modalBody").innerHTML = "";
  $("#modalFoot").innerHTML = "";
}

$("#modalClose").addEventListener("click", closeModal);
$("#modalBackdrop").addEventListener("click", (e) => {
  if (e.target === $("#modalBackdrop")) closeModal();
});
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

/* ------------------------------------------------------------------ */
/* 标签页                                                              */
/* ------------------------------------------------------------------ */

$$(".tab").forEach((tab) => {
  tab.addEventListener("click", () => switchTab(tab.dataset.tab));
});

function switchTab(name) {
  state.tab = name;
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "panel-" + name));
  if (name === "sessions") loadSessions();
  if (name === "logs") loadLogs();
}

/* ------------------------------------------------------------------ */
/* 概览                                                                */
/* ------------------------------------------------------------------ */

async function loadStats() {
  const data = await api("/stats");
  const boxes = [
    { label: "账号总数", value: data.accounts.total, sub: `${data.accounts.enabled} 个已启用`, cls: "" },
    { label: "健康账号", value: data.accounts.healthy, sub: `冷却 ${data.accounts.cooldown} · 告警 ${data.accounts.low_quota}`, cls: "ok" },
    {
      label: "剩余额度合计",
      value: fmtNumber(data.credits.remaining),
      sub: data.credits.percent !== null
        ? `共 ${fmtNumber(data.credits.total)} Credit · ${(data.credits.percent * 100).toFixed(1)}%`
        : "Credit 总量未知",
      cls: "accent",
    },
    { label: "余额合计", value: fmtNumber(data.money.balance, 4), sub: data.money.currency, cls: "" },
    { label: "路由模式", value: data.routing.mode, sub: `代理策略 ${data.routing.proxy_strategy}`, cls: "" },
  ];

  $("#statCards").innerHTML = boxes
    .map((b) => `<div class="stat ${b.cls}">
        <div class="label">${esc(b.label)}</div>
        <div class="value">${esc(b.value)}</div>
        <div class="sub">${esc(b.sub)}</div>
      </div>`)
    .join("");

  const inflight = Object.entries(data.routing.inflight || {});
  $("#routingState").innerHTML = `
    <dt>路由模式</dt><dd>${esc(data.routing.mode)}</dd>
    <dt>粘性保留</dt><dd>${fmtDuration(data.routing.affinity_ttl)}</dd>
    <dt>代理轮转</dt><dd>${esc(data.routing.proxy_strategy)}</dd>
    <dt>在途请求</dt><dd>${inflight.length ? inflight.map(([k, v]) => `#${esc(k)}=${esc(v)}`).join(" · ") : "无"}</dd>
    <dt>额度刷新</dt><dd>${fmtTime(data.last_refresh_at)}${data.last_refresh_error ? ` <span class="pill danger">${esc(data.last_refresh_error)}</span>` : ""}</dd>`;

  const usage = data.usage_24h || [];
  $("#overviewUsage").innerHTML = usage.length
    ? usage.map((u) => `
        <div class="list-item">
          <div class="grow">
            <div>${esc(u.account_name || "账号 #" + u.account_id)}</div>
            <div class="muted small">成功 ${u.ok} · 失败 ${u.failed} · 平均 ${u.avg_ms ?? "—"} ms</div>
          </div>
          <div class="nowrap">${u.requests} 次<br><span class="muted small">${fmtNumber(u.tokens, 0)} tok</span></div>
        </div>`).join("")
    : '<div class="empty">近 24 小时暂无请求</div>';
}

function renderOverviewAccounts() {
  const rows = state.accounts.filter((a) => a.enabled).slice(0, 30);
  $("#overviewAccounts").innerHTML = rows.length
    ? rows.map((a) => `
      <div class="list-item">
        <div class="grow">
          <div>${esc(a.name)} <span class="muted small">#${a.id}</span></div>
          <div class="bar ${quotaBarClass(a.percent_remaining)}">
            <span style="width:${a.percent_remaining === null ? 0 : (a.percent_remaining * 100).toFixed(1)}%"></span>
          </div>
          <div class="muted small" style="margin-top:3px">
            ${a.credits_remaining === null ? "额度未知" : fmtNumber(a.credits_remaining) + " Credit 剩余"}
            · ${esc(a.plan_name || "套餐未知")}
          </div>
        </div>
        <div class="nowrap" style="text-align:right">
          ${statusPill(a.status)}
          <div class="muted small" style="margin-top:4px">${fmtDuration(a.seconds_remaining)}</div>
        </div>
      </div>`).join("")
    : '<div class="empty">还没有账号，去「账号」页导入</div>';
}

/* ------------------------------------------------------------------ */
/* 账号                                                                */
/* ------------------------------------------------------------------ */

async function loadAccounts() {
  const data = await api("/accounts");
  state.accounts = data.accounts;
  $("#badgeAccounts").textContent = data.accounts.length;
  renderAccounts();
  renderOverviewAccounts();
}

function accountMatches(a) {
  const keyword = ($("#filterAccounts").value || "").trim().toLowerCase();
  const status = $("#filterStatus").value;
  const onlyLow = $("#onlyLowQuota").checked;

  if (status && (a.status || "") !== status) return false;
  if (onlyLow && !a.low_quota) return false;
  if (keyword) {
    const hay = [a.name, a.group_name, a.key_hint, String(a.id), a.note].join(" ").toLowerCase();
    if (!hay.includes(keyword)) return false;
  }
  return true;
}

function proxyLabel(a) {
  const mode = a.proxy_mode || "inherit";
  if (mode === "direct") return '<span class="pill plain">直连</span>';
  if (mode === "dedicated") return `<span class="pill purple">专属 #${a.proxy_id ?? "?"}</span>`;
  if (mode === "pool") return `<span class="pill info">池 #${a.pool_id ?? "?"}</span>`;
  return '<span class="pill plain">全局</span>';
}

function renderAccounts() {
  const rows = state.accounts.filter(accountMatches);
  const tbody = $("#accountsTable tbody");

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="11"><div class="empty">没有匹配的账号</div></td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map((a) => {
    const pct = a.percent_remaining === null ? null : (a.percent_remaining * 100);
    const quotaText = a.credits_remaining === null
      ? '<span class="muted">未知</span>'
      : `${fmtNumber(a.credits_remaining)} / ${a.credits_total ? fmtNumber(a.credits_total) : "?"}`;
    const planBadge = a.plan_name
      ? `<span class="pill info">${esc(a.plan_name)}</span>`
      : '<span class="pill plain">未知</span>';
    const expiry = a.plan_expired
      ? '<span class="pill danger">已过期</span>'
      : fmtDuration(a.seconds_remaining);

    return `<tr data-id="${a.id}">
      <td class="muted">${a.id}</td>
      <td>
        <div>${esc(a.name)}</div>
        <div class="muted small">${esc(a.group_name || "default")}${a.priority !== 100 ? ` · P${a.priority}` : ""}${a.weight !== 1 ? ` · W${a.weight}` : ""}</div>
      </td>
      <td class="mono">${esc(a.key_hint)}</td>
      <td>
        ${planBadge}
        ${a.plan_status ? `<div class="muted small">${esc(a.plan_status)}${a.auto_renew ? " · 自动续费" : ""}</div>` : ""}
        ${a.five_hour_left_rate !== null && a.five_hour_left_rate !== undefined
          ? `<div class="muted small">5h ${(a.five_hour_left_rate * 100).toFixed(0)}%</div>` : ""}
        ${a.weekly_left_rate !== null && a.weekly_left_rate !== undefined
          ? `<div class="muted small">周 ${(a.weekly_left_rate * 100).toFixed(0)}%</div>` : ""}
        ${a.console_error
          ? `<div class="muted small truncate" title="${esc(a.console_error)}">控制台：${esc(a.console_error.slice(0, 28))}…</div>`
          : (a.console_configured ? "" : `<div class="muted small">未配控制台</div>`)}
      </td>
      <td class="w-quota">
        <div>${quotaText}${a.low_quota ? ' <span class="pill warn">告警</span>' : ""}</div>
        <div class="bar ${quotaBarClass(a.percent_remaining)}"><span style="width:${pct === null ? 0 : Math.max(2, pct).toFixed(1)}%"></span></div>
        <div class="muted small">${pct === null ? "比例未知" : pct.toFixed(1) + "%"}</div>
      </td>
      <td class="nowrap">${expiry}${a.quota_reset_at ? `<div class="muted small">重置 ${fmtTime(a.quota_reset_at)}</div>` : ""}</td>
      <td class="nowrap">
        ${a.balance === null ? '<span class="muted">—</span>' : `<span title="现金 ${a.cash_balance ?? "?"} / 赠送 ${a.voucher_balance ?? "?"}">${fmtNumber(a.balance, 4)} ${esc(a.currency || "")}</span>`}
        ${a.quota_source === "balance" ? '<div class="muted small">额度回落余额通道</div>' : ""}
      </td>
      <td>${statusPill(a.status)}
        ${a.quota_error ? `<div class="muted small truncate" title="${esc(a.quota_error)}">${esc(a.quota_error)}</div>` : ""}
        ${a.last_error && a.status !== "healthy" ? `<div class="muted small truncate" title="${esc(a.last_error)}">${esc(a.last_error)}</div>` : ""}
      </td>
      <td>${proxyLabel(a)}${a.max_concurrency ? `<div class="muted small">并发 ${a.max_concurrency}</div>` : ""}</td>
      <td class="nowrap muted small">
        ${a.total_requests || 0} 次<br>${a.success_count || 0} / ${a.failure_count || 0}
      </td>
      <td>
        <div class="actions">
          <button class="btn btn-sm" data-act="refresh" data-id="${a.id}" title="重新查询额度">刷新</button>
          <button class="btn btn-sm" data-act="edit" data-id="${a.id}">编辑</button>
          <button class="btn btn-sm" data-act="probe" data-id="${a.id}" title="探测 Step Plan 额度端点">探测</button>
          <button class="btn btn-sm" data-act="toggle" data-id="${a.id}">${a.enabled ? "禁用" : "启用"}</button>
          <button class="btn btn-sm" data-act="reset" data-id="${a.id}" title="清除冷却与失败计数">复位</button>
          <button class="btn btn-sm btn-danger" data-act="delete" data-id="${a.id}">删除</button>
        </div>
      </td>
    </tr>`;
  }).join("");

  tbody.onclick = async (event) => {
    const btn = event.target.closest("button[data-act]");
    if (!btn) return;
    const id = Number(btn.dataset.id);
    const act = btn.dataset.act;
    const account = state.accounts.find((a) => a.id === id);
    btn.disabled = true;
    try {
      if (act === "refresh") {
        const r = await api(`/accounts/${id}/refresh`, { method: "POST" });
        const ok = r.result && r.result.ok;
        toast(ok ? `#${id} 额度已更新` : `#${id} 刷新失败：${r.result.error || "未知错误"}`, ok ? "ok" : "err");
        await loadAccounts();
      } else if (act === "probe") {
        const r = await api(`/accounts/${id}/probe-endpoint`, { method: "POST" });
        openModal(`账号 #${id} 端点探测`, `<pre class="code">${esc((r.detail || []).join("\n"))}</pre>
          <p class="muted small" style="margin-top:12px">${r.hit ? "命中端点已记录，后续刷新会优先使用它。" : "未命中任何候选端点，网关会回落到按量通道余额。"}</p>`,
          `<button class="btn btn-primary" onclick="closeModal()">关闭</button>`, true);
        if (r.hit) await loadAccounts();
      } else if (act === "toggle") {
        await api(`/accounts/${id}/toggle`, { method: "POST" });
        await loadAccounts();
      } else if (act === "reset") {
        await api(`/accounts/${id}/reset`, { method: "POST" });
        toast(`#${id} 已复位`, "ok");
        await loadAccounts();
      } else if (act === "edit") {
        openAccountModal(account);
      } else if (act === "delete") {
        if (!confirm(`确定删除账号 #${id}「${account.name}」？该操作不可恢复。`)) return;
        await api(`/accounts/${id}`, { method: "DELETE" });
        toast(`已删除 #${id}`, "ok");
        await loadAccounts();
      }
    } catch (err) {
      toast(err.message, "err");
    } finally {
      btn.disabled = false;
    }
  };
}

["filterAccounts", "filterStatus", "onlyLowQuota"].forEach((id) => {
  const el = document.getElementById(id);
  const evt = el.type === "checkbox" ? "change" : "input";
  el.addEventListener(evt, renderAccounts);
});

function proxyOptions(selected) {
  return state.proxies
    .map((p) => `<option value="${p.id}" ${p.id === selected ? "selected" : ""}>#${p.id} ${esc(p.label)} (${esc(p.status)})</option>`)
    .join("");
}

function poolOptions(selected) {
  return state.pools
    .map((p) => `<option value="${p.id}" ${p.id === selected ? "selected" : ""}>#${p.id} ${esc(p.name)} · ${p.member_count} 个代理</option>`)
    .join("");
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
      ${isEdit ? "" : `<label>API Key（仅支持国外站 account.stepfun.ai） <input id="mKey" placeholder="sk-..."></label>`}
      ${isEdit ? `<label>替换 API Key（留空则不修改） <input id="mKey" placeholder="sk-..."></label>` : ""}
      <div class="row">
        <label>权重 <input type="number" id="mWeight" min="1" max="100" value="${a.weight ?? 1}"></label>
        <label>优先级（越小越先） <input type="number" id="mPriority" min="0" value="${a.priority ?? 100}"></label>
        <label>并发上限（0=默认） <input type="number" id="mConcurrency" min="0" value="${a.max_concurrency ?? 0}"></label>
      </div>
      <label>代理模式
        <select id="mProxyMode">
          <option value="inherit" ${(a.proxy_mode || "inherit") === "inherit" ? "selected" : ""}>inherit · 使用全局代理</option>
          <option value="direct" ${a.proxy_mode === "direct" ? "selected" : ""}>direct · 直连，不走代理</option>
          <option value="dedicated" ${a.proxy_mode === "dedicated" ? "selected" : ""}>dedicated · 绑定单个专属代理</option>
          <option value="pool" ${a.proxy_mode === "pool" ? "selected" : ""}>pool · 绑定代理池并轮转</option>
        </select>
      </label>
      <div class="row">
        <label id="wrapDedicated">专属代理 <select id="mProxyId"><option value="">—</option>${proxyOptions(a.proxy_id)}</select></label>
        <label id="wrapPool">代理池 <select id="mPoolId"><option value="">—</option>${poolOptions(a.pool_id)}</select></label>
      </div>
      <label class="inline"><input type="checkbox" id="mRotation" ${a.proxy_rotation !== false ? "checked" : ""}> 池内允许轮转（关闭则同一会话固定用同一出口）</label>
      <label>备注 <input id="mNote" value="${esc(a.note || "")}"></label>
      <div class="row">
        <label>控制台 Oasis-Token（可选，用于查真实订阅额度）
          <input id="mConsoleToken" placeholder="${a.console_configured ? "已配置，留空则不修改" : "Cookie Oasis-Token"}">
        </label>
        <label>控制台 web_id（localStorage，非 Cookie）
          <input id="mConsoleWebid" placeholder="${a.console_configured ? "已配置，留空则不修改" : "localStorage.web_id"}">
        </label>
      </div>
      <div class="row">
        <label>Step Plan 基址（留空用默认）
          <input id="mPlanBase" value="${esc(a.plan_base || "")}" placeholder="${esc(state.settings?.plan_base || "")}">
        </label>
        <label>按量计费基址（留空用默认）
          <input id="mBalanceBase" value="${esc(a.balance_base || "")}" placeholder="${esc(state.settings?.upstream_base || "")}">
        </label>
      </div>
      ${isEdit ? "" : '<label class="inline"><input type="checkbox" id="mVerify" checked> 保存后立即查询额度</label>'}
    </div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn btn-primary" id="mSave">${isEdit ? "保存" : "添加"}</button>`);

  const syncProxyMode = () => {
    const mode = $("#mProxyMode").value;
    $("#wrapDedicated").style.display = mode === "dedicated" ? "" : "none";
    $("#wrapPool").style.display = mode === "pool" ? "" : "none";
  };
  $("#mProxyMode").addEventListener("change", syncProxyMode);
  syncProxyMode();

  $("#mSave").addEventListener("click", async () => {
    const btn = $("#mSave");
    btn.disabled = true;
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
      };
      const cToken = $("#mConsoleToken").value.trim();
      const cWebid = $("#mConsoleWebid").value.trim();
      const proxyId = $("#mProxyId").value;
      const poolId = $("#mPoolId").value;
      payload.proxy_id = proxyId ? Number(proxyId) : null;
      payload.pool_id = poolId ? Number(poolId) : null;

      const key = $("#mKey").value.trim();
      if (!key && !isEdit) return toast("请填写 API Key", "err");

      if (isEdit) {
        if (key) payload.api_key = key;
        await api(`/accounts/${account.id}`, { method: "PATCH", body: JSON.stringify(payload) });
        if (cToken || cWebid) {
          const r = await api(`/accounts/${account.id}/console`, {
            method: "POST",
            body: JSON.stringify({ token: cToken, webid: cWebid, verify: true }),
          });
          const acc = r.account || {};
          toast(acc.plan_name
            ? `控制台额度已同步：${acc.plan_name}`
            : `控制台凭据已保存${acc.console_error ? "（" + acc.console_error.slice(0, 40) + "）" : ""}`,
            acc.plan_name ? "ok" : "warn");
        } else {
          toast("已保存", "ok");
        }
      } else {
        payload.api_key = key;
        payload.verify = $("#mVerify").checked;
        const r = await api("/accounts", { method: "POST", body: JSON.stringify(payload) });
        const v = r.verified;
        toast(v && v.ok ? "账号已添加并查询成功" : `账号已添加（额度查询：${v ? (v.error || "未知") : "跳过"}）`, v && v.ok ? "ok" : "warn");
      }
      closeModal();
      await loadAccounts();
      await loadStats();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      btn.disabled = false;
    }
  });
}

$("#btnAddAccount").addEventListener("click", () => openAccountModal(null));

/* ---------------------------- 导入 ---------------------------- */

$("#btnImport").addEventListener("click", () => {
  openModal("导入账号（仅国外站）", `
    <div class="form">
      <p class="muted small">
        支持每行一个 Key，或 <code>Key|代理URL</code>；也可直接粘贴 JSON 数组。
        只接受国外站 <a href="${esc(state.portal)}" target="_blank" rel="noreferrer">${esc(state.portal)}</a> 签发的 Key，
        含 stepfun.com 的内容会被拒绝。
      </p>
      <label>账号内容
        <textarea id="impContent" rows="9" placeholder="sk-xxxxxxxx
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
        <label>池名称 <input id="impPoolName" placeholder="留空自动生成"></label>
      </div>
      <label class="inline"><input type="checkbox" id="impVerify" checked> 导入后立即查询额度</label>
      <label class="inline"><input type="checkbox" id="impDedupe" checked> 跳过重复 Key</label>
    </div>`,
    `<button class="btn" id="impPreview">预览解析</button>
     <button class="btn" onclick="closeModal()">取消</button>
     <button class="btn btn-primary" id="impSubmit">开始导入</button>`);

  const collect = () => ({
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
  });

  $("#impPreview").addEventListener("click", async () => {
    try {
      const r = await api("/import/preview", { method: "POST", body: JSON.stringify(collect()) });
      const lines = (r.items || []).map((i) =>
        `${i.ok ? "✓" : "✗"} ${i.key_hint}${i.label ? "  " + i.label : ""}${i.proxy ? "  " + i.proxy : ""}${i.reason ? "  ← " + i.reason : ""}`
      ).join("\n");
      openModal("解析预览", `<pre class="code">${esc(lines || "没有解析到任何 Key")}</pre>
        <p class="muted small" style="margin-top:12px">共 ${r.total} 条，可导入 ${r.importable} 条，拒绝 ${r.rejected} 条。</p>`,
        `<button class="btn" onclick="closeModal()">关闭</button>`, true);
    } catch (err) {
      toast(err.message, "err");
    }
  });

  $("#impSubmit").addEventListener("click", async () => {
    const btn = $("#impSubmit");
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span> 导入中…';
    try {
      const r = await api("/import", { method: "POST", body: JSON.stringify(collect()) });
      const okCount = (r.imported || []).filter((i) => i.ok).length;
      toast(`导入完成：${okCount} 个成功，${(r.skipped || []).length} 个跳过`, okCount ? "ok" : "warn");
      closeModal();
      switchTab("accounts");
      await Promise.all([loadAccounts(), loadStats()]);
    } catch (err) {
      toast(err.message, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = "开始导入";
    }
  });
});

/* ------------------------------------------------------------------ */
/* 代理                                                                */
/* ------------------------------------------------------------------ */

async function loadProxies() {
  const data = await api("/proxies");
  state.proxies = data.proxies;
  $("#badgeProxies").textContent = data.proxies.length;
  renderProxies();
}

function renderProxies() {
  const tbody = $("#proxiesTable tbody");
  if (!state.proxies.length) {
    tbody.innerHTML = `<tr><td colspan="9"><div class="empty">还没有代理</div></td></tr>`;
    return;
  }

  tbody.innerHTML = state.proxies.map((p) => `
    <tr>
      <td class="muted">${p.id}</td>
      <td>${esc(p.label)}</td>
      <td class="mono truncate" title="${esc(p.url)}">${esc(p.url)}</td>
      <td><span class="pill plain">${esc(p.scheme)}</span></td>
      <td>${statusPill(p.status === "unhealthy" ? "degraded" : p.status === "healthy" ? "healthy" : "unknown")}
        ${p.last_error ? `<div class="muted small truncate" title="${esc(p.last_error)}">${esc(p.last_error)}</div>` : ""}</td>
      <td class="nowrap">${p.latency_ms === null || p.latency_ms === undefined ? "—" : Math.round(p.latency_ms) + " ms"}</td>
      <td class="nowrap muted small">${p.success_count} / ${p.failure_count}</td>
      <td class="nowrap muted small">${fmtTime(p.last_check_at)}</td>
      <td>
        <div class="actions">
          <button class="btn btn-sm" data-act="check" data-id="${p.id}">检测</button>
          <button class="btn btn-sm" data-act="toggle" data-id="${p.id}">${p.enabled ? "禁用" : "启用"}</button>
          <button class="btn btn-sm btn-danger" data-act="delete" data-id="${p.id}">删除</button>
        </div>
      </td>
    </tr>`).join("");

  tbody.onclick = async (event) => {
    const btn = event.target.closest("button[data-act]");
    if (!btn) return;
    const id = Number(btn.dataset.id);
    const proxy = state.proxies.find((p) => p.id === id);
    btn.disabled = true;
    try {
      if (btn.dataset.act === "check") {
        const r = await api("/proxies/check", { method: "POST", body: JSON.stringify([id]) });
        const item = (r.results || [])[0];
        toast(item ? `#${id} ${item.ok ? "可用 " + Math.round(item.latency_ms || 0) + "ms" : "不可用：" + item.message}` : "无结果",
          item && item.ok ? "ok" : "err");
        await loadProxies();
      } else if (btn.dataset.act === "toggle") {
        await api(`/proxies/${id}`, { method: "PATCH", body: JSON.stringify({ enabled: !proxy.enabled }) });
        await loadProxies();
      } else if (btn.dataset.act === "delete") {
        if (!confirm(`删除代理 #${id}？`)) return;
        await api(`/proxies/${id}`, { method: "DELETE" });
        toast("已删除", "ok");
        await Promise.all([loadProxies(), loadPools()]);
      }
    } catch (err) {
      toast(err.message, "err");
    } finally {
      btn.disabled = false;
    }
  };
}

$("#btnAddProxy").addEventListener("click", () => {
  openModal("添加代理", `
    <div class="form">
      <label>代理地址
        <input id="pxUrl" placeholder="http://user:pass@host:port 或 socks5://host:port">
      </label>
      <label>标签 <input id="pxLabel" placeholder="us-1"></label>
      <label class="inline"><input type="checkbox" id="pxCheck" checked> 添加后立即检测连通性</label>
    </div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn btn-primary" id="pxSave">添加</button>`);

  $("#pxSave").addEventListener("click", async () => {
    try {
      await api("/proxies", {
        method: "POST",
        body: JSON.stringify({
          url: $("#pxUrl").value.trim(),
          label: $("#pxLabel").value.trim(),
          check: $("#pxCheck").checked,
        }),
      });
      toast("代理已添加", "ok");
      closeModal();
      await loadProxies();
    } catch (err) {
      toast(err.message, "err");
    }
  });
});

$("#btnBulkProxy").addEventListener("click", () => {
  openModal("批量导入代理", `
    <div class="form">
      <p class="muted small">每行一个，支持 http / https / socks5 / socks4；<code>host:port</code> 会按 http 处理。</p>
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
    const btn = $("#bulkSave");
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span> 处理中…';
    try {
      const r = await api("/proxies/bulk", {
        method: "POST",
        body: JSON.stringify({
          urls: $("#bulkUrls").value,
          prefix: $("#bulkPrefix").value,
          check: $("#bulkCheck").checked,
        }),
      });
      toast(`新增 ${r.created.length} 个代理，跳过 ${r.skipped.length} 个`, "ok");
      closeModal();
      await loadProxies();
    } catch (err) {
      toast(err.message, "err");
    } finally {
      btn.disabled = false;
      btn.textContent = "导入";
    }
  });
});

$("#btnCheckProxies").addEventListener("click", async (event) => {
  const btn = event.currentTarget;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> 检测中…';
  try {
    const r = await api("/proxies/check", { method: "POST", body: JSON.stringify(null) });
    const ok = (r.results || []).filter((x) => x.ok).length;
    toast(`检测完成：${ok}/${(r.results || []).length} 可用`, ok ? "ok" : "warn");
    await loadProxies();
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "健康检查";
  }
});

/* ------------------------------------------------------------------ */
/* 代理池                                                              */
/* ------------------------------------------------------------------ */

async function loadPools() {
  const data = await api("/pools");
  state.pools = data.pools;
  $("#badgePools").textContent = data.pools.length;
  renderPools();
}

function renderPools() {
  const container = $("#poolsList");
  if (!state.pools.length) {
    container.innerHTML = '<div class="card"><div class="empty">还没有代理池</div></div>';
    return;
  }

  container.innerHTML = state.pools.map((pool) => `
    <div class="card">
      <div class="pool-head">
        <div>
          <h4>${esc(pool.name)} <span class="muted small">#${pool.id}</span></h4>
          <span class="muted small">${esc(pool.note || "")}</span>
        </div>
        ${pool.enabled ? '<span class="pill ok">启用</span>' : '<span class="pill plain">停用</span>'}
      </div>
      <div class="pool-meta">
        <span class="pill info">策略 ${esc(pool.strategy)}</span>
        <span class="pill ${pool.affinity === "sticky" ? "purple" : "warn"}">${pool.affinity === "sticky" ? "会话粘性" : "每次轮转"}</span>
        <span class="pill plain">${pool.member_count} 个代理</span>
        <span class="pill ${pool.fallback_direct ? "plain" : "warn"}">${pool.fallback_direct ? "允许直连回退" : "禁止直连回退"}</span>
      </div>
      <div class="member-chips">
        ${pool.members.length
          ? pool.members.map((m) => `<span class="pill ${m.status === "healthy" ? "ok" : m.status === "unhealthy" ? "danger" : "plain"}"
              title="${esc(m.url)}">#${m.id} ${esc(m.label)}${m.latency_ms ? " · " + Math.round(m.latency_ms) + "ms" : ""}</span>`).join("")
          : '<span class="muted small">池内暂无代理</span>'}
      </div>
      <div class="row" style="margin-top:14px">
        <button class="btn btn-sm" data-act="edit" data-id="${pool.id}">编辑成员与策略</button>
        <button class="btn btn-sm" data-act="rotate" data-id="${pool.id}" title="清空会话的代理绑定，下次请求重新轮转">立即轮转</button>
        <button class="btn btn-sm" data-act="check" data-id="${pool.id}">检测池内代理</button>
        <button class="btn btn-sm btn-danger" data-act="delete" data-id="${pool.id}">删除</button>
      </div>
    </div>`).join("");

  container.onclick = async (event) => {
    const btn = event.target.closest("button[data-act]");
    if (!btn) return;
    const id = Number(btn.dataset.id);
    const pool = state.pools.find((p) => p.id === id);
    btn.disabled = true;
    try {
      if (btn.dataset.act === "edit") {
        openPoolModal(pool);
      } else if (btn.dataset.act === "rotate") {
        const r = await api(`/pools/${id}/rotate`, { method: "POST" });
        toast(`已清空 ${r.rotated} 个会话的代理绑定`, "ok");
      } else if (btn.dataset.act === "check") {
        const r = await api("/proxies/check", { method: "POST", body: JSON.stringify(pool.member_ids) });
        const ok = (r.results || []).filter((x) => x.ok).length;
        toast(`池 #${id}：${ok}/${(r.results || []).length} 可用`, ok ? "ok" : "warn");
        await Promise.all([loadProxies(), loadPools()]);
      } else if (btn.dataset.act === "delete") {
        if (!confirm(`删除代理池「${pool.name}」？绑定该池的账号会回退到全局代理。`)) return;
        await api(`/pools/${id}`, { method: "DELETE" });
        toast("已删除", "ok");
        await Promise.all([loadPools(), loadAccounts()]);
      }
    } catch (err) {
      toast(err.message, "err");
    } finally {
      btn.disabled = false;
    }
  };
}

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
      <label>会话与代理的关系
        <select id="plAffinity">
          <option value="sticky" ${(p.affinity || "sticky") === "sticky" ? "selected" : ""}>sticky · 同一会话固定同一出口 IP</option>
          <option value="rotate" ${p.affinity === "rotate" ? "selected" : ""}>rotate · 每个请求都重新轮转</option>
        </select>
      </label>
      <label class="inline"><input type="checkbox" id="plFallback" ${p.fallback_direct !== false ? "checked" : ""}> 池内无可用代理时允许直连回退</label>
      <label>备注 <input id="plNote" value="${esc(p.note || "")}"></label>
      <label>池成员</label>
      <div class="member-chips" style="max-height:220px;border:1px solid var(--border);border-radius:8px;padding:11px">
        ${state.proxies.length
          ? state.proxies.map((x) => `<label class="inline" style="min-width:210px">
              <input type="checkbox" class="plMember" value="${x.id}"
                ${(p.member_ids || []).includes(x.id) ? "checked" : ""}>
              #${x.id} ${esc(x.label)} <span class="muted small">${esc(x.scheme)}</span>
            </label>`).join("")
          : '<span class="muted small">还没有代理，请先在「代理」页添加</span>'}
      </div>
    </div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn btn-primary" id="plSave">${isEdit ? "保存" : "创建"}</button>`);

  $("#plSave").addEventListener("click", async () => {
    const proxyIds = $$(".plMember:checked").map((el) => Number(el.value));
    const payload = {
      name: $("#plName").value.trim(),
      strategy: $("#plStrategy").value,
      affinity: $("#plAffinity").value,
      fallback_direct: $("#plFallback").checked,
      note: $("#plNote").value,
      proxy_ids: proxyIds,
    };
    if (!payload.name) return toast("请填写池名称", "err");
    try {
      if (isEdit) {
        await api(`/pools/${pool.id}`, { method: "PATCH", body: JSON.stringify(payload) });
        toast("已保存", "ok");
      } else {
        await api("/pools", { method: "POST", body: JSON.stringify(payload) });
        toast("代理池已创建", "ok");
      }
      closeModal();
      await loadPools();
    } catch (err) {
      toast(err.message, "err");
    }
  });
}

$("#btnAddPool").addEventListener("click", () => openPoolModal(null));

/* ------------------------------------------------------------------ */
/* 会话                                                                */
/* ------------------------------------------------------------------ */

async function loadSessions() {
  const data = await api("/sessions?limit=300");
  const rows = data.sessions || [];
  $("#badgeSessions").textContent = rows.length;

  const tbody = $("#sessionsTable tbody");
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="7"><div class="empty">暂无粘性会话</div></td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map((s) => `
    <tr>
      <td class="mono truncate" title="${esc(s.session_key)}">${esc(s.session_key)}</td>
      <td>${esc(s.account_name || "")} <span class="muted small">#${s.account_id}</span></td>
      <td class="mono small">${esc(s.proxy || "直连")}</td>
      <td>${s.hits}</td>
      <td class="muted small nowrap">${fmtTime(s.created_at)}</td>
      <td class="muted small nowrap">${fmtTime(s.expires_at)}</td>
      <td><button class="btn btn-sm btn-danger" data-key="${esc(s.session_key)}">清除</button></td>
    </tr>`).join("");

  tbody.onclick = async (event) => {
    const btn = event.target.closest("button[data-key]");
    if (!btn) return;
    try {
      await api("/sessions/" + encodeURIComponent(btn.dataset.key), { method: "DELETE" });
      toast("已清除", "ok");
      await loadSessions();
    } catch (err) {
      toast(err.message, "err");
    }
  };
}

$("#btnReloadSessions").addEventListener("click", loadSessions);

/* ------------------------------------------------------------------ */
/* 日志                                                                */
/* ------------------------------------------------------------------ */

async function loadLogs() {
  const data = await api("/logs?limit=200");
  const rows = data.logs || [];
  const tbody = $("#logsTable tbody");

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="12"><div class="empty">暂无请求日志</div></td></tr>`;
    return;
  }

  tbody.innerHTML = rows.map((l) => {
    const statusCls = l.status_code >= 200 && l.status_code < 300 ? "ok" : l.status_code >= 400 ? "danger" : "warn";
    return `<tr>
      <td class="muted small nowrap">${fmtTime(l.created_at)}</td>
      <td class="nowrap">${esc(l.account_name || "")} <span class="muted small">#${l.account_id ?? "—"}</span></td>
      <td class="muted small">${esc(l.method || "")}</td>
      <td class="mono small truncate" title="${esc(l.path || "")}">${esc(l.path || "")}</td>
      <td class="small">${esc(l.model || "—")}</td>
      <td>${l.channel === "plan" ? '<span class="pill purple">plan</span>' : '<span class="pill plain">api</span>'}</td>
      <td><span class="pill ${statusCls}">${l.status_code ?? "—"}</span></td>
      <td class="nowrap muted small">${l.duration_ms ? Math.round(l.duration_ms) + " ms" : "—"}</td>
      <td class="muted small">${l.attempts || 1}</td>
      <td class="muted small truncate" title="${esc(l.proxy_label || "")}">${esc(l.proxy_label || "—")}</td>
      <td class="muted small">${l.total_tokens ? fmtNumber(l.total_tokens, 0) : "—"}</td>
      <td class="muted small truncate" title="${esc(l.error || "")}">${esc(l.error || "")}</td>
    </tr>`;
  }).join("");
}

$("#btnReloadLogs").addEventListener("click", loadLogs);
$("#btnClearLogs").addEventListener("click", async () => {
  if (!confirm("清空全部请求日志？")) return;
  await api("/logs", { method: "DELETE" });
  toast("日志已清空", "ok");
  await loadLogs();
});

/* ------------------------------------------------------------------ */
/* 设置                                                                */
/* ------------------------------------------------------------------ */

async function loadSettings() {
  const data = await api("/settings");
  state.settings = data.settings;
  state.portal = data.portal;

  const s = data.settings;
  $("#setRoutingMode").value = s.routing_mode;
  $("#setAffinityTtl").value = Math.round(s.affinity_ttl);
  $("#setCooldown").value = Math.round(s.cooldown_seconds);
  $("#setMaxRetries").value = s.max_retries;
  $("#setMaxConcurrency").value = s.default_max_concurrency;
  $("#setLowQuota").value = s.low_quota_ratio;
  $("#setRefreshInterval").value = Math.round(s.refresh_interval);
  $("#setProxyStrategy").value = s.proxy_strategy;
  $("#setGlobalProxy").value = "";
  $("#setGlobalProxy").placeholder = s.global_proxy || "http://user:pass@host:port";
  $("#globalProxyState").textContent = s.global_proxy
    ? `当前全局代理：${s.global_proxy}`
    : "当前未设置全局代理，inherit 的账号将直连。";

  $("#siteInfo").innerHTML = `
    <dt>控制台</dt><dd><a href="${esc(state.portal)}" target="_blank" rel="noreferrer">${esc(state.portal)}</a></dd>
    <dt>Step Plan 通道</dt><dd class="mono small">${esc(s.plan_base)}</dd>
    <dt>按量计费通道</dt><dd class="mono small">${esc(s.upstream_base)}</dd>
    <dt>余额接口</dt><dd class="mono small">${esc(s.upstream_base + s.balance_path)}</dd>
    <dt>额度探测候选</dt><dd class="mono small">${(s.plan_quota_paths || []).map(esc).join(" · ")}</dd>
    <dt>数据目录</dt><dd class="mono small">${esc(s.data_dir)}</dd>
    <dt>站点限制</dt><dd>${s.foreign_site_only ? '<span class="pill ok">仅国外站</span>' : '<span class="pill warn">已放开国内站</span>'}</dd>
    <dt>管理鉴权</dt><dd>${s.admin_token_required ? '<span class="pill ok">已开启</span>' : '<span class="pill warn">未设置令牌</span>'}</dd>
    <dt>网关鉴权</dt><dd>${s.gateway_tokens_required ? '<span class="pill ok">已开启</span>' : '<span class="pill plain">未设置</span>'}</dd>`;

  try {
    const exp = await api("/export/claude-code");
    $("#clientConfig").textContent =
      "# Claude Code (Step Plan 通道)\n" +
      Object.entries(exp.env).map(([k, v]) => `${k}=${v}`).join("\n") +
      "\n\n# OpenAI SDK\n" +
      `base_url = ${exp.openai.base_url}\napi_key  = ${exp.openai.api_key}`;
  } catch {
    $("#clientConfig").textContent = "加载失败";
  }
}

$("#btnSaveRouting").addEventListener("click", async () => {
  try {
    await api("/settings/routing", {
      method: "POST",
      body: JSON.stringify({
        routing_mode: $("#setRoutingMode").value,
        proxy_strategy: $("#setProxyStrategy").value,
        affinity_ttl: Number($("#setAffinityTtl").value),
        cooldown_seconds: Number($("#setCooldown").value),
        max_retries: Number($("#setMaxRetries").value),
        default_max_concurrency: Number($("#setMaxConcurrency").value),
        low_quota_ratio: Number($("#setLowQuota").value),
        refresh_interval: Number($("#setRefreshInterval").value),
        global_proxy: $("#setGlobalProxy").value.trim() || null,
      }),
    });
    toast("设置已保存", "ok");
    await loadSettings();
  } catch (err) {
    toast(err.message, "err");
  }
});

$("#btnClearGlobalProxy").addEventListener("click", async () => {
  try {
    await api("/settings/routing", { method: "POST", body: JSON.stringify({ clear_global_proxy: true }) });
    toast("已清除全局代理", "ok");
    await loadSettings();
  } catch (err) {
    toast(err.message, "err");
  }
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
      <p class="muted small">若服务端设置了 <code>STEP2API_ADMIN_TOKEN</code>，在这里填入同样的值即可访问管理接口。令牌只保存在浏览器 localStorage。</p>
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

$("#btnRefreshAll").addEventListener("click", async (event) => {
  const btn = event.currentTarget;
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> 刷新中…';
  try {
    const r = await api("/refresh-all", { method: "POST" });
    const ok = (r.results || []).filter((x) => x.ok).length;
    toast(`已刷新 ${r.count} 个账号，${ok} 个查询成功`, ok ? "ok" : "warn");
    await Promise.all([loadAccounts(), loadStats()]);
  } catch (err) {
    toast(err.message, "err");
  } finally {
    btn.disabled = false;
    btn.textContent = "刷新额度";
  }
});

/* ------------------------------------------------------------------ */
/* 健康 & 启动                                                         */
/* ------------------------------------------------------------------ */

async function checkHealth() {
  const dot = $("#healthDot");
  const text = $("#healthText");
  try {
    const r = await fetch("/api/health").then((x) => x.json());
    dot.className = "dot ok";
    text.textContent = `v${r.version} · ${r.accounts} 账号`;
  } catch {
    dot.className = "dot bad";
    text.textContent = "服务不可达";
  }
}

async function refreshAll() {
  await Promise.all([loadStats(), loadAccounts(), loadProxies(), loadPools(), loadSettings(), checkHealth()]);
}

(function bootstrap() {
  $("#setAdminToken").value = state.token;
  refreshAll().catch((err) => toast("加载失败：" + err.message, "err"));
  setInterval(checkHealth, 30000);
  setInterval(() => {
    if (state.tab === "overview") loadStats().catch(() => {});
  }, 60000);
})();

window.closeModal = closeModal;
