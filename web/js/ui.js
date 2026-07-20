import { getAdminAudit, getAdminStatus, getAuthStatus, getHealth, login, logout, setApiOnline } from "./api.js";
import { initCommands, loadCustomCommands } from "./commands.js";
import { initDraft } from "./draft.js";
import { initRandomizer } from "./randomizer.js";
import { initSettings, loadSettings } from "./settings.js";
import { escapeHtml } from "./security.js";

export { escapeHtml };

export const dashboardTitles = {
  overview: "Overview",
  commands: "Command Config",
  randomizer: "Randomizer",
  builds: "Builds",
  drafts: "Drafts",
  settings: "Settings",
};

let isAuthenticated = false;
let selectedGuildId = window.localStorage.getItem("godforge:selectedGuildId") || "";

export function $(selector, root = document) {
  return root.querySelector(selector);
}

export function $all(selector, root = document) {
  return Array.from(root.querySelectorAll(selector));
}

export function showToast(message = "Action complete.") {
  const toast = $("[data-toast]");

  if (!toast) {
    return;
  }

  toast.textContent = message;
  toast.classList.add("is-visible");
  window.clearTimeout(showToast.timeout);
  showToast.timeout = window.setTimeout(() => {
    toast.classList.remove("is-visible");
  }, 3200);
}

function setApiStatus(online) {
  setApiOnline(online);
  const status = $("#api-status");

  if (!status) {
    return;
  }

  status.textContent = online ? "Local API online" : "API fallback mode";
  status.classList.toggle("live", online);
  status.classList.toggle("prototype", !online);
}

async function loadAdminStatus() {
  const summary = $("#admin-status-summary");
  const serverGrid = $("#admin-server-grid");
  const guildList = $("#admin-guild-list");
  const moduleGrid = $("#module-health-grid");
  const syncState = $("#admin-sync-state");

  if (!summary && !serverGrid && !guildList && !moduleGrid && !syncState) {
    return;
  }

  try {
    const payload = await getAdminStatus();
    const status = payload.status || {};
    const bot = status.bot || {};

    if (summary) {
      summary.innerHTML = `
        <article class="ops-stat">
          <span>Bot</span>
          <strong>${bot.connected ? "Online" : "Offline"}</strong>
          <small>${escapeHtml(bot.user || "No Discord client detected")}</small>
        </article>
        <article class="ops-stat">
          <span>Guilds</span>
          <strong>${bot.guildCount ?? 0}</strong>
          <small>${bot.latencyMs === null || bot.latencyMs === undefined ? "Latency unavailable" : `${bot.latencyMs}ms latency`}</small>
        </article>
        <article class="ops-stat">
          <span>Party workflow</span>
          <strong>Standalone</strong>
          <small>Companion services are optional</small>
        </article>
        <article class="ops-stat">
          <span>Draft rooms</span>
          <strong>${status.draftRooms ?? 0}</strong>
          <small>${status.draftRooms ?? 0} active web draft rooms</small>
        </article>
        <article class="ops-stat">
          <span>Storage</span>
          <strong>${escapeHtml(storageLabel(status.storage))}</strong>
          <small>${escapeHtml(storageDetail(status.storage))}</small>
        </article>
      `;
    }

    if (guildList) {
      const guilds = bot.guilds || [];
      guildList.innerHTML = guilds.length
        ? guilds.map((guild) => `<span>${escapeHtml(guild.name)}</span>`).join("")
        : `<span>No guilds reported yet.</span>`;
    }

    if (serverGrid) {
      serverGrid.innerHTML = renderServerGrid(bot.guilds || []);
      bindServerSelectActions(serverGrid);
    }

    if (moduleGrid) {
      moduleGrid.innerHTML = renderModuleHealth(status.modules || []);
    }

    if (syncState) {
      syncState.textContent = `${status.draftRooms ?? 0} active draft rooms`;
    }
  } catch (error) {
    if (summary) {
      summary.innerHTML = `<p class="empty-state">${error.status === 401 ? "Admin login required." : "Status unavailable."}</p>`;
    }
    if (guildList) {
      guildList.innerHTML = `<span>Login required</span>`;
    }
    if (serverGrid) {
      serverGrid.innerHTML = `<p class="empty-state">${error.status === 401 ? "Admin login required." : "Servers unavailable."}</p>`;
    }
    if (moduleGrid) {
      moduleGrid.innerHTML = `<p class="empty-state">${error.status === 401 ? "Admin login required." : "Module health unavailable."}</p>`;
    }
    if (syncState) {
      syncState.textContent = "Locked";
    }
  }
}

function storageLabel(storage = {}) {
  if (storage.kind === "sqlite" && storage.available) {
    return "SQLite";
  }
  if (storage.kind === "sqlite") {
    return "SQLite error";
  }
  return "JSON";
}

function storageDetail(storage = {}) {
  if (storage.error) {
    return storage.error;
  }
  return storage.path || "data/*.json";
}

