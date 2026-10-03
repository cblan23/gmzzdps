"use strict";

const storageKey = "gmzzdps-settings-design-20261003-v3-dark";
const defaults = {
  show_names: true, show_main_totals: true, show_deaths: true, show_combat_time: true,
  highlight_self: true, show_team_dps: true, team_rating_preview: true, show_pvp_button: true,
  show_boss_hp: true, boss_enrage_prediction: true,
  audience_metric: "HPS", warrior_metric: "DPS",
  font_size: 14, ui_scale: 100, row_mask_opacity: 0,
  visibility_enabled: true, visibility_hotkey: "Home",
  unlock_enabled: false, unlock_hotkey: "", lock_toggle_enabled: false,
};
const settings = { ...defaults };
try {
  const saved = JSON.parse(localStorage.getItem(storageKey));
  for (const key of Object.keys(defaults)) {
    if (saved && typeof saved[key] === typeof defaults[key]) settings[key] = saved[key];
  }
} catch { /* This preview also works without browser storage. */ }

function toggleRow(key, title, description = "") {
  return `<label class="setting-row" for="${key}"><span><span class="row-title">${title}</span>${description ? `<span class="row-description">${description}</span>` : ""}</span><span class="switch"><input type="checkbox" id="${key}" data-setting="${key}"><span class="switch-track" aria-hidden="true"></span></span></label>`;
}
document.querySelector("#display-options").innerHTML = [
  ["show_names", "玩家名称"], ["show_main_totals", "伤害 / 治疗总量"],
  ["show_deaths", "死亡次数"], ["show_team_dps", "全队秒伤"],
  ["highlight_self", "本人高亮"], ["show_boss_hp", "首领血条"],
].map(row => toggleRow(...row)).join("");
document.querySelector("#more-display-options").innerHTML = [
  ["show_combat_time", "战斗时间"], ["show_pvp_button", "PVP 切换按钮"],
  ["team_rating_preview", "战前非凡评分预览"],
  ["boss_enrage_prediction", "首领狂暴节奏预测", "使用已验证的首领时间资料"],
].map(row => toggleRow(...row)).join("");
document.querySelector("#lock-toggle-option").innerHTML = toggleRow("lock_toggle_enabled", "同一快捷键锁定 / 解锁", "使用上方解锁快捷键");

