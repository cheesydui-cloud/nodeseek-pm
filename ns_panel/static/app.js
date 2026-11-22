const titles = {
  overview: ["总览", "有人发来私信、@你、回复你的帖子，或新帖命中关键词时，推送到 Telegram。"],
  messages: ["私信", "最近一次成功拉到的会话。"],
  mentions: ["提到我", "有人在帖子里@你时，列出来并推送到 Telegram。"],
  replies: ["回复我", "有人回复你的帖子时，列出来并推送到 Telegram。"],
  keywords: ["关键词", "新帖标题或摘要里出现关键词时，列出来并推送到 Telegram。"],
  settings: ["设置", "面板账号、登录浏览器、Telegram，以及检查间隔。"],
  logs: ["日志", "本机运行记录，不会上传。"],
};

const hitStatus = {
  sent: "已推送",
  pending: "待推送",
  listed: "已记录",
};

const $ = (selector) => document.querySelector(selector);
const toastEl = $("#toast");
let toastTimer = 0;
let status = null;
let boardNames = {};
let postsFormReady = false;

function toast(text, kind) {
  toastEl.hidden = false;
  toastEl.textContent = text;
  toastEl.className = kind === "err" ? "toast err" : "toast";
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    toastEl.hidden = true;
  }, 3600);
}

function esc(text) {
  return String(text ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[char]));
}

