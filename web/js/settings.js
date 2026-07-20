import { getSettings, saveSettings } from "./api.js";
import { $, escapeHtml, showToast } from "./ui.js";

const DEFAULT_GUILD_ID = "global";

export function initSettings() {
  $("#settings-refresh")?.addEventListener("click", loadSettings);
  $("#settings-form")?.addEventListener("submit", submitSettings);
}

export async function loadSettings() {
  try {
    const payload = await getSettings(DEFAULT_GUILD_ID);
    renderSettings(payload.settings);
  } catch (error) {
    showToast(error.status === 401 ? "Admin login required." : error.message || "Settings unavailable.");
  }
}

function renderSettings(settings) {
  if (!settings) {
    return;
  }

  setChecked("setting-bot-enabled", settings.features?.botEnabled);
  setChecked("setting-randomizer-enabled", settings.features?.randomizerEnabled);
  setChecked("setting-drafts-enabled", settings.features?.draftsEnabled);
  setValue("setting-match-channel", settings.channels?.matchChannel);
  setValue("setting-admin-channel", settings.channels?.adminChannel);
  setValue("setting-admin-role", settings.roles?.adminRole);
  setValue("setting-captain-role", settings.roles?.captainRole);
  setRadio("setting-monetize-access", settings.permissions?.monetizeAccess || "none");
  renderBotMasterChips(settings.roles || {});

  const meta = $("#settings-meta");
  if (meta) {
    meta.textContent = settings.updated_at
      ? `Last saved by ${settings.updated_by || "web-admin"} at ${new Date(settings.updated_at * 1000).toLocaleString()}`
      : "No saved settings yet.";
  }
}

async function submitSettings(event) {
  event.preventDefault();

  const payload = {
    guild_id: DEFAULT_GUILD_ID,
    updated_by: "web-dashboard",
    features: {
      botEnabled: $("#setting-bot-enabled")?.checked ?? true,
      randomizerEnabled: $("#setting-randomizer-enabled")?.checked ?? true,
      draftsEnabled: $("#setting-drafts-enabled")?.checked ?? true,
    },
    channels: {
      matchChannel: $("#setting-match-channel")?.value || "",
      adminChannel: $("#setting-admin-channel")?.value || "",
    },
    roles: {
      adminRole: $("#setting-admin-role")?.value || "",
      captainRole: $("#setting-captain-role")?.value || "",
    },
    permissions: {
      monetizeAccess: document.querySelector('input[name="setting-monetize-access"]:checked')?.value || "none",
    },
  };

  try {
    const response = await saveSettings(payload);
    renderSettings(response.settings);
    showToast("Server settings saved.");
  } catch (error) {
    showToast(error.message || "Settings save failed.");
  }
}

function setChecked(id, value) {
  const input = $(`#${id}`);
  if (input) {
    input.checked = Boolean(value);
  }
}

function setValue(id, value) {
  const input = $(`#${id}`);
  if (input) {
    input.value = value || "";
  }
}

function setRadio(name, value) {
  document.querySelectorAll(`input[name="${name}"]`).forEach((input) => {
    input.checked = input.value === value;
  });
}

function renderBotMasterChips(roles) {
  const container = $("#bot-master-role-chips");

  if (!container) {
    return;
  }

  const chips = [roles.adminRole, roles.captainRole].filter(Boolean);
  container.innerHTML = chips.length
    ? chips.map((role) => `<span>${escapeHtml(role)}</span>`).join("")
    : `<span>No role labels configured yet</span>`;
}
