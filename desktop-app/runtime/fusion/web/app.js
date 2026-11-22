"use strict";

// Mounted under a reverse-proxy prefix (for example /dutang) this keeps every
// API call under the same path; standalone deployments resolve to "".
const BASE_PATH = (() => {
  try {
    if (typeof location === "undefined") return "";
    const path = new URL(".", location.href).pathname.replace(/\/+$/, "");
    return path === "/" ? "" : path;
  } catch { return ""; }
})();

// Deployment configuration, not an authorization mechanism. Each prefix must
// route to a separately configured company Store. Never send company in POST.
const COMPANIES = [{ id: "default", name: "当前公司 / default", base: "" }];
const ACTORS = { taizi: "太子判定", zhongshu: "中书计划", menxia: "门下裁决", shangshu: "尚书派发", gongbu: "工部产物" };

function safeDownloadURL(value) {
  if (typeof value !== "string" || !value.startsWith("/") || value.startsWith("//") || /[\\\s\x00-\x1f\x7f]/.test(value)) return null;
  const url = new URL(value, "http://fusion.local");
  return url.origin === "http://fusion.local" ? value : null;
}

function renderRun(view) {
  if (Array.isArray(view.timeline)) return view; // fusion.webui adapter output
  let attempt = 0;
  let rejected = false;
  const timeline = (view.steps || []).map((step, index) => {
    const decision = step.decision || {};
    const valid = ["valid", "ledger"].includes(step.status);
    const review = step.actor === "menxia" && valid && ["reject", "approve"].includes(decision.verdict);
    if (review) attempt += 1;
    const revised = review && rejected;
    if (review && decision.verdict === "reject") rejected = true;
    return { ...step, sequence: index + 1, label: ACTORS[step.actor] || step.actor || "未知步骤",
      model_attempt: step.attempt || 1, review_attempt: review ? attempt : null,
      verdict: review ? decision.verdict : null, revised,
      passed_after_revision: revised && decision.verdict === "approve" };
  });
  const artifacts = (view.artifacts || []).filter(item => item && typeof item === "object").map(item => ({
    ...item, name: String(item.path || "").replace(/\\/g, "/").split("/").pop() || "未命名产物",
    download_url: safeDownloadURL(item.download_url)
  }));
  return { ...view, timeline, artifacts };
}