function renderServerGrid(guilds) {
  if (!guilds.length) {
    return `<p class="empty-state">No Discord servers reported by the bot yet.</p>`;
  }

  return guilds.map((guild, index) => {
    const name = guild.name || "Discord Server";
    const initials = serverInitials(name);
    const action = index < 6 ? "Open" : "Setup";
    const guildId = String(guild.id || "");
    const selected = selectedGuildId && guildId === selectedGuildId;

    return `
      <article class="server-select-card ${selected ? "is-selected" : ""}">
        <div class="server-card-art server-card-art--${index % 6}">
          <span>${escapeHtml(initials)}</span>
        </div>
        <div class="server-select-footer">
          <div>
            <strong>${escapeHtml(name)}</strong>
            <small>${selected ? "Selected server" : "Bot connected"}</small>
          </div>
          <button class="button ${action === "Open" ? "button-primary" : "button-secondary"}" type="button" data-demo-action="server-select" data-guild-id="${escapeHtml(guildId)}" data-guild-name="${escapeHtml(name)}">${selected ? "Selected" : action}</button>
        </div>
      </article>
    `;
  }).join("");
}

function serverInitials(name) {
  return String(name || "GF")
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0])
    .join("")
    .toUpperCase() || "GF";
}

function bindServerSelectActions(root) {
  $all("[data-demo-action='server-select']", root).forEach((button) => {
    button.addEventListener("click", () => {
      const guildId = button.dataset.guildId || "";
      const guildName = button.dataset.guildName || "Discord Server";

      selectedGuildId = guildId;
      window.localStorage.setItem("godforge:selectedGuildId", guildId);
      updateSelectedServer(guildName, guildId);
      loadAdminStatus();
      showToast("Server selected locally. OAuth will make this guild-scoped.");
    });
  });
}

function updateSelectedServer(name, guildId = "") {
  const avatar = $("#selected-server-avatar");
  const title = $("#selected-server-name");
  const meta = $("#selected-server-meta");

  if (avatar) {
    avatar.textContent = serverInitials(name);
  }
  if (title) {
    title.textContent = name;
  }
  if (meta) {
    meta.textContent = guildId ? `Guild ${guildId}` : "Demo server";
  }
}

function renderModuleHealth(modules) {
  if (!modules.length) {
    return `<p class="empty-state">No modules reported yet.</p>`;
  }

  const stateLabels = {
    ready: "Ready",
    needs_setup: "Needs setup",
    staged: "Staged",
    disabled: "Disabled",
  };

  return modules.map((module) => {
    const needs = module.needs?.length
      ? module.needs.map((need) => `<span>${escapeHtml(need)}</span>`).join("")
      : `<span>All required setup present</span>`;
    const state = module.state || "staged";

    return `
      <article class="module-health-card module-health-card--${escapeHtml(state)}">
        <div>
          <strong>${escapeHtml(module.label)}</strong>
          <span class="status-badge status-badge--${escapeHtml(state)}">${escapeHtml(stateLabels[state] || state)}</span>
        </div>
        <p>${escapeHtml(module.detail || "")}</p>
        <div class="module-needs">${needs}</div>
      </article>
    `;
  }).join("");
}

async function loadAdminAudit() {
  const list = $("#admin-audit-list");

  if (!list) {
    return;
  }

  try {
    const payload = await getAdminAudit(8);
    const events = payload.events || [];
    list.innerHTML = events.length
      ? events.map((event) => {
        const when = event.ts ? new Date(event.ts * 1000).toLocaleString() : "Unknown time";
        return `
          <article class="audit-row">
            <div>
              <strong>${escapeHtml(event.action)}</strong>
              <span>${escapeHtml(event.target || "dashboard")}</span>
            </div>
            <small>${escapeHtml(when)}</small>
          </article>
        `;
      }).join("")
      : `<p class="empty-state">No admin activity logged yet.</p>`;
  } catch (error) {
    list.innerHTML = `<p class="empty-state">${error.status === 401 ? "Admin login required." : "Activity unavailable."}</p>`;
  }
}

async function checkApiHealth() {
  try {
    await getHealth();
    setApiStatus(true);
  } catch {
    setApiStatus(false);
  }
}

function activateDashboardTab(tabName) {
  const targetPanel = $(`[data-dashboard-panel="${tabName}"]`);
  const protectedPanel = Boolean(targetPanel?.dataset.adminPanel !== undefined);

  $all("[data-dashboard-tab]").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.dashboardTab === tabName);
  });

  $all("[data-dashboard-panel]").forEach((panel) => {
    const locked = panel.dataset.adminPanel !== undefined && !isAuthenticated;
    panel.classList.toggle("is-locked", locked);
    panel.classList.toggle("is-active", panel.dataset.dashboardPanel === tabName && !locked);
  });

  const title = $("#dashboard-title");

  if (title) {
    title.textContent = dashboardTitles[tabName] || "Dashboard";
  }

  $("#admin-login-panel")?.classList.toggle("is-visible", protectedPanel && !isAuthenticated);

  if (isAuthenticated && tabName === "commands") {
    loadCustomCommands();
  }
  if (isAuthenticated && tabName === "overview") {
    loadAdminStatus();
    loadAdminAudit();
  }
  if (isAuthenticated && tabName === "settings") {
    loadSettings();
  }
}