function when(value) {
  if (!value) return "还没有";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

async function api(path, options) {
  const response = await fetch(path, options);
  let data = {};
  try {
    data = await response.json();
  } catch (_error) {
    data = {};
  }
  if (response.status === 401) {
    location.href = "/login";
    throw new Error(data.error || "请先登录");
  }
  if (!response.ok || data.ok === false) {
    throw new Error(data.error || "操作失败");
  }
  return data;
}

function post(path, body) {
  return api(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

function showPage(name) {
  const page = titles[name] ? name : "overview";
  document.querySelectorAll("[data-nav]").forEach((button) => {
    button.classList.toggle("active", button.dataset.nav === page);
  });
  document.querySelectorAll(".page").forEach((section) => {
    section.classList.toggle("active", section.dataset.page === page);
  });
  $("#page-title").textContent = titles[page][0];
  $("#page-desc").textContent = titles[page][1];
  history.replaceState(null, "", `#${page}`);
  if (page === "messages") loadConversations();
  if (page === "logs") loadLogs();
  if (page === "overview") loadEvents();
  if (page === "keywords") loadPosts();
  if (page === "mentions") loadMentions();
  if (page === "replies") loadReplies();
}

function renderStatus(data) {
  status = data;
  const bad = Boolean(data.last_error);
  const pill = $("#status-pill");
  pill.className = "pill";
  if (data.running && bad) {
    pill.textContent = "运行异常";
    pill.classList.add("bad");
  } else if (data.running) {
    pill.textContent = "运行中";
    pill.classList.add("run");
  } else {
    pill.textContent = "未运行";
  }
  $("#card-running").textContent = data.running ? `每 ${data.interval} 秒` : "未运行";
  $("#card-account").textContent = data.self_name || (data.self_id ? `ID ${data.self_id}` : "还没识别");
  $("#card-success").textContent = when(data.last_success);
  $("#card-pushes").textContent = String(data.today_pushes || 0);
  $("#start-btn").disabled = data.running;
  $("#stop-btn").disabled = !data.running;

  const docker = data.runtime === "docker";
  const brand = $("#brand-note");
  if (brand) brand.textContent = docker ? "服务器" : "本机面板";
  const desk = $("#docker-desktop-note");
  const localNote = $("#browser-note");
  if (desk) desk.hidden = !docker;
  if (localNote) localNote.hidden = docker;
  const side = $("#side-note");
  if (side && docker) {
    side.textContent = "跑在 1Panel 的容器里。停掉容器后，提醒会停。登录请打开网页桌面。";
  }
  const banners = [];
  const pmReady = data.has_cookie || data.auth_mode === "browser";
  if (!pmReady && !data.has_keywords) {
    banners.push(`<div class="banner info">先到「设置」打开登录浏览器，或到「关键词」页填写要盯的词。</div>`);
  }
  if (docker && data.auth_mode === "browser" && data.browser_open && !data.self_name) {
    banners.push(`<div class="banner info">请打开网页桌面，登录 NodeSeek。登录一次后保持容器运行。</div>`);
  } else if (data.auth_mode === "browser" && !data.browser_open) {
    banners.push(`<div class="banner warn">${docker ? "容器里的浏览器还没连上。请看 1Panel 的容器日志。" : "登录浏览器没开。请到「设置」点「打开登录浏览器」，登录后不要关窗口。"}</div>`);
  } else if (data.auth_mode !== "browser" && data.has_cookie) {
    banners.push(`<div class="banner warn">复制 Cookie 容易被 Cloudflare 拦住。请到「设置」点「打开登录浏览器」，登录后保持窗口开着。</div>`);
  }
  if (!data.has_telegram) {
    banners.push(`<div class="banner info">先到「设置」里保存 Telegram，才能开始监控。</div>`);
  }
  if (data.last_error) {
    banners.push(`<div class="banner bad">${esc(data.last_error)}</div>`);
  } else if (data.running && pmReady && !data.baseline_done) {
    banners.push(`<div class="banner warn">还没有私信基线。第一次成功检查只会记住当前私信，不会把历史消息推出去。</div>`);
  }
  if (data.mentions_last_error) {
    banners.push(`<div class="banner warn">${esc(data.mentions_last_error)}</div>`);
  } else if (data.running && pmReady && !data.mentions_baseline_done) {
    banners.push(`<div class="banner warn">@还没有基线。第一次成功检查只会记住当前提到，不会把历史@推出去。</div>`);
  }
  if (data.replies_last_error) {
    banners.push(`<div class="banner warn">${esc(data.replies_last_error)}</div>`);
  } else if (data.running && pmReady && !data.replies_baseline_done) {
    banners.push(`<div class="banner warn">回复还没有基线。第一次成功检查只会记住当前回复，不会把历史回复推出去。</div>`);
  }
  if (data.quiet_active) {
    banners.push(`<div class="banner info">现在是关键词免打扰。命中先记下，结束后再推。私信、@和回复照常发。</div>`);
  }
  if (data.posts_last_error) {
    banners.push(`<div class="banner warn">${esc(data.posts_last_error)}</div>`);
  } else if (data.running && data.has_keywords && !data.posts_baseline_done) {
    banners.push(`<div class="banner warn">关键词还没有基线。第一次成功检索只会记住当前帖子，不会把历史帖推出去。</div>`);
  }
  $("#overview-banners").innerHTML = banners.join("");

  if (data.running && data.next_check_at) {
    const seconds = Math.max(0, Math.round(data.next_check_at - Date.now() / 1000));
    $("#next-check").textContent = `${seconds} 秒后再次检查`;
  } else {
    $("#next-check").textContent = "";
  }
  $("#cookie-state").textContent = data.has_cookie ? "Cookie 已保存在本机。输入框留空再保存，不会覆盖它。" : "还没有保存 Cookie。";
  const browserState = $("#browser-state");
  if (browserState) {
    if (data.runtime === "docker" && data.browser_open) {
      browserState.textContent = "容器里的浏览器已连上。请在网页桌面登录 NodeSeek，并保持容器运行。";
    } else if (data.runtime === "docker") {
      browserState.textContent = "容器里的浏览器还没连上。请看 1Panel 的容器日志。";
    } else if (data.auth_mode === "browser" && data.browser_open) {
      browserState.textContent = "正在用登录浏览器读私信，窗口已连上。请保持那个 Edge 开着。";
    } else if (data.auth_mode === "browser") {
      browserState.textContent = "已选择登录浏览器，但窗口没开。点「打开登录浏览器」。";
    } else {
      browserState.textContent = "当前仍在用复制的 Cookie。这种方式会被 Cloudflare 拦住。";
    }
  }
  $("#token-state").textContent = data.has_telegram ? "Telegram 已保存。Token 框留空再保存，不会覆盖它。" : "还没有完整的 Telegram 配置。";
  const logout = $("#logout-btn");
  if (logout) logout.hidden = !data.login_required;
}

async function refreshStatus() {
  const data = await api("/api/status");
  renderStatus(data);
}

async function loadEvents() {
  const data = await api("/api/events?limit=20");
  const box = $("#event-list");
  if (!data.items.length) {
    box.innerHTML = `<div class="empty">还没有推送。监控跑起来之后，新私信和关键词帖子会出现在这里。</div>`;
    return;
  }
  box.innerHTML = data.items.map((item) => `
    <article>
      <strong>${esc(item.title)}</strong>
      <time>${esc(when(item.created_at))}</time>
      <p>${esc(item.body)}</p>
    </article>
  `).join("");
}

async function loadConversations() {
  const data = await api("/api/conversations");
  const body = $("#conversation-body");
  if (!data.items.length) {
    body.innerHTML = `<tr><td colspan="4" class="muted">还没有会话。开始监控并完成一次检查后会显示在这里。</td></tr>`;
    return;
  }
  body.innerHTML = data.items.map((item) => `
    <tr>
      <td>${esc(item.peer_name)}</td>
      <td class="content">${esc(item.content || "（没有文字）")}</td>
      <td>${esc(item.created_at || "")}</td>
      <td><a href="https://www.nodeseek.com/notification#/message?mode=talk&to=${Number(item.peer_id)}" target="_blank" rel="noopener">打开</a></td>
    </tr>
  `).join("");
}

async function loadLogs() {
  const data = await api("/api/logs?limit=200");
  const box = $("#log-list");
  if (!data.items.length) {
    box.innerHTML = `<div class="empty">还没有日志。</div>`;
    return;
  }
  box.innerHTML = data.items.map((item) => `
    <article class="${esc(item.level)}">
      <time>${esc(when(item.created_at))} · ${esc(item.level)}</time>
      <p>${esc(item.message)}</p>
    </article>
  `).join("");
}

function renderBoards(data) {
  const box = $("#board-list");
  const selected = new Set(data.boards || []);
  const checkAll = Boolean(data.boards_all);
  box.innerHTML = (data.available || []).map((board) => `
    <label>
      <input type="checkbox" name="board" value="${esc(board.slug)}" ${(checkAll || selected.has(board.slug)) ? "checked" : ""}>
      ${esc(board.name)}
    </label>
  `).join("");
}

function renderHits(items) {
  const body = $("#post-body");
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="5" class="muted">还没有命中。保存关键词并检索后，符合的帖子会显示在这里。</td></tr>`;
    return;
  }
  body.innerHTML = items.map((item) => `
    <tr>
      <td>${esc(boardNames[item.category] || item.category || "")}</td>
      <td class="content">${esc(item.title || "")}</td>
      <td>${esc(item.keywords || "")}</td>
      <td><span class="tag ${esc(item.status || "")}">${esc(hitStatus[item.status] || item.status || "")}</span></td>
      <td><a href="${esc(item.link)}" target="_blank" rel="noopener">打开</a></td>
    </tr>
  `).join("");
}

async function loadPosts() {
  const data = await api("/api/posts");
  boardNames = Object.fromEntries((data.available || []).map((board) => [board.slug, board.name]));
  if (!postsFormReady) {
    $("#keywords").value = data.keywords || "";
    $("#excludes").value = data.excludes || "";
    renderBoards(data);
    postsFormReady = true;
  }
  const state = $("#posts-state");
  if (data.last_error) {
    state.textContent = data.last_error;
  } else if (data.baseline_done) {
    state.textContent = "帖子基线已建立。之后出现的新帖才会推送。";
  } else if (data.keywords) {
    state.textContent = "还没有帖子基线。点「立即检索」或开始监控后，第一次只会记住当前帖子。";
  } else {
    state.textContent = "还没有保存关键词。";
  }
  renderHits(data.items || []);
  const banners = $("#posts-banners");
  banners.innerHTML = data.last_error ? `<div class="banner warn">${esc(data.last_error)}</div>` : "";
}

async function loadMentions() {
  const data = await api("/api/mentions");
  const state = $("#mentions-state");
  if (data.last_error) {
    state.textContent = data.last_error;
  } else if (data.baseline_done) {
    state.textContent = "基线已建立。之后新的@才会推送。";
  } else {
    state.textContent = "还没有基线。开始监控后，第一次只会记住当前@。";
  }
  const banners = $("#mentions-banners");
  banners.innerHTML = data.last_error ? `<div class="banner warn">${esc(data.last_error)}</div>` : "";
  const body = $("#mention-body");
  const items = data.items || [];
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="5" class="muted">还没有@记录。开始监控并完成一次检查后会显示在这里。</td></tr>`;
    return;
  }
  body.innerHTML = items.map((item) => `
    <tr>
      <td>${esc(item.commenter_name || "")}</td>
      <td class="content">${esc(item.title || "")}</td>
      <td>${esc(item.created_at || "")}</td>
      <td><span class="tag ${esc(item.status || "")}">${esc(hitStatus[item.status] || item.status || "")}</span></td>
      <td><a href="${esc(item.link)}" target="_blank" rel="noopener">打开</a></td>
    </tr>
  `).join("");
}

async function loadReplies() {
  const data = await api("/api/replies");
  const state = $("#replies-state");
  if (data.last_error) {
    state.textContent = data.last_error;
  } else if (data.baseline_done) {
    state.textContent = "基线已建立。之后新的回复才会推送。";
  } else {
    state.textContent = "还没有基线。开始监控后，第一次只会记住当前回复。";
  }
  const banners = $("#replies-banners");
  banners.innerHTML = data.last_error ? `<div class="banner warn">${esc(data.last_error)}</div>` : "";
  const body = $("#reply-body");
  const items = data.items || [];
  if (!items.length) {
    body.innerHTML = `<tr><td colspan="5" class="muted">还没有回复记录。开始监控并完成一次检查后会显示在这里。</td></tr>`;
    return;
  }
  body.innerHTML = items.map((item) => `
    <tr>
      <td>${esc(item.commenter_name || "")}</td>
      <td class="content">${esc(item.title || "")}</td>
      <td>${esc(item.created_at || "")}</td>
      <td><span class="tag ${esc(item.status || "")}">${esc(hitStatus[item.status] || item.status || "")}</span></td>
      <td><a href="${esc(item.link)}" target="_blank" rel="noopener">打开</a></td>
    </tr>
  `).join("");
}

function selectedBoards() {
  const boxes = [...document.querySelectorAll("#board-list input[name=board]")];
  if (!boxes.length || boxes.every((box) => box.checked)) return "*";
  return boxes.filter((box) => box.checked).map((box) => box.value);
}

async function loadSettings() {
  const data = await api("/api/settings");
  $("#interval").value = data.interval;
  $("#tg-chat-id").value = data.tg_chat_id || "";
  $("#heartbeat-enabled").checked = data.heartbeat_enabled !== false;
  $("#heartbeat-hour").value = data.heartbeat_hour ?? 9;
  $("#quiet-enabled").checked = Boolean(data.quiet_enabled);
  $("#quiet-start").value = data.quiet_start || "23:00";
  $("#quiet-end").value = data.quiet_end || "08:00";
  const panelUser = $("#panel-user");
  if (panelUser) panelUser.value = data.panel_user || "";
}

async function withButton(button, work) {
  button.disabled = true;
  try {
    await work();
  } catch (error) {
    toast(error.message || "操作失败", "err");
  } finally {
    try {
      await refreshStatus();
    } catch (_error) {
      /* 状态刷新失败时保留刚才的提示 */
    }
    if (button.id === "start-btn") button.disabled = Boolean(status && status.running);
    else if (button.id === "stop-btn") button.disabled = !status || !status.running;
    else button.disabled = false;
  }
}

document.querySelectorAll("[data-nav]").forEach((button) => {
  button.addEventListener("click", () => showPage(button.dataset.nav));
});

$("#start-btn").addEventListener("click", () => withButton($("#start-btn"), async () => {
  await post("/api/monitor/start");
  toast("监控已开始");
}));

$("#stop-btn").addEventListener("click", () => withButton($("#stop-btn"), async () => {
  await post("/api/monitor/stop");
  toast("监控已停止");
}));

$("#check-btn").addEventListener("click", () => withButton($("#check-btn"), async () => {
  const result = await post("/api/monitor/check");
  toast(result.message || "检查完成");
  await loadEvents();
}));

$("#baseline-btn").addEventListener("click", () => withButton($("#baseline-btn"), async () => {
  const result = await post("/api/baseline");
  toast(result.message || "已重新建立基线");
  await loadConversations();
}));

$("#account-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = event.submitter || $("#account-form button[type=submit]");
  withButton(button, async () => {
    const result = await post("/api/panel-account", {
      panel_user: $("#panel-user").value.trim(),
      current_password: $("#current-password").value,
      panel_password: $("#new-password").value,
      panel_password_confirm: $("#confirm-password").value,
    });
    $("#current-password").value = "";
    $("#new-password").value = "";
    $("#confirm-password").value = "";
    toast(result.message || "账号已保存");
    await loadSettings();
  });
});

$("#logout-btn").addEventListener("click", () => withButton($("#logout-btn"), async () => {
  await post("/api/logout");
  location.href = "/login";
}));

$("#settings-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = event.submitter || $("#settings-form button[type=submit]");
  withButton(button, async () => {
    const payload = {
      interval: Number($("#interval").value),
      tg_chat_id: $("#tg-chat-id").value.trim(),
      heartbeat_enabled: $("#heartbeat-enabled").checked,
      heartbeat_hour: Number($("#heartbeat-hour").value),
      quiet_enabled: $("#quiet-enabled").checked,
      quiet_start: $("#quiet-start").value,
      quiet_end: $("#quiet-end").value,
    };
    const cookie = $("#cookie").value.trim();
    const token = $("#tg-token").value.trim();
    if (cookie) payload.cookie = cookie;
    if (token) payload.tg_token = token;
    await post("/api/settings", payload);
    $("#cookie").value = "";
    $("#tg-token").value = "";
    toast("设置已保存");
    await loadSettings();
  });
});