let recording = null;
function feedback(message = "", error = false) {
  const target = document.querySelector("#hotkey-feedback");
  target.textContent = message;
  target.classList.toggle("error", error);
}
function refresh() {
  const preview = document.querySelector(".hud-preview");
  preview.style.setProperty("--preview-font", `${settings.font_size}px`);
  preview.style.setProperty("--preview-mask", settings.row_mask_opacity / 100 * 0.25);
  preview.style.setProperty("--preview-scale", settings.ui_scale / 100);
  document.querySelectorAll("[data-setting]").forEach(input => {
    const key = input.dataset.setting;
    if (input.type === "checkbox") input.checked = settings[key];
    else if (input.type === "radio") input.checked = settings[key] === input.value;
    else {
      input.value = settings[key];
      settings[key] = Number(input.value);
      input.style.setProperty("--progress", `${100 * (input.value - input.min) / (input.max - input.min)}%`);
      document.querySelector(`#value-${key}`).innerHTML = `${input.value}<span>${key === "font_size" ? " px" : "%"}</span>`;
    }
  });
  document.querySelectorAll("[data-hotkey]").forEach(button => {
    const active = recording === button.dataset.hotkey;
    button.classList.toggle("recording", active);
    button.querySelector("span").textContent = active ? "请按下快捷键…" : settings[button.dataset.hotkey] || "录入快捷键";
    button.setAttribute("aria-label", `${button.dataset.hotkey === "visibility_hotkey" ? "录入显示或隐藏快捷键" : "录入解锁快捷键"}，${settings[button.dataset.hotkey] || "未设置"}`);
  });
  document.querySelectorAll("[data-clear]").forEach(button => { button.style.visibility = settings[button.dataset.clear] ? "visible" : "hidden"; });
}
function save() {
  refresh();
  try {
    localStorage.setItem(storageKey, JSON.stringify(settings));
    document.querySelector("#save-status").textContent = "预览已保存";
  } catch { document.querySelector("#save-status").textContent = "预览已更新"; }
}
function showPage(page) {
  if (!["display", "appearance", "shortcuts"].includes(page)) page = "display";
  recording = null;
  feedback();
  refresh();
  document.querySelectorAll(".settings-page").forEach(section => { section.hidden = section.id !== `page-${page}`; });
  document.querySelectorAll("[data-page]").forEach(button => {
    const selected = button.dataset.page === page;
    button.classList.toggle("active", selected);
    if (selected) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  document.querySelector(".page-scroll").scrollTop = 0;
}
document.addEventListener("input", event => {
  const input = event.target;
  const key = input.dataset.setting;
  if (!key) return;
  if ((key === "unlock_enabled" || key === "lock_toggle_enabled") && input.checked && !settings.unlock_hotkey) {
    feedback("请先录入解锁快捷键。", true);
    refresh();
    return;
  }
  if (key === "visibility_enabled" && input.checked && !settings.visibility_hotkey) settings.visibility_hotkey = "Home";
  settings[key] = input.type === "checkbox" ? input.checked : input.type === "range" ? Number(input.value) : input.value;
  feedback();
  save();
});
document.addEventListener("click", event => {
  const nav = event.target.closest("[data-page]");
  if (nav) { location.hash = nav.dataset.page; return; }
  const recorder = event.target.closest("[data-hotkey]");
  if (recorder) { recording = recorder.dataset.hotkey; feedback("按下新快捷键，Esc 取消。"); refresh(); return; }
  const clear = event.target.closest("[data-clear]");
  if (clear) {
    recording = null;
    settings[clear.dataset.clear] = "";
    if (clear.dataset.clear === "unlock_hotkey") { settings.unlock_enabled = false; settings.lock_toggle_enabled = false; }
    else settings.visibility_enabled = false;
    feedback();
    save();
    return;
  }
  if (recording) { recording = null; feedback(); refresh(); }
});
document.addEventListener("keydown", event => {
  if (!recording) return;
  if (event.key === "Tab") { recording = null; feedback(); refresh(); return; }
  event.preventDefault();
  if (event.key === "Escape") { recording = null; feedback(); refresh(); return; }
  if (["Control", "Alt", "Shift", "Meta"].includes(event.key)) return;
  const singleKey = /^(Home|End|Insert|Delete|PageUp|PageDown|Pause|F([1-9]|1[0-2]))$/;
  const aliases = { " ": "Space", ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right" };
  const key = aliases[event.key] || (event.key.length === 1 ? event.key.toUpperCase() : event.key);
  const supported = singleKey.test(key) || /^[A-Z0-9]$/.test(key) || ["Space", "Enter", "Backspace", "Up", "Down", "Left", "Right"].includes(key);
  if (event.metaKey || !supported || (!event.ctrlKey && !event.altKey && !singleKey.test(key))) {
    feedback("请使用 Ctrl / Alt 组合，或 Home、F1–F12 等单键。", true);
    return;
  }
  const candidate = [event.ctrlKey && "Ctrl", event.altKey && "Alt", event.shiftKey && "Shift", key].filter(Boolean).join("+");
  const otherKey = recording === "unlock_hotkey" ? "visibility_hotkey" : "unlock_hotkey";
  if (settings[otherKey] === candidate) { feedback("这个按键已用于另一项操作，请换一个。", true); return; }
  settings[recording] = candidate;
  recording = null;
  feedback();
  save();
});
document.querySelector("#reset-preview").addEventListener("click", () => {
  recording = null;
  Object.assign(settings, defaults);
  document.querySelector("#more-display").open = false;
  feedback();
  save();
});
window.addEventListener("hashchange", () => showPage(location.hash.slice(1)));
showPage(location.hash.slice(1));
