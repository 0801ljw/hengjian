"use strict";
(function () {
  const $ = id => document.getElementById(id);
  const status = (text, bad = false) => {
    $("login-status").textContent = text;
    $("login-status").dataset.bad = bad ? "1" : "";
  };
  function show(tab) {
    $("form-login").hidden = tab !== "login";
    $("form-register").hidden = tab !== "register";
    $("tab-login").setAttribute("aria-selected", String(tab === "login"));
    $("tab-register").setAttribute("aria-selected", String(tab === "register"));
    status("");
  }
  async function request(path, body) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Fusion-Request": "1" },
      credentials: "same-origin", body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `请求失败（HTTP ${response.status}）`);
    return data;
  }
  function enter() {
    // 会话由服务端写进 httpOnly cookie；这个可读标记只让工作台知道可走 cookie。
    document.cookie = "fusion_auth=1; Path=/; SameSite=Strict; Max-Age=28800";
    window.location.replace("/");
  }
  $("tab-login").addEventListener("click", () => show("login"));
  $("tab-register").addEventListener("click", () => show("register"));
  $("form-login").addEventListener("submit", async event => {
    event.preventDefault();
    status("登录中…");
    try {
      enter(await request("/api/login", {
        username: $("login-name").value, password: $("login-password").value,
      }));
    } catch (error) { status(error.message, true); }
  });
  $("form-register").addEventListener("submit", async event => {
    event.preventDefault();
    if ($("register-password").value !== $("register-confirm").value) {
      status("两次密码不一致。", true); return;
    }
    status("注册中…");
    try {
      enter(await request("/api/register", {
        username: $("register-name").value, password: $("register-password").value,
      }));
    } catch (error) { status(error.message, true); }
  });
  if (new URLSearchParams(window.location.search).get("tab") === "register") show("register");
  // 已登录就跳过登录页（cookie 会自动带上）
  fetch("/api/me", { credentials: "same-origin" })
    .then(response => { if (response.ok) window.location.replace("/"); })
    .catch(() => {});
}());