$("#open-browser-btn").addEventListener("click", () => withButton($("#open-browser-btn"), async () => {
  const result = await post("/api/browser/open");
  toast(result.message || "已打开登录浏览器");
}));

$("#test-browser-btn").addEventListener("click", () => withButton($("#test-browser-btn"), async () => {
  const result = await post("/api/browser/check");
  toast(result.message || "登录窗口可用");
}));

$("#use-cookie-btn").addEventListener("click", () => withButton($("#use-cookie-btn"), async () => {
  await post("/api/browser/cookie");
  toast("已改回使用 Cookie");
}));

$("#test-cookie-btn").addEventListener("click", () => withButton($("#test-cookie-btn"), async () => {
  const cookie = $("#cookie").value.trim();
  const result = await post("/api/test/cookie", cookie ? { cookie } : {});
  toast(result.message || "Cookie 可用");
}));

$("#clear-cookie-btn").addEventListener("click", () => withButton($("#clear-cookie-btn"), async () => {
  await post("/api/settings", { clear_cookie: true });
  $("#cookie").value = "";
  toast("已清除 Cookie");
}));

$("#discover-btn").addEventListener("click", () => withButton($("#discover-btn"), async () => {
  const token = $("#tg-token").value.trim();
  const result = await post("/api/telegram/discover", token ? { tg_token: token } : {});
  $("#tg-chat-id").value = result.tg_chat_id;
  toast("已填入 Chat ID，记得点保存设置");
}));