function boot() {
  const $ = id => document.getElementById(id);
  // 会话在 httpOnly cookie 里，JS 拿不到也不该拿；这里不再读取任何本地 token。
  let epoch = 0;
  let detailEpoch = 0;
  let selected = null;
  let pollTimer = null;
  let current = null;
  let submitting = false;
  let company = COMPANIES[0];
  let bindingEpoch = 0;
  let installTimer = null;
  let installing = false;
  let filterState = "all";
  let me = null;
  let issueCache = [];
  let openIssue = null;
  let currentAgent = null;
  // Embedded under the DSH panel: keep the 衡鉴 token in the parent frame's
  // memory so switching DSH panels (which remounts the iframe) does not log the
  // user out. The token is never written to storage.
  const embedded = (() => {
    try { return typeof window !== "undefined" && window.parent !== window; } catch { return false; }
  })();
  const postBridge = message => {
    if (!embedded) return;
    try { window.parent.postMessage(message, window.location.origin); } catch { /* ignore */ }
  };
  if (embedded) {
    document.documentElement.dataset.embed = "1";
    window.addEventListener("message", event => {
      if (event.origin !== window.location.origin) return;
      const data = event.data;
      if (!data || typeof data.type !== "string") return;
      if (data.type === "dutang:theme") {
        try { applyTheme(data.theme); } catch { /* boot not ready yet */ }
        return;
      }
      if (data.type !== "dutang:restore") return;
      const input = $("token");
      if (input && !input.value && typeof data.token === "string") input.value = data.token;
      if (typeof data.selection === "string" && data.selection) {
        setTimeout(() => { selectRun(data.selection).catch(() => {}); }, 80);
      }
      if (typeof data.scroll === "number" && Number.isFinite(data.scroll)) {
        setTimeout(() => window.scrollTo(0, Math.max(0, data.scroll)), 350);
      }
      if (typeof data.theme === "string") {
        try { applyTheme(data.theme); } catch { /* boot not ready yet */ }
      }
    });
    $("token").addEventListener("input", () => postBridge({ type: "dutang:token", token: $("token").value }));
    let scrollTimer = 0;
    window.addEventListener("scroll", () => {
      if (scrollTimer) return;
      scrollTimer = setTimeout(() => {
        scrollTimer = 0;
        postBridge({ type: "dutang:scroll", scroll: window.scrollY });
      }, 150);
    }, { passive: true });
    postBridge({ type: "dutang:ready" });
  }
  let entriesCache = new Map();
  let taskTitles = new Map();
  const text = (tag, value) => {
    const node = document.createElement(tag);
    node.textContent = value == null ? "—" : String(value);
    return node;
  };
  function status(id, message, error = false) {
    $(id).textContent = message;
    $(id).classList.toggle("error", error);
  }

  const STATE_INFO = {
    Done: ["已办结", "done"], Cancelled: ["已撤销", "off"],
    Running: ["执行中", "active"], Doing: ["执行中", "active"],
    Queued: ["排队中", "wait"], pending: ["排队中", "wait"],
    Review: ["待复核", "wait"], PendingConfirm: ["待确认", "wait"],
    Menxia: ["门下审议", "wait"], Assigned: ["已派发", "wait"],
    Blocked: ["受阻", "bad"], error: ["失败", "bad"], failed: ["失败", "bad"],
    awaiting_board: ["待签署", "wait"], succeeded: ["已完成", "done"],
    incomplete: ["未完成", "off"], validation: ["校验中", "active"],
  };
  const ACTOR_MARK = { taizi: "太", zhongshu: "中", menxia: "门", shangshu: "尚", gongbu: "工" };

  function stateInfo(state) {
    const [label, tone] = STATE_INFO[state] || [String(state || "未知"), "off"];
    return { label, tone, raw: String(state || "") };
  }

  function fmtTime(value) {
    if (!value) return "时间未记录";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return String(value);
    const pad = number => String(number).padStart(2, "0");
    return `${parsed.getMonth() + 1}/${pad(parsed.getDate())} ${pad(parsed.getHours())}:${pad(parsed.getMinutes())}`;
  }

  function setConnection(kind, label) {
    $("conn-chip").className = "conn" + (kind ? " is-" + kind : "");
    $("conn-label").textContent = label;
  }

  function openDrawer(id) {
    closeDrawer("compose-drawer");
    closeDrawer("settings-drawer", true);
    if (id !== "issue-drawer") closeDrawer("issue-drawer");
    if (id !== "agent-drawer") closeDrawer("agent-drawer");
    const drawer = $(id);
    if (!drawer) return;
    drawer.hidden = false;
    $("scrim").hidden = false;
    const button = id === "compose-drawer" ? $("open-compose") : $("open-settings");
    if (button) button.setAttribute("aria-expanded", "true");
    const focusTarget = drawer.querySelector("textarea, input, select, button");
    if (focusTarget) focusTarget.focus({ preventScroll: true });
  }

  function closeDrawer(id, keepScrim = false) {
    const drawer = id ? $(id) : null;
    if (drawer && !drawer.hidden) drawer.hidden = true;
    const anyOpen = ["compose-drawer", "settings-drawer", "issue-drawer", "agent-drawer"].some(key => {
      const node = $(key);
      return node && !node.hidden;
    });
    if (!anyOpen && !keepScrim) $("scrim").hidden = true;
    for (const [key, button] of [["open-compose", "compose-drawer"], ["open-settings", "settings-drawer"]]) {
      const node = $(key);
      if (node) node.setAttribute("aria-expanded", String(!$(button).hidden));
    }
  }
  function clearDetail() {
    current = null;
    $("timeline").replaceChildren();
    $("artifacts").replaceChildren();
    const preview = $("artifact-preview");
    if (preview) { preview.hidden = true; preview.textContent = ""; }
    $("run-summary").textContent = "";
    $("board-guide").hidden = true;
    $("board-guide").replaceChildren();
    $("export").disabled = true;
    $("detail-body").hidden = true;
    $("detail-empty").hidden = false;
    $("detail-badge").hidden = true;
    status("detail-status", "");
    status("artifact-status", "暂无产物。");
  }
  async function api(path, options = {}) {
    const token = $("token").value.trim();
    const publicCall = options.public === true;
    const cookieHint = /(?:^|;\s*)fusion_auth=1(?:;|$)/.test(document.cookie || "");
    if (!token && !publicCall && !cookieHint) throw new Error("请填写 API Token 后重试。");
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(BASE_PATH + company.base + path, {
        credentials: "same-origin",
        ...options, signal: controller.signal, cache: "no-store", credentials: "same-origin", redirect: "error",
        headers: {
          "Content-Type": "application/json",
          "X-Fusion-Request": "1",
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        }
      });
      if (!response.ok) {
        const messages = { 401: "Token 无效或已撤销。", 403: "没有此公司或资源的权限。", 404: "接口或记录不存在。", 405: "服务不支持此操作。" };
        if (messages[response.status]) throw new Error(messages[response.status]);
        const detail = await response.json().catch(() => ({}));
        throw new Error(messages[response.status] || detail.error || `服务请求失败（HTTP ${response.status}），请稍后重试。`);
      }
      return await (options.binary ? response.blob() : response.json());
    } catch (error) {
      if (error.name === "AbortError") throw new Error("请求超时，请重试。");
      if (error instanceof TypeError || error instanceof SyntaxError) throw new Error("连接失败或服务响应无效，请检查服务后重试。");
      throw error;
    } finally { clearTimeout(timeout); }
  }
  function resetBindings() {
    ++bindingEpoch;
    clearTimeout(installTimer);
    installing = false;
    $("bindings-rows").replaceChildren();
    $("apply-preset").disabled = true;
    $("save-bindings").disabled = true;
    $("bindings-current").hidden = true;
    $("install-progress").hidden = true;
    status("bindings-status", "请使用 admin 会话连接以查看预设。");
    $("merit-rows").replaceChildren();
    status("merit-status", "连接后读取各衙署功过。");
  }
  async function loadAgentLifecycle(agentId) {
    try {
      const data = await api(`/api/agents/${encodeURIComponent(agentId)}/lifecycle`);
      $("agent-lifecycle-config").value = data.config ? JSON.stringify(data.config, null, 2) : "";
      const status = data.status || {};
      $("agent-lifecycle-status").textContent = status.running
        ? `运行中 · pid ${status.pid}` : (status.configured ? "已配置，未运行" : "未配置生命周期");
    } catch (error) {
      $("agent-lifecycle-status").textContent = "需要 admin 会话才能查看/操作生命周期。";
      $("agent-lifecycle-config").value = "";
    }
  }

  async function lifecycleAction(action) {
    if (!currentAgent) return;
    try {
      const data = await api(`/api/agents/${encodeURIComponent(currentAgent)}/lifecycle/${action}`, {
        method: "POST", body: "{}",
      });
      status("agent-detail-status", `${action}: ${data.running ? "running" : "stopped"}`);
      await loadAgentLifecycle(currentAgent);
    } catch (error) {
      status("agent-detail-status", error.message, true);
    }
  }

  async function saveAgentLifecycle() {
    if (!currentAgent) return;
    try {
      const config = JSON.parse($("agent-lifecycle-config").value || "{}");
      await api(`/api/agents/${encodeURIComponent(currentAgent)}/lifecycle`, {
        method: "PUT", body: JSON.stringify(config),
      });
      status("agent-detail-status", "生命周期配置已保存。");
      await loadAgentLifecycle(currentAgent);
    } catch (error) {
      status("agent-detail-status", error.message, true);
    }
  }

  async function openAgentDetail(agentId) {
    try {
      const inventory = await api("/api/agents/inventory");
      const agent = (inventory.agents || []).find(item => item.id === agentId);
      const data = await api(`/api/agents/${encodeURIComponent(agentId)}/runs?limit=10`);
      currentAgent = agentId;
      $("agent-drawer-title").textContent = agentId;
      $("agent-detail-meta").textContent = agent
        ? `${agent.title || agentId} · ${agent.health?.state || "unknown"} · ${agent.version || "版本未知"}`
        : agentId;
      $("agent-detail-workspace").textContent = agent?.workspace
        ? `workspace: ${agent.workspace}` : "尚无 workspace";
      const list = $("agent-detail-runs");
      list.replaceChildren();
      for (const run of data.runs || []) {
        const item = text("li", "");
        item.append(text("strong", run.issue_id || run.run));
        item.append(text("span", ` · ${new Date(run.at * 1000).toLocaleString()}`));
        for (const step of run.steps || []) {
          item.append(text("div", `${step.step || "step"} · ${step.status || "?"} · ${step.model || ""}`));
        }
        if (run.artifacts?.length) item.append(text("div", `artifacts: ${run.artifacts.join(", ")}`));
        list.append(item);
      }
      if (!list.children.length) list.append(text("li", "暂无运行记录。"));
      status("agent-detail-status", "");
      await loadAgentLifecycle(agentId);
      openDrawer("agent-drawer");
    } catch (error) {
      status("agent-detail-status", error.message, true);
    }
  }

  async function refreshAgentRegistry() {
    const body = $("agent-registry-rows");
    if (!body) return;
    body.replaceChildren();
    try {
      const data = await api("/api/agents/inventory");
      const label = agent => {
        const health = agent.health || {};
        if (agent.ready) return "就绪";
        if (health.state === "login_required") return "需登录";
        if (health.state === "not_configured") return "未配置";
        if (health.state === "not_installed") return "未安装";
        return "不可用";
      };
      const all = Array.isArray(data.agents) ? data.agents : [];
      const visible = all.filter(agent => agent.found || agent.bound_roles?.length);
      for (const agent of visible) {
        const tr = text("tr", "");
        const state = text("td", label(agent));
        const health = agent.health || {};
        state.title = [health.message, health.next_step].filter(Boolean).join("；");
        const action = text("td", "");
        const detail = text("button", "查看");
        detail.type = "button";
        detail.className = "btn btn-quiet btn-small";
        detail.addEventListener("click", () => openAgentDetail(agent.id));
        action.append(detail);
        tr.append(
          text("td", agent.id),
          text("td", agent.version ? String(agent.version).split("\n")[0].slice(0, 60) : "—"),
          state,
          text("td", agent.bound_labels?.length ? agent.bound_labels.join("、") : "未绑定"),
          action,
        );
        body.append(tr);
      }
      status("agent-registry-status",
        `${visible.length} 个已接入/已发现 · ${data.ready || 0} 就绪 · 另有 ${Math.max(0, all.length - visible.length)} 个目录项未接入`);
    } catch (error) {
      status("agent-registry-status", "需要连接后读取 Agent 清单。", true);
    }
  }
  async function loadMe() {
    try { me = await api("/api/me"); } catch { me = null; }
  }

  function issueLabel(state) {
    return { queued: "待领", blocked: "受阻", running: "执行中", review: "待审",
      done: "已办", cancelled: "已撤销" }[state] || String(state || "未知");
  }

  function issueCard(issue) {
    const li = text("li", "");
    li.className = "issue-card";
    li.tabIndex = 0;
    const head = text("div", "");
    head.className = "issue-card-head";
    head.append(text("strong", issue.title || issue.id), text("span", issueLabel(issue.state)));
    head.lastChild.className = "chip chip-muted";
    const meta = text("p", `${issue.id} · ${issue.assignee || "—"}`);
    meta.className = "fineprint";
    const checks = issue.result?.checks || [];
    const attempt = issue.attempts ? ` · attempt ${issue.attempts}/${issue.max_attempts}` : "";
    const checkLine = checks.length
      ? `${checks.filter(item => item.ok).length}/${checks.length} checks${attempt}`
      : `${issue.reviewer ? `review ${issue.reviewer}` : "无人审"}${attempt}`;
    const foot = text("p", checkLine);
    foot.className = "issue-card-foot";
    li.append(head, meta, foot);
    const open = () => openIssueDetail(issue.id);
    li.addEventListener("click", open);
    li.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
    });
    return li;
  }

  function renderIssueBoard() {
    const groups = {
      queued: ["queued", "blocked"], running: ["running"],
      review: ["review"], done: ["done", "cancelled"],
    };
    for (const [key, states] of Object.entries(groups)) {
      const list = $("issue-list-" + key);
      if (!list) continue;
      list.replaceChildren();
      const rows = issueCache.filter(issue => states.includes(issue.state));
      rows.forEach(issue => list.append(issueCard(issue)));
      $("issue-count-" + key).textContent = String(rows.length);
    }
    const mine = me?.auth === "session"
      ? issueCache.filter(issue => issue.state === "review"
          && (me.role === "admin" || issue.reviewer === me.name || issue.approver === me.name))
      : [];
    const inbox = $("issue-inbox");
    const list = $("issue-inbox-list");
    list.replaceChildren();
    mine.forEach(issue => list.append(issueCard(issue)));
    $("issue-inbox-count").textContent = String(mine.length);
    inbox.hidden = mine.length === 0;
    status("issues-status", mine.length
      ? `${issueCache.length} 条 · ${mine.length} 条待我处理`
      : `${issueCache.length} 条 · 无待办`);
  }

  async function refreshIssues() {
    try {
      await loadMe();
      const data = await api("/api/issues");
      issueCache = Array.isArray(data.issues) ? data.issues : [];
      renderIssueBoard();
    } catch (error) {
      status("issues-status", "需要连接后读取 Issue 队列。", true);
    }
  }

  function checkItem(item) {
    const li = text("li", "");
    li.className = item.ok ? "check-ok" : "check-bad";
    li.textContent = `${item.type} · ${item.path} · ${item.ok ? "通过" : (item.detail || "失败")}`;
    return li;
  }

  async function openIssueDetail(id) {
    try {
      const issue = await api("/api/issues/" + encodeURIComponent(id));
      openIssue = issue;
      $("issue-drawer-title").textContent = issue.title || issue.id;
      $("issue-detail-meta").textContent =
        `${issue.id} · assignee ${issue.assignee || "—"} · reviewer ${issue.reviewer || "—"} · approver ${issue.approver || "—"}`
        + ` · attempt ${issue.attempts || 0}/${issue.max_attempts || 3}`
        + (issue.result?.usage?.total_tokens != null
          ? ` · tokens ${issue.result.usage.total_tokens}/${issue.max_tokens || 100000}` : "")
        + (issue.result?.usage?.cost_usd != null
          ? ` · $${issue.result.usage.cost_usd}` : " · unpriced")
        + (issue.result?.error ? ` · 最近错误：${issue.result.error}` : "");
      $("issue-detail-state").textContent =
        `${issueLabel(issue.state)}${issue.review_approved ? " · 评审已过" : ""}`;
      $("issue-detail-summary").textContent = issue.result?.summary || "尚无执行结果。";
      $("issue-detail-checks").replaceChildren();
      for (const item of issue.result?.checks || issue.checks.map(check => ({ ...check, ok: false }))) {
        $("issue-detail-checks").append(checkItem(item));
      }
      $("issue-detail-children").replaceChildren();
      for (const child of issue.children || []) {
        const item = text("li", `${child.title || child.id} · ${child.state} · ${child.assignee || "—"}`);
        item.className = "issue-child";
        item.tabIndex = 0;
        const open = () => openIssueDetail(child.id);
        item.addEventListener("click", open);
        item.addEventListener("keydown", event => {
          if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
        });
        $("issue-detail-children").append(item);
      }
      $("issue-detail-artifacts").replaceChildren();
      for (const item of issue.result?.artifacts || []) {
        $("issue-detail-artifacts").append(text("li", `${item.path} · ${item.bytes} B · ${item.sha256}`));
      }
      $("issue-detail-comments").replaceChildren();
      for (const comment of issue.comments || []) {
        $("issue-detail-comments").append(text("li", `${comment.author} · ${comment.body}`));
      }
      $("issue-comment-body").value = "";
      const session = me?.auth === "session";
      const canReview = session && issue.state === "review"
        && (me.role === "admin" || issue.reviewer === me.name);
      const canApprove = session && issue.state === "review"
        && (me.role === "admin" || issue.approver === me.name)
        && (!issue.reviewer || issue.review_approved);
      $("issue-review-approve").hidden = !canReview;
      $("issue-review-reject").hidden = !canReview;
      $("issue-approve").hidden = !canApprove;
      status("issue-action-status", issue.state === "review" && !session
        ? "请以 reviewer/approver 的人类会话登录后评审或签批。"
        : (issue.state === "review" && issue.reviewer && !issue.review_approved
          ? "先由 reviewer 评审通过，再签批。" : ""));
      openDrawer("issue-drawer");
    } catch (error) {
      status("issues-status", error.message, true);
    }
  }

  async function reviewIssue(verdict) {
    if (!openIssue) return;
    try {
      await api(`/api/issues/${encodeURIComponent(openIssue.id)}/review`, {
        method: "POST",
        body: JSON.stringify({ verdict, comment: $("issue-comment-body").value }),
      });
      await refreshIssues();
      await openIssueDetail(openIssue.id);
      status("issue-action-status", verdict === "approve" ? "评审已通过。" : "已驳回，Issue 回到待领。");
    } catch (error) {
      status("issue-action-status", error.message, true);
    }
  }

  async function approveIssue() {
    if (!openIssue) return;
    try {
      await api(`/api/issues/${encodeURIComponent(openIssue.id)}/approve`, { method: "POST", body: "{}" });
      await refreshIssues();
      await openIssueDetail(openIssue.id);
      status("issue-action-status", "签批通过，Issue 已完成。");
    } catch (error) {
      status("issue-action-status", error.message, true);
    }
  }

  async function commentIssue() {
    if (!openIssue) return;
    const body = $("issue-comment-body").value.trim();
    if (!body) { status("issue-action-status", "请先写评论。", true); return; }
    try {
      await api(`/api/issues/${encodeURIComponent(openIssue.id)}/comment`, {
        method: "POST", body: JSON.stringify({ body }),
      });
      await refreshIssues();
      await openIssueDetail(openIssue.id);
      status("issue-action-status", "评论已写入。");
    } catch (error) {
      status("issue-action-status", error.message, true);
    }
  }

  const POLICY_ROLES = [["taizi", "太子"], ["zhongshu", "中书"], ["menxia", "门下"],
    ["shangshu", "尚书"], ["gongbu", "工部"]];

  async function refreshPolicy() {
    const body = $("policy-role-rows");
    if (!body) return;
    body.replaceChildren();
    try {
      const data = await api("/api/issue-policy");
      $("policy-max-tokens").value = data.max_tokens_per_issue || "";
      $("policy-max-calls").value = data.max_calls_per_issue || "";
      $("policy-monthly-tokens").value = data.monthly_tokens || 0;
      $("policy-max-cost").value = data.max_cost_usd_per_issue || 0;
      $("policy-monthly-cost").value = data.monthly_cost_usd || 0;
      for (const [role, label] of POLICY_ROLES) {
        const tr = text("tr", "");
        tr.append(text("td", label));
        for (const [key, source] of [["tokens", "role_max_tokens"], ["calls", "role_max_calls"],
                                     ["monthly", "monthly_tokens_per_role"],
                                     ["cost", "role_max_cost_usd"],
                                     ["monthlyCost", "monthly_cost_usd_per_role"]]) {
          const td = text("td", "");
          const input = document.createElement("input");
          input.type = "number";
          input.min = key === "tokens" ? "1000" : "1";
          input.max = key === "tokens" || key === "monthly" ? "10000000"
            : (key === "cost" || key === "monthlyCost" ? "1000000" : "8");
          input.step = key === "cost" || key === "monthlyCost" ? "0.0001" : "1";
          input.className = "policy-role-" + key;
          input.dataset.role = role;
          input.value = data[source]?.[role] || "";
          input.placeholder = "继承默认";
          td.append(input);
          tr.append(td);
        }
        body.append(tr);
      }
      status("policy-status", "已读取；仅 admin 可保存。");
    } catch (error) {
      status("policy-status", "需要 admin 会话才能读取或保存预算策略。", true);
    }
  }

  async function savePolicy() {
    const payload = {
      max_tokens_per_issue: Number($("policy-max-tokens").value) || 100000,
      max_calls_per_issue: Number($("policy-max-calls").value) || 4,
      monthly_tokens: Number($("policy-monthly-tokens").value) || 0,
      max_cost_usd_per_issue: Number($("policy-max-cost").value) || 0,
      monthly_cost_usd: Number($("policy-monthly-cost").value) || 0,
      role_max_tokens: {}, role_max_calls: {}, monthly_tokens_per_role: {},
      role_max_cost_usd: {}, monthly_cost_usd_per_role: {},
    };
    for (const input of $("policy-role-rows").querySelectorAll(".policy-role-tokens")) {
      const value = Number(input.value);
      if (value) payload.role_max_tokens[input.dataset.role] = value;
    }
    for (const input of $("policy-role-rows").querySelectorAll(".policy-role-calls")) {
      const value = Number(input.value);
      if (value) payload.role_max_calls[input.dataset.role] = value;
    }
    for (const input of $("policy-role-rows").querySelectorAll(".policy-role-monthly")) {
      const value = Number(input.value);
      if (value) payload.monthly_tokens_per_role[input.dataset.role] = value;
    }
    for (const input of $("policy-role-rows").querySelectorAll(".policy-role-cost")) {
      const value = Number(input.value);
      if (value) payload.role_max_cost_usd[input.dataset.role] = value;
    }
    for (const input of $("policy-role-rows").querySelectorAll(".policy-role-monthlyCost")) {
      const value = Number(input.value);
      if (value) payload.monthly_cost_usd_per_role[input.dataset.role] = value;
    }
    try {
      await api("/api/issue-policy", { method: "PUT", body: JSON.stringify(payload) });
      await refreshPolicy();
      status("policy-status", "预算策略已保存。");
    } catch (error) {
      status("policy-status", error.message, true);
    }
  }

  async function refreshPricing() {
    try {
      const data = await api("/api/pricing");
      $("pricing-json").value = JSON.stringify(data.models || {}, null, 2);
      status("pricing-status", "已读取；未配置的模型会显示 unpriced。");
    } catch {
      status("pricing-status", "需要 admin 会话才能读取或保存模型价格。", true);
    }
  }

  async function savePricing() {
    try {
      const models = JSON.parse($("pricing-json").value || "{}");
      await api("/api/pricing", { method: "PUT", body: JSON.stringify({ models }) });
      await refreshPricing();
      status("pricing-status", "模型价格已保存。");
    } catch (error) {
      status("pricing-status", error.message || "保存失败。", true);
    }
  }

  async function createCloudTask() {
    const title = $("dispatch-title").value.trim();
    const assignee = $("dispatch-assignee").value.trim() || "gongbu";
    const launcher = $("dispatch-launcher").value;
    if (!title) { status("dispatch-status", "标题必填。", true); return; }
    if (!launcher) { status("dispatch-status", "还没有已连接 Launcher。", true); return; }
    try {
      const issue = await api("/api/issues", {
        method: "POST",
        body: JSON.stringify({ title, assignee, body: $("dispatch-body").value,
                               launcher_id: launcher, max_tokens: 50000 }),
      });
      $("dispatch-status").textContent = `已创建 ${issue.id}，等待 Launcher 领取。`;
      $("dispatch-title").value = ""; $("dispatch-body").value = "";
    } catch (error) {
      status("dispatch-status", error.message, true);
    }
  }

  async function refreshLauncherStatus() {
    try {
      const data = await api("/api/launcher");
      $("launcher-status").textContent = data.paired
        ? `已连接 ${data.cloud_url} · ${data.name} · 上次心跳 ${data.last_heartbeat || "无"}`
        : "未连接云空间。";
    } catch {
      $("launcher-status").textContent = "需要人类会话才能连接云空间。";
    }
  }

  async function loginLauncher() {
    let cloud = $("launcher-cloud-url").value.trim();
    if (cloud && !/^https?:\/\//.test(cloud)) cloud = "https:" + "//" + cloud;
    try {
      const data = await api("/api/launcher/login", {
        method: "POST",
        body: JSON.stringify({
          cloud_url: cloud,
          username: $("launcher-username").value.trim(),
          password: $("launcher-password").value,
          name: $("launcher-name").value.trim() || "Hengjian Launcher",
        }),
      });
      $("launcher-status").textContent = `已连接 ${data.cloud_url} · ${data.name}`;
      $("launcher-password").value = "";
    } catch (error) {
      status("launcher-status", error.message, true);
    }
  }

  async function pairLauncher() {
    try {
      let cloud = $("launcher-cloud-url").value.trim();
      if (cloud && !/^https?:\/\//.test(cloud)) cloud = "https:" + "//" + cloud;
      const data = await api("/api/launcher/pair", {
        method: "POST",
        body: JSON.stringify({
          cloud_url: cloud,
          code: $("launcher-code").value.trim(),
          name: $("launcher-name").value.trim() || "Hengjian Launcher",
        }),
      });
      $("launcher-status").textContent = `已连接 ${data.cloud_url} · ${data.name}`;
    } catch (error) {
      status("launcher-status", error.message, true);
    }
  }

  async function disconnectLauncher() {
    try {
      await api("/api/launcher/disconnect", { method: "POST", body: "{}" });
      await refreshLauncherStatus();
    } catch (error) { status("launcher-status", error.message, true); }
  }

  async function generateLauncherCode() {
    try {
      const data = await api("/api/launchers/pair", { method: "POST", body: "{}" });
      $("launcher-code-display").textContent = `配对码：${data.code}（${Math.round(data.ttl / 60)} 分钟内有效）`;
      await refreshLaunchers();
    } catch (error) { status("launchers-status", error.message, true); }
  }

  async function executeCloudTask(issueId) {
    try {
      status("cloud-tasks-status", "执行中…");
      const data = await api("/api/launcher/cloud-tasks/execute", {
        method: "POST", body: JSON.stringify({ issue_id: issueId || "" }),
      });
      const result = data.result || {};
      status("cloud-tasks-status", result.status === "done"
        ? `完成 ${result.issue || ""}` : `未完成 ${result.status || "unknown"}`);
      await refreshCloudTasks();
    } catch (error) {
      status("cloud-tasks-status", error.message, true);
    }
  }

  async function refreshCloudTasks() {
    const list = $("cloud-tasks-list");
    list.replaceChildren();
    try {
      const data = await api("/api/launcher/cloud-tasks");
      for (const task of data.tasks || []) {
        const item = text("li", "");
        item.append(text("strong", task.title || task.id), text("div", `${task.id} · ${task.assignee || ""}`));
        if (task.body) item.append(text("div", String(task.body).slice(0, 300)));
        const execute = text("button", "执行");
        execute.type = "button"; execute.className = "btn btn-quiet btn-small";
        execute.addEventListener("click", () => executeCloudTask(task.id));
        item.append(execute);
        list.append(item);
      }
      status("cloud-tasks-status", (data.tasks || []).length ? `${data.tasks.length} 个待领取任务` : "没有待领取任务。");
    } catch (error) {
      status("cloud-tasks-status", error.message, true);
    }
  }

  async function refreshLaunchers() {
    const list = $("launchers-list");
    list.replaceChildren();
    try {
      const data = await api("/api/launchers");
      const select = $("dispatch-launcher");
      if (select) {
        const previous = select.value;
        select.replaceChildren();
        for (const item of data.launchers || []) {
          const option = document.createElement("option");
          option.value = item.id;
          option.textContent = `${item.name}${item.host ? " · " + item.host : ""}`;
          select.append(option);
        }
        if (previous) select.value = previous;
      }
      for (const item of data.launchers || []) {
        const row = text("li", `${item.name} · ${item.host || "未知主机"} · ${item.last_seen || "从未心跳"}`);
        if (me?.auth === "session" && me.role === "admin") {
          const remove = text("button", "移除");
          remove.type = "button"; remove.className = "btn btn-quiet btn-small";
          remove.addEventListener("click", async () => {
            try {
              await api(`/api/launchers/${encodeURIComponent(item.id)}`, { method: "DELETE" });
              await refreshLaunchers();
            } catch (error) { status("launchers-status", error.message, true); }
          });
          row.append(text("span", " "), remove);
        }
        list.append(row);
      }
      status("launchers-status", (data.launchers || []).length ? `${data.launchers.length} 个 Launcher` : "还没有 Launcher。");
    } catch {
      status("launchers-status", "需要云空间账户才能查看 Launcher。", true);
    }
  }

  async function refreshWorkspaces() {
    const select = $("workspace-select");
    try {
      const data = await api("/api/workspaces");
      select.replaceChildren();
      for (const item of data.workspaces || []) {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = `${item.name} · ${item.role}`;
        select.append(option);
      }
      if (me?.workspace) select.value = me.workspace;
      status("workspace-status", (data.workspaces || []).length
        ? `${data.workspaces.length} 个 Workspace` : "没有 Workspace");
    } catch {
      status("workspace-status", "需要登录后管理 Workspace。", true);
    }
  }

  async function switchWorkspace() {
    try {
      await api("/api/workspace/switch", {
        method: "POST", body: JSON.stringify({ workspace: $("workspace-select").value }),
      });
      location.reload();
    } catch (error) { status("workspace-status", error.message, true); }
  }

  async function joinWorkspace() {
    try {
      await api("/api/workspace/join", {
        method: "POST", body: JSON.stringify({ invite: $("workspace-join-code").value.trim() }),
      });
      location.reload();
    } catch (error) { status("workspace-status", error.message, true); }
  }

  async function leaveWorkspace() {
    try {
      await api("/api/workspace/leave", { method: "POST", body: "{}" });
      location.reload();
    } catch (error) { status("workspace-status", error.message, true); }
  }

  async function setMemberRole(name, role) {
    try {
      await api(`/api/workspace/members/${encodeURIComponent(name)}/role`, {
        method: "POST", body: JSON.stringify({ role }),
      });
      await refreshMembers();
    } catch (error) { status("members-status", error.message, true); }
  }

  async function transferWorkspaceOwner(name) {
    if (!confirm(`把 Workspace owner 转让给 ${name}？`)) return;
    try {
      await api("/api/workspace/transfer", {
        method: "POST", body: JSON.stringify({ member: name }),
      });
      location.reload();
    } catch (error) { status("members-status", error.message, true); }
  }

  async function refreshMembers() {
    const list = $("members-list");
    if (!list) return;
    list.replaceChildren();
    try {
      const data = await api("/api/workspace/members");
      const selfMember = (data.members || []).find(member => member.name === me?.name);
      const selfRole = selfMember?.workspace_role;
      const canManage = me?.auth === "session" && (me.role === "admin" || selfRole === "owner" || selfRole === "admin");
      const isOwner = data.workspace?.owner === me?.name;
      for (const member of data.members || []) {
        const item = text("li", "");
        item.append(text("strong", member.name), text("span", ` · ${member.role}${member.workspace_role ? " / " + member.workspace_role : ""}${member.owner ? " · owner" : ""}`));
        if (canManage && member.name !== me.name) {
          const remove = text("button", "移除");
          remove.type = "button";
          remove.className = "btn btn-quiet btn-small";
          remove.addEventListener("click", async () => {
            try {
              await api(`/api/members/${encodeURIComponent(member.name)}`, { method: "DELETE" });
              await refreshMembers();
            } catch (error) { status("members-status", error.message, true); }
          });
          item.append(text("span", " "), remove);
        }
        if (canManage && !member.owner) {
          const admin = text("button", "设为 admin");
          admin.type = "button"; admin.className = "btn btn-quiet btn-small";
          admin.addEventListener("click", () => setMemberRole(member.name, "admin"));
          const plain = text("button", "设为 member");
          plain.type = "button"; plain.className = "btn btn-quiet btn-small";
          plain.addEventListener("click", () => setMemberRole(member.name, "member"));
          const transfer = text("button", "转让 owner");
          transfer.type = "button"; transfer.className = "btn btn-quiet btn-small";
          transfer.addEventListener("click", () => transferWorkspaceOwner(member.name));
          item.append(text("span", " "), admin, plain);
          if (isOwner) item.append(transfer);
        }
        list.append(item);
      }
      status("members-status", `${(data.members || []).length} 名成员 · ${data.workspace?.name || ""} · owner ${data.workspace?.owner || "未设置"}`);
    } catch {
      status("members-status", "需要 admin 会话才能管理成员。", true);
    }
  }

  async function createWorkspaceInvite() {
    try {
      const data = await api("/api/workspaces/invite", { method: "POST", body: "{}" });
      $("member-invite-code").textContent =
        `邀请码：${data.code}（7 天内有效，一次性；注册时填写即加入当前 Workspace）`;
    } catch (error) { status("members-status", error.message, true); }
  }

  async function addMember() {
    const name = $("member-name").value.trim();
    const password = $("member-password").value;
    const role = $("member-role").value;
    if (!name || password.length < 8) {
      status("members-status", "用户名必填，密码至少 8 位。", true);
      return;
    }
    try {
      await api("/api/members", { method: "POST", body: JSON.stringify({ username: name, password, role }) });
      $("member-name").value = ""; $("member-password").value = "";
      await refreshMembers();
      status("members-status", "成员已新增。");
    } catch (error) { status("members-status", error.message, true); }
  }

  async function refreshAlerts() {
    const list = $("alerts-list");
    if (!list) return;
    list.replaceChildren();
    try {
      const data = await api("/api/alerts");
      for (const alert of data.alerts || []) {
        const item = text("li", "");
        item.append(text("strong", alert.title), text("div", `${alert.agent} · ${alert.at}`));
        if (alert.ref) item.append(text("div", `Issue: ${alert.ref}`));
        item.append(text("div", alert.body));
        if (me?.auth === "session") {
          const button = text("button", "确认");
          button.type = "button";
          button.className = "btn btn-quiet btn-small";
          button.addEventListener("click", async () => {
            try {
              await api(`/api/alerts/${alert.id}/ack`, { method: "POST", body: "{}" });
              await refreshAlerts();
            } catch (error) { status("alerts-status", error.message, true); }
          });
          item.append(button);
        }
        list.append(item);
      }
      status("alerts-status", (data.alerts || []).length ? `${data.alerts.length} 条未确认告警` : "无未确认告警。");
    } catch (error) {
      status("alerts-status", "需要连接后读取告警。", true);
    }
  }

  async function refreshBindings() {
    const captured = bindingEpoch;
    try {
      const data = await api("/api/agents");
      if (captured !== bindingEpoch) return;
      $("bindings-rows").replaceChildren();
      const recommendations = $("binding-recommendations");
      if (recommendations) {
        recommendations.replaceChildren();
        if (Array.isArray(data.recommendations) && data.recommendations.length) {
          const title = text("p", "推荐配置（按最强能力 + 独立审查 + 成本）");
          title.className = "fineprint";
          recommendations.append(title);
          for (const row of data.recommendations) {
            const line = text("p", `${row.label || ACTORS[row.role] || row.role}：${row.agent} / ${row.model || "agent 默认模型"}`);
            line.className = "binding-recommendation";
            line.title = row.reason || "";
            recommendations.append(line);
            if (row.reason) {
              const why = text("span", row.reason);
              why.className = "binding-recommendation-why";
              recommendations.append(why);
            }
          }
        }
      }
      if (!data.agents || !data.catalog) {
        // Compatibility with older hosts/tests that predate the agent catalog.
        for (const row of data.preset) {
          const tr = text("tr", "");
          tr.append(text("td", ACTORS[row.role] || row.role),
            text("td", `${row.agent} / ${row.model || "agent 默认模型"}`),
            (() => {
              const state = text("td", row.found ? "已就绪" : "缺失");
              if (row.found) state.title = row.path || "";
              return state;
            })());
          const action = text("td", "");
          const capability = data.installability[row.agent];
          if (!row.found && capability?.installable) {
            const button = text("button", "一键安装");
            button.type = "button";
            button.disabled = installing;
            button.addEventListener("click", () => installAgent(row.agent));
            action.append(button);
          } else if (!row.found && capability?.docs) {
            const link = text("a", "官方文档");
            link.href = capability.docs;
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            action.append(text("span", "需手动安装 · "), link);
          }
          tr.append(action);
          $("bindings-rows").append(tr);
        }
        $("save-bindings").disabled = true;
      } else {
      const facts = data.agents || {};
      const integrated = (Array.isArray(data.catalog) ? data.catalog : []).filter(entry => entry.integrated);
      const available = integrated.filter(entry => facts[entry.id]?.found);
      const current = data.binding?.bindings || {};
      const cliNames = new Set(["gemini", "opencode", "openclaw", "qwen", "copilot"]);
      for (const row of data.preset) {
        const tr = text("tr", "");
        tr.append(text("td", ACTORS[row.role] || row.role));
        const agentCell = text("td", "");
        const select = document.createElement("select");
        select.dataset.role = row.role;
        select.className = "binding-agent";
        const selectedAgent = current[row.role]?.agent || row.agent;
        for (const entry of available) {
          const option = document.createElement("option");
          option.value = entry.id;
          option.textContent = entry.id;
          option.selected = entry.id === selectedAgent;
          select.append(option);
        }
        if (!available.some(entry => entry.id === selectedAgent)) {
          const option = document.createElement("option");
          option.value = selectedAgent;
          option.textContent = selectedAgent + "（未就绪）";
          option.selected = true;
          select.append(option);
        }
        agentCell.append(select);
        const modelCell = text("td", "");
        const model = document.createElement("input");
        model.dataset.role = row.role;
        model.className = "binding-model";
        model.value = current[row.role]?.model || row.model || "default";
        model.placeholder = "模型或 default";
        const choices = document.createElement("datalist");
        choices.id = "binding-model-list-" + row.role;
        model.setAttribute("list", choices.id);
        modelCell.append(model, choices);
        const fillChoices = async () => {
          choices.replaceChildren();
          const entry = integrated.find(item => item.id === select.value);
          let rows = Array.isArray(entry?.models) ? entry.models : [];
          if (!rows.length && ["codex", "pi"].includes(select.value)) {
            try {
              const data = await api("/api/agents/models", {method: "POST", body: JSON.stringify({agent: select.value})});
              rows = data.models || [];
            } catch { return; }
          }
          for (const item of rows) {
            const option = document.createElement("option");
            option.value = item.id;
            option.label = item.label || item.id;
            choices.append(option);
          }
          if (!model.value.trim() && rows.length) model.value = rows[0].id;
        };
        fillChoices();
        const describe = name => {
          const health = data.health?.[name] || {};
          const label = health.state === "ready" ? "就绪"
            : health.state === "login_required" ? "需登录"
              : health.state === "not_configured" ? "未配置"
                : health.state === "not_installed" ? "未安装" : "不可用";
          return { label, title: [health.message, health.next_step].filter(Boolean).join("；") };
        };
        const state = text("td", describe(selectedAgent).label);
        state.title = describe(selectedAgent).title;
        const probe = text("button", "实测");
        probe.type = "button";
        probe.className = "btn";
        probe.title = "会真实调用一次模型，消耗少量 token；每小时最多 5 次";
        probe.addEventListener("click", async () => {
          probe.disabled = true;
          status("bindings-status", `${row.role}：实测中（会消耗少量 token）…`);
          try {
            const result = await api("/api/agents/live-probe", {
              method: "POST",
              body: JSON.stringify({agent: select.value, model: model.value.trim() || undefined}),
            });
            status("bindings-status", `实测通过 · ${result.model} · ${result.reply} · ${result.elapsed_sec}s`);
          } catch (error) {
            status("bindings-status", `实测失败：${error.message}`, true);
          } finally { probe.disabled = false; }
        });
        state.append(probe);
        select.addEventListener("change", () => {
          if (cliNames.has(select.value)) model.value = "default";
          fillChoices();
          const next = describe(select.value);
          state.textContent = next.label;
          state.title = next.title;
        });
        tr.append(agentCell, modelCell, state);
        $("bindings-rows").append(tr);
      }
      $("save-bindings").disabled = installing || !available.length;
      }

      $("apply-preset").disabled = installing;
      $("bindings-current").hidden = false;
      $("bindings-current").textContent = JSON.stringify(data.binding, null, 2);
      status("bindings-status", "预设仅绑定已就绪角色；缺失角色不会写入绑定。");
    } catch (error) {
      if (captured === bindingEpoch) {
        $("bindings-rows").replaceChildren();
        $("bindings-current").hidden = true;
        $("apply-preset").disabled = true;
        $("save-bindings").disabled = true;
        status("bindings-status", `读取失败：${error.message}`, true);
      }
    }
  }
  async function installAgent(agent) {
    if (installing) return;
    installing = true;
    const captured = bindingEpoch;
    const box = $("install-progress");
    box.hidden = false;
    box.textContent = `${agent} · 正在启动安装…`;
    $("apply-preset").disabled = true;
    for (const button of $("bindings-rows").querySelectorAll("button")) button.disabled = true;
    try {
      const job = await api("/api/agents/install", { method: "POST", body: JSON.stringify({ agent }) });
      async function pollInstall() {
        if (captured !== bindingEpoch) return;
        try {
          const state = await api(`/api/agents/install/${encodeURIComponent(job.job)}`);
          if (captured !== bindingEpoch) return;
          box.textContent = `${agent} · ${state.status} · exit ${state.exit_code ?? "—"}\njob ${job.job}\n${state.tail}`;
          if (state.status === "running") installTimer = setTimeout(pollInstall, 1000);
          else { installing = false; await refreshBindings(); }
        } catch (error) {
          if (captured === bindingEpoch) {
            installing = false;
            box.textContent = `进度读取失败：${error.message} 安装可能仍在后台运行，请连接刷新后核查。`;
            await refreshBindings();
          }
        }
      }
      await pollInstall();
    } catch (error) {
      if (captured === bindingEpoch) {
        installing = false;
        box.textContent = `安装失败：${error.message}`;
        await refreshBindings();
      }
    }
  }
  $("save-bindings").addEventListener("click", async () => {
    const captured = bindingEpoch;
    const bindings = {};
    for (const row of $("bindings-rows").querySelectorAll(".binding-agent")) {
      const role = row.dataset.role;
      const model = $("bindings-rows").querySelector(`.binding-model[data-role="${role}"]`)?.value.trim();
      if (!model) { status("bindings-status", "模型不能为空", true); return; }
      bindings[role] = { agent: row.value, model };
    }
    if (Object.keys(bindings).length !== 5) { status("bindings-status", "请先补齐五个角色", true); return; }
    try {
      const result = await api("/api/agents/bind", { method: "POST", body: JSON.stringify({ mode: "multi", bindings }) });
      if (captured !== bindingEpoch) return;
      await refreshBindings();
      status("bindings-status", `已保存自定义绑定${result.warnings?.length ? "；" + result.warnings.join("；") : ""}`);
    } catch (error) {
      if (captured === bindingEpoch) status("bindings-status", `保存失败：${error.message}`, true);
    }
  });
  $("env-agent").addEventListener("change", () => {
    const values = envValues[$("env-agent").value] || {};
    $("env-base").value = values.LLM_BASE_URL || "";
    $("env-model").value = values.LLM_MODEL || "";
    $("env-key").value = "";
    status("env-status", values.LLM_API_KEY === "***" ? "已保存 Key；留空保持不变" : "");
  });
  $("env-save").addEventListener("click", async () => {
    const agent = $("env-agent").value;
    if (!agent) { status("env-status", "请选择 Agent", true); return; }
    const values = {
      LLM_BASE_URL: $("env-base").value.trim(),
      LLM_MODEL: $("env-model").value.trim(),
    };
    if ($("env-key").value.trim()) values.LLM_API_KEY = $("env-key").value.trim();
    status("env-status", "保存中…");
    try {
      await api("/api/agents/env", {method: "POST", body: JSON.stringify({agent, values})});
      $("env-key").value = "";
      await refreshEnv();
      status("env-status", "Agent 环境已保存。");
    } catch (error) { status("env-status", `保存失败：${error.message}`, true); }
  });
  $("add-model").addEventListener("click", openModelForm);
  $("model-cancel").addEventListener("click", closeModelForm);
  $("model-provider").addEventListener("change", syncModelProvider);
  $("model-test").addEventListener("click", async () => {
    status("model-status", "正在验证…");
    try {
      const result = await api("/api/models/test", {method: "POST", body: JSON.stringify(modelPayload())});
      modelProbe = Array.isArray(result.models) ? result.models : [];
      const options = $("model-options");
      options.replaceChildren();
      for (const item of modelProbe) {
        const option = document.createElement("option");
        option.value = item.id;
        option.label = item.label || item.id;
        options.append(option);
      }
      if (modelProbe.length && !$("model-name-id").value.trim()) $("model-name-id").value = modelProbe[0].id;
      if (result.ok) status("model-status", `Key 可用，共 ${modelProbe.length} 个模型`);
      else if (result.keyOk === null) status("model-status", `暂无法确认 Key：${result.error || "端点未返回模型列表"}，可先保存`, true);
      else status("model-status", `验证失败：${result.error || result.status}`, true);
    } catch (error) { status("model-status", `验证失败：${error.message}`, true); }
  });
  $("model-save").addEventListener("click", async () => {
    status("model-status", "正在保存…");
    try {
      const payload = modelPayload();
      await api("/api/models", {method: "POST", body: JSON.stringify(payload)});
      closeModelForm();
      await refreshModels();
      await refreshBindings();
      status("bindings-status", "模型访问已保存，可在角色绑定中选择。");
    } catch (error) { status("model-status", `保存失败：${error.message}`, true); }
  });
  $("policy-save").addEventListener("click", savePolicy);
  $("pricing-save").addEventListener("click", savePricing);
  $("workspace-switch").addEventListener("click", switchWorkspace);
  $("workspace-join").addEventListener("click", joinWorkspace);
  $("workspace-leave").addEventListener("click", leaveWorkspace);
  $("member-add").addEventListener("click", addMember);
  $("member-invite").addEventListener("click", createWorkspaceInvite);
  $("launcher-pair").addEventListener("click", pairLauncher);
  $("launcher-login").addEventListener("click", loginLauncher);
  $("launcher-disconnect").addEventListener("click", disconnectLauncher);
  $("launcher-code-generate").addEventListener("click", generateLauncherCode);
  $("cloud-tasks-refresh").addEventListener("click", refreshCloudTasks);
  $("cloud-tasks-execute").addEventListener("click", () => executeCloudTask(""));
  $("dispatch-create").addEventListener("click", createCloudTask);
  $("agent-lifecycle-save").addEventListener("click", saveAgentLifecycle);
  $("agent-lifecycle-start").addEventListener("click", () => lifecycleAction("start"));
  $("agent-lifecycle-stop").addEventListener("click", () => lifecycleAction("stop"));
  $("agent-lifecycle-restart").addEventListener("click", () => lifecycleAction("restart"));
  $("issue-comment-save").addEventListener("click", commentIssue);
  $("issue-review-approve").addEventListener("click", () => reviewIssue("approve"));
  $("issue-review-reject").addEventListener("click", () => reviewIssue("reject"));
  $("issue-approve").addEventListener("click", approveIssue);

  $("apply-preset").addEventListener("click", async () => {
    const captured = bindingEpoch;
    $("apply-preset").disabled = true;
    try {
      const result = await api("/api/agents/bind", { method: "POST", body: "{}" });
      if (captured !== bindingEpoch) return;
      await refreshBindings();
      status("bindings-status", `预设已应用；缺失角色：${result.missing.join("、") || "无"}`);
    } catch (error) {
      if (captured === bindingEpoch) status("bindings-status", `应用失败：${error.message}`, true);
    } finally { if (captured === bindingEpoch) $("apply-preset").disabled = installing; }
  });
  function saveBlob(blob, name) {
    const url = URL.createObjectURL(blob);
    const link = text("a", "下载");
    link.href = url;
    link.download = name;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function showRun(raw) {
    const view = renderRun(raw);
    current = view;
    $("detail-empty").hidden = true;
    $("detail-body").hidden = false;
    const title = view.title || taskTitles.get(view.id) || view.id;
    $("detail-status").textContent = title;
    const tone = stateInfo(view.state || view.status);
    const badge = $("detail-badge");
    badge.hidden = false;
    badge.textContent = tone.label;
    badge.dataset.tone = tone.tone;
    badge.className = "chip";
    const guide = $("board-guide");
    if (guide) {
      guide.hidden = true;
      guide.replaceChildren();
      if (view.status === "awaiting_board") {
        guide.hidden = false;
        guide.append(text("h3", "等待签批处签章"));
        guide.append(text("p", "这一步需要人拿着私钥在离线设备上签名，系统不会替你签。"));
        guide.append(text("p", "1) 在主机上生成待签文档；2) 把文档拿到你的设备上执行 board-sign.py；3) 回传签名后运行："));
        const code = text("pre", "fusion approve --request <run>/board-request.json \\\n  --envelope signed-envelope.json \\\n  --home <instance> --board-key-file <public.pem>\n\n# 由指定第二方单独执行：\nfusion confirm --home <instance> --board-key-file <public.pem> \\\n  --task <task_id>");
        guide.append(code);
        const hint = text("p", "签名完成后点「连接 / 刷新」即可看到状态变为 done。");
        guide.append(hint);
      }
    }
    $("run-summary").textContent = `来源：${view.source || "ledger"} / 状态：${view.state || "—"} / ${view.id}`;
    if (!view.timeline.length) status("detail-status", `${view.id} · ${view.status || "incomplete"}；暂无决策记录，可能仍在排队。`);
    if (view.error && typeof view.error === "object") {
      status("detail-status", `${view.id} · ${view.status} · ${view.error.error_type} · ${view.error.message} · ${view.error.at}`, true);
    }
    for (const step of view.timeline) {
      const item = text("li", "");
      item.className = step.verdict || "";
      const mark = text("span", ACTOR_MARK[step.actor] || "令");
      mark.className = "step-mark";
      mark.setAttribute("aria-hidden", "true");
      const body = text("div", "");
      body.className = "step-body";
      const head = text("div", "");
      head.className = "step-head";
      let heading = step.label;
      if (step.review_attempt) heading += ` · 第 ${step.review_attempt} 次审议 · ${step.verdict === "reject" ? "封驳" : "通过"}`;
      if (step.passed_after_revision) heading += " · 修订后通过";
      else if (step.revised) heading += " · 修订后仍未通过";
      head.append(text("h3", heading));
      const time = text("span", fmtTime(step.timestamp));
      time.className = "step-time";
      head.append(time);
      body.append(head);
      const meta = `计划版本 ${step.plan_revision ?? "—"} · 模型尝试 ${step.model_attempt || 1} · ${step.status || "—"}`;
      const metaLine = text("p", meta);
      metaLine.className = "step-meta";
      body.append(metaLine);
      for (const reason of step.reasons || []) body.append(text("p", `理由：${reason}`));
      for (const change of step.required_changes || []) {
        const line = text("p", `修订要求：${change}`);
        line.className = "req";
        body.append(line);
      }
      if (step.error) body.append(text("p", "此步骤失败，未形成有效裁决。"));
      const details = text("details", "");
      details.append(text("summary", "查看决策内容"), text("pre", JSON.stringify(step.decision || {}, null, 2)));
      body.append(details);
      item.append(mark, body);
      $("timeline").append(item);
    }
    for (const artifact of view.artifacts) {
      const item = text("li", "");
      item.append(text("h3", artifact.name), text("p", artifact.path));
      item.append(text("p", `${artifact.bytes ?? "未知"} bytes / SHA-256: ${artifact.sha256 || "未记录"}`));
      const url = safeDownloadURL(artifact.download_url);
      if (url && (!company.base || url.startsWith(company.base + "/"))) {
        const link = text("a", "下载文件");
        link.href = url;
        link.addEventListener("click", async event => {
          event.preventDefault();
          const captured = epoch;
          // api() prefixes paths, so remove the configured prefix exactly once.
          try {
            const blob = await api(url.slice(company.base.length), { binary: true });
            if (captured === epoch) saveBlob(blob, artifact.name);
          } catch (error) { if (captured === epoch) status("artifact-status", error.message, true); }
        });
        item.append(link);
      } else item.append(text("p", "文件下载未配置；绝对路径仅作元数据，不作为公开链接。"));
      if (artifact.previewable) {
        const preview = text("button", "预览内容");
        preview.type = "button";
        preview.addEventListener("click", async () => {
          const box = $("artifact-preview");
          box.hidden = false;
          box.textContent = "加载中…";
          try {
            const data = await api(`/api/artifact?run=${encodeURIComponent(view.id)}&name=${encodeURIComponent(artifact.relative || artifact.name)}`);
            box.textContent = data.text;
          } catch (error) {
            box.textContent = `无法预览：${error.message}`;
          }
        });
        item.append(preview);
      }
      $("artifacts").append(item);
    }
    if (view.artifacts.length) {
      // The ledger only knows the manifest; ask the server for the files inside it.
      const match = /\/runs\/([0-9a-f]{32})\//.exec(view.artifacts[0].path || "");
      if (match) {
        try {
          const listing = await api(`/api/artifacts?run=${match[1]}`);
          if (listing.artifacts.length) {
            const heading = text("li", "");
            heading.className = "docs-heading";
            heading.append(text("h3", "运行产物文件"));
            $("artifacts").append(heading);
            for (const file of listing.artifacts) {
              const item = text("li", "");
              const label = `${file.relative} · ${file.bytes} bytes`;
              const heading = text("p", label);
              if (file.primary) {
                const badge = text("strong", file.attribution === "inferred" ? "主产物（推测）" : "主产物");
                badge.className = "badge";
                heading.prepend(badge, text("span", " "));
              }
              if (file.subtask) heading.append(text("span", ` · 子任务 ${file.subtask}`));
              item.append(heading);
              if (file.previewable) {
                const button = text("button", "预览内容");
                button.type = "button";
                button.addEventListener("click", async () => {
                  const box = $("artifact-preview");
                  box.hidden = false;
                  box.textContent = "加载中…";
                  try {
                    const data = await api(`/api/artifact?run=${match[1]}&name=${encodeURIComponent(file.relative)}`);
                    box.textContent = data.text;
                  } catch (error) { box.textContent = `无法预览：${error.message}`; }
                });
                item.append(button);
              }
              $("artifacts").append(item);
            }
          }
        } catch (error) {
          status("artifact-status", `产物清单读取失败：${error.message}`, true);
        }
      }
    }
    status("artifact-status", view.artifacts.length ? `${view.artifacts.length} 项产物；可导出清单。` : "暂无产物。");
    $("export").disabled = !view.artifacts.length;
  }
  async function selectRun(id) {
    selected = id;
    postBridge({ type: "dutang:selection", id });
    const version = ++detailEpoch;
    const captured = epoch;
    clearDetail();
    for (const button of $("tasks").querySelectorAll("button")) button.setAttribute("aria-current", String(button.dataset.id === id));
    status("detail-status", "加载中，正在读取决策链…");
    try {
      const view = await api(`/api/runs/${encodeURIComponent(id)}`);
      if (captured === epoch && version === detailEpoch) showRun(view);
    } catch (error) {
      if (captured === epoch && version === detailEpoch) status("detail-status", `加载错误：${error.message}`, true);
    }
  }
  async function refreshMerit(captured) {
    const rows = $("merit-rows");
    try {
      const data = await api("/api/merit");
      if (captured !== epoch) return;
      rows.replaceChildren();
      for (const role of data.roles || []) {
        const row = text("div", "");
        row.className = "merit-row";
        row.dataset.badge = role.badge || "";
        const head = text("div", "");
        head.className = "merit-head";
        head.append(text("span", role.name || role.role));
        head.firstChild.className = "merit-name";
        const badge = text("span", role.badge || "—");
        badge.className = "merit-badge";
        badge.dataset.badge = role.badge || "";
        const participated = text("span", `${role.participated ?? 0} 案`);
        participated.className = "merit-participated";
        head.append(badge, participated);
        row.append(head);
        const metrics = (data.metrics || {})[role.role] || {};
        let ratio = null;
        if (role.role === "gongbu" && typeof metrics.success_rate === "number") ratio = metrics.success_rate;
        if (role.role === "zhongshu" && typeof metrics.rework_rate === "number") ratio = Math.max(0, 1 - metrics.rework_rate);
        if (ratio !== null) {
          const track = text("div", "");
          track.className = "merit-bar";
          const fill = text("i", "");
          fill.style.width = `${Math.round(ratio * 100)}%`;
          track.append(fill);
          row.append(track);
        }
        row.append(text("p", role.summary || "—"));
        row.lastChild.className = "merit-summary";
        row.append(text("p", role.reason || ""));
        row.lastChild.className = "merit-reason";
        rows.append(row);
      }
      const stats = data.overall || {};
      const recent = data.recent || {};
      if (typeof stats.completion_rate === "number") {
        $("kpi-success-foot").textContent = `全司办结率 ${(stats.completion_rate * 100).toFixed(0)}%`;
      }
      const gongbu = (data.metrics || {}).gongbu || {};
      if (typeof gongbu.success_rate === "number") {
        $("kpi-success").textContent = `${(gongbu.success_rate * 100).toFixed(0)}%`;
        $("kpi-success-foot").textContent = `执行 ${gongbu.runs ?? 0} · 成 ${gongbu.succeeded ?? 0} / 败 ${gongbu.failed ?? 0}`;
      }
      const doneFoot = $("kpi-done-foot");
      if (doneFoot && typeof recent.done === "number") doneFoot.textContent = `近 ${recent.days ?? 7} 日完成 ${recent.done} · 失败 ${recent.failed_runs ?? 0}`;
      $("merit-status").textContent = `${fmtTime(data.generated_at)} 更新`;
    } catch (error) {
      if (captured !== epoch) return;
      rows.replaceChildren();
      status("merit-status", `功过簿加载失败：${error.message}`, true);
    }
  }

  function renderTaskList() {
    const list = $("tasks");
    list.replaceChildren();
    const showWorker = $("show-worker")?.checked;
    const entries = [...entriesCache.values()].map(task => {
      const failed = ["error", "failed", "blocked"].includes(task.status || task.state);
      const isWorker = /^[0-9a-f]{32}$/.test(task.id) || (task.title === undefined && task.state === undefined);
      return { task, failed, isWorker };
    });
    const visible = entries.filter(({ task, failed, isWorker }) => {
      if (!showWorker && (isWorker || failed)) return false;
      const state = task.status || task.state || "";
      if (filterState === "open") return !["Done", "Cancelled"].includes(state);
      if (filterState === "done") return state === "Done";
      return true;
    });
    for (const { task, isWorker } of visible) {
      const li = text("li", "");
      const button = text("button", "");
      button.type = "button";
      button.className = "case";
      button.dataset.id = task.id;
      button.setAttribute("aria-current", String(task.id === selected));
      const top = text("span", "");
      top.className = "case-top";
      const info = stateInfo(task.state || task.status);
      const chip = text("span", info.label);
      chip.className = "chip";
      chip.dataset.tone = isWorker ? "off" : info.tone;
      const id = text("span", String(task.id || "").slice(0, 18));
      id.className = "case-id";
      const time = text("span", fmtTime(task.updatedAt || task.createdAt));
      time.className = "case-time";
      top.append(chip, id, time);
      const title = text("span", isWorker
        ? `工部运行 ${String(task.id || "").slice(0, 8)}`
        : (task.title || task.payload?.title || task.id));
      title.className = "case-title";
      button.append(top, title);
      if (isWorker && task.status) {
        const sub = text("span", `状态 ${task.status}`);
        sub.className = "case-sub";
        button.append(sub);
      }
      button.addEventListener("click", () => selectRun(task.id));
      li.append(button);
      list.append(li);
    }
    const empty = $("tasks-empty");
    empty.hidden = visible.length > 0;
    if (visible.length && selected) {
      for (const button of list.querySelectorAll("button")) {
        button.setAttribute("aria-current", String(button.dataset.id === selected));
      }
    }
  }

  function updateKpis(tasks) {
    const total = tasks.length;
    const done = tasks.filter(task => (task.state || task.status) === "Done").length;
    const cancelled = tasks.filter(task => (task.state || task.status) === "Cancelled").length;
    const running = tasks.filter(task => ["Running", "Doing"].includes(task.state || task.status)).length;
    const waiting = tasks.filter(task => ["Review", "PendingConfirm", "Menxia"].includes(task.state || task.status)).length;
    $("kpi-total").textContent = total;
    $("kpi-total-foot").textContent = cancelled ? `其中撤销 ${cancelled}` : "在册案卷";
    $("kpi-open").textContent = Math.max(0, total - done - cancelled);
    $("kpi-open-foot").textContent = `执行 ${running} · 待复核 ${waiting}`;
    $("kpi-done").textContent = done;
    if (!done) $("kpi-done-foot").textContent = "暂无办结";
  }

  let modelProviders = [];
  let modelProbe = [];
  function syncModelProvider() {
    const select = $("model-provider");
    const preset = modelProviders.find(entry => entry.id === select.value);
    if (!preset) return;
    $("model-protocol").value = preset.protocol;
    if (preset.base_url) $("model-base").value = preset.base_url;
    if (!preset.base_url) $("model-base").value = "";
    const link = $("model-key-link");
    link.hidden = !preset.key_url;
    if (preset.key_url) link.href = preset.key_url;
    if (!$("model-name").value.trim()) $("model-name").value = preset.label;
  }
  function fillModelProviders(data) {
    modelProviders = data.providers || [];
    const select = $("model-provider");
    const selected = select.value;
    select.replaceChildren();
    for (const group of data.groups || []) {
      const entries = modelProviders.filter(entry => entry.group === group.id);
      if (!entries.length) continue;
      const optgroup = document.createElement("optgroup");
      optgroup.label = group.title;
      for (const entry of entries) {
        const option = document.createElement("option");
        option.value = entry.id;
        option.textContent = entry.label;
        optgroup.append(option);
      }
      select.append(optgroup);
    }
    if (selected && modelProviders.some(entry => entry.id === selected)) select.value = selected;
    syncModelProvider();
  }
  let envAgents = [];
  let envValues = {};
  function fillEnvAgents(catalog) {
    const select = $("env-agent");
    if (!select) return;
    const selected = select.value;
    envAgents = (catalog || []).filter(entry => entry.integrated);
    select.replaceChildren();
    for (const entry of envAgents) {
      const option = document.createElement("option");
      option.value = entry.id;
      option.textContent = entry.title || entry.id;
      select.append(option);
    }
    if (selected && envAgents.some(entry => entry.id === selected)) select.value = selected;
    const values = envValues[select.value] || {};
    $("env-base").value = values.LLM_BASE_URL || "";
    $("env-model").value = values.LLM_MODEL || "";
    $("env-key").value = "";
    status("env-status", values.LLM_API_KEY === "***" ? "已保存 Key；留空保持不变" : "");
  }
  async function refreshEnv() {
    if (!$("env-agent") || !$("token").value.trim()) return;
    try {
      const [agents, env] = await Promise.all([api("/api/agents"), api("/api/agents/env")]);
      envValues = env.agents || {};
      fillEnvAgents(agents.catalog);
    } catch { /* admin-only */ }
  }

  async function loadProviders() {
    try {
      fillModelProviders(await api("/api/models/providers", { public: true }));
    } catch { /* provider metadata is optional until the dialog can load */ }
  }
  async function refreshModels() {
    const list = $("model-list");
    if (!list || !$("token").value.trim()) return;
    list.replaceChildren();
    await loadProviders();
    try {
      const data = await api("/api/models");
      for (const row of data.models || []) {
        const li = text("li", "");
        const count = Array.isArray(row.models) ? row.models.length : 0;
        const label = text("span", `${row.name} · ${row.provider || "custom"} · ${row.model}${count ? ` · ${count} 个模型` : ""}`);
        label.className = "model-label";
        const del = text("button", "删除");
        del.type = "button";
        del.className = "btn";
        del.addEventListener("click", async () => {
          try {
            await api("/api/models/" + encodeURIComponent(row.id), {method: "DELETE"});
            await refreshModels();
            await refreshBindings();
          } catch (error) { status("model-status", `删除失败：${error.message}`, true); }
        });
        li.append(label, del);
        list.append(li);
      }
    } catch { /* admin-only and optional */ }
  }
  function modelPayload() {
    return {
      provider: $("model-provider").value,
      name: $("model-name").value.trim(),
      protocol: $("model-protocol").value,
      base_url: $("model-base").value.trim(),
      model: $("model-name-id").value.trim(),
      api_key: $("model-key").value,
      models: modelProbe,
    };
  }
  async function openModelForm() {
    $("model-modal").hidden = false;
    status("model-status", "");
    modelProbe = [];
    $("model-options").replaceChildren();
    if ($("token").value.trim()) await loadProviders();
    $("model-name").value = "";
    $("model-base").value = "";
    $("model-name-id").value = "";
    $("model-key").value = "";
    if (!$("model-provider").children.length) {
      const option = document.createElement("option");
      option.value = "custom"; option.textContent = "自定义 OpenAI 兼容";
      $("model-provider").append(option);
    }
    syncModelProvider();
    $("model-name").focus();
  }
  function closeModelForm() { $("model-modal").hidden = true; $("model-key").value = ""; }

  async function refresh() {
    refreshBindings();
    refreshPolicy();
    refreshPricing();
    refreshAlerts();
    refreshMembers();
    refreshWorkspaces();
    refreshLauncherStatus();
    refreshLaunchers();
    refreshCloudTasks();
    refreshAgentRegistry();
    refreshIssues();
    refreshModels();
    refreshEnv();
    const captured = ++epoch;
    refreshMerit(captured);
    ++detailEpoch;
    clearDetail();
    $("tasks").replaceChildren();
    status("detail-status", "暂无数据，请选择任务。");
    status("list-status", "加载中，正在读取任务与运行…");
    try {
      const [tasks, views] = await Promise.all([api("/api/tasks"), api("/api/ai-runs")]);
      if (captured !== epoch) return;
      const entries = new Map((tasks.tasks || []).map(task => [task.id, task]));
      for (const view of views.runs || []) entries.set(view.id, { ...entries.get(view.id), ...view });
      if (selected && !entries.has(selected)) entries.set(selected, { id: selected, status: "queued" });
      entriesCache = entries;
      taskTitles = new Map((tasks.tasks || []).map(task => [task.id, task.title || task.payload?.title || task.id]));
      renderTaskList();
      updateKpis(tasks.tasks || []);
      const showWorker = $("show-worker")?.checked;
      let hidden = 0;
      for (const task of entries.values()) {
        const failed = ["error", "failed", "blocked"].includes(task.status || task.state);
        const isWorker = /^[0-9a-f]{32}$/.test(task.id) || (task.title === undefined && task.state === undefined);
        if (!showWorker && (isWorker || failed)) hidden += 1;
      }
      const live = [...entries.values()].filter(e => ["queued", "running"].includes(e.status));
      setConnection("ok", "已连接");
      const suffix = hidden ? ` · 已折叠 ${hidden} 项工部运行/失败项` : "";
      status("list-status", entries.size
        ? `${entries.size - hidden} 项案卷${suffix}`
        : "暂无任务或运行，提交第一条请求开始。");
      if (live.length && $("auto-refresh")?.checked) {
        clearTimeout(pollTimer);
        pollTimer = setTimeout(() => { refresh().catch(() => {}); }, 3000);
      }
      if (selected && entries.has(selected)) await selectRun(selected);
    } catch (error) {
      if (captured === epoch) {
        setConnection("error", "连接失败");
        status("list-status", `加载错误：${error.message}`, true);
      }
    }
  }
  for (const item of COMPANIES) {
    const option = text("option", item.name);
    option.value = item.id;
    $("company").append(option);
  }
  $("company").addEventListener("change", () => {
    resetBindings();
    company = COMPANIES.find(item => item.id === $("company").value);
    selected = null;
    $("token").value = "";
    ++epoch;
    ++detailEpoch;
    clearDetail();
    $("tasks").replaceChildren();
    status("list-status", "公司已切换，请重新填写对应 Token 后连接。");
    status("detail-status", "暂无数据，请选择任务。");
    status("submit-status", "提交只进入队列，不会在页面中调用模型。");
  });
  $("login").addEventListener("click", async () => {
    const username = $("username").value.trim();
    const password = $("password").value;
    if (!username || !password) {
      $("login-status").textContent = "请填写用户名和密码。";
      return;
    }
    $("login-status").textContent = "登录中…";
    try {
      const response = await fetch(BASE_PATH + company.base + "/api/login", {
        method: "POST",
        cache: "no-store",
        credentials: "omit",
        redirect: "error",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password })
      });
      if (!response.ok) {
        throw new Error(response.status === 401 ? "用户名或密码错误。" : `登录失败（HTTP ${response.status}）。`);
      }
      const data = await response.json();
      resetBindings();
      $("token").value = data.session;
      document.cookie = "fusion_auth=1; Path=/; SameSite=Strict; Max-Age=28800";
      postBridge({ type: "dutang:token", token: data.session });
      $("password").value = "";
      $("logout").disabled = false;
      $("login-status").textContent = `已登录：${data.name}（${data.role}）`;
      setConnection("ok", "已登录");
      closeDrawer();
      await refresh();
    } catch (error) {
      $("login-status").textContent = error.message || "登录失败。";
    }
  });
  $("logout").addEventListener("click", async () => {
    const token = $("token").value.trim();
    if (token || /(?:^|;\s*)fusion_auth=1(?:;|$)/.test(document.cookie)) {
      try {
        await fetch(BASE_PATH + company.base + "/api/logout", {
          method: "POST", cache: "no-store", credentials: "same-origin", redirect: "error",
          headers: {
            "Content-Type": "application/json",
            "X-Fusion-Request": "1",
            ...(token ? { Authorization: `Bearer ${token}` } : {}),
          }
        });
      } catch (error) {
        // logout is best effort; the token is dropped locally regardless
      }
    }
    $("token").value = "";
    // 服务端 /api/logout 会清掉 httpOnly cookie
    document.cookie = "fusion_auth=; Path=/; SameSite=Strict; Max-Age=0";
    postBridge({ type: "dutang:token", token: "" });
    resetBindings();
    $("logout").disabled = true;
    $("login-status").textContent = "已登出。";
    setConnection("", "未连接");
    entriesCache = new Map();
    renderTaskList();
    updateKpis([]);
    clearDetail();
  });
  $("show-worker").addEventListener("change", () => refresh().catch(() => {}));
  $("token").addEventListener("input", resetBindings);
  // 三套主题循环：宣纸 → 乌金 → 青瓷 → 宣纸。按钮标签显示“下一套”。
  const THEMES = ["paper", "wujin", "qingci"];
  const THEME_LABEL = { paper: "宣纸", wujin: "乌金", qingci: "青瓷" };
  const themeRoot = (typeof document !== "undefined" && document.documentElement) || null;
  const applyTheme = next => {
    if (!themeRoot || !themeRoot.dataset) return;
    themeRoot.dataset.theme = next;
    $("theme").textContent = THEME_LABEL[next];
    $("theme").setAttribute("aria-pressed", String(next !== "paper"));
    $("theme").setAttribute("title", "当前：" + THEME_LABEL[next] + "；点击切换");
  };
  applyTheme((themeRoot && themeRoot.dataset.theme) || "paper");
  $("theme").addEventListener("click", () => {
    const now = (themeRoot && themeRoot.dataset.theme) || "paper";
    applyTheme(THEMES[(THEMES.indexOf(now) + 1) % THEMES.length]);
  });

  const queryAll = selector => (typeof document.querySelectorAll === "function"
    ? Array.from(document.querySelectorAll(selector)) : []);
  $("open-compose").addEventListener("click", () => openDrawer("compose-drawer"));
  $("open-settings").addEventListener("click", () => openDrawer("settings-drawer"));
  for (const button of queryAll(".js-compose")) {
    button.addEventListener("click", () => openDrawer("compose-drawer"));
  }
  for (const button of queryAll(".js-settings")) {
    button.addEventListener("click", () => openDrawer("settings-drawer"));
  }
  $("scrim").addEventListener("click", () => closeDrawer());
  for (const button of queryAll(".drawer-close")) {
    button.addEventListener("click", () => closeDrawer(button.dataset.close));
  }
  if (typeof document.addEventListener === "function") {
    document.addEventListener("keydown", event => {
      if (event.key === "Escape") closeDrawer();
    });
  }
  for (const button of queryAll(".seg-btn")) {
    button.addEventListener("click", () => {
      filterState = button.dataset.filter || "all";
      for (const other of queryAll(".seg-btn")) {
        other.classList.toggle("is-on", other === button);
      }
      renderTaskList();
    });
  }
  $("refresh").addEventListener("click", refresh);
  $("request-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (submitting) return;
    const request = $("request").value.trim();
    if (!request || request.length > 12000) return status("submit-status", "请输入 1–12000 字符的请求。", true);
    submitting = true;
    $("submit").disabled = true;
    $("company").disabled = true;
    status("submit-status", "提交中，请勿重复提交…");
    try {
      const result = await api("/api/requests", { method: "POST", body: JSON.stringify({ request }) });
      selected = result.id;
      status("submit-status", `已入队：${result.id}；等待独立执行器处理。`);
      $("request").value = "";
      await refresh();
    } catch (error) { status("submit-status", `${error.message} 若网络中断，提交结果可能未知，请先核查再重试。`, true); }
    finally { submitting = false; $("submit").disabled = false; $("company").disabled = false; }
  });
  $("export").addEventListener("click", () => {
    if (!current) return;
    saveBlob(new Blob([JSON.stringify({ id: current.id, artifacts: current.artifacts }, null, 2)], { type: "application/json" }), "artifacts.json");
  });
  if (/(?:^|;\s*)fusion_auth=1(?:;|$)/.test(document.cookie || "")) {
    refresh().catch(() => {});
  }
}

if (typeof module !== "undefined") module.exports = { renderRun, safeDownloadURL };
if (typeof document !== "undefined") boot();
