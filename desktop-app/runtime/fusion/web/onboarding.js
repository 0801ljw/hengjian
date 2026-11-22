"use strict";

function bootOnboarding() {
  const $ = id => document.getElementById(id);
  const roles = {taizi: "太子", zhongshu: "中书", menxia: "门下", shangshu: "尚书", gongbu: "工部"};
  let mode = null, selected = null, facts = null, busy = false;
  // 会话在 httpOnly cookie 里，前端不持有 token。
  let epoch = 0, timer = null, installing = false;
  let auth = {users: false, onboarding_required: true}, authLoaded = false;
  const node = (tag, value) => {
    const el = document.createElement(tag);
    el.textContent = value;
    return el;
  };
  const message = value => { $("ob-status").textContent = value; };
  const markSession = () => {
    document.cookie = "fusion_auth=1; Path=/; SameSite=Strict; Max-Age=28800";
  };
  // CLI --version output can be multi-line marketing text (Hermes); keep one short line.
  const versionLabel = value => {
    const line = String(value || "").split("\n")[0].trim();
    return line.length > 44 ? line.slice(0, 44) + "…" : (line || "版本未知");
  };
  function page(number) {
    for (let i = 1; i <= 3; i++) $("ob-step" + i).hidden = i !== number;
    $("ob-progress").textContent = `第 ${number} 页 / 3 · ${["", "选择运行方式", "绑定 Agent", "确认配置"][number]}`;
    $("ob-title" + number).focus();
  }
  async function api(path, data) {
    // 会话在 httpOnly cookie 里；状态变更必须带 CSRF 头（见 server.py）
    const options = {credentials: "same-origin", headers: {"X-Fusion-Request": "1"}};
    if (data !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(data);
    }
    const response = await fetch(path, options);
    const result = await response.json();
    if (!response.ok) {
      if ([401, 403].includes(response.status)) $("ob-login").hidden = false;
      throw new Error([401, 403].includes(response.status) ? "请使用管理员账号登录后重试。" : (result.error || "请求失败"));
    }
    return result;
  }
  function ready() {
    $("ob-start").disabled = busy || installing || !facts || (mode === "single" && !selected);
    $("ob-refresh").disabled = busy || installing;
    $("ob-back").disabled = busy || installing;
  }
  function renderRecommendations() {
    const host = $("ob-recommendations");
    host.replaceChildren();
    const rows = Array.isArray(facts.recommendations) ? facts.recommendations : [];
    if (!rows.length) return;
    const heading = node("div", "");
    heading.className = "ob-catalog-head";
    heading.append(node("h2", "角色推荐"));
    heading.append(node("p", "按“最强能力 + 独立审查 + 成本”给出的默认配置；应用预设即可写入。"));
    host.append(heading);
    const table = node("table", "");
    const head = node("tr", "");
    for (const title of ["角色", "推荐 Agent", "推荐模型", "为什么"]) head.append(node("th", title));
    const thead = node("thead", ""); thead.append(head); table.append(thead);
    const tbody = node("tbody", "");
    for (const row of rows) {
      const tr = node("tr", "");
      tr.append(node("td", row.label || roles[row.role] || row.role));
      tr.append(node("td", row.agent));
      tr.append(node("td", row.model || "Agent 默认模型"));
      const why = node("td", row.reason || "");
      if (Array.isArray(row.alternatives) && row.alternatives.length) {
        const alt = node("span", "备选：" + row.alternatives.map(item => `${item.agent}/${item.model}`).join("、"));
        alt.className = "ob-recommendation-alt";
        why.append(alt);
      }
      tr.append(why);
      tbody.append(tr);
    }
    table.append(tbody); host.append(table);
  }
  function renderCatalog() {
    const host = $("ob-catalog");
    host.replaceChildren();
    const catalog = Array.isArray(facts.catalog) ? facts.catalog : [];
    if (!catalog.length) return;
    const detected = catalog.filter(entry => entry.found).length;
    const integrated = catalog.filter(entry => entry.integrated).length;
    const ready = catalog.filter(entry => entry.health?.state === "ready").length;
    const heading = node("div", "");
    heading.className = "ob-catalog-head";
    heading.append(node("h2", "智能体接入"));
    heading.append(node("p", `本机已检测到 ${detected} 个 · 就绪 ${ready} 个 · 调度已接入 ${integrated} 个`));
    host.append(heading);
    const grid = node("div", "");
    grid.className = "ob-agent-grid";
    for (const entry of catalog) {
      const card = node("article", "");
      card.className = "ob-agent-card";
      card.dataset.ready = entry.found ? "true" : "false";
      const top = node("div", "");
      top.className = "ob-agent-title";
      const title = node("strong", entry.title);
      const vendor = node("span", entry.vendor);
      vendor.className = "ob-agent-vendor";
      top.append(title, vendor);
      card.append(top);
      const description = node("p", entry.description);
      description.className = "ob-agent-desc";
      card.append(description);
      if (entry.roles && entry.roles.length) {
        const roles = node("p", "角色：" + entry.roles.join(" / "));
        roles.className = "ob-agent-roles";
        card.append(roles);
      }
      const footer = node("div", "");
      footer.className = "ob-agent-actions";
      const h = entry.health || {};
      const auth = h.auth_mode === "cli_login" ? " · CLI 登录" : h.auth_mode === "api_key" ? " · API Key" : "";
      const label = h.state === "ready" ? `就绪${auth}${entry.version ? " · " + versionLabel(entry.version) : ""}`
        : h.state === "login_required" ? "已安装 · 需登录"
          : h.state === "not_configured" ? "未配置通道"
            : h.state === "not_installed" ? "未安装"
              : h.state === "error" ? "未接入调度" : "未安装";
      const state = node("span", label);
      state.className = "ob-agent-state";
      state.dataset.tone = h.state === "ready" ? "ok" : h.state === "login_required" || h.state === "not_configured" ? "warn" : "muted";
      state.title = h.message || "";
      footer.append(state);
      if (h.next_step) {
        const next = node("span", h.next_step);
        next.className = "ob-agent-next";
        footer.append(next);
      }
      if (!entry.found && entry.installable) {
        const button = node("button", "一键安装");
        button.type = "button";
        button.className = "btn";
        button.disabled = installing;
        button.onclick = () => install(entry.id, "install");
        footer.append(button);
      } else if (entry.found && entry.installable) {
        const update = node("button", "更新");
        update.type = "button";
        update.className = "btn";
        update.disabled = installing;
        update.onclick = () => install(entry.id, "update");
        footer.append(update);
        const remove = node("button", "卸载");
        remove.type = "button";
        remove.className = "btn";
        remove.disabled = installing;
        remove.onclick = () => {
          if (typeof window.confirm === "function" && !window.confirm(`卸载 ${entry.title}？`)) return;
          install(entry.id, "uninstall");
        };
        footer.append(remove);
      } else if (!entry.found && entry.docs) {
        const link = node("a", "安装文档");
        // Only the catalog's fixed docs URLs are rendered as links.
        if (typeof entry.docs === "string" && entry.docs.startsWith("https://")) link.href = entry.docs;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.className = "ob-agent-doc";
        footer.append(link);
      }
      card.append(footer);
      grid.append(card);
    }
    host.append(grid);
    const note = node("p", "安装与探测只在本机进行。CLI Agent 会以空工作目录和只读/无工具参数启动，但仍拥有本机账户权限；生产环境建议放进专用用户或容器。");
    note.className = "ob-catalog-note";
    host.append(note);
  }
  function render() {
    renderCatalog();
    renderRecommendations();
    const container = $("ob-config");
    container.replaceChildren();
    const detected = facts.agents.filter(entry => entry.found);
    if (!detected.length) container.append(node("p", "未探测到任何 Agent，请先安装，再点击重新探测。"));
    if (mode === "single") {
      container.append(node("h2", "选哪个 agent 跑全部角色"));
      const group = node("fieldset", "");
      group.append(node("legend", "已探测到的 Agent 及版本"));
      for (const entry of detected) {
        const label = node("label", "");
        label.className = "ob-agent";
        const radio = document.createElement("input");
        radio.type = "radio"; radio.name = "agent"; radio.value = entry.agent;
        radio.checked = entry.agent === selected;
        radio.onchange = () => { selected = entry.agent; ready(); };
        label.append(radio, node("span", `${entry.agent} · ${versionLabel(entry.version)}${entry.agent === facts.suggested ? " · 默认推荐" : ""}`));
        group.append(label);
      }
      container.append(group);
    } else {
      container.append(node("h2", "五个角色绑定预设"));
      const table = node("table", "");
      const head = node("tr", "");
      for (const title of ["角色", "Agent", "模型", "探测 / 版本"]) head.append(node("th", title));
      const thead = node("thead", ""); thead.append(head); table.append(thead);
      const tbody = node("tbody", "");
      for (const row of facts.preset) {
        const tr = node("tr", "");
        const agent = facts.agents.find(entry => entry.agent === row.agent);
        for (const value of [roles[row.role], row.agent, row.model || "Agent 默认模型",
          row.found ? `已探测 · ${versionLabel(agent?.version)}` : "缺失（不会写入绑定）"]) tr.append(node("td", value));
        tbody.append(tr);
      }
      table.append(tbody); container.append(table);
    }
    if (!detected.length) selected = null;
    ready();
  }
  async function refresh() {
    const current = ++epoch;
    busy = true; ready(); message("正在探测本机 Agent…");
    try {
      const result = await api("/api/onboarding");
      if (current !== epoch) return;
      facts = result;
      if (!facts.agents.some(entry => entry.agent === selected && entry.found)) selected = facts.suggested;
      // Other detected agents remain selectable even when none has a capability rank.
      render(); message("");
    } catch (error) {
      if (current === epoch) {
        if (error.status === 401) {
          facts = null;
          $("ob-login").hidden = false;
          $("ob-register").hidden = true;
          message("请先登录或注册管理员账号。");
        } else {
          message(error.message);
        }
      }
    }
    finally { if (current === epoch) { busy = false; ready(); } }
  }
  async function install(name, action = "install") {
    if (busy || installing) return;
    installing = true; render(); ready();
    const labels = {install: "正在安装", update: "正在更新", uninstall: "正在卸载"};
    message(labels[action] + " " + name + "…");
    const current = epoch;
    try {
      const endpoint = action === "install" ? "/api/agents/install" : "/api/agents/" + action;
      const job = await api(endpoint, {agent: name});
      async function poll() {
        try {
          const state = await api("/api/agents/install/" + job.job);
          if (current !== epoch) return;
          $("ob-install-log").textContent = state.tail || "";
          if (state.status === "running") { timer = setTimeout(poll, 1000); return; }
          installing = false;
          await refresh();
          message(state.status === "succeeded" ? (action === "uninstall" ? "卸载完成。" : "安装完成。") : "操作未完成：" + state.status);
        } catch (error) { installing = false; render(); message(error.message); }
      }
      await poll();
    } catch (error) { installing = false; render(); message(error.message); }
  }
  for (const value of ["single", "multi"]) $("ob-" + value).onclick = () => {
    mode = value; page(2); refresh();
  };
  $("ob-refresh").onclick = refresh;
  $("ob-back").onclick = () => { ++epoch; facts = null; page(1); message(""); };
  async function getAuthStatus() {
    if (!authLoaded) {
      try {
        const response = await fetch("/api/auth/status", {credentials: "same-origin"});
        if (response.ok) auth = await response.json();
      } catch { /* keep the conservative default */ }
      authLoaded = true;
      if (auth.users) $("ob-show-register").textContent = "普通成员登录 / 注册";
    }
    return auth;
  }
  $("ob-show-register").onclick = async () => {
    const status = await getAuthStatus();
    if (status.users) {
      window.location.href = "/login?tab=register";
      return;
    }
    $("ob-login").hidden = true;
    $("ob-register").hidden = false;
    $("ob-reg-username").focus();
  };
  $("ob-cancel-register").onclick = () => {
    $("ob-register").hidden = true;
    $("ob-login").hidden = false;
  };
  $("ob-register").onsubmit = async event => {
    event.preventDefault();
    if ($("ob-reg-password").value !== $("ob-reg-confirm").value) {
      message("两次密码不一致。");
      return;
    }
    try {
      const session = await api("/api/register", {
        username: $("ob-reg-username").value,
        password: $("ob-reg-password").value,
      });
      markSession();
      // cookie 由服务端下发
      $("ob-register").hidden = true;
      $("ob-login").hidden = true;
      if (session.role !== "admin") {
        const status = await getAuthStatus();
        if (!status.onboarding_required) {
          window.location.replace("/");
          return;
        }
        message("注册成功，但初始化需要管理员账号；请让首位管理员完成初始化。");
        return;
      }
      message("注册成功，已登录。");
      await refresh();
    } catch (error) { message(error.message); }
    finally {
      $("ob-reg-password").value = "";
      $("ob-reg-confirm").value = "";
    }
  };
  $("ob-login").onsubmit = async event => {
    event.preventDefault();
    try {
      const session = await api("/api/login", {username: $("ob-username").value, password: $("ob-password").value});
      markSession();
      // cookie 由服务端下发
      $("ob-login").hidden = true;
      if (session.role !== "admin") {
        const status = await getAuthStatus();
        if (!status.onboarding_required) {
          window.location.replace("/");
          return;
        }
        message("需要管理员账号完成初始化；普通成员请从 /login 进入。");
        return;
      }
      const status = await getAuthStatus();
      if (!status.onboarding_required) {
        window.location.replace("/");
        return;
      }
      await refresh();
    } catch (error) { message(error.message); }
    finally { $("ob-password").value = ""; }
  };
  $("ob-start").onclick = async () => {
    if (busy || installing) return;
    busy = true; ready();
    try {
      const result = await api("/api/onboarding/" + mode, mode === "single" ? {agent: selected} : {});
      await api("/api/onboarding/complete", {});
      const summary = $("ob-summary"); summary.replaceChildren(node("p", mode === "single" ? "单个 Agent" : "多个 Agent"));
      for (const [role, entry] of Object.entries(result.bindings)) summary.append(node("p", `${roles[role]}：${entry.agent} / ${entry.model || "Agent 默认模型"}`));
      if (result.missing.length) summary.append(node("p", "尚未绑定：" + result.missing.map(role => roles[role]).join("、") + "。请安装缺失 Agent 后在工作台应用预设。"));
      page(3); message("");
    } catch (error) { message(error.message); }
    finally { busy = false; ready(); }
  };
  const themes = ["paper", "wujin", "qingci"], labels = ["宣纸", "乌金", "青瓷"];
  $("ob-theme").onclick = () => {
    const index = (themes.indexOf(document.documentElement.dataset.theme || "paper") + 1) % themes.length;
    document.documentElement.dataset.theme = themes[index];
    $("ob-theme").textContent = "主题：" + labels[index];
  };
  window.addEventListener("pagehide", () => { ++epoch; clearTimeout(timer); });
}

if (typeof document !== "undefined") bootOnboarding();
if (typeof module !== "undefined") module.exports = {bootOnboarding};