$("#test-tg-btn").addEventListener("click", () => withButton($("#test-tg-btn"), async () => {
  const payload = {};
  const token = $("#tg-token").value.trim();
  const chatId = $("#tg-chat-id").value.trim();
  if (token) payload.tg_token = token;
  if (chatId) payload.tg_chat_id = chatId;
  const result = await post("/api/test/telegram", payload);
  toast(result.message || "已发送");
}));

$("#clear-token-btn").addEventListener("click", () => withButton($("#clear-token-btn"), async () => {
  await post("/api/settings", { clear_token: true });
  $("#tg-token").value = "";
  toast("已清除 Token");
}));

$("#keyword-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const button = event.submitter || $("#keyword-form button[type=submit]");
  withButton(button, async () => {
    await post("/api/keywords", {
      keywords: $("#keywords").value,
      excludes: $("#excludes").value,
      boards: selectedBoards(),
    });
    postsFormReady = false;
    toast("关键词已保存");
    await loadPosts();
  });
});

$("#mentions-baseline-btn").addEventListener("click", () => withButton($("#mentions-baseline-btn"), async () => {
  const result = await post("/api/mentions/baseline");
  toast(result.message || "已重新建立@基线");
  await loadMentions();
}));

$("#replies-baseline-btn").addEventListener("click", () => withButton($("#replies-baseline-btn"), async () => {
  const result = await post("/api/replies/baseline");
  toast(result.message || "已重新建立回复基线");
  await loadReplies();
}));