function activateToolTab(tabName) {
  const targetPanel = $(`[data-tool-panel="${tabName}"]`);
  const protectedPanel = Boolean(targetPanel?.dataset.adminPanel !== undefined);

  $all("[data-tool-tab]").forEach((tab) => {
    tab.classList.toggle("is-active", tab.dataset.toolTab === tabName);
  });

  $all("[data-tool-panel]").forEach((panel) => {
    const locked = panel.dataset.adminPanel !== undefined && !isAuthenticated;
    panel.classList.toggle("is-locked", locked);
    panel.classList.toggle("is-active", panel.dataset.toolPanel === tabName && !locked);
  });

  $("#admin-login-panel")?.classList.toggle("is-visible", protectedPanel && !isAuthenticated);
}

function updateCommandPreview() {
  const trigger = $("#command-trigger");
  const response = $("#command-response");
  const role = $("#command-role");
  const cooldown = $("#command-cooldown");
  const enabled = $("#command-enabled");

  if (!trigger || !response || !role || !cooldown || !enabled) {
    return;
  }

  $("#preview-trigger").textContent = trigger.value || ".custom";
  $("#preview-response").textContent = response.value || "Write the response your server should see.";
  $("#preview-role").textContent = role.value;
  $("#preview-cooldown").textContent = `${cooldown.value || "0s"} cooldown`;
  $("#preview-enabled").textContent = enabled.checked ? "Enabled" : "Disabled";
}

function bindNavigation() {
  $all("[data-dashboard-tab]").forEach((tab) => {
    tab.addEventListener("click", () => activateDashboardTab(tab.dataset.dashboardTab));
  });

  $all("[data-tool-tab]").forEach((tab) => {
    tab.addEventListener("click", () => activateToolTab(tab.dataset.toolTab));
  });

  $all('a[href^="#"]').forEach((link) => {
    link.addEventListener("click", (event) => {
      const targetId = link.getAttribute("href");

      if (!targetId || targetId === "#") {
        event.preventDefault();
        return;
      }

      const target = $(targetId);

      if (target) {
        event.preventDefault();
        target.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    });
  });
}

function bindCommandPreview() {
  $all("#command-trigger, #command-response, #command-role, #command-cooldown, #command-enabled").forEach((field) => {
    field.addEventListener("input", updateCommandPreview);
    field.addEventListener("change", updateCommandPreview);
  });
}

function bindDemoActions() {
  $all("[data-demo-action='connect']").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.preventDefault();
      showToast();
    });
  });
}

function setAuthState(authenticated, configured = true) {
  isAuthenticated = authenticated;
  const authStatus = $("#auth-status");
  const logoutButton = $("#admin-logout");
  const activeTab = $("[data-dashboard-tab].is-active")?.dataset.dashboardTab || "overview";
  const activeToolTab = $("[data-tool-tab].is-active")?.dataset.toolTab || "single-roll";

  if (authStatus) {
    authStatus.textContent = authenticated ? "Admin unlocked" : configured ? "Admin locked" : "Admin password missing";
    authStatus.classList.toggle("live", authenticated);
    authStatus.classList.toggle("prototype", !authenticated);
  }

  logoutButton?.classList.toggle("is-hidden", !authenticated);
  activateDashboardTab(activeTab);
  activateToolTab(activeToolTab);
}

async function refreshAuthStatus() {
  try {
    const status = await getAuthStatus();
    setAuthState(Boolean(status.authenticated), Boolean(status.configured));
    if (status.authenticated) {
      await Promise.all([loadAdminStatus(), loadAdminAudit()]);
    }
  } catch {
    setAuthState(false, false);
  }
}

function bindAuth() {
  $("#admin-login-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const password = $("#admin-password")?.value || "";

    try {
      await login(password);
      $("#admin-password").value = "";
      setAuthState(true, true);
      showToast("Admin dashboard unlocked.");
      await Promise.all([loadAdminStatus(), loadAdminAudit(), loadSettings(), loadCustomCommands()]);
    } catch (error) {
      showToast(error.message || "Admin login failed.");
    }
  });

  $("#admin-logout")?.addEventListener("click", async () => {
    try {
      await logout();
    } finally {
      setAuthState(false, true);
      showToast("Admin dashboard locked.");
    }
  });
}

function bindAdminControls() {
  $("#admin-status-refresh")?.addEventListener("click", loadAdminStatus);
}

function init() {
  bindNavigation();
  bindCommandPreview();
  bindDemoActions();
  bindAuth();
  bindAdminControls();
  initCommands();
  initRandomizer();
  initDraft();
  initSettings();
  checkApiHealth();
  refreshAuthStatus();
  updateCommandPreview();
  updateSelectedServer($("#selected-server-name")?.textContent || "Smite Night Arena", selectedGuildId);
}

init();