$("#scan-posts-btn").addEventListener("click", () => withButton($("#scan-posts-btn"), async () => {
  const result = await post("/api/posts/scan");
  toast(result.message || "检索完成");
  await loadPosts();
}));

$("#posts-baseline-btn").addEventListener("click", () => withButton($("#posts-baseline-btn"), async () => {
  const result = await post("/api/posts/baseline");
  toast(result.message || "已重新建立帖子基线");
  await loadPosts();
}));

$("#reload-logs-btn").addEventListener("click", () => loadLogs().catch((error) => toast(error.message, "err")));

window.addEventListener("hashchange", () => showPage(location.hash.replace("#", "")));
showPage(location.hash.replace("#", "") || "overview");
refreshStatus().catch((error) => toast(error.message, "err"));
loadSettings().catch((error) => toast(error.message, "err"));
loadEvents().catch(() => {});

setInterval(() => {
  refreshStatus().catch(() => {});
  const page = (location.hash || "#overview").replace("#", "");
  if (page === "overview") loadEvents().catch(() => {});
  if (page === "messages") loadConversations().catch(() => {});
  if (page === "keywords") loadPosts().catch(() => {});
  if (page === "mentions") loadMentions().catch(() => {});
  if (page === "replies") loadReplies().catch(() => {});
}, 3000);
