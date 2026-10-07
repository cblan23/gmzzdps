(() => {
  "use strict";

  const scene = document.getElementById("scene");
  const searchForm = document.querySelector(".search");
  const searchInput = searchForm?.querySelector("input");
  const navLinks = Array.from(document.querySelectorAll(".nav a"));
  const shareLink = document.querySelector(".share-link");
  const recentSearchesNode = document.getElementById("recentSearches");
  const API_ROOT = "/api/v1/dps/public";
  const RECENT_SEARCH_COOKIE = "gmzz_recent_searches";
  const RECENT_SEARCH_LIMIT = 5;
  const ECHARTS_URL = "vendor/echarts.min.js?v=20261006-echarts-1";
  const BOSS_ASSET_VERSION = "20261006-banner-2";
  const PROFESSION_ASSET_VERSION = "20261006-color-2";
  const RANKING_ASSET_VERSION = "20261007-art-1";
  const RANKING_CATEGORY_ART = {
    raid: "category-raid-v1.webp",
    party: "category-party-v1.webp",
    family: "category-family-v1.webp",
    daily: "category-daily-v1.webp",
  };
  const RANKING_DUNGEON_ART = {
    "may-manor-garden": "dungeon-may-manor-garden-v1.webp",
    "may-manor-castle": "dungeon-may-manor-castle-v1.webp",
    "emperor-returns": "dungeon-emperor-returns-v1.webp",
    "antigonus-notes": "dungeon-antigonus-notes-v1.webp",
    "abundant-tree": "dungeon-abundant-tree-v1.webp",
    "memory-johnny": "dungeon-memory-johnny-v1.webp",
    "memory-fire-dragon": "dungeon-memory-fire-dragon-v1.webp",
    "memory-bunny": "dungeon-memory-bunny-v1.webp",
    "memory-drill": "dungeon-memory-drill-v1.webp",
    "night-watch": "dungeon-night-watch-v1.webp",
    "special-duty": "dungeon-special-duty-v1.webp",
  };
  const PROFESSIONS = {
    1200001: "歌颂者",
    1200002: "观众",
    1200003: "占卜家",
    1200004: "仲裁人",
    1200005: "学徒",
    1200006: "战士",
    1200007: "窥秘人",
  };
  const PROFESSION_COLORS = {
    1200001: "#f2cd32",
    1200002: "#7ecfa5",
    1200003: "#5869c4",
    1200004: "#6687c5",
    1200005: "#68b6e5",
    1200006: "#ee8c2f",
    1200007: "#a255c7",
  };
  if (!scene || !searchForm || !searchInput) return;

  const portal = document.createElement("section");
  portal.className = "portal-shell";
  portal.setAttribute("aria-hidden", "true");
  portal.innerHTML = `
    <div class="portal-scroll">
      <div class="portal-inner">
        <header class="portal-heading">
          <button class="portal-back" type="button" aria-label="返回首页" title="返回首页">&#8592;</button>
          <div class="portal-title-wrap">
            <p class="portal-kicker"></p>
            <h1 class="portal-title"></h1>
          </div>
          <button class="portal-refresh" type="button">刷新数据</button>
        </header>
        <p class="portal-lead"></p>
        <div class="portal-content" aria-live="polite"></div>
      </div>
    </div>`;
  scene.appendChild(portal);

  const scrollArea = portal.querySelector(".portal-scroll");
  const kickerNode = portal.querySelector(".portal-kicker");
  const titleNode = portal.querySelector(".portal-title");
  const leadNode = portal.querySelector(".portal-lead");
  const contentNode = portal.querySelector(".portal-content");
  const backButton = portal.querySelector(".portal-back");
  const refreshButton = portal.querySelector(".portal-refresh");

  const state = {
    statistics: null,
    leaderboards: null,
    bossCatalog: null,
    bannerCatalog: null,
    catalog: null,
    skillNames: null,
    iconCatalog: null,
    basePromise: null,
    skillNamesPromise: null,
    renderToken: 0,
    insightsRequestToken: 0,
    insightsSelectedProfession: 0,
    insightsInitialized: false,
    insightsFilterOpen: false,
    insightsAdvancedOpen: null,
    insightsFilters: {
      boss: "name:子嗣守护",
      category: "raid",
      dungeon: "may-manor-castle",
      difficulty: "all",
      brassTome: "all",
      metric: "dps",
      ratingBasis: "extraordinary",
      minRating: 0,
      maxRating: 200000,
      gameVersion: "all",
      sort: "p50",
      calibrated: false,
    },
    encounterMode: "dps",
    encounterSlot: 0,
    encounterDetailTab: "overview",
    encounterCurveTab: "team-dps",
    detailReturnRoute: "#home",
    echartsPromise: null,
  };

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }

  function button(className, label, title) {
    const node = element("button", className, label);
    node.type = "button";
    if (title) {
      node.title = title;
      node.setAttribute("aria-label", title);
    }
    return node;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (character) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[character]);
  }

  function disposeCharts(root) {
    root?.querySelectorAll?.(".echart").forEach((node) => node._disposeChart?.());
  }

  function loadECharts() {
    if (window.echarts) return Promise.resolve(window.echarts);
    if (state.echartsPromise) return state.echartsPromise;
    state.echartsPromise = new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = ECHARTS_URL;
      script.async = true;
      script.addEventListener("load", () => resolve(window.echarts), {once: true});
      script.addEventListener("error", () => reject(new Error("echarts_load_failed")), {once: true});
      document.head.appendChild(script);
    }).catch((error) => {
      state.echartsPromise = null;
      throw error;
    });
    return state.echartsPromise;
  }

  function mountEChart(className, option, ariaLabel, onClick) {
    const container = element("div", `echart ${className}`);
    container.setAttribute("role", "img");
    container.setAttribute("aria-label", ariaLabel);
    let chart = null;
    let observer = null;
    const initialize = () => {
      if (chart || !container.isConnected || container.clientWidth < 10 || container.clientHeight < 10) return;
      if (!window.echarts) {
        container.replaceChildren(element("p", "detail-note", "图表组件加载失败，请刷新页面重试。"));
        return;
      }
      chart = window.echarts.init(container, null, {renderer: "canvas"});
      chart.setOption(option);
      if (onClick) chart.on("click", onClick);
    };
    observer = new ResizeObserver(() => {
      initialize();
      chart?.resize();
    });
    observer.observe(container);
    window.requestAnimationFrame(initialize);
    container._disposeChart = () => {
      observer?.disconnect();
      observer = null;
      chart?.dispose();
      chart = null;
    };
    return container;
  }

  function chartNumber(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    if (Math.abs(number) >= 100000000) return `${(number / 100000000).toFixed(number >= 1000000000 ? 1 : 2).replace(/\.0+$/, "")}亿`;
    if (Math.abs(number) >= 10000) return `${(number / 10000).toFixed(number >= 100000 ? 1 : 2).replace(/\.0+$/, "")}万`;
    return formatInteger(number);
  }

  function baseChartOption() {
    return {
      animationDuration: 450,
      textStyle: {fontFamily: '"Microsoft YaHei UI","PingFang SC",sans-serif', color: "#c8d5df"},
      aria: {enabled: true},
      tooltip: {
        trigger: "item",
        renderMode: "richText",
        backgroundColor: "rgba(7,15,23,.96)",
        borderColor: "#34495b",
        textStyle: {color: "#eef6fb", fontSize: 12},
      },
    };
  }

  async function fetchJson(url) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), 10000);
    try {
      const response = await fetch(url, {
        cache: "no-store",
        credentials: "omit",
        referrerPolicy: "no-referrer",
        signal: controller.signal,
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok || payload.ok === false) {
        throw new Error(payload.error || `HTTP ${response.status}`);
      }
      return payload;
    } finally {
      window.clearTimeout(timer);
    }
  }

  async function loadBaseData(force = false) {
    if (force) {
      state.statistics = null;
      state.leaderboards = null;
      state.bossCatalog = null;
      state.bannerCatalog = null;
      state.catalog = null;
      state.basePromise = null;
    }
    if (state.statistics && state.leaderboards && state.bossCatalog && state.bannerCatalog && state.catalog) return;
    if (state.basePromise) return state.basePromise;
    state.basePromise = Promise.all([
      fetchJson(`${API_ROOT}/statistics`),
      fetchJson(`${API_ROOT}/leaderboards?limit=500`),
      fetchJson("assets/bosses/boss_icon_sources.json"),
      fetchJson("assets/bosses/boss_banner_catalog.json"),
      fetchJson(`${API_ROOT}/catalog`),
      fetchJson("assets/icon_catalog.json"),
    ])
      .then(([statistics, leaderboards, bossCatalog, bannerCatalog, catalog, iconCatalog]) => {
        state.iconCatalog = iconCatalog;
        state.statistics = statistics.statistics || {};
        state.leaderboards = Array.isArray(leaderboards.leaderboards)
          ? leaderboards.leaderboards
          : [];
        state.bossCatalog = bossCatalog || {};
        state.bannerCatalog = bannerCatalog || {};
        state.catalog = catalog.catalog || {};
      })
      .finally(() => {
        state.basePromise = null;
      });
    return state.basePromise;
  }

  async function loadSkillNames() {
    if (state.skillNames) return;
    if (state.skillNamesPromise) return state.skillNamesPromise;
    state.skillNamesPromise = fetchJson("assets/skill_names.json")
      .then((payload) => {
        state.skillNames = payload && typeof payload === "object" && !Array.isArray(payload)
          ? payload
          : {};
      })
      .catch((error) => {
        console.warn("技能名称表加载失败", error);
        state.skillNames = {};
      })
      .finally(() => {
        state.skillNamesPromise = null;
      });
    return state.skillNamesPromise;
  }

  function formatInteger(value) {
    if (value === undefined || value === null || value === "") return "--";
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(number);
  }

  function formatCompact(value) {
    if (value === undefined || value === null || value === "") return "--";
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    if (Math.abs(number) < 10000) return formatInteger(number);
    return new Intl.NumberFormat("zh-CN", {
      notation: "compact",
      maximumFractionDigits: 1,
    }).format(number);
  }

  function formatDuration(value) {
    const seconds = Math.max(0, Math.round(Number(value) || 0));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const rest = seconds % 60;
    if (hours) return `${hours}时 ${String(minutes).padStart(2, "0")}分`;
    return `${minutes}:${String(rest).padStart(2, "0")}`;
  }

  function formatDateTime(value, includeSeconds = false) {
    const numeric = Number(value);
    const date = Number.isFinite(numeric) && numeric > 0
      ? new Date(numeric * 1000)
      : new Date(String(value || ""));
    if (!Number.isFinite(date.getTime())) return "--";
    const parts = Object.fromEntries(new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: includeSeconds ? "2-digit" : undefined,
      hourCycle: "h23",
    }).formatToParts(date).map((part) => [part.type, part.value]));
    return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}${includeSeconds ? `:${parts.second}` : ""}`;
  }

  function formatDate(value) {
    return formatDateTime(value);
  }

  function formatPercent(value) {
    if (value === undefined || value === null || value === "") return "--";
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    return `${(number * 100).toFixed(number >= 0.1 ? 1 : 2)}%`;
  }

  function professionName(id) {
    return PROFESSIONS[Number(id)] || "未知职业";
  }

  function professionColor(id) {
    return PROFESSION_COLORS[Number(id)] || "#71869a";
  }

  function displayAttributeName(value, fallback = "未知属性") {
    return (String(value || fallback).replace(/^非凡\s*/, "") || fallback).trim();
  }

  function professionIcon(id) {
    const image = element("img", "profession-icon");
    image.src = Object.hasOwn(PROFESSIONS, Number(id))
      ? `assets/professions/${Number(id)}.png?v=${PROFESSION_ASSET_VERSION}`
      : "assets/skills/0.png";
    image.alt = professionName(id);
    image.loading = "lazy";
    image.addEventListener("error", () => { image.src = "assets/skills/0.png"; }, { once: true });
    return image;
  }

  function normalizeRecentSearch(value) {
    return String(value || "").trim().replace(/\s+/g, " ").slice(0, 48);
  }

  function readRecentSearches() {
    const prefix = `${RECENT_SEARCH_COOKIE}=`;
    const entry = document.cookie.split("; ").find((value) => value.startsWith(prefix));
    if (!entry) return [];
    try {
      const values = JSON.parse(decodeURIComponent(entry.slice(prefix.length)));
      if (!Array.isArray(values)) return [];
      const searches = [];
      const seen = new Set();
      values.forEach((value) => {
        const query = normalizeRecentSearch(value);
        const key = query.toLocaleLowerCase("zh-CN");
        if (!query || seen.has(key) || searches.length >= RECENT_SEARCH_LIMIT) return;
        seen.add(key);
        searches.push(query);
      });
      return searches;
    } catch (_error) {
      return [];
    }
  }

  function writeRecentSearches(searches) {
    const secure = window.location.protocol === "https:" ? "; Secure" : "";
    document.cookie = `${RECENT_SEARCH_COOKIE}=${encodeURIComponent(JSON.stringify(searches))}; Path=/; Max-Age=15552000; SameSite=Lax${secure}`;
  }

  function saveRecentSearch(value) {
    const query = normalizeRecentSearch(value);
    if (!query) return;
    const key = query.toLocaleLowerCase("zh-CN");
    const searches = [query, ...readRecentSearches().filter(
      (item) => item.toLocaleLowerCase("zh-CN") !== key,
    )].slice(0, RECENT_SEARCH_LIMIT);
    writeRecentSearches(searches);
  }

  function renderRecentSearches() {
    if (!recentSearchesNode) return;
    const searches = readRecentSearches();
    recentSearchesNode.replaceChildren();
    recentSearchesNode.hidden = !searches.length;
    if (!searches.length) return;
    const list = element("div", "recent-searches-list");
    searches.forEach((query) => {
      const item = button("recent-search-item", query, `再次搜索 ${query}`);
      item.addEventListener("click", () => {
        searchInput.value = query;
        saveRecentSearch(query);
        renderRecentSearches();
        window.location.hash = `search?q=${encodeURIComponent(query)}`;
      });
      list.appendChild(item);
    });
    recentSearchesNode.append(element("span", "recent-searches-label", "最近搜索"), list);
  }

  function bossNameCandidates(bossName) {
    const exact = String(bossName || "").trim();
    if (!exact) return [];
    const withoutTitle = exact.replace(/^[“"][^”"]+[”"]\s*/u, "").trim();
    return [...new Set([exact, withoutTitle].filter(Boolean))];
  }

  function bossIconName(stageId, bossName = "") {
    for (const nameKey of bossNameCandidates(bossName)) {
      const named = state.bossCatalog?.stage_name_index?.[nameKey];
      if (!Array.isArray(named)) continue;
      const numericStageId = Number(stageId) || 0;
      const match = named.find((item) =>
        typeof item?.icon === "string"
        && Array.isArray(item.stage_ids)
        && item.stage_ids.map(Number).includes(numericStageId)
      ) || named.find((item) => typeof item?.icon === "string");
      if (match) return match.icon;
    }
    const stages = state.bossCatalog?.stages;
    const stage = stages && stages[String(Number(stageId) || 0)];
    return stage && typeof stage.icon === "string" ? stage.icon : "";
  }

  function bossIconUrl(filename) {
    return `assets/bosses/${encodeURIComponent(filename)}?v=${BOSS_ASSET_VERSION}`;
  }

  function bossBannerName(stageId, bossName = "") {
    for (const nameKey of bossNameCandidates(bossName)) {
      const named = state.bannerCatalog?.names?.[nameKey];
      if (typeof named === "string") return named;
    }
    const stageIdKey = String(Number(stageId) || 0);
    const filename = state.bannerCatalog?.stages?.[stageIdKey];
    if (typeof filename === "string") return filename;
    const iconName = bossIconName(stageId);
    const iconBanner = iconName && state.bannerCatalog?.icons?.[iconName];
    if (typeof iconBanner === "string") return iconBanner;
    return typeof state.bannerCatalog?.fallback === "string" ? state.bannerCatalog.fallback : "";
  }

  function bossBannerUrl(filename) {
    return `assets/bosses/banners/${encodeURIComponent(filename)}?v=${BOSS_ASSET_VERSION}`;
  }

  function bossIcon(stageId, className = "boss-icon", bossName = "") {
    const image = element("img", className);
    const filename = bossIconName(stageId, bossName);
    if (!filename) {
      image.hidden = true;
      return image;
    }
    image.src = bossIconUrl(filename);
    image.alt = "";
    image.loading = "lazy";
    image.addEventListener("error", () => {
      image.hidden = true;
    }, { once: true });
    return image;
  }

  function setPage(kicker, title, lead = "") {
    kickerNode.textContent = kicker;
    titleNode.textContent = title;
    leadNode.textContent = lead;
    leadNode.hidden = !lead;
    disposeCharts(contentNode);
    contentNode.classList.remove("is-updating");
    contentNode.removeAttribute("aria-busy");
    contentNode.replaceChildren();
    scrollArea.scrollTop = 0;
  }

  function showStatus(title, detail = "", retry = false) {
    const status = element("div", "portal-status");
    const wrap = element("div");
    wrap.appendChild(element("strong", "", title));
    if (detail) wrap.appendChild(element("span", "", detail));
    if (retry) {
      const action = button("portal-refresh error-action", "重新加载");
      action.addEventListener("click", () => renderRoute(true));
      wrap.appendChild(action);
    }
    status.appendChild(wrap);
    contentNode.replaceChildren(status);
  }

  function metricCard(label, value, note = "") {
    const card = element("article", "metric-card");
    card.appendChild(element("div", "metric-label", label));
    card.appendChild(element("div", "metric-value", value));
    if (note) card.appendChild(element("div", "metric-note", note));
    return card;
  }

  function sectionHeading(title, meta = "") {
    const head = element("div", "section-head");
    head.appendChild(element("h2", "", title));
    if (meta) head.appendChild(element("div", "section-meta", meta));
    return head;
  }

  function identityCell(row) {
    const cell = element("div", "identity-cell");
    cell.appendChild(professionIcon(row.profession_id));
    const copy = element("div");
    copy.appendChild(element("div", "identity-name", row.display_name || "未记录姓名"));
    copy.appendChild(element("div", "identity-kind", professionName(row.profession_id)));
    cell.appendChild(copy);
    return cell;
  }

  function bossCell(row) {
    const cell = element("div", "boss-cell");
    const image = bossIcon(row.stage_id, "boss-icon", row.boss_name);
    if (!image.hidden) cell.appendChild(image);
    const copy = element("div");
    copy.appendChild(element("div", "boss-name", row.boss_name || "未知首领"));
    copy.appendChild(element("div", "boss-stage"));
    cell.appendChild(copy);
    return cell;
  }

  function openEncounter(encounterId) {
    const id = String(encounterId || "");
    if (!/^enc_[A-Za-z0-9_-]{8,64}$/.test(id)) return;
    state.detailReturnRoute = window.location.hash || "#home";
    state.encounterSlot = 0;
    window.location.hash = `battle?id=${encodeURIComponent(id)}`;
  }

  function recordList(rows, options = {}) {
    const list = element("div", "records-list");
    const head = element("div", "record-head");
    ["名次", "玩家", "首领", "伤害 DPS", "总伤害", "发生时间", ""].forEach((label) => {
      head.appendChild(element("div", "", label));
    });
    list.appendChild(head);

    rows.forEach((row, index) => {
      const record = element("article", "record-row");
      const shownRank = options.sequentialRank ? index + 1 : Number(row.rank) || index + 1;
      record.appendChild(element("div", `rank-cell${shownRank <= 3 ? " is-top" : ""}`, shownRank));
      record.appendChild(identityCell(row));
      const boss = bossCell(row);
      const bossMeta = boss.querySelector(".boss-stage");
      if (row.dungeon_name) bossMeta.appendChild(element("span", "boss-tag dungeon-tag", row.dungeon_name));
      bossMeta.appendChild(element("span", "boss-tag difficulty-tag", performanceDifficultyLabel(row.difficulty || "unknown")));
      const brassLabel = brassTomeLabel(row.brass_tome_status);
      if (brassLabel) bossMeta.appendChild(element("span", `boss-tag tome-tag brass-${row.brass_tome_status || "unknown"}`, `黄铜书${brassLabel}`));
      bossMeta.classList.add("boss-tags");
      record.appendChild(boss);
      record.appendChild(element("div", "number-cell primary", formatInteger(row.dps)));
      record.appendChild(element("div", "number-cell", formatCompact(row.damage)));
      record.appendChild(element("div", "time-cell", formatDate(row.ended_at)));
      const open = button("row-open", "›", "查看战斗详情");
      open.addEventListener("click", () => openEncounter(row.encounter_id, row.profile_id));
      record.appendChild(open);
      list.appendChild(record);
    });
    return list;
  }

  function performanceMetricLabel(metric) {
    return metric === "boss_damage" ? "首领伤害" : "伤害 DPS";
  }

  function performanceDifficultyLabel(difficulty) {
    return {
      all: "全部难度",
      normal: "普通",
      hard: "困难",
      nightmare: "噩梦",
      epic: "史诗",
      heroic: "英雄",
      mythic: "神话",
      final_challenge: "终局挑战",
      unknown: "未标记难度",
    }[difficulty] || difficulty || "全部难度";
  }

  function brassTomeLabel(status) {
    return {
      enabled: "已开启",
      disabled: "未开启",
      unknown: "资料未确认",
    }[String(status || "unknown").toLowerCase()] || "";
  }

  function brassTomeFilterLabel(status) {
    return {
      all: "全部模式",
      enabled: "黄铜书开启",
      disabled: "黄铜书未开启",
      unknown: "资料未确认",
    }[String(status || "all").toLowerCase()] || "全部模式";
  }

  function performanceValue(value, metric) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    return metric === "boss_damage" ? formatCompact(number) : formatInteger(number);
  }

  async function fetchPerformance(filters) {
    const params = new URLSearchParams({
      boss: filters.boss,
      category: filters.category,
      dungeon: filters.dungeon,
      difficulty: filters.difficulty,
      brass_tome: filters.brassTome,
      metric: filters.metric,
      rating_basis: filters.ratingBasis,
      min_rating: String(filters.minRating),
      max_rating: String(filters.maxRating),
      game_version: filters.gameVersion,
      sort: filters.sort,
      calibrated: filters.calibrated ? "1" : "0",
    });
    const payload = await fetchJson(`${API_ROOT}/performance?${params}`);
    const performance = payload.performance || {};
    return performance;
  }

  function performancePosition(value, minimum, maximum) {
    if (!Number.isFinite(Number(value)) || maximum <= minimum) return 50;
    return Math.max(2, Math.min(98, (Number(value) - minimum) / (maximum - minimum) * 100));
  }

  function performanceFilterField(labelText, control) {
    const field = element("div", "performance-filter-field");
    field.appendChild(element("label", "", labelText));
    field.appendChild(control);
    return field;
  }

  function performanceSelect(options, current, onChange, labelText) {
    const wrap = element("div", "performance-select-wrap");
    const select = document.createElement("select");
    select.className = "performance-select";
    select.setAttribute("aria-label", labelText);
    options.forEach((item) => {
      const option = document.createElement("option");
      option.value = item.value;
      option.textContent = item.label;
      option.selected = item.value === current;
      option.disabled = Boolean(item.disabled);
      select.appendChild(option);
    });
    select.addEventListener("change", () => onChange(select.value));
    wrap.appendChild(select);
    wrap.appendChild(element("span", "performance-select-arrow", "⌄"));
    return wrap;
  }

  function performanceSegments(options, current, onChange) {
    const group = element("div", "performance-segments");
    options.forEach((item) => {
      const control = button(
        `performance-segment${item.value === current ? " is-active" : ""}`,
        item.label,
        item.title || "",
      );
      control.disabled = Boolean(item.disabled);
      control.addEventListener("click", () => onChange(item.value));
      group.appendChild(control);
    });
    return group;
  }

  function performanceConfidence(samples, encounters) {
    const sampleCount = Number(samples) || 0;
    const encounterCount = Number(encounters) || 0;
    if (encounterCount < 5 || sampleCount < 40) {
      return {
        tone: "low",
        label: "样本不足",
        text: "目前只有 " + formatInteger(encounterCount) + " 场战斗、"
          + formatInteger(sampleCount) + " 个角色样本，仅展示已有记录，暂不用于判断职业强弱。",
      };
    }
    if (encounterCount < 15 || sampleCount < 120) {
      return {
        tone: "medium",
        label: "初步参考",
        text: "已有 " + formatInteger(encounterCount) + " 场战斗、"
          + formatInteger(sampleCount) + " 个角色样本，可以观察趋势，仍可能受队伍配置和打法影响。",
      };
    }
    return {
      tone: "high",
      label: "样本较充分",
      text: "当前筛选包含 " + formatInteger(encounterCount) + " 场战斗、"
        + formatInteger(sampleCount) + " 个角色样本，可用于观察这一条件下的职业表现分布。",
    };
  }

  function professionSampleConfidence(sampleCount) {
    const count = Number(sampleCount) || 0;
    if (count < 4) return {tone: "low", label: "样本极少"};
    if (count < 10) return {tone: "medium", label: "样本较少"};
    return {tone: "high", label: "可作参考"};
  }

  function performanceSummaryItem(labelText, value, note) {
    const item = element("div", "performance-summary-item");
    item.appendChild(element("span", "", labelText));
    item.appendChild(element("strong", "", value));
    if (note) item.appendChild(element("small", "", note));
    return item;
  }

  function performanceMetricItem(labelText, value, className = "", note = "") {
    const item = element("div", "performance-row-metric" + (className ? " " + className : ""));
    item.appendChild(element("span", "", labelText));
    item.appendChild(element("strong", "", value));
    if (note) item.appendChild(element("small", "", note));
    return item;
  }

  function performanceSortLabel(value) {
    return {
      p50: "P50 中位",
      p75: "P75 较好表现",
      p90: "P90 优秀表现",
      best: "最佳单场",
      sample_count: "样本数量",
    }[value] || "P50 中位";
  }

  function performanceFilterGroup(title, note = "", options = {}) {
    const collapsible = Boolean(options.collapsible);
    const group = element(
      collapsible ? "details" : "section",
      `performance-filter-group${collapsible ? " is-collapsible" : ""}`,
    );
    if (collapsible) group.open = Boolean(options.open);
    const head = element(collapsible ? "summary" : "div", "performance-filter-group-head");
    head.appendChild(element("strong", "", title));
    if (note) head.appendChild(element("span", "", note));
    const body = element("div", "performance-filter-group-body");
    group.append(head, body);
    return {group, body};
  }

  function performanceResults(groups, filters) {
    const results = element("section", "performance-results performance-card");
    const head = element("div", "performance-results-head");
    const copy = element("div");
    copy.append(
      element("h2", "", "职业数据明细"),
      element("p", "", "直接查看各职业的中位、常见区间、优秀表现和最佳单场"),
    );
    head.append(copy, element("span", "", `${groups.length} 个职业`));
    results.appendChild(head);
    const rows = element("div", "performance-rows");
    groups.forEach((group, index) => {
      const row = element("article", "performance-row");
      row.style.setProperty("--profession-accent", professionColor(group.profession_id));
      const identity = element("div", "performance-profession");
      identity.appendChild(element("span", "performance-rank", `#${index + 1}`));
      const icon = professionIcon(group.profession_id);
      icon.className = "performance-profession-icon";
      const professionCopy = element("div", "performance-profession-copy");
      const confidence = professionSampleConfidence(group.sample_count);
      professionCopy.append(
        element("h3", "", professionName(group.profession_id)),
        element("p", "", `${formatInteger(group.sample_count)} 个角色样本`),
        element("span", `performance-sample-badge is-${confidence.tone}`, confidence.label),
      );
      identity.append(icon, professionCopy);

      const metrics = element("div", "performance-row-metrics");
      metrics.append(
        performanceMetricItem("P50 中位", performanceValue(group.p50, filters.metric), "is-primary", "典型表现"),
        performanceMetricItem(
          "P25–P75 常见区间",
          `${performanceValue(group.p25, filters.metric)} – ${performanceValue(group.p75, filters.metric)}`,
          "is-range",
          "中间 50% 样本",
        ),
        performanceMetricItem("P90 优秀表现", performanceValue(group.p90, filters.metric), "", "前 10% 门槛"),
        performanceMetricItem("最佳单场", performanceValue(group.best, filters.metric), "is-best", "当前筛选内"),
      );
      const footer = element("div", "performance-row-footer");
      footer.appendChild(element(
        "p",
        "",
        `P10 ${performanceValue(group.p10, filters.metric)} · 超凡评分中位 ${formatInteger(group.target_rating)}`,
      ));
      const encounterId = String(group.best_record?.encounter_id || "");
      const open = button("performance-record-link", "查看最佳单场 →");
      open.disabled = !/^enc_[A-Za-z0-9_-]{8,64}$/.test(encounterId);
      if (!open.disabled) open.addEventListener("click", () => openEncounter(encounterId));
      footer.appendChild(open);
      row.append(identity, metrics, footer);
      rows.appendChild(row);
    });
    if (groups.length) results.appendChild(rows);
    return results;
  }

  function dungeonCategories() {
    return Array.isArray(state.catalog?.dungeon_categories)
      ? state.catalog.dungeon_categories
      : [];
  }

  function rankingCategories() {
    const order = new Map(["raid", "party", "family", "daily"].map((key, index) => [key, index]));
    return [...dungeonCategories()].sort(
      (left, right) => (order.get(left.key) ?? 99) - (order.get(right.key) ?? 99),
    );
  }

  function dungeonCategory(categoryKey) {
    return dungeonCategories().find((category) => category.key === categoryKey) || null;
  }

  function dungeonEntry(categoryKey, dungeonKey) {
    const category = dungeonCategory(categoryKey);
    return (category?.dungeons || []).find((dungeon) => dungeon.key === dungeonKey) || null;
  }

  function preferredDungeon(category) {
    const dungeons = Array.isArray(category?.dungeons) ? category.dungeons : [];
    return dungeons.find((dungeon) => Number(dungeon.included) > 0)
      || dungeons.find((dungeon) => Number(dungeon.records) > 0)
      || dungeons[0]
      || null;
  }

  function preferredBoss(dungeon) {
    const bosses = Array.isArray(dungeon?.bosses) ? dungeon.bosses : [];
    return bosses.find((boss) => Number(boss.included) > 0)
      || bosses.find((boss) => Number(boss.records) > 0)
      || bosses[0]
      || null;
  }

  function applyRankingArtwork(node, artwork) {
    if (!node || typeof artwork !== "string") return;
    node.style.setProperty(
      "--ranking-art",
      `url("assets/ranking/${encodeURIComponent(artwork)}?v=${RANKING_ASSET_VERSION}")`,
    );
  }

  function professionAssetUrl(professionId) {
    return Object.hasOwn(PROFESSIONS, Number(professionId))
      ? `assets/professions/${Number(professionId)}.png?v=${PROFESSION_ASSET_VERSION}`
      : "assets/skills/0.png";
  }

  function performanceChartOption(groups, filters) {
    const professionIds = groups.map((group) => String(group.profession_id));
    const rich = {
      name: {color: "#eef6fb", fontSize: 15, fontWeight: 700, padding: [0, 0, 0, 9]},
      rank: {color: "#8397a9", fontSize: 12, width: 24, align: "right"},
    };
    groups.forEach((group) => {
      rich[`p${group.profession_id}`] = {
        width: 38,
        height: 38,
        backgroundColor: {image: professionAssetUrl(group.profession_id)},
      };
    });
    const maximum = Math.max(1, ...groups.map((group) => Number(group.best) || 0));
    const tooltip = (parameters) => {
      const first = Array.isArray(parameters) ? parameters[0] : parameters;
      const group = groups[Number(first?.dataIndex) || 0];
      if (!group) return "";
      const metric = (value) => escapeHtml(performanceValue(value, filters.metric));
      return `<div class="performance-tooltip">
        <div class="performance-tooltip-head"><img src="${professionAssetUrl(group.profession_id)}" alt=""><div><b>${escapeHtml(professionName(group.profession_id))}</b><span>${formatInteger(group.sample_count)} 个角色样本</span></div></div>
        <div class="performance-tooltip-grid"><span>P10<b>${metric(group.p10)}</b></span><span>P25<b>${metric(group.p25)}</b></span><span>P50 中位<b>${metric(group.p50)}</b></span><span>P75<b>${metric(group.p75)}</b></span><span>P90<b>${metric(group.p90)}</b></span><span>最佳值<b>${metric(group.best)}</b></span></div>
        <div class="performance-tooltip-foot">超凡评分中位 ${formatInteger(group.target_rating)}</div>
      </div>`;
    };
    return {
      ...baseChartOption(),
      animationDuration: 560,
      grid: {left: 208, right: 72, top: 42, bottom: 68, containLabel: false},
      tooltip: {
        trigger: "axis",
        renderMode: "html",
        confine: true,
        axisPointer: {type: "shadow", shadowStyle: {color: "rgba(121,188,224,.045)"}},
        backgroundColor: "rgba(7,15,23,.98)",
        borderColor: "#3b5265",
        padding: 0,
        extraCssText: "border-radius:10px;box-shadow:0 18px 50px rgba(0,0,0,.42)",
        formatter: tooltip,
      },
      xAxis: {
        type: "value",
        min: 0,
        max: Math.ceil(maximum * 1.12),
        name: performanceMetricLabel(filters.metric),
        nameLocation: "middle",
        nameGap: 44,
        nameTextStyle: {color: "#91a4b5", fontSize: 12, fontWeight: 600},
        axisLine: {show: false},
        axisTick: {show: false},
        axisLabel: {color: "#8397a9", fontSize: 12, formatter: chartNumber},
        splitLine: {lineStyle: {color: "rgba(126,151,173,.10)", type: "dashed"}},
      },
      yAxis: {
        type: "category",
        inverse: true,
        data: professionIds,
        axisLine: {show: false},
        axisTick: {show: false},
        axisLabel: {
          margin: 20,
          formatter: (value, index) => `{rank|${index + 1}} {p${value}|} {name|${professionName(value)}}`,
          rich,
        },
      },
      series: [
        {
          name: "P50 中位",
          type: "bar",
          barWidth: 24,
          z: 1,
          data: groups.map((group) => ({
            value: Number(group.p50) || 0,
            itemStyle: {
              color: professionColor(group.profession_id) + "55",
              borderColor: professionColor(group.profession_id) + "aa",
              borderWidth: 1,
              borderRadius: [0, 6, 6, 0],
            },
          })),
          label: {
            show: false,
          },
        },
        {
          name: "表现分布",
          type: "custom",
          z: 4,
          silent: true,
          data: groups.map((group) => [
            Number(group.p10) || 0,
            Number(group.p25) || 0,
            Number(group.p50) || 0,
            Number(group.p75) || 0,
            Number(group.p90) || 0,
            Number(group.best) || 0,
            String(group.profession_id),
          ]),
          renderItem: (params, api) => {
            const category = params.dataIndex;
            const point = (value) => api.coord([value, category]);
            const p10 = point(api.value(0));
            const p25 = point(api.value(1));
            const p50 = point(api.value(2));
            const p75 = point(api.value(3));
            const p90 = point(api.value(4));
            const best = point(api.value(5));
            const color = professionColor(groups[params.dataIndex]?.profession_id);
            return {
              type: "group",
              children: [
                {type: "line", shape: {x1: p10[0], y1: p10[1], x2: p90[0], y2: p90[1]}, style: {stroke: "#7891a5", lineWidth: 3}},
                {type: "rect", shape: {x: p25[0], y: p25[1] - 7, width: Math.max(2, p75[0] - p25[0]), height: 14, r: 7}, style: {fill: color, opacity: 0.9}},
                {type: "circle", shape: {cx: p50[0], cy: p50[1], r: 6}, style: {fill: "#f3f8fb", stroke: color, lineWidth: 3}},
                {type: "circle", shape: {cx: p90[0], cy: p90[1], r: 5}, style: {fill: "#101c27", stroke: "#b9a4ff", lineWidth: 3}},
                {type: "polygon", shape: {points: [[best[0], best[1] - 6], [best[0] + 6, best[1]], [best[0], best[1] + 6], [best[0] - 6, best[1]]]}, style: {fill: "#f1c46f", stroke: "#fff0c5", lineWidth: 1}},
                {
                  type: "text",
                  style: {
                    x: p50[0] + 8,
                    y: p50[1] - 18,
                    text: chartNumber(api.value(2)),
                    fill: "#dce9f1",
                    font: "700 13px 'Microsoft YaHei UI'",
                    align: "left",
                    verticalAlign: "middle",
                  },
                },
              ],
            };
          },
        },
      ],
      media: [{
        query: {maxWidth: 620},
        option: {grid: {left: 148, right: 42, top: 32, bottom: 62}},
      }],
    };
  }

  function renderPerformancePage(performance, routeToken) {
    const filters = state.insightsFilters;
    const selection = performance.selection || {};
    const availability = performance.availability || {};
    const groups = Array.isArray(performance.groups) ? performance.groups : [];
    const fragment = document.createDocumentFragment();
    const selectedCategory = dungeonCategory(filters.category);
    const selectedDungeon = dungeonEntry(filters.category, filters.dungeon);

    const syncRoute = () => {
      const params = new URLSearchParams({
        category: filters.category,
        dungeon: filters.dungeon,
        boss: filters.boss,
        difficulty: filters.difficulty,
        brass: filters.brassTome,
        metric: filters.metric,
        minRating: String(filters.minRating),
        maxRating: String(filters.maxRating),
        version: filters.gameVersion,
        sort: filters.sort,
        calibrated: filters.calibrated ? "1" : "0",
      });
      window.history.replaceState(null, "", `#insights?${params}`);
    };
    const updateFilters = (patch) => {
      Object.assign(filters, patch);
      filters.ratingBasis = "extraordinary";
      state.insightsSelectedProfession = 0;
      syncRoute();
      renderInsights(routeToken, true).catch(() => {
        if (routeToken === state.renderToken) showStatus("统计暂时无法加载", "请稍后重试。", true);
      });
    };

    const overview = element("section", "performance-overview performance-card");
    const overviewCopy = element("div", "performance-overview-copy");
    overviewCopy.appendChild(element("span", "performance-source", "真实上传统计"));
    overviewCopy.appendChild(element("h2", "", selection.boss_name || "暂无 Boss 数据"));
    const contextText = [
      selectedCategory?.name,
      selectedDungeon?.name || selection.dungeon_name,
      performanceDifficultyLabel(filters.difficulty),
      brassTomeFilterLabel(filters.brassTome),
    ].filter(Boolean).join(" · ");
    overviewCopy.appendChild(element("p", "", contextText));
    overview.appendChild(overviewCopy);
    const summary = element("div", "performance-summary-grid");
    summary.append(
      performanceSummaryItem("有效战斗", `${formatInteger(performance.total_encounters)} 场`, "符合当前全部条件"),
      performanceSummaryItem("角色样本", formatInteger(performance.total_samples), "每名角色每场计一个样本"),
      performanceSummaryItem("超凡评分范围", `${formatInteger(filters.minRating)} – ${formatInteger(filters.maxRating)}`, "当前筛选区间"),
      performanceSummaryItem("数据更新", formatDate(performance.updated_at), "北京时间"),
    );
    overview.appendChild(summary);
    fragment.appendChild(overview);

    const layout = element("div", "performance-layout");
    const main = element("div", "performance-main");
    const confidence = performanceConfidence(performance.total_samples, performance.total_encounters);
    const confidenceBanner = element("section", "performance-confidence is-" + confidence.tone);
    confidenceBanner.appendChild(element("strong", "", confidence.label));
    confidenceBanner.appendChild(element("p", "", confidence.text));
    main.appendChild(confidenceBanner);

    const chartCard = element("section", "performance-chart-card performance-card");
    const chartHead = element("div", "performance-chart-head");
    const chartCopy = element("div");
    chartCopy.appendChild(element("h2", "", "各职业强度"));
    chartCopy.appendChild(element(
      "p",
      "",
      `${performanceMetricLabel(filters.metric)} · ${performanceSortLabel(filters.sort)}排序 · 超凡评分 ${formatInteger(filters.minRating)}–${formatInteger(filters.maxRating)}`,
    ));
    chartHead.appendChild(chartCopy);
    chartHead.appendChild(element("span", "performance-chart-count", `${groups.length} 个职业`));
    chartCard.appendChild(chartHead);
    if (groups.length) {
      const chart = mountEChart(
        "performance-chart",
        performanceChartOption(groups, filters),
        "各职业 P25、P50、P75、P90 和最佳表现对比图",
      );
      chart.style.height = `${Math.max(500, groups.length * 78 + 100)}px`;
      chartCard.appendChild(chart);
      const legend = element("div", "performance-chart-legend");
      [
        ["is-range", "P25–P75 常见区间"],
        ["is-median", "P50 中位"],
        ["is-p90", "P90 优秀表现"],
        ["is-best", "最佳单场"],
      ].forEach(([className, label]) => {
        const item = element("span");
        item.append(element("i", className), label);
        legend.appendChild(item);
      });
      chartCard.appendChild(legend);
      chartCard.appendChild(element("p", "performance-chart-tip", "将鼠标移到任一职业上，可查看样本量和完整分位数。"));
    } else {
      const empty = element("div", "performance-empty");
      empty.appendChild(element("strong", "", "当前条件下暂无有效样本"));
      empty.appendChild(element("span", "", "可调整 Boss、黄铜书状态或超凡评分区间后重试。"));
      chartCard.appendChild(empty);
    }
    main.appendChild(chartCard);
    if (groups.length) main.appendChild(performanceResults(groups, filters));
    const method = element("section", "performance-method performance-card");
    method.appendChild(element("strong", "", "图表怎么看"));
    method.appendChild(element("p", "", "柱长表示 P50 中位表现；彩色粗线是 P25–P75 常见区间，紫色圆点是 P90，金色菱形是最佳单场。请同时查看样本量和区间跨度，避免用少量记录直接判断职业强弱。"));
    main.appendChild(method);

    const sidebar = element("aside", "performance-sidebar");
    const filter = element("details", "performance-filter performance-card");
    filter.open = window.matchMedia("(min-width: 801px)").matches || state.insightsFilterOpen;
    const filterHead = element("summary", "performance-filter-head");
    const filterCopy = element("div");
    filterCopy.appendChild(element("strong", "", "调整对比条件"));
    filterCopy.appendChild(element(
      "span",
      "performance-filter-current",
      `${selectedDungeon?.name || "选择副本"} · ${selection.boss_name || "选择 Boss"}`,
    ));
    filterHead.append(filterCopy, element("span", "performance-filter-toggle", "筛选"));
    filterHead.addEventListener("click", (event) => {
      if (window.matchMedia("(min-width: 801px)").matches) event.preventDefault();
    });
    filter.addEventListener("toggle", () => {
      if (!window.matchMedia("(min-width: 801px)").matches) {
        state.insightsFilterOpen = filter.open;
      }
    });
    filter.appendChild(filterHead);
    const filterBody = element("div", "performance-filter-stack");
    const scopeGroup = performanceFilterGroup("战斗范围", "选择同一副本和 Boss");
    const metricGroup = performanceFilterGroup("统计口径", "选择比较指标");
    const ratingGroup = performanceFilterGroup("超凡评分", "按 2 万分区间快速筛选");
    const advancedSelectionActive = filters.calibrated || filters.gameVersion !== "all" || filters.sort !== "p50";
    const advancedGroup = performanceFilterGroup("更多选项", "校准、版本与排序", {
      collapsible: true,
      open: state.insightsAdvancedOpen ?? advancedSelectionActive,
    });
    advancedGroup.group.addEventListener("toggle", () => {
      state.insightsAdvancedOpen = advancedGroup.group.open;
    });
    filterBody.append(scopeGroup.group, metricGroup.group, ratingGroup.group, advancedGroup.group);

    const categories = dungeonCategories();
    scopeGroup.body.appendChild(performanceFilterField("副本类型", performanceSegments(
      categories.map((category) => ({value: category.key, label: category.name})),
      filters.category,
      (value) => {
        const category = dungeonCategory(value);
        const dungeon = preferredDungeon(category);
        const boss = preferredBoss(dungeon);
        updateFilters({
          category: value,
          dungeon: dungeon?.key || "all",
          boss: `name:${boss?.name || ""}`,
          difficulty: "all",
          brassTome: "all",
          gameVersion: "all",
        });
      },
    )));

    const dungeonOptions = (selectedCategory?.dungeons || []).map((dungeon) => ({
      value: dungeon.key,
      label: dungeon.name,
    }));
    scopeGroup.body.appendChild(performanceFilterField("副本", performanceSelect(
      dungeonOptions,
      filters.dungeon,
      (value) => {
        const dungeon = dungeonEntry(filters.category, value);
        const boss = preferredBoss(dungeon);
        updateFilters({
          dungeon: value,
          boss: `name:${boss?.name || ""}`,
          difficulty: "all",
          brassTome: "all",
          gameVersion: "all",
        });
      },
      "副本",
    )));

    const bossOptions = (selectedDungeon?.bosses || []).map((boss) => ({
      value: `name:${boss.name}`,
      label: boss.name,
    }));
    if (!bossOptions.some((boss) => boss.value === filters.boss)) {
      bossOptions.unshift({value: filters.boss, label: selection.boss_name || "暂无 Boss 数据"});
    }
    if (selectedCategory?.key !== "family") {
      scopeGroup.body.appendChild(performanceFilterField("Boss", performanceSelect(
        bossOptions,
        filters.boss,
        (value) => updateFilters({boss: value, difficulty: "all", brassTome: "all", gameVersion: "all"}),
        "Boss",
      )));
    }

    const dungeonDifficulties = Array.from(new Set(selectedDungeon?.difficulties || []));
    scopeGroup.body.appendChild(performanceFilterField("难度", performanceSegments([
      {value: "all", label: "全部"},
      ...dungeonDifficulties.map((value) => ({value, label: performanceDifficultyLabel(value)})),
    ], filters.difficulty, (value) => updateFilters({difficulty: value}))));

    const dungeonBrassStatuses = new Set(selectedDungeon?.brass_tome_statuses || []);
    scopeGroup.body.appendChild(performanceFilterField("黄铜书模式", performanceSegments([
      {value: "all", label: "全部"},
      {value: "enabled", label: "开启", disabled: dungeonBrassStatuses.size > 0 && !dungeonBrassStatuses.has("enabled")},
      {value: "disabled", label: "未开启", disabled: dungeonBrassStatuses.size > 0 && !dungeonBrassStatuses.has("disabled")},
      {value: "unknown", label: "未确认", disabled: dungeonBrassStatuses.size > 0 && !dungeonBrassStatuses.has("unknown")},
    ], filters.brassTome, (value) => updateFilters({brassTome: value}))));

    metricGroup.body.appendChild(performanceFilterField("统计指标", performanceSegments([
      {value: "dps", label: "DPS"},
      {value: "boss_damage", label: "Boss 伤害"},
    ], filters.metric, (value) => updateFilters({metric: value}))));

    const rangeMaximum = Math.max(
      200000,
      Math.ceil(Number(availability.rating_max || filters.maxRating || 0) / 20000) * 20000,
    );
    const range = element("div", "performance-range-control");
    const rangeLabels = element("div", "performance-range-labels");
    const minimumLabel = element("span", "", formatInteger(filters.minRating));
    const maximumLabel = element("span", "", formatInteger(filters.maxRating));
    rangeLabels.append(minimumLabel, maximumLabel);
    range.appendChild(rangeLabels);
    const slider = element("div", "performance-range-slider");
    const lower = document.createElement("input");
    const upper = document.createElement("input");
    [lower, upper].forEach((input) => {
      input.type = "range";
      input.min = "0";
      input.max = String(rangeMaximum);
      input.step = "1000";
    });
    lower.setAttribute("aria-label", "最低超凡评分");
    upper.setAttribute("aria-label", "最高超凡评分");
    lower.value = String(Math.min(filters.minRating, rangeMaximum));
    upper.value = String(Math.min(filters.maxRating, rangeMaximum));
    const updateRangeVisual = () => {
      let low = Number(lower.value);
      let high = Number(upper.value);
      if (low > high - 1000) {
        if (document.activeElement === lower) low = high - 1000;
        else high = low + 1000;
      }
      low = Math.max(0, low);
      high = Math.min(rangeMaximum, high);
      lower.value = String(low);
      upper.value = String(high);
      minimumLabel.textContent = formatInteger(low);
      maximumLabel.textContent = formatInteger(high);
      slider.style.setProperty("--range-left", `${low / rangeMaximum * 100}%`);
      slider.style.setProperty("--range-right", `${100 - high / rangeMaximum * 100}%`);
    };
    lower.addEventListener("input", updateRangeVisual);
    upper.addEventListener("input", updateRangeVisual);
    lower.addEventListener("change", () => updateFilters({minRating: Number(lower.value), maxRating: Number(upper.value)}));
    upper.addEventListener("change", () => updateFilters({minRating: Number(lower.value), maxRating: Number(upper.value)}));
    slider.append(lower, upper);
    range.appendChild(slider);
    const presets = element("div", "performance-range-presets");
    const ratingPresets = [["全部", 0, rangeMaximum]];
    for (let minimum = 100000; minimum < rangeMaximum; minimum += 20000) {
      const maximum = Math.min(minimum + 20000, rangeMaximum);
      ratingPresets.push([`${minimum / 10000}–${maximum / 10000}万`, minimum, maximum]);
    }
    ratingPresets.forEach(([label, minimum, maximum]) => {
      const preset = button(
        `performance-range-preset${filters.minRating === minimum && filters.maxRating === maximum ? " is-active" : ""}`,
        label,
      );
      preset.addEventListener("click", () => updateFilters({minRating: minimum, maxRating: maximum}));
      presets.appendChild(preset);
    });
    range.appendChild(presets);
    updateRangeVisual();
    ratingGroup.body.appendChild(performanceFilterField("评分范围", range));

    advancedGroup.body.appendChild(performanceFilterField("对比口径", performanceSegments([
      {value: "raw", label: "原始表现"},
      {value: "calibrated", label: "评分校准", title: "按职业内评分变化校准到中位评分"},
    ], filters.calibrated ? "calibrated" : "raw", (value) => updateFilters({calibrated: value === "calibrated"}))));
    const versionOptions = [{value: "all", label: "全部版本"}];
    (availability.game_versions || []).forEach((value) => versionOptions.push({
      value,
      label: value === "unknown" ? "未标记版本" : value,
    }));
    advancedGroup.body.appendChild(performanceFilterField("游戏版本", performanceSelect(
      versionOptions,
      filters.gameVersion,
      (value) => updateFilters({gameVersion: value}),
      "游戏版本",
    )));
    advancedGroup.body.appendChild(performanceFilterField("排序方式", performanceSelect([
      {value: "p50", label: "P50 中位"},
      {value: "p75", label: "P75 较好表现"},
      {value: "p90", label: "P90 优秀表现"},
      {value: "best", label: "最佳单场"},
      {value: "sample_count", label: "样本数量"},
    ], filters.sort, (value) => updateFilters({sort: value}), "排序方式")));
    filter.appendChild(filterBody);
    const filterFooter = element("div", "performance-filter-footer");
    const rules = element("div", "performance-rules");
    ["仅完整战斗", "仅胜利记录", "排除人机与投影"].forEach((label) => {
      const item = element("span");
      item.append(element("i", "", "✓"), label);
      rules.appendChild(item);
    });
    filterFooter.appendChild(rules);
    filterFooter.appendChild(element(
      "p",
      "",
      filters.calibrated && !Number(performance.calibration?.applied_groups)
        ? "当前样本不足，评分校准尚未生效。"
        : "评分范围按每名角色上传的超凡评分筛选。",
    ));
    const resetFilters = button("performance-filter-reset", "恢复默认条件");
    resetFilters.addEventListener("click", () => {
      state.insightsAdvancedOpen = false;
      updateFilters({
        category: "raid",
        dungeon: "may-manor-castle",
        boss: "name:子嗣守护",
        difficulty: "all",
        brassTome: "all",
        metric: "dps",
        minRating: 0,
        maxRating: 200000,
        gameVersion: "all",
        sort: "p50",
        calibrated: false,
      });
    });
    filterFooter.appendChild(resetFilters);
    filter.appendChild(filterFooter);
    sidebar.appendChild(filter);
    layout.append(main, sidebar);
    fragment.appendChild(layout);
    contentNode.replaceChildren(fragment);
  }
  async function renderInsights(token, preserveContent = false) {
    if (!preserveContent) {
      setPage(
        "PROFESSION ANALYSIS / 职业分析",
        "职业表现分析",
        "选择同一副本、Boss 和评分范围，查看各职业的典型表现与完整分布。",
      );
      showStatus("正在计算职业表现分布");
    } else {
      contentNode.classList.add("is-updating");
      contentNode.setAttribute("aria-busy", "true");
    }
    const requestToken = ++state.insightsRequestToken;
    try {
      await Promise.all([loadBaseData(), loadECharts()]);
      if (token !== state.renderToken) return;
      if (!state.insightsInitialized) {
        const filters = state.insightsFilters;
        let category = dungeonCategory(filters.category);
        if (!category) {
          category = dungeonCategories().find((item) => item.key === "raid")
            || dungeonCategories().find((item) => Number(item.included) > 0)
            || dungeonCategories()[0]
            || null;
        }
        let dungeon = dungeonEntry(category?.key, filters.dungeon);
        if (!dungeon) dungeon = preferredDungeon(category);
        const bosses = Array.isArray(dungeon?.bosses) ? dungeon.bosses : [];
        let boss = bosses.find((item) => `name:${item.name}` === filters.boss);
        if (!boss) boss = preferredBoss(dungeon);
        filters.category = category?.key || "all";
        filters.dungeon = dungeon?.key || "all";
        filters.boss = `name:${boss?.name || ""}`;
        filters.ratingBasis = "extraordinary";
      }
      state.insightsInitialized = true;
      const performance = await fetchPerformance(state.insightsFilters);
      if (token !== state.renderToken || requestToken !== state.insightsRequestToken) return;
      renderPerformancePage(performance, token);
    } finally {
      if (requestToken === state.insightsRequestToken) {
        contentNode.classList.remove("is-updating");
        contentNode.removeAttribute("aria-busy");
      }
    }
  }

  function resolveRankingFilters(params) {
    const categories = rankingCategories();
    let category = dungeonCategory(params.get("category"));
    if (!category) {
      category = categories.find((item) => item.key === "raid") || categories[0] || null;
    }
    const dungeon = dungeonEntry(category?.key, params.get("dungeon"));
    const bosses = Array.isArray(dungeon?.bosses) ? dungeon.bosses : [];
    const requestedBoss = params.get("boss") || "";
    const boss = bosses.find((item) => item.name === requestedBoss)
      || (category?.key === "family" ? preferredBoss(dungeon) : null);
    const knownDifficulties = new Set(dungeon?.difficulties || []);
    const requestedDifficulty = params.get("difficulty") || "all";
    const difficulty = requestedDifficulty === "all" || knownDifficulties.has(requestedDifficulty)
      ? requestedDifficulty
      : "all";
    const requestedBrass = params.get("brass") || "all";
    const brass = ["all", "enabled", "disabled", "unknown"].includes(requestedBrass)
      ? requestedBrass
      : "all";
    const profession = Object.hasOwn(PROFESSIONS, Number(params.get("profession")))
      ? String(Number(params.get("profession")))
      : "";
    return {category, dungeon, boss, difficulty, brass, profession};
  }

  function rankingFilters(params, selected) {
    const panel = element("section", "ranking-filter performance-card");
    const head = element("div", "ranking-filter-head");
    const copy = element("div");
    copy.append(
      element("strong", "", "排行筛选"),
      element("span", "", selected.category?.key === "family"
        ? "家族本选定后直接显示对应 Boss"
        : "先选类型，再选择具体副本和 Boss"),
    );
    head.append(copy, element("span", "performance-filter-tag", "RANKINGS"));
    panel.appendChild(head);
    const update = (patch, remove = []) => {
      const next = new URLSearchParams(params);
      remove.forEach((name) => next.delete(name));
      Object.entries(patch).forEach(([name, value]) => {
        if (value) next.set(name, value);
        else next.delete(name);
      });
      window.location.hash = `records?${next}`;
    };

    const categoryTabs = element("div", "ranking-category-tabs");
    rankingCategories().forEach((category) => {
      const tab = button(`ranking-category${category.key === selected.category?.key ? " is-active" : ""}`);
      applyRankingArtwork(tab, RANKING_CATEGORY_ART[category.key]);
      tab.append(
        element("strong", "", category.name),
        element("span", "", `总计 ${formatInteger(category.ranking_entries)} 条可排行`),
      );
      tab.addEventListener("click", () => update(
        {category: category.key},
        ["dungeon", "boss", "difficulty", "brass"],
      ));
      categoryTabs.appendChild(tab);
    });
    panel.appendChild(categoryTabs);

    const controls = element("div", "ranking-filter-grid");
    const dungeonField = element("div", "ranking-choice-field ranking-dungeon-field");
    dungeonField.appendChild(element("div", "ranking-choice-label", "选择副本"));
    const dungeons = element("div", "ranking-dungeons");
    (selected.category?.dungeons || []).forEach((dungeon) => {
      const item = button(`ranking-dungeon${dungeon.key === selected.dungeon?.key ? " is-active" : ""}`);
      applyRankingArtwork(item, RANKING_DUNGEON_ART[dungeon.key]);
      item.append(
        element("strong", "", dungeon.name),
        element("span", "", `总计 ${formatInteger(dungeon.ranking_entries)} 条可排行`),
      );
      item.addEventListener("click", () => update(
        {dungeon: dungeon.key},
        ["boss", "difficulty", "brass"],
      ));
      dungeons.appendChild(item);
    });
    dungeonField.appendChild(dungeons);
    controls.appendChild(dungeonField);
    if (selected.dungeon && selected.category?.key !== "family") {
      const bosses = selected.dungeon.bosses || [];
      const bossField = element("div", "ranking-choice-field ranking-boss-field");
      bossField.appendChild(element("div", "ranking-choice-label", "选择 Boss"));
      const bossChoices = element("div", "ranking-bosses");
      bosses.forEach((boss) => {
        const item = button(`ranking-boss${boss.name === selected.boss?.name ? " is-active" : ""}`);
        const icon = bossIcon(boss.stage_id, "ranking-boss-icon", boss.name);
        if (!icon.hidden) item.appendChild(icon);
        const itemCopy = element("span", "ranking-boss-copy");
        itemCopy.append(
          element("strong", "", boss.name),
          element("small", "", `总计 ${formatInteger(boss.ranking_entries)} 条可排行`),
        );
        item.appendChild(itemCopy);
        item.addEventListener("click", () => update(
          {boss: boss.name},
          ["difficulty", "brass"],
        ));
        bossChoices.appendChild(item);
      });
      if (!bosses.length) {
        bossChoices.appendChild(element("div", "ranking-choice-empty", "该副本暂无 Boss 数据"));
      }
      bossField.appendChild(bossChoices);
      controls.appendChild(bossField);
    }
    if (selected.boss) {
      const segments = element("div", "ranking-segment-grid");
      segments.appendChild(performanceFilterField("难度", performanceSegments([
        {value: "all", label: "全部"},
        ...(selected.dungeon?.difficulties || []).map((value) => ({
          value,
          label: performanceDifficultyLabel(value),
        })),
      ], selected.difficulty, (value) => update({difficulty: value === "all" ? "" : value}))));
      const availableBrass = new Set(selected.dungeon?.brass_tome_statuses || []);
      segments.appendChild(performanceFilterField("黄铜书模式", performanceSegments([
        {value: "all", label: "全部"},
        {value: "enabled", label: "开启", disabled: availableBrass.size > 0 && !availableBrass.has("enabled")},
        {value: "disabled", label: "未开启", disabled: availableBrass.size > 0 && !availableBrass.has("disabled")},
        {value: "unknown", label: "未确认", disabled: availableBrass.size > 0 && !availableBrass.has("unknown")},
      ], selected.brass, (value) => update({brass: value === "all" ? "" : value}))));
      controls.appendChild(segments);
    }
    panel.appendChild(controls);

    const professionField = element("div", "ranking-profession-field");
    professionField.appendChild(element("label", "", "职业"));
    const professions = element("div", "ranking-professions");
    const all = button(`ranking-profession${selected.profession ? "" : " is-active"}`);
    all.append(element("span", "ranking-profession-all", "ALL"), element("b", "", "全部职业"));
    all.addEventListener("click", () => update({profession: ""}));
    professions.appendChild(all);
    Object.entries(PROFESSIONS).forEach(([id, name]) => {
      const item = button(`ranking-profession${selected.profession === id ? " is-active" : ""}`);
      const icon = professionIcon(id);
      item.style.setProperty("--profession-color", professionColor(id));
      item.append(icon, element("b", "", name));
      item.addEventListener("click", () => update({profession: id}));
      professions.appendChild(item);
    });
    professionField.appendChild(professions);
    panel.appendChild(professionField);
    const footer = element("div", "ranking-filter-footer");
    footer.appendChild(element(
      "p",
      "",
      "排行只包含身份完整、通过校验且不含人机或投影的胜利记录。资料未确认的旧记录可单独筛选。",
    ));
    const reset = element("a", "history-reset", "重置筛选");
    reset.href = "#records";
    footer.appendChild(reset);
    panel.appendChild(footer);
    return panel;
  }

  async function renderRecords(token) {
    setPage("DATA RANKINGS", "数据排行", "选择类型和副本后查看对应职业排行；难度与黄铜书模式分别筛选。");
    showStatus("正在读取巅峰记录");
    await loadBaseData();
    if (token !== state.renderToken) return;

    const params = parseRoute().params;
    const selected = resolveRankingFilters(params);
    const ready = Boolean(selected.dungeon && selected.boss);
    const rows = ready ? [...(state.leaderboards || [])].filter((row) =>
      row.category === selected.category?.key
      && row.dungeon_key === selected.dungeon?.key
      && row.boss_name === selected.boss?.name
      && (selected.difficulty === "all" || (row.difficulty || "unknown") === selected.difficulty)
      && (selected.brass === "all" || (row.brass_tome_status || "unknown") === selected.brass)
      && (!selected.profession || String(row.profession_id) === selected.profession)
    ).sort((a, b) => {
      const dps = Number(b.dps || 0) - Number(a.dps || 0);
      return dps || Number(b.ended_at || 0) - Number(a.ended_at || 0);
    }) : [];
    const fragment = document.createDocumentFragment();
    fragment.appendChild(rankingFilters(params, selected));
    const title = selected.profession
      ? `${professionName(selected.profession)} · 伤害 DPS 排行`
      : "伤害 DPS 排行";
    const context = [
      selected.category?.name,
      selected.dungeon?.name,
      selected.category?.key === "family" ? "" : selected.boss?.name,
      ...(selected.boss ? [
        performanceDifficultyLabel(selected.difficulty),
        brassTomeFilterLabel(selected.brass),
      ] : []),
    ].filter(Boolean).join(" · ");
    fragment.appendChild(sectionHeading(title, rows.length ? `${context} · ${rows.length} 条` : context));
    if (rows.length) {
      fragment.appendChild(recordList(rows));
    } else {
      const empty = element("div", "portal-status ranking-empty");
      if (!selected.dungeon) {
        empty.appendChild(element("strong", "", `请选择一个具体${selected.category?.name || "副本"}`));
        empty.appendChild(element(
          "span",
          "",
          selected.category?.key === "family"
            ? "选择家族本后，将直接显示该 Boss 的排行。"
            : "选择具体副本后，将显示该副本对应的 Boss。",
        ));
      } else if (!selected.boss) {
        empty.appendChild(element("strong", "", "请选择 Boss"));
        empty.appendChild(element("span", "", `当前副本：${selected.dungeon.name}`));
      } else {
        empty.appendChild(element("strong", "", "当前筛选暂无排行记录"));
        empty.appendChild(element("span", "", "可以切换难度、黄铜书模式或职业查看其他记录。"));
      }
      fragment.appendChild(empty);
    }
    contentNode.replaceChildren(fragment);
  }

  async function renderSearch(token, query) {
    const characterName = String(query || "").trim().slice(0, 48);
    if (!characterName) {
      window.location.replace("#home");
      return;
    }
    await renderHistory(token, characterName);
  }

  function historyFilters(params, ranking = false) {
    const form = element("form", "history-filters");
    const field = (labelText, name, options, target = form, showLabel = true) => {
      const label = element("label", "history-field");
      if (showLabel) label.appendChild(element("span", "", labelText));
      const control = element(options ? "select" : "input");
      control.name = name;
      control.setAttribute("aria-label", labelText);
      if (options) options.forEach(([value, text]) => {
        const option = element("option", "", text);
        option.value = value;
        control.appendChild(option);
      });
      else control.type = name === "from" || name === "to" ? "date" : "search";
      control.value = params.get(name) || "";
      if (name === "q") { control.placeholder = "请输入角色名称"; control.maxLength = 48; control.required = true; }
      label.appendChild(control);
      target.appendChild(label);
      if (options || control.type === "date") control.addEventListener("change", () => form.requestSubmit());
    };
    if (!ranking) field("角色名称", "q");
    field("Boss", "boss", [["", "全部 Boss"], ...(state.catalog?.bosses || []).map((boss) => [boss.name, `${boss.name} (${boss.records})`])]);
    field("职业", "profession", [["", "全部职业"], ...Object.entries(PROFESSIONS)]);
    if (!ranking) {
      field("数据条件", "eligibility", [["", "全部历史"], ["included", "有效统计"], ["not_eligible", "未纳入统计"]]);
      field("难度", "difficulty", [["", "全部难度"], ...(state.catalog?.difficulties || []).map((value) => [value, performanceDifficultyLabel(value)])]);
      const dateRange = element("div", "history-date-range");
      dateRange.appendChild(element("span", "history-date-title", "战斗日期"));
      const dateControls = element("div", "history-date-controls");
      field("开始日期", "from", null, dateControls, false);
      dateControls.appendChild(element("span", "history-date-separator", "至"));
      field("结束日期", "to", null, dateControls, false);
      dateRange.appendChild(dateControls);
      form.appendChild(dateRange);
    }
    const submit = button("portal-refresh", "查询");
    submit.type = "submit";
    const clear = element("a", "history-reset", "清空筛选");
    clear.href = ranking ? "#records" : "#home";
    form.append(submit, clear);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const values = new URLSearchParams();
      new FormData(form).forEach((value, name) => { if (String(value).trim()) values.set(name, String(value).trim()); });
      if (!ranking && !values.get("q")) {
        form.querySelector('[name="q"]')?.focus();
        return;
      }
      const next = `${ranking ? "#records" : "#search"}${values.size ? `?${values}` : ""}`;
      if (next === window.location.hash) renderRoute(true);
      else window.location.hash = next;
    });
    return form;
  }

  function qualificationText(reasons) {
    const labels = {
      ENCOUNTER_NOT_COMPLETED: "战斗未胜利", PARTICIPANT_IDENTITY_INCOMPLETE: "队友身份记录不完整",
      TEAM_TOTAL_MISMATCH: "团队总量不一致", CAPTURE_INCOMPLETE: "采集不完整",
      BOSS_UNSUPPORTED: "首领未纳入统计", BOSS_DATA_DIRTY: "首领数据已标记为脏数据", DIFFICULTY_INVALID: "关卡信息不完整",
      CLIENT_VERSION_UNSUPPORTED: "客户端版本未支持", GAME_VERSION_UNSUPPORTED: "游戏版本未支持",
      DURATION_INVALID: "战斗时长异常", KEY_DATA_MISSING: "关键数据缺失",
      AI_PARTICIPANT: "包含人机成员",
    };
    return (Array.isArray(reasons) ? reasons : []).map((reason) => labels[reason] || "数据未满足统计条件").join("、");
  }

  async function renderHistory(token, searchQuery = null) {
    const params = parseRoute().params;
    if (searchQuery !== null) params.set("q", searchQuery);
    setPage("BATTLE HISTORY", params.get("q") ? `角色历史 · ${params.get("q")}` : "历史战斗",
      "浏览已收录的战斗、角色表现与死亡次数，点击详情查看技能、暴击运气和装备。");
    showStatus("正在读取历史战斗");
    const request = new URLSearchParams();
    ["q", "boss", "profession", "difficulty", "eligibility", "offset"].forEach((name) => {
      if (params.get(name)) request.set(name, params.get(name));
    });
    request.set("limit", "25");
    for (const [field, key] of [["from", "after"], ["to", "before"]]) {
      const value = params.get(field);
      if (/^\d{4}-\d{2}-\d{2}$/.test(value || "")) {
        let epoch = new Date(`${value}T00:00:00+08:00`).getTime() / 1000;
        if (field === "to") epoch += 86400;
        request.set(key, String(epoch));
      }
    }
    const [, history] = await Promise.all([loadBaseData(), fetchJson(`${API_ROOT}/records?${request}`)]);
    if (token !== state.renderToken) return;
    const fragment = document.createDocumentFragment();
    fragment.appendChild(historyFilters(params));
    fragment.appendChild(sectionHeading("战斗记录", `共 ${formatInteger(history.total)} 场 · 时间按北京时间显示`));
    const list = element("div", "history-list");
    const head = element("div", "history-row history-head");
    ["Boss / 结果", "角色", "团队 DPS / 总伤害", "时长 / 死亡", "时间 / 数据状态", ""].forEach((label) => head.appendChild(element("div", "", label)));
    list.appendChild(head);
    for (const row of history.records || []) {
      const record = element("article", "history-row");
      const boss = bossCell(row);
      const [result, className] = resultLabel(row.result, row.completion_confirmed);
      const tomeLabel = brassTomeLabel(row.brass_tome_status);
      const bossTags = boss.querySelector(".boss-stage");
      bossTags.classList.add("boss-tags");
      bossTags.appendChild(element("span", "boss-tag difficulty-tag", performanceDifficultyLabel(row.difficulty || "unknown")));
      if (tomeLabel) bossTags.appendChild(element("span", `boss-tag tome-tag brass-${row.brass_tome_status || "unknown"}`, `黄铜书${tomeLabel}`));
      bossTags.appendChild(element("span", `boss-tag result-tag${className}`, result));
      boss.classList.add(className.trim() || "is-complete");
      record.append(boss, identityCell(row));
      const totals = element("div", "history-number");
      totals.append(element("b", "", formatInteger(row.team_dps)), element("small", "", `${formatCompact(row.team_total_damage)} 总伤害`));
      const time = element("div", "history-number");
      time.append(element("b", "", formatDuration(row.duration_seconds)), element("small", "", `${row.team_size} 人`), element("small", "death-count", deathSummaryText(row)));
      const info = element("div", "history-info");
      info.appendChild(element("span", "", formatDate(row.ended_at)));
      const hasAi = Boolean(row.has_ai) || (row.qualification_reasons || []).includes("AI_PARTICIPANT");
      const incompleteIdentity = (row.qualification_reasons || []).includes("PARTICIPANT_IDENTITY_INCOMPLETE");
      const status = element("small", `history-status ${row.statistics_status === "included" && !hasAi && !incompleteIdentity ? "history-included" : "history-partial"}`,
        hasAi
          ? "人机场次 · 仅供查询"
          : row.statistics_status === "included" && incompleteIdentity
            ? "队友身份未完整记录 · 不进排行/职业分析"
            : row.statistics_status === "included"
              ? "有效统计"
              : qualificationText(row.qualification_reasons) || "未纳入统计");
      info.appendChild(status);
      const open = button("row-open", "详情 →", `查看 ${row.boss_name} 战斗详情`);
      open.addEventListener("click", () => openEncounter(row.encounter_id));
      record.append(totals, time, info, open);
      list.appendChild(record);
    }
    if ((history.records || []).length) fragment.appendChild(list);
    else {
      const empty = element("div", "portal-status");
      empty.appendChild(element("span", "", "没有符合条件的战斗。可清空筛选，或检查记录是否已上传。"));
      fragment.appendChild(empty);
    }
    const pagination = element("nav", "history-pagination");
    pagination.setAttribute("aria-label", "历史分页");
    const move = (offset) => {
      params.set("offset", String(offset));
      window.location.hash = `search?${params}`;
    };
    const previous = button("portal-refresh", "上一页");
    previous.disabled = history.offset === 0;
    previous.addEventListener("click", () => move(Math.max(0, history.offset - history.limit)));
    const next = button("portal-refresh", "下一页");
    next.disabled = history.offset + history.limit >= history.total;
    next.addEventListener("click", () => move(history.offset + history.limit));
    pagination.append(previous, element("span", "", `${Math.floor(history.offset / history.limit) + 1} / ${Math.max(1, Math.ceil(history.total / history.limit))}`), next);
    fragment.appendChild(pagination);
    contentNode.replaceChildren(fragment);
  }

  function renderShare() {
    setPage("UPLOAD GUIDE", "如何上传战斗记录", "加QQ群下载叨叨dps-log：1094925831 / 165966739。通过客户端上传战斗记录。");
    const flow = element("div", "share-flow");
    [
      ["完成一场 Boss 战斗", "登录客户端并保持采集运行。Boss 战胜利后会自动上传；伤害木桩不进入上传。"],
      ["查询与分享", "自动上传完成后，在首页搜索角色名称。打开搜索结果中的详情，可查看已捕获模块并复制战报链接；缺少的模块会明确标注覆盖状态。"],
    ].forEach(([title, copy], index) => {
      const step = element("article", "share-step");
      step.appendChild(element("div", "step-index", String(index + 1).padStart(2, "0")));
      const text = element("div");
      text.appendChild(element("h2", "", title));
      text.appendChild(element("p", "", copy));
      step.appendChild(text);
      flow.appendChild(step);
    });
    contentNode.appendChild(flow);
  }

  function renderLab() {
    setPage(
      "DAODAO LAB / 数据研究",
      "叨叨实验室",
      "用足够多的真实战斗样本，分析可验证的 Boss 属性。",
    );
    const shell = element("section", "lab-shell");
    const copy = element("div", "lab-copy");
    copy.append(
      element("span", "lab-badge", "暂未开放"),
      element("h2", "", "用大量真实数据还原 Boss 属性"),
      element("p", "", "叨叨实验室将结合大量真实战斗记录，交叉分析 Boss 的血量、承伤与防御规律、难度差异等相关属性。所有结果都需要足够样本验证后再展示。"),
    );
    const topics = element("div", "lab-topics");
    ["真实血量", "承伤与防御规律", "副本难度差异", "黄铜书挑战差异"].forEach((label) => {
      topics.appendChild(element("span", "lab-topic", label));
    });
    copy.append(
      topics,
      element("div", "lab-collecting", "正在收集数据中"),
      element("p", "lab-note", "达到可交叉验证的样本量后开放，当前不会展示推测结果。"),
    );
    const visual = element("div", "lab-visual");
    visual.setAttribute("aria-hidden", "true");
    const core = element("div", "lab-core", "BOSS DATA");
    core.appendChild(element("small", "", "ANALYZING"));
    visual.append(element("div", "lab-orbit"), core);
    shell.append(copy, visual);
    contentNode.replaceChildren(shell);
    document.title = "叨叨实验室 · 叨叨诡秘";
  }

  function encounterMetric(mode, participant, durationSeconds = 0) {
    if (mode === "hps") {
      const reportedHps = Number(participant.hps || participant.stats?.hps || 0);
      if (reportedHps > 0) return reportedHps;
      const duration = Number(durationSeconds);
      const effectiveHealing = Number(participant.stats?.effective_healing || 0);
      return participant.stats?.effective_healing !== undefined && duration > 0 ? effectiveHealing / duration : Number.NaN;
    }
    if (mode === "dt") return participant.stats?.taken !== undefined || Number(participant.taken) > 0
      ? Number(participant.taken || participant.stats?.taken || 0) : Number.NaN;
    return Number(participant.dps || participant.stats?.dps || 0);
  }

  function encounterShare(mode, participant, participants, durationSeconds) {
    const storedShare = mode === "dps"
      ? Number(participant.stats?.share)
      : mode === "dt"
        ? Number(participant.stats?.taken_share)
        : Number.NaN;
    if (Number.isFinite(storedShare)) return storedShare;
    const total = participants.reduce(
      (sum, row) => sum + (encounterMetric(mode, row, durationSeconds) || 0),
      0,
    );
    return total > 0
      ? encounterMetric(mode, participant, durationSeconds) / total
      : Number.NaN;
  }

  function encounterMetricLabel(mode) {
    if (mode === "hps") return "治疗 HPS";
    if (mode === "dt") return "承伤 DT";
    return "伤害 DPS";
  }

  function participantIdentity(participant) {
    const cell = element("div", "identity-cell");
    cell.appendChild(professionIcon(participant.profession_id));
    const copy = element("div");
    copy.appendChild(element("div", "identity-name", participant.display_name || "未记录姓名"));
    let detail = participant.is_ai ? "人机" : professionName(participant.profession_id);
    const rating = Number(participant.stats?.extraordinary_rating);
    if (!participant.is_ai && Number.isFinite(rating) && rating > 0) {
      detail += ` · 评分 ${formatInteger(rating)}`;
    }
    copy.appendChild(element("div", "identity-kind", detail));
    cell.appendChild(copy);
    return cell;
  }

  function skillDisplayName(skill, mode = "dps") {
    const skillId = Number(skill?.skill_id);
    if (skillId === 0 && mode === "hps") return "未归类治疗";
    const rawName = String(skill?.name || "").trim();
    const placeholder = !rawName
      || rawName === "未知技能"
      || /^(?:技能|skill)\s*(?:(?:编号|id)\s*)?[:：#-]?\s*\d+$/i.test(rawName)
      || (/^\d+$/.test(rawName) && Number(rawName) === skillId);
    if (!placeholder) return rawName;

    const catalogName = Number.isSafeInteger(skillId) && skillId > 0
      ? String(state.skillNames?.[String(skillId)] || "").trim()
      : "";
    if (catalogName) return catalogName;
    return Number.isSafeInteger(skillId) && skillId > 0 ? "未知技能" : "未归类伤害";
  }

  const SKILL_COLORS = ["#61b6ff", "#bd95ff", "#48d6b0", "#ffbf6b", "#fa87b1", "#6dd3e7", "#8cca73", "#f39069", "#7f91ff", "#d77ee9", "#ded45f", "#55c98f"];

  function deathSummaryText(record) {
    if (record.team_deaths !== null && record.team_deaths !== undefined) return `死亡 ${formatInteger(record.team_deaths)} 次`;
    if (record.death_recorded_members > 0) return `已记录死亡 ${formatInteger(record.recorded_deaths)} 次 · ${record.death_recorded_members}/${record.death_total_members} 人`;
    return "死亡次数未记录";
  }

  function assetIcon(group, filename, name, className) {
    const image = element("img", className);
    image.src = filename ? `assets/${group}/${encodeURIComponent(filename)}` : "assets/skills/0.png";
    image.alt = name;
    image.title = filename ? name : `${name} · 原图未收录`;
    image.loading = "lazy";
    image.addEventListener("error", () => { image.src = "assets/skills/0.png"; image.title = `${name} · 原图未收录`; }, { once: true });
    return image;
  }

  function skillIcon(skill) {
    return assetIcon("skills", state.iconCatalog?.skills?.[String(skill.skill_id)], skillDisplayName(skill), "skill-icon");
  }

  function skillIconUrl(skill) {
    const filename = state.iconCatalog?.skills?.[String(skill.skill_id)];
    return filename ? `assets/skills/${encodeURIComponent(filename)}` : "assets/skills/0.png";
  }

  function renderEventTable(events, className = "skill-events", metadata = {}) {
    const details = element("details", className);
    const total = Number(metadata.total);
    const countText = events.length
      ? `${formatInteger(events.length)} 条${Number.isFinite(total) && total > events.length ? ` / 共 ${formatInteger(total)} 条` : ""}`
      : "本场未上传";
    details.appendChild(element("summary", "", `逐次打击记录 · ${countText}`));
    if (!events.length) {
      details.appendChild(element("p", "detail-note", "该成员未采集逐次命中事件，仍可查看已收录的技能汇总。"));
      return details;
    }
    if (metadata.truncated) {
      details.appendChild(element("p", "detail-note", "事件数量超过单场上传上限；这里显示已上传的真实事件，覆盖状态已标记为截断。"));
    }
    const body = element("div", "skill-event-body");
    const showTarget = events.some((event) => event.target_name || Number(event.target_id));
    let page = 0;
    const render = () => {
      body.replaceChildren();
      const table = element("table", "event-table");
      const head = element("thead"), header = element("tr");
      ["时间", "技能", ...(showTarget ? ["目标"] : []), "伤害", "类型"].forEach((text) => header.appendChild(element("th", "", text)));
      head.appendChild(header);
      table.appendChild(head);
      const tbody = element("tbody");
      events.slice(page * 50, (page + 1) * 50).forEach((event) => {
        const row = element("tr"), name = element("td"), identity = element("span", "event-skill");
        identity.append(skillIcon(event), element("span", "", skillDisplayName(event)));
        name.appendChild(identity);
        const flags = [event.critical === true ? "暴击" : "", event.penetrating === true ? "穿刺" : ""].filter(Boolean);
        const type = flags.join(" · ") || (event.critical === false && event.penetrating === false ? "普通" : "标记不完整");
        row.append(element("td", "", `${(Number(event.time_ms) / 1000).toFixed(2)} 秒`), name);
        if (showTarget) {
          row.appendChild(element("td", "event-target", event.target_name || (Number(event.target_id) ? `目标 ${event.target_id}` : "--")));
        }
        row.append(element("td", "", formatInteger(event.damage)), element("td", event.critical ? "crit-text" : event.penetrating ? "pierce-text" : "", type));
        tbody.appendChild(row);
      });
      table.appendChild(tbody);
      body.appendChild(table);
      const pager = element("div", "history-pagination");
      const previous = button("portal-refresh", "上一页"), next = button("portal-refresh", "下一页");
      previous.disabled = page === 0;
      next.disabled = (page + 1) * 50 >= events.length;
      previous.addEventListener("click", () => { page--; render(); });
      next.addEventListener("click", () => { page++; render(); });
      pager.append(previous, element("span", "", `${page + 1} / ${Math.ceil(events.length / 50)}`), next);
      body.appendChild(pager);
    };
    render();
    details.appendChild(body);
    return details;
  }

  function renderSkillTimeline(events, skills, metadata = {}) {
    const section = element("section", "skill-timeline-panel");
    const rows = (Array.isArray(events) ? events : [])
      .filter((event) => Number.isFinite(Number(event.time_ms)) && Number(event.skill_id) > 0)
      .sort((left, right) => Number(left.time_ms) - Number(right.time_ms));
    const total = Number(metadata.total);
    const totalText = Number.isFinite(total) && total > rows.length
      ? `${formatInteger(rows.length)} / ${formatInteger(total)} 条真实事件`
      : `${formatInteger(rows.length)} 条真实事件`;
    section.appendChild(sectionHeading("技能命中时间轴", rows.length ? totalText : "未采集"));
    if (!rows.length) {
      section.appendChild(element("p", "detail-note", "该成员没有上传逐次命中时间，可切换团队成员查看已上传的记录。"));
      return section;
    }

    const skillById = new Map();
    (Array.isArray(skills) ? skills : []).forEach((skill) => {
      const skillId = Number(skill.skill_id);
      if (skillId > 0) skillById.set(skillId, skill);
    });
    const eventIds = new Set(rows.map((event) => Number(event.skill_id)));
    const timelineSkills = [];
    skillById.forEach((skill, skillId) => {
      if (eventIds.has(skillId)) timelineSkills.push(skill);
    });
    eventIds.forEach((skillId) => {
      if (!skillById.has(skillId)) timelineSkills.push({skill_id: skillId});
    });
    timelineSkills.sort((left, right) => {
      const damageDifference = Number(right.damage || 0) - Number(left.damage || 0);
      return damageDifference || Number(left.skill_id) - Number(right.skill_id);
    });

    const skillRows = timelineSkills.map((skill, index) => ({
      skill,
      skillId: Number(skill.skill_id),
      name: skillDisplayName(skill),
      icon: skillIconUrl(skill),
      color: SKILL_COLORS[index % SKILL_COLORS.length],
    }));
    const skillRowById = new Map(skillRows.map((row) => [row.skillId, row]));
    const eventsBySkill = new Map(skillRows.map((row) => [row.skillId, []]));
    rows.forEach((event) => eventsBySkill.get(Number(event.skill_id))?.push(event));
    eventsBySkill.forEach((skillEvents) => {
      skillEvents.forEach((event, index) => {
        event._timelineOrdinal = index + 1;
        event._timelineCount = skillEvents.length;
      });
    });

    const toolbar = element("div", "skill-timeline-toolbar");
    const skillFilters = element("div", "timeline-skill-filters");
    const controls = element("div", "timeline-controls");
    toolbar.append(skillFilters, controls);
    const chartFrame = element("div", "skill-timeline-chart-frame");
    const hitDetail = element("div", "timeline-hit-detail");
    hitDetail.setAttribute("aria-live", "polite");
    hitDetail.appendChild(element("p", "detail-note", "点击任一技能命中点，查看该次伤害、目标与命中标记。"));
    section.append(toolbar, chartFrame, hitDetail);

    if (metadata.truncated) {
      section.appendChild(element("p", "equipment-data-warning", "命中事件超过单场上传上限；时间轴只展示已上传的真实事件。"));
    }
    section.appendChild(element("p", "detail-note", "横轴使用上传的真实毫秒时间；滚轮可缩放，底部滑块可调整查看范围。技能每页显示 6 项，可按技能隐藏、反选或恢复全部。"));

    const pageSize = 6;
    const pageCount = Math.max(1, Math.ceil(skillRows.length / pageSize));
    const hiddenSkillIds = new Set();
    let page = 0;

    const showHit = (event) => {
      if (!event) return;
      const row = skillRowById.get(Number(event.skill_id)) || {skill: event, name: skillDisplayName(event)};
      const identity = element("div", "timeline-hit-identity");
      identity.append(skillIcon(row.skill), element("div", "", row.name));
      const values = element("div", "timeline-hit-values");
      values.append(
        element("strong", "", `${(Number(event.time_ms) / 1000).toFixed(3)} 秒`),
        element("b", "", `${formatInteger(event.damage)} 伤害`),
        element("span", "", event.target_name || (Number(event.target_id) ? `目标 ${event.target_id}` : "目标未记录")),
        element("span", "", `第 ${formatInteger(event._timelineOrdinal)} / ${formatInteger(event._timelineCount)} 次命中`),
      );
      const flags = element("div", "timeline-hit-flags");
      [["暴击", event.critical, "is-critical"], ["穿刺", event.penetrating, "is-penetrating"]].forEach(([label, value, className]) => {
        const text = value === true ? label : value === false ? `非${label}` : `${label}未知`;
        flags.appendChild(element("span", `timeline-hit-flag${value === true ? ` ${className}` : ""}`, text));
      });
      hitDetail.replaceChildren(identity, values, flags);
    };

    const draw = () => {
      const pageSkills = skillRows.slice(page * pageSize, (page + 1) * pageSize);
      skillFilters.replaceChildren();
      pageSkills.forEach((row) => {
        const isHidden = hiddenSkillIds.has(row.skillId);
        const toggle = button(`timeline-skill-filter${isHidden ? " is-hidden" : ""}`, "", `${isHidden ? "显示" : "隐藏"}${row.name}`);
        toggle.style.setProperty("--skill-color", row.color);
        toggle.setAttribute("aria-pressed", String(!isHidden));
        toggle.append(skillIcon(row.skill), element("span", "", row.name), element("small", "", `${formatInteger(eventsBySkill.get(row.skillId)?.length || 0)} 次`));
        toggle.addEventListener("click", () => {
          if (isHidden) hiddenSkillIds.delete(row.skillId);
          else hiddenSkillIds.add(row.skillId);
          draw();
        });
        skillFilters.appendChild(toggle);
      });

      controls.replaceChildren();
      const previous = button("timeline-control", "‹", "上一页技能");
      const next = button("timeline-control", "›", "下一页技能");
      previous.disabled = page === 0;
      next.disabled = page >= pageCount - 1;
      previous.addEventListener("click", () => { page -= 1; draw(); });
      next.addEventListener("click", () => { page += 1; draw(); });
      const showAll = button("timeline-control", "全部");
      showAll.addEventListener("click", () => { hiddenSkillIds.clear(); draw(); });
      const invert = button("timeline-control", "反选");
      invert.addEventListener("click", () => {
        skillRows.forEach((row) => {
          if (hiddenSkillIds.has(row.skillId)) hiddenSkillIds.delete(row.skillId);
          else hiddenSkillIds.add(row.skillId);
        });
        draw();
      });
      const reset = button("timeline-control", "重置");
      reset.addEventListener("click", () => { page = 0; hiddenSkillIds.clear(); draw(); });
      controls.append(previous, element("span", "timeline-page", `${page + 1} / ${pageCount}`), next, showAll, invert, reset);

      const compact = window.matchMedia("(max-width: 600px)").matches;
      const duration = Math.max(1, ...rows.map((event) => Number(event.time_ms) / 1000));
      const activeRows = rows.filter((event) => !hiddenSkillIds.has(Number(event.skill_id)));
      const binCount = compact ? 36 : 64;
      const damageBins = Array(binCount).fill(0);
      const criticalBins = Array(binCount).fill(0);
      activeRows.forEach((event) => {
        const bin = Math.min(binCount - 1, Math.max(0, Math.floor(Number(event.time_ms) / 1000 / duration * binCount)));
        const damage = Math.max(0, Number(event.damage) || 0);
        damageBins[bin] += damage;
        if (event.critical === true) criticalBins[bin] += damage;
      });
      const rhythm = (values) => {
        const average = values.reduce((sum, value) => sum + value, 0) / Math.max(1, values.length);
        return values.map((value, index) => [duration * (index + .5) / values.length, average > 0 ? Math.min(180, value / average * 100) : 0]);
      };

      const richLabels = {};
      pageSkills.forEach((row, index) => {
        richLabels[`icon${index}`] = {width: compact ? 18 : 22, height: compact ? 18 : 22, borderRadius: 4, backgroundColor: {image: row.icon}};
        richLabels[`name${index}`] = {width: compact ? 54 : 96, color: hiddenSkillIds.has(row.skillId) ? "#62717d" : "#dce7ee", fontSize: compact ? 9 : 10, fontWeight: 600, overflow: "truncate"};
      });
      const rhythmSeries = [
        {name: "伤害节奏", color: "#e4665f", data: rhythm(damageBins)},
        {name: "暴击节奏", color: "#e5b443", data: rhythm(criticalBins)},
      ].map((item) => ({
        name: item.name, type: "line", yAxisIndex: 1, data: item.data,
        showSymbol: false, silent: true,
        lineStyle: {color: item.color, width: 1, type: "dashed", opacity: .72},
        emphasis: {disabled: true}, z: 1,
      }));
      const hitSeries = pageSkills.map((row) => {
        const collision = new Map();
        const data = (eventsBySkill.get(row.skillId) || []).map((event) => {
          const collisionKey = Math.round(Number(event.time_ms) / 80);
          const level = collision.get(collisionKey) || 0;
          collision.set(collisionKey, level + 1);
          const offsets = [0, -7, 7, -10, 10];
          return {
            value: [Number(event.time_ms) / 1000, row.name, event.critical === true ? 1 : 0, event.penetrating === true ? 1 : 0, offsets[level % offsets.length]],
            event,
          };
        });
        return {
          name: row.name, type: "custom", coordinateSystem: "cartesian2d", encode: {x: 0, y: 1}, z: 5,
          silent: hiddenSkillIds.has(row.skillId),
          data: hiddenSkillIds.has(row.skillId) ? [] : data,
          renderItem: (_params, api) => {
            const point = api.coord([api.value(0), api.value(1)]);
            const critical = Number(api.value(2)) === 1;
            const penetrating = Number(api.value(3)) === 1;
            const offset = Number(api.value(4)) || 0;
            const size = compact ? 20 : 24;
            const left = point[0] - size / 2;
            const top = point[1] - size / 2 + offset;
            return {
              type: "group",
              children: [
                {type: "rect", shape: {x: left - 2, y: top - 2, width: size + 4, height: size + 4, r: 4}, style: {fill: "#0b141c", stroke: critical ? "#d9b8ff" : penetrating ? "#9ad9ff" : row.color, lineWidth: critical || penetrating ? 2 : 1}},
                {type: "image", style: {image: row.icon, x: left, y: top, width: size, height: size}},
              ],
            };
          },
        };
      });
      const option = {
        ...baseChartOption(),
        animationDuration: 240,
        tooltip: {
          ...baseChartOption().tooltip,
          formatter: (params) => {
            const event = params.data?.event;
            if (!event) return `${params.seriesName}\n${formatInteger(params.value?.[1])}%`;
            const flags = [event.critical === true ? "暴击" : "", event.penetrating === true ? "穿刺" : ""].filter(Boolean).join(" · ") || "普通命中";
            return `${params.seriesName}\n${(Number(event.time_ms) / 1000).toFixed(3)} 秒\n${formatInteger(event.damage)} 伤害 · ${flags}\n${event.target_name || "目标未记录"}`;
          },
        },
        legend: {top: 2, right: compact ? 4 : 12, data: ["伤害节奏", "暴击节奏"], textStyle: {color: "#91a3af", fontSize: compact ? 9 : 10}},
        grid: {left: compact ? 88 : 150, right: compact ? 42 : 58, top: 48, bottom: 76},
        xAxis: {
          type: "value", min: 0, max: Math.ceil(duration), name: "战斗时间", nameTextStyle: {color: "#8295a3"},
          axisLabel: {color: "#91a3af", formatter: formatDuration},
          axisLine: {lineStyle: {color: "#3a4a57"}}, splitLine: {lineStyle: {color: "rgba(102,126,144,.16)"}},
        },
        yAxis: [
          {
            type: "category", inverse: true, data: pageSkills.map((row) => row.name), axisLine: {show: false}, axisTick: {show: false},
            axisLabel: {interval: 0, margin: compact ? 6 : 10, rich: richLabels, formatter: (value, index) => `{icon${index}|} {name${index}|${value}}`},
            splitArea: {show: true, areaStyle: {color: ["rgba(255,255,255,.018)", "rgba(255,255,255,.006)"]}},
          },
          {
            type: "value", min: 0, max: 180, interval: 60,
            axisLabel: {color: "#667783", fontSize: 9, formatter: "{value}%"}, axisLine: {show: false}, axisTick: {show: false}, splitLine: {show: false},
          },
        ],
        dataZoom: [
          {type: "inside", xAxisIndex: 0, filterMode: "none", zoomOnMouseWheel: true, moveOnMouseMove: true},
          {type: "slider", xAxisIndex: 0, filterMode: "none", bottom: 12, height: 20, borderColor: "#30404c", backgroundColor: "#0b141c", fillerColor: "rgba(97,182,255,.18)", dataBackground: {lineStyle: {color: "#526a7c"}, areaStyle: {color: "rgba(82,106,124,.18)"}}, textStyle: {color: "#8194a2"}},
        ],
        series: [...rhythmSeries, ...hitSeries],
      };
      disposeCharts(chartFrame);
      chartFrame.replaceChildren(mountEChart(
        "skill-hit-timeline-echart",
        option,
        `技能命中时间轴，共 ${rows.length} 条真实事件；每个图标代表一次技能命中`,
        (params) => showHit(params.data?.event),
      ));
    };

    draw();
    return section;
  }

  function criticalSummaryData(stats) {
    const hits = Number(stats?.damage_hits ?? stats?.hits);
    const criticalHits = Number(stats?.critical_hits);
    if (!Number.isFinite(hits) || hits <= 0 || !Number.isFinite(criticalHits)) return null;
    const reportedRate = Number(stats?.critical_rate);
    return {
      hits,
      criticalHits,
      rate: Number.isFinite(reportedRate) ? reportedRate : criticalHits / hits,
    };
  }

  function renderCriticalLuck(stats) {
    const section = element("section", "luck-panel");
    section.appendChild(sectionHeading("暴击运气", "暴击落点估计"));
    const model = stats.critical_luck;
    if (!model) {
      const summary = criticalSummaryData(stats);
      const knownEvents = (Array.isArray(stats.skill_timeline) ? stats.skill_timeline : [])
        .filter((event) => typeof event?.critical === "boolean" && Number(event.damage) > 0)
        .length;
      if (summary) {
        const metrics = element("div", "luck-metrics luck-summary-metrics");
        metrics.append(
          metricCard("汇总打击次数", formatInteger(summary.hits)),
          metricCard("汇总暴击次数", formatInteger(summary.criticalHits)),
          metricCard("汇总暴击率", formatPercent(summary.rate)),
        );
        section.appendChild(metrics);
      }
      section.appendChild(element(
        "p",
        "detail-note",
        `本场只有 ${formatInteger(knownEvents)} 次带明确暴击标记和逐次伤害的事件；至少需要 8 次才计算暴击落点运气。汇总暴击数据照常展示，但不会据此虚构运气分布。`,
      ));
      return section;
    }
    const verdict = element("div", "luck-verdict");
    verdict.append(element("strong", "", model.verdict), element("span", "", `百分位 ${formatPercent(model.percentile)}`));
    section.appendChild(verdict);
    const observedZ = Math.max(-3.5, Math.min(3.5, Number(model.z_score)));
    const distribution = Array.from({length: 101}, (_, index) => {
      const z = -3.5 + index * .07;
      return [Number(z.toFixed(2)), Math.exp(-z * z / 2)];
    });
    const chartOption = {
      ...baseChartOption(),
      grid: {left: 34, right: 34, top: 50, bottom: 38},
      xAxis: {
        type: "value", min: -3.5, max: 3.5, interval: 1,
        axisLabel: {color: "#93a4b1", formatter: (value) => `${value > 0 ? "+" : ""}${value}σ`},
        axisLine: {lineStyle: {color: "#344452"}}, splitLine: {show: false},
      },
      yAxis: {type: "value", min: 0, max: 1.12, show: false},
      series: [{
        name: "暴击落点概率",
        type: "line",
        smooth: true,
        symbol: "none",
        data: distribution,
        lineStyle: {width: 2, color: "#bd95ff"},
        areaStyle: {color: "rgba(189,149,255,.2)"},
        markLine: {
          symbol: "none",
          silent: true,
          label: {show: true, color: "#eef5f9", backgroundColor: "rgba(8,16,24,.82)", padding: [4, 7], borderRadius: 4},
          data: [
            {name: "期望位置 0σ", xAxis: 0, lineStyle: {color: "#48d6b0", type: "dashed"}, label: {formatter: "期望位置 0σ", position: "insideEndTop"}},
            {name: `本场 ${observedZ >= 0 ? "+" : ""}${observedZ.toFixed(2)}σ`, xAxis: observedZ, lineStyle: {color: "#bd95ff", width: 2}, label: {formatter: `本场 ${observedZ >= 0 ? "+" : ""}${observedZ.toFixed(2)}σ`, position: "insideEndBottom"}},
          ],
        },
        markPoint: {
          symbol: "circle", symbolSize: 10,
          data: [{coord: [observedZ, Math.exp(-observedZ * observedZ / 2)], name: model.verdict}],
          itemStyle: {color: "#fa87b1"},
          label: {show: false},
        },
      }],
    };
    section.appendChild(mountEChart("luck-echart", chartOption, `暴击运气分布：${model.verdict}，百分位 ${formatPercent(model.percentile)}`));
    const metrics = element("div", "luck-metrics");
    [["实测伤害", model.observed], ["期望伤害", model.expected], ["运气增减伤害", model.extra]].forEach(([label, value]) => {
      metrics.appendChild(metricCard(label, `${label === "运气增减伤害" && value > 0 ? "+" : ""}${formatInteger(value)}`));
    });
    section.append(metrics, element("p", "detail-note", `${formatInteger(model.known_hits)} 次已知打击 · ${formatInteger(model.critical_hits)} 次暴击 · 伤害覆盖 ${formatPercent(model.coverage)}。固定每个技能的实测暴击次数，估计暴击落在不同伤害打击时的分布；曲线使用正态近似，并按本场总伤害折算。该结果不代表面板暴击率或未来战斗表现。`));
    return section;
  }

  function renderOpeningSequence(stats) {
    const section = element("section", "detail-module opening-panel");
    const rawRows = (Array.isArray(stats.opening_sequence) ? [...stats.opening_sequence] : [])
      .sort((a, b) => Number(a.time_ms) - Number(b.time_ms) || Number(a.sequence) - Number(b.sequence));
    const zeroTimeCount = rawRows.filter((entry) => Number(entry.time_ms) === 0).length;
    const rows = zeroTimeCount > 1
      ? rawRows.filter((entry) => Number(entry.time_ms) > 0)
      : rawRows;
    const source = stats.opening_sequence_source || rawRows[0]?.source;
    const sourceLabel = source === "successful_cast"
      ? "真实成功施法"
      : source === "cast_broadcast"
        ? "真实施法广播"
      : source === "damage_hit"
        ? "真实伤害命中顺序"
        : "来源未标记";
    const headingMeta = rows.length
      ? `${rows.length} 条 · ${sourceLabel}${zeroTimeCount > 1 ? ` · 已排除 ${zeroTimeCount} 条无效时间` : ""}`
      : zeroTimeCount > 1
        ? `${sourceLabel} · ${zeroTimeCount} 条时间无效`
        : "未采集";
    section.appendChild(sectionHeading("起手序列", headingMeta));
    if (!rows.length) {
      section.appendChild(element(
        "p",
        "detail-note",
        zeroTimeCount > 1
          ? "该场施法发生在校正后的战斗起点之前，精确相对时间已经丢失，因此不作为起手序列展示。"
          : "该成员没有开战后前 30 秒的施法或命中记录。",
      ));
      return section;
    }
    const timeline = element("div", "opening-timeline");
    timeline.tabIndex = 0;
    timeline.setAttribute("aria-label", `开战后前 30 秒起手时间轴，共 ${rows.length} 次技能`);
    const sequence = element("div", "opening-sequence");
    const hasReliableTime = (entry) => {
      if (entry?.time_ms === null || entry?.time_ms === undefined || entry?.time_ms === "") return false;
      const value = Number(entry.time_ms);
      return Number.isFinite(value) && value >= 0 && (value > 0 || zeroTimeCount <= 1);
    };
    rows.forEach((entry, index) => {
      const timeKnown = hasReliableTime(entry);
      const timeMs = timeKnown ? Math.max(0, Number(entry.time_ms)) : null;
      if (index > 0) {
        const previousKnown = hasReliableTime(rows[index - 1]);
        const explicitInterval = Number(entry.interval_ms);
        const intervalKnown = Number.isFinite(explicitInterval)
          || (timeKnown && previousKnown);
        const intervalMs = Number.isFinite(explicitInterval)
          ? Math.max(0, explicitInterval)
          : intervalKnown
            ? Math.max(0, timeMs - Number(rows[index - 1].time_ms))
            : null;
        const connector = element(`div`, `opening-connector${intervalKnown ? "" : " is-unknown"}`);
        connector.style.setProperty("--opening-gap", `${intervalKnown ? Math.max(52, Math.min(116, 48 + intervalMs / 120)) : 58}px`);
        connector.append(
          element("span", "opening-interval", intervalKnown ? `+${(intervalMs / 1000).toFixed(2)} 秒` : "间隔未记录"),
          element("span", "opening-arrow", ""),
        );
        sequence.appendChild(connector);
      }
      const item = element("article", "opening-node");
      const skill = {skill_id: entry.skill_id};
      const skillName = skillDisplayName(skill);
      const icon = element("div", "opening-skill-icon");
      icon.append(
        skillIcon(skill),
        element("span", "opening-order", String(index + 1).padStart(2, "0")),
      );
      item.append(
        icon,
        element("strong", "opening-skill-name", skillName),
        element("time", `opening-time${timeKnown ? "" : " is-unknown"}`, timeKnown ? `${(timeMs / 1000).toFixed(2)} 秒` : "时间未记录"),
      );
      item.title = [
        skillName,
        timeKnown ? `释放 ${(timeMs / 1000).toFixed(2)} 秒` : "释放时间未记录",
        entry.target_name ? `目标 ${entry.target_name}` : "",
        Number(entry.damage) > 0 ? `${formatInteger(entry.damage)} 伤害` : "",
      ].filter(Boolean).join(" · ");
      sequence.appendChild(item);
    });
    timeline.appendChild(sequence);
    section.appendChild(timeline);
    section.appendChild(element("p", "detail-note opening-help", "按真实施法或命中时间从左到右排列；箭头上方显示距上一技能的间隔。可横向滚动查看完整起手。"));
    if (zeroTimeCount > 1) {
      section.appendChild(element("p", "detail-note", `已隐藏 ${formatInteger(zeroTimeCount)} 条发生在校正后战斗起点之前的施法；这些记录无法恢复精确相对时间，不会再显示为 0 秒。`));
    }
    if (source === "damage_hit") {
      section.appendChild(element("p", "detail-note", "该成员没有可用的成功施法回执；顺序来自真实伤害命中时间，不代表技能按键或施法成功时间。"));
    }
    return section;
  }

  function renderEquipment(snapshot) {
    const section = element("section", "equipment-panel");
    const equipment = Array.isArray(snapshot?.equipment) ? snapshot.equipment : [];
    section.appendChild(sectionHeading("战斗装备", equipment.length ? `${equipment.length} 件 · ${snapshot.partial ? "部分快照" : "已采集快照"}` : "未采集"));
    if (!equipment.length) { section.appendChild(element("p", "detail-note", "本场未上传装备快照，无法展示当时的装备与属性。")); return section; }
    const metrics = element("div", "equipment-metrics");
    metrics.append(metricCard(snapshot.equipment_score_complete ? "完整装备评分" : "已知装备评分", formatInteger(snapshot.equipment_score_complete ? snapshot.equipment_score : snapshot.equipment_known_score ?? snapshot.equipment_score)),
      metricCard("评分", formatInteger(snapshot.extraordinary_rating)));
    section.appendChild(metrics);
    if (snapshot.captured_at) section.appendChild(element("p", "detail-note", `快照采集时间：${formatDateTime(snapshot.captured_at, true)}（北京时间）`));
    const grid = element("div", "equipment-grid");
    const properties = (rows, container) => {
      (Array.isArray(rows) ? rows : []).forEach((prop) => {
        const label = displayAttributeName(prop.name || state.iconCatalog?.attributes?.[prop.key] || prop.key);
        container.appendChild(element("span", "equipment-property", `${label} ${prop.value === null ? "未记录" : formatInteger(prop.value)}`));
      });
    };
    equipment.forEach((item) => {
      const detail = element("details", "equipment-item");
      detail.dataset.itemId = item.item_id;
      const summary = element("summary", "equipment-summary");
      const rawKey = String(item.metadata?.icon || "").split("/").pop().split(".")[0];
      const key = state.iconCatalog?.items?.[String(item.item_id)] || rawKey || String(item.item_id);
      summary.appendChild(assetIcon("equipment", state.iconCatalog?.equipment?.[key], item.item_name || "装备", "equipment-icon"));
      const copy = element("div");
      copy.append(element("strong", `quality-${Number(item.quality) || 0}`, item.item_name || `装备 ${item.item_id}`),
        element("span", "", `${item.slot_name || "未知部位"} · ${item.quality_name || "品质未记录"}${item.enhance_level !== undefined ? ` · 强化 +${item.enhance_level}` : ""}`));
      const qualityColors = {"红": "#fa87a0", "黄": "#ffbf6b", "紫": "#bd95ff", "蓝": "#61b6ff", "绿": "#48d6b0"};
      const quality = Object.keys(qualityColors).find((name) => String(item.quality_name || "").includes(name));
      if (quality) copy.firstChild.style.color = qualityColors[quality];
      summary.append(copy, element("span", "equipment-score", formatInteger(item.total_score ?? item.known_score ?? item.item_score)));
      detail.appendChild(summary);
      const body = element("div", "equipment-body");
      if (item.enhance_level === undefined || item.enhance_level === null) {
        body.appendChild(element("p", "equipment-data-warning", "未采集到强化等级，当前装备的强化数值无法确认。"));
      }
      const enhancementValues = [
        ["当前强化", item.enhance_level === undefined ? null : `+${item.enhance_level}`],
        ["强化进度", item.enhance_level_progress_percent === undefined ? null : `${item.enhance_level_progress_percent}%`],
        ["下一级", item.next_enhance_level === undefined ? null : `+${item.next_enhance_level}`],
        ["还需评分", item.next_enhance_remaining],
        ["基础评分", item.base_score],
        ["词条评分", item.word_score ?? item.random_score],
      ].filter((entry) => entry[1] !== null && entry[1] !== undefined);
      if (enhancementValues.length) {
        const enhancement = element("div", "enhancement-grid");
        enhancementValues.forEach(([label, value]) => {
          const metric = element("div");
          metric.append(element("span", "", label), element("b", "", typeof value === "number" ? formatInteger(value) : value));
          enhancement.appendChild(metric);
        });
        body.appendChild(enhancement);
      }
      (item.affixes || []).forEach((affix) => {
        const row = element("div", "equipment-affix");
        row.appendChild(element("strong", "", `${displayAttributeName(affix.name, "未知词条")}${Number(affix.score) > 0 ? ` · ${formatInteger(affix.score)} 分` : ""}`));
        properties(affix.properties, row);
        body.appendChild(row);
      });
      if (item.special_affix) {
        const special = element("div", "equipment-special");
        special.appendChild(element("strong", "", displayAttributeName(item.special_affix.name, "特殊词条")));
        properties(item.special_affix.properties, special);
        (item.special_affix.passive_skill_ids || []).forEach((skill_id) => {
          const skill = {skill_id}, row = element("div", "event-skill");
          row.append(skillIcon(skill), element("span", "", skillDisplayName(skill)));
          special.appendChild(row);
        });
        body.appendChild(special);
      }
      if (!item.affixes?.length && !item.special_affix) body.appendChild(element("p", "detail-note", "未记录词条详情。"));
      detail.appendChild(body);
      grid.appendChild(detail);
    });
    section.appendChild(grid);
    const attributes = Object.entries(snapshot.attributes || {});
    if (attributes.length) {
      section.appendChild(sectionHeading("已采集装备属性"));
      const list = element("div", "equipment-attributes");
      attributes.forEach(([key, value]) => { const row = element("div"); row.append(element("span", "", displayAttributeName(state.iconCatalog?.attributes?.[key] || key)), element("b", "", formatInteger(value))); list.appendChild(row); });
      section.appendChild(list);
    }
    return section;
  }

  function renderDamageDistribution(events, container) {
    const damages = events.map((event) => Number(event.damage)).filter((value) => value > 0);
    if (!damages.length) return;
    const min = Math.min(...damages), max = Math.max(...damages), bins = Array.from({length: 10}, () => ({normal: 0, critical: 0, unknown: 0}));
    events.filter((event) => Number(event.damage) > 0).forEach((event) => {
      const index = Math.min(9, Math.floor((Number(event.damage) - min) / Math.max(1, max - min) * 10));
      bins[index][event.critical === true ? "critical" : event.critical === false ? "normal" : "unknown"]++;
    });
    container.appendChild(sectionHeading("单次伤害分布", `${damages.length} 条伤害事件`));
    const ranges = bins.map((_, index) => {
      const start = min + index / 10 * (max - min);
      const end = min + (index + 1) / 10 * (max - min);
      return max === min ? chartNumber(min) : `${chartNumber(start)}–${chartNumber(end)}`;
    });
    const chartOption = {
      ...baseChartOption(),
      color: ["#61b6ff", "#bd95ff", "#667582"],
      tooltip: {...baseChartOption().tooltip, trigger: "axis", axisPointer: {type: "shadow"}},
      legend: {top: 2, textStyle: {color: "#b8c7d2"}, data: ["普通", "暴击", "标记未知"]},
      grid: {left: 44, right: 18, top: 40, bottom: 70},
      xAxis: {
        type: "category", data: ranges,
        axisLabel: {color: "#8fa1ae", fontSize: 10, rotate: 28, interval: 0},
        axisLine: {lineStyle: {color: "#344452"}}, axisTick: {show: false},
      },
      yAxis: {
        type: "value", minInterval: 1,
        axisLabel: {color: "#8fa1ae"},
        splitLine: {lineStyle: {color: "rgba(104,127,145,.16)"}},
      },
      series: [
        ["普通", "normal"], ["暴击", "critical"], ["标记未知", "unknown"],
      ].map(([name, key]) => ({
        name, type: "bar", stack: "hits", barMaxWidth: 44,
        data: bins.map((bin) => bin[key]),
        label: {show: true, position: "inside", color: "#07111a", fontWeight: 700, formatter: ({value}) => value ? value : ""},
      })),
    };
    container.appendChild(mountEChart("damage-distribution-echart", chartOption, "单次伤害分布：蓝色普通，紫色暴击，灰色未知"));
  }

  function renderSkills(panel, participant, mode) {
    disposeCharts(panel);
    panel.replaceChildren();
    const stats = participant?.stats || {};
    const overview = element("div", "participant-overview");
    overview.appendChild(sectionHeading(participant ? `${participant.display_name} · 个人数据` : "个人数据"));
    if (participant && participant.public_mode !== "unrecorded") {
      const history = element("a", "history-reset", "查看该角色的历史 →");
      history.href = `#search?q=${encodeURIComponent(participant.display_name)}`;
      overview.appendChild(history);
    }
    panel.appendChild(overview);
    if (!participant) {
      panel.appendChild(element("p", "detail-note", "本场没有可选择的团队成员。"));
      return;
    }

    const timeline = Array.isArray(stats.skill_timeline) ? [...stats.skill_timeline].sort((a, b) => a.time_ms - b.time_ms) : [];
    const tabPanels = new Map();
    const createTabPanel = (name) => {
      const section = element("section", "participant-tab-panel");
      section.dataset.tab = name;
      section.setAttribute("role", "tabpanel");
      tabPanels.set(name, section);
      return section;
    };

    const overviewPanel = createTabPanel("overview");
    const metrics = element("div", "participant-metrics");
    for (const [label, value] of [["总伤害", participant.damage], ["有效治疗", stats.effective_healing], ["承伤", stats.taken], ["死亡次数", stats.deaths], ["死亡时长", stats.death_duration_seconds], ["评分", stats.extraordinary_rating]]) {
      const card = metricCard(label, value === undefined || value === null ? "未记录" : label === "死亡时长" ? `${Number(value).toFixed(1)} 秒` : formatInteger(value));
      card.classList.add(label === "死亡次数" ? "death-metric" : label === "有效治疗" ? "healing-metric" : label === "承伤" ? "taken-metric" : "damage-metric");
      metrics.appendChild(card);
    }
    overviewPanel.appendChild(metrics);

    const skillPanel = createTabPanel("skills");
    skillPanel.appendChild(sectionHeading(mode === "hps" ? "治疗技能构成" : "技能构成", mode === "hps"
      ? "按有效治疗排序 · 点击技能展开详情"
      : `暴击 ${formatPercent(stats.critical_rate)} · 穿刺 ${formatPercent(stats.penetration_rate)} · 点击技能展开详情`));
    const key = mode === "hps" ? "effective_healing" : "damage";
    const skills = (Array.isArray(stats.skills) ? stats.skills : []).map((skill) => ({...skill, metricValue: Number(skill[key] || 0)})).filter((skill) => skill.metricValue > 0).sort((a, b) => b.metricValue - a.metricValue);
    if (mode === "dt" || !skills.length) {
      skillPanel.appendChild(element("p", "detail-note", mode === "dt" ? "当前记录没有可靠的逐技能承伤归属。" : "该成员未上传此项逐技能数据。"));
    } else {
      const total = mode === "hps" ? Number(stats.effective_healing) || skills.reduce((sum, skill) => sum + skill.metricValue, 0) : Number(participant.damage) || skills.reduce((sum, skill) => sum + skill.metricValue, 0);
      const eventKey = (skillId) => {
        const numeric = Number(skillId);
        return Number.isFinite(numeric) ? String(numeric) : String(skillId ?? "");
      };
      const eventsBySkill = new Map();
      timeline.forEach((event) => {
        const skillId = eventKey(event.skill_id);
        if (!eventsBySkill.has(skillId)) eventsBySkill.set(skillId, []);
        eventsBySkill.get(skillId).push(event);
      });
      const skillRows = skills.map((skill, index) => {
        const events = eventsBySkill.get(eventKey(skill.skill_id)) || [];
        const damageEvents = events.filter((event) => Number(event.damage) > 0);
        const recordedHits = Number(skill.hits);
        const recordedMaxHit = Number(skill.max_hit);
        return {
          skill,
          events,
          damageEvents,
          color: SKILL_COLORS[index % SKILL_COLORS.length],
          share: skill.metricValue / Math.max(1, total),
          shownCount: mode === "hps"
            ? skill.healing_skill_count ?? skill.healing_events ?? null
            : Number.isFinite(recordedHits) && recordedHits > 0
              ? recordedHits
              : damageEvents.length || null,
          lastMetric: mode === "hps"
            ? skill.overhealing
            : Number.isFinite(recordedMaxHit) && recordedMaxHit > 0
              ? recordedMaxHit
              : damageEvents.length
                ? Math.max(...damageEvents.map((event) => Number(event.damage)))
                : null,
        };
      });
      const metricTotal = skills.reduce((sum, skill) => sum + skill.metricValue, 0);
      const missing = Math.max(0, total - metricTotal);
      const statistics = element("div", "skill-statistics");
      const donutCard = element("div", "skill-stat-card skill-donut-card");
      const donutTitle = element("div", "skill-stat-title");
      donutTitle.append(element("strong", "", mode === "hps" ? "治疗占比" : "伤害占比"), element("span", "", `${skills.length} 个技能`));
      const detailsBySkill = new Map();
      let openSkill = () => {};
      const pieRich = {};
      skillRows.forEach((row, index) => {
        pieRich[`icon${index}`] = {width: 20, height: 20, borderRadius: 4, backgroundColor: {image: skillIconUrl(row.skill)}};
      });
      const pieData = skillRows.map((row) => ({
        name: skillDisplayName(row.skill, mode).replace(/[{}]/g, ""),
        value: row.skill.metricValue,
        skillId: row.skill.skill_id,
        itemStyle: {color: row.color},
      }));
      if (missing > 0) pieData.push({name: "未归类", value: missing, skillId: null, itemStyle: {color: "#52606c"}});
      const pieOption = {
        ...baseChartOption(),
        tooltip: {
          ...baseChartOption().tooltip,
          formatter: ({name, value, percent}) => `${name}\n${mode === "hps" ? "有效治疗" : "伤害"} ${formatInteger(value)}\n占比 ${Number(percent).toFixed(1)}%`,
        },
        graphic: [{
          type: "group", left: "center", top: "43%", children: [
            {type: "text", left: "center", style: {text: chartNumber(total), fill: "#f4f9fc", font: '700 21px "Microsoft YaHei UI"', textAlign: "center"}},
            {type: "text", left: "center", top: 29, style: {text: mode === "hps" ? "总有效治疗" : "总伤害", fill: "#8599a8", font: '11px "Microsoft YaHei UI"', textAlign: "center"}},
          ],
        }],
        series: [{
          name: mode === "hps" ? "治疗技能" : "伤害技能",
          type: "pie",
          radius: ["35%", "55%"],
          center: ["50%", "50%"],
          minAngle: 1,
          avoidLabelOverlap: true,
          labelLayout: {moveOverlap: "shiftY", hideOverlap: false},
          labelLine: {length: 10, length2: 8, lineStyle: {color: "#587083"}},
          label: {
            show: true,
            color: "#dce7ee",
            fontSize: 10,
            lineHeight: 17,
            formatter: (params) => `${params.data.skillId ? `{icon${params.dataIndex}|} ` : ""}{name|${params.name}}\n{metric|${chartNumber(params.value)}} {share|${Number(params.percent).toFixed(1)}%}`,
            rich: {
              ...pieRich,
              name: {color: "#e7f0f5", fontSize: 10, fontWeight: 600},
              metric: {color: "#aebfca", fontSize: 10},
              share: {color: "#61b6ff", fontSize: 10, fontWeight: 700},
            },
          },
          emphasis: {scaleSize: 7},
          data: pieData,
        }],
      };
      const pieChart = mountEChart("skill-composition-echart", pieOption, mode === "hps" ? "治疗技能占比图，图中显示技能名称、有效治疗和占比" : "技能伤害占比图，图中显示技能名称、伤害和占比", (params) => {
        if (params.data?.skillId !== null && params.data?.skillId !== undefined) openSkill(params.data.skillId);
      });
      donutCard.append(donutTitle, pieChart);

      const rankCard = element("div", "skill-stat-card skill-rank-card");
      const rankTitle = element("div", "skill-stat-title");
      rankTitle.append(element("strong", "", "全部技能排行"), element("span", "", `${skills.length} 项 · 图中直接显示数值与占比`));
      const rankRich = {};
      const rankNames = skillRows.map((row, index) => {
        rankRich[`icon${index}`] = {width: 22, height: 22, borderRadius: 4, backgroundColor: {image: skillIconUrl(row.skill)}};
        return skillDisplayName(row.skill, mode).replace(/[{}]/g, "");
      });
      const rankOption = {
        ...baseChartOption(),
        tooltip: {
          ...baseChartOption().tooltip,
          formatter: ({data, name, value}) => `${name}\n${mode === "hps" ? "有效治疗" : "伤害"} ${formatInteger(value)}\n占比 ${formatPercent(data.share)}`,
        },
        grid: {left: 160, right: 112, top: 8, bottom: 8},
        xAxis: {type: "value", show: false, max: Math.max(1, ...skillRows.map((row) => row.skill.metricValue)) * 1.12},
        yAxis: {
          type: "category", inverse: true, data: rankNames,
          axisLine: {show: false}, axisTick: {show: false},
          axisLabel: {
            color: "#dce7ee", fontSize: 10, margin: 12,
            formatter: (value, index) => `{rank|${String(index + 1).padStart(2, "0")}} {icon${index}|} {name|${value}}`,
            rich: {
              ...rankRich,
              rank: {width: 19, color: "#718797", fontSize: 9, fontFamily: "monospace", align: "center"},
              name: {width: 92, color: "#dce7ee", fontSize: 10, fontWeight: 600, overflow: "truncate"},
            },
          },
        },
        series: [{
          name: mode === "hps" ? "有效治疗" : "伤害",
          type: "bar", barWidth: 9, showBackground: true,
          backgroundStyle: {color: "rgba(83,105,121,.18)", borderRadius: 5},
          data: skillRows.map((row) => ({
            value: row.skill.metricValue,
            share: row.share,
            skillId: row.skill.skill_id,
            itemStyle: {color: row.color, borderRadius: [0, 5, 5, 0]},
          })),
          label: {
            show: true, position: "right", distance: 8, color: "#edf5fa", fontSize: 10, fontWeight: 700,
            formatter: ({data, value}) => `${chartNumber(value)}  ${formatPercent(data.share)}`,
          },
        }],
      };
      const rankChart = mountEChart("skill-ranking-echart", rankOption, "全部技能排行，图中显示每个技能的数值与占比", (params) => openSkill(params.data?.skillId));
      rankChart.style.height = `${Math.max(300, skillRows.length * 31 + 24)}px`;
      rankCard.append(rankTitle, rankChart);
      statistics.append(donutCard, rankCard);

      const listHeading = element("div", "skill-list-heading");
      listHeading.append(element("strong", "", "全部技能明细"), element("span", "", `${skills.length} 项 · 点击一项查看命中表现与逐次记录`));
      const head = element("div", "skill-row skill-head");
      ["技能 / 展开详情", mode === "hps" ? "有效治疗" : "伤害", "占比", mode === "hps" ? "技能次数" : "记录次数", mode === "hps" ? "过量治疗" : "最高一击"].forEach((label) => head.appendChild(element("div", "", label)));
      const list = element("div", "skill-list");
      skillRows.forEach((row) => {
        const {skill, events, damageEvents, color, share, shownCount, lastMetric} = row;
        const detail = element("details", "skill-detail");
        detail.dataset.skillId = skill.skill_id;
        detail.style.setProperty("--skill-color", color);
        detailsBySkill.set(eventKey(skill.skill_id), detail);
        const summary = element("summary", "skill-row");
        const identity = element("div", "skill-identity"), copy = element("div");
        copy.append(element("span", "skill-name", skillDisplayName(skill, mode)), element("span", "skill-share-track"));
        copy.lastChild.style.setProperty("--share", `${Math.min(100, share * 100)}%`);
        identity.append(skillIcon(skill), copy);
        summary.append(identity, element("div", "number-cell", formatInteger(skill.metricValue)), element("div", "number-cell", formatPercent(share)), element("div", "number-cell", shownCount !== null && shownCount !== undefined ? formatInteger(shownCount) : "--"), element("div", "number-cell", lastMetric !== null && lastMetric !== undefined ? formatInteger(lastMetric) : "--"));
        detail.appendChild(summary);
        const body = element("div", "skill-detail-body");
        const sampled = events.reduce((sum, event) => sum + Number(event.damage || 0), 0);
        const knownCrit = events.filter((event) => typeof event.critical === "boolean" && Number(event.damage) > 0);
        const knownPierce = events.filter((event) => typeof event.penetrating === "boolean" && Number(event.damage) > 0);
        const critRate = skill.critical_rate ?? (knownCrit.length ? knownCrit.filter((event) => event.critical).length / knownCrit.length : null);
        const pierceRate = skill.penetration_rate ?? (knownPierce.length ? knownPierce.filter((event) => event.penetrating).length / knownPierce.length : null);
        const cards = element("div", "skill-detail-metrics");
        const castCount = skill.count_semantics === "server_skill_count" || skill.count_semantics === "casts";
        const average = mode === "dps" && damageEvents.length ? sampled / damageEvents.length : null;
        if (mode === "hps") {
          [["有效治疗", skill.effective_healing], ["总治疗", skill.total_healing], ["过量治疗", skill.overhealing], ["技能次数", skill.healing_skill_count ?? skill.healing_events]].forEach(([label, value]) => cards.appendChild(metricCard(label, value === null || value === undefined ? "未记录" : formatInteger(value))));
        } else {
          [["记录次数", shownCount], ["平均每次打击", average], ["最高一击", lastMetric]].forEach(([label, value]) => cards.appendChild(metricCard(label, formatInteger(value))));
          cards.append(metricCard(skill.critical_rate !== undefined ? "汇总暴击率" : "事件暴击率", formatPercent(critRate)), metricCard(skill.penetration_rate !== undefined ? "汇总穿刺率" : "事件穿刺率", formatPercent(pierceRate)));
        }
        body.append(cards, element("p", "detail-note", mode === "dps"
          ? `技能编号 ${skill.skill_id}${castCount ? " · 次数为服务端技能计数，不能当作打击次数" : ""}。已采集 ${events.length} 条事件，覆盖该技能伤害 ${formatPercent(sampled / Math.max(1, skill.damage))}；暴击标记 ${knownCrit.length} 条，穿刺标记 ${knownPierce.length} 条。平均每次打击按已采集的伤害事件计算。`
          : `技能编号 ${skill.skill_id}。治疗逐次事件未上传，技能次数为服务端汇总。`));
        let initialized = false;
        detail.addEventListener("toggle", () => {
          if (!detail.open || initialized) return;
          initialized = true;
          if (mode === "dps") { renderDamageDistribution(events, body); body.appendChild(renderEventTable(events, "per-skill-events")); }
        });
        detail.appendChild(body);
        list.appendChild(detail);
      });
      openSkill = (skillId) => {
        const detail = detailsBySkill.get(eventKey(skillId));
        if (!detail) return;
        detail.open = true;
        detail.scrollIntoView({block: "nearest", behavior: "smooth"});
      };
      skillPanel.append(statistics, listHeading, head, list);
    }

    const luckPanel = createTabPanel("luck");
    luckPanel.appendChild(renderCriticalLuck(stats));
    const openingPanel = createTabPanel("opening");
    openingPanel.appendChild(renderOpeningSequence(stats));
    const timelinePanel = createTabPanel("timeline");
    timelinePanel.appendChild(renderSkillTimeline(timeline, stats.skills, {
      total: stats.skill_timeline_total,
      truncated: stats.skill_timeline_truncated,
    }));
    const equipmentPanel = createTabPanel("equipment");
    equipmentPanel.appendChild(renderEquipment(stats.equipment_snapshot));

    const hasEquipment = Array.isArray(stats.equipment_snapshot?.equipment)
      && stats.equipment_snapshot.equipment.length > 0;
    const definitions = [
      ["overview", "个人总览", true],
      ["skills", mode === "hps" ? "治疗技能" : "技能伤害", mode !== "dt" && skills.length > 0],
      ["luck", "暴击运气", Boolean(stats.critical_luck || criticalSummaryData(stats))],
      ["opening", "起手序列", Array.isArray(stats.opening_sequence) && stats.opening_sequence.length > 0],
      ["timeline", "命中时间轴", timeline.length > 0],
      ["equipment", "装备快照", hasEquipment],
    ].sort((left, right) => Number(right[2]) - Number(left[2]));
    const tabs = element("div", "participant-data-tabs");
    tabs.setAttribute("role", "tablist");
    tabs.setAttribute("aria-label", `${participant.display_name || "当前成员"}的数据分类`);
    const body = element("div", "participant-data-body");
    const tabButtons = new Map();
    const activeTab = definitions.some(([name]) => name === state.encounterDetailTab)
      ? state.encounterDetailTab
      : "overview";
    const activate = (name) => {
      state.encounterDetailTab = name;
      definitions.forEach(([tabName]) => {
        const selected = tabName === name;
        const tab = tabButtons.get(tabName);
        tab.classList.toggle("is-active", selected);
        tab.setAttribute("aria-selected", String(selected));
        tab.tabIndex = selected ? 0 : -1;
        tabPanels.get(tabName).hidden = !selected;
      });
      window.requestAnimationFrame(() => {
        const activePanel = tabPanels.get(name);
        activePanel?.querySelectorAll(".echart").forEach((node) => {
          window.echarts?.getInstanceByDom(node)?.resize();
        });
      });
    };
    definitions.forEach(([name, label, hasData]) => {
      const tab = button("participant-data-tab", label);
      if (!hasData) {
        tab.classList.add("is-empty");
        tab.appendChild(element("span", "participant-tab-empty", "无数据"));
      }
      tab.setAttribute("role", "tab");
      tab.addEventListener("click", () => activate(name));
      tabButtons.set(name, tab);
      tabs.appendChild(tab);
      body.appendChild(tabPanels.get(name));
    });
    panel.append(tabs, body);
    activate(activeTab);
  }


  function resultLabel(value, confirmed = true) {
    if (value === "defeated" && !confirmed) return ["击败 · 未确认结算", " is-interrupted"];
    if (value === "defeated") return ["胜利", ""];
    if (value === "failed") return ["失败", " is-failed"];
    if (value === "interrupted") return ["中断", " is-interrupted"];
    return ["未确认", " is-interrupted"];
  }

  function encounterHealingTotals(encounter) {
    const participants = Array.isArray(encounter.participants) ? encounter.participants : [];
    const known = participants
      .map((participant) => participant.stats?.effective_healing)
      .filter((value) => value !== undefined && value !== null && Number.isFinite(Number(value)));
    if (!known.length) {
      return {
        healing: encounter.data?.team_effective_healing,
        hps: encounter.data?.team_hps,
      };
    }
    const healing = known.reduce((sum, value) => sum + Number(value), 0);
    const duration = Number(encounter.data?.hps_duration_seconds || encounter.duration_seconds || 0);
    return {healing, hps: duration > 0 ? healing / duration : 0};
  }

  function observedBossDpsTimeline(encounter, windowSeconds = 10) {
    const rows = compactLogRows(encounter.data?.boss_hp_damage_samples)
      .map((row) => [Math.floor(Number(row.time_seconds)), Math.max(0, Number(row.observed_boss_hp_loss))])
      .filter(([second, loss]) => Number.isFinite(second) && second >= 0 && Number.isFinite(loss));
    const samples = new Map(rows);
    const seconds = Array.from(samples.keys()).sort((a, b) => a - b);
    if (seconds.length < 3 || seconds.at(-1) - seconds[0] < 2) return [];
    if (seconds.length / (seconds.at(-1) - seconds[0] + 1) < 0.65) return [];
    if (seconds.some((second, index) => index && samples.get(second) < samples.get(seconds[index - 1]))) return [];
    const values = [];
    const perSecond = [];
    let previous = samples.get(seconds[0]);
    for (let second = seconds[0] + 1; second <= seconds.at(-1); second += 1) {
      const total = samples.has(second) ? samples.get(second) : previous;
      perSecond.push(total - previous);
      const rolling = perSecond.slice(-windowSeconds).reduce((sum, value) => sum + value, 0);
      const dps = rolling / Math.min(windowSeconds, second - seconds[0]);
      values.push({time: second, dps, team_dps: dps, source: "observed_boss_hp_loss"});
      previous = total;
    }
    return values;
  }

  function teamTimeline(encounter) {
    const source = encounter.data?.team_dps_timeline;
    const uploaded = (Array.isArray(source) ? source : [])
      .filter((point) => Number.isFinite(Number(point.time)) && Number.isFinite(Number(point.team_dps ?? point.dps)))
      .sort((a, b) => Number(a.time) - Number(b.time));
    const sources = new Set(uploaded.map((point) => String(point.source || "")));
    const expectedDps = Number(encounter.data?.team_dps || 0);
    const invalidDisplay = sources.has("live_display_team_dps")
      && expectedDps > 0
      && Math.max(0, ...uploaded.map((point) => Number(point.team_dps ?? point.dps))) < expectedDps * 0.5;
    const trustedTeam = sources.has("live_team_cumulative") || sources.has("complete_damage_events");
    const uploadedBossLoss = sources.has("observed_boss_hp_loss");
    const recovered = !trustedTeam && !uploadedBossLoss ? observedBossDpsTimeline(encounter) : [];
    const points = trustedTeam || uploadedBossLoss
      ? uploaded
      : recovered.length
        ? recovered
        : invalidDisplay ? [] : uploaded;
    const section = element("section", "history-chart");
    const timed = points.some((point) => Number(point.time) > 0);
    const usesBossLoss = points.some((point) => point.source === "observed_boss_hp_loss");
    section.dataset.hasData = String(points.length > 0);
    section.appendChild(sectionHeading(
      timed ? "团队 DPS 时间曲线" : "团队 DPS 采样曲线",
      points.length ? `${usesBossLoss ? "Boss 真实失血还原 · " : ""}${formatInteger(points.length)} 个采样点` : "无可验证数据",
    ));
    if (!points.length) {
      section.appendChild(element("p", "detail-note", invalidDisplay
        ? "旧采样与最终团队 DPS 的量级不一致，已停止展示；本场也没有可用于恢复的 Boss 血量采样。"
        : "本场没有可用的团队时间曲线，可继续查看成员和技能汇总。"));
      return section;
    }
    const position = (point, index) => timed ? Number(point.time) : index;
    const seriesName = usesBossLoss ? "Boss 实际失血 DPS" : "团队伤害 DPS";
    const chartData = points.map((point, index) => [position(point, index), Number(point.team_dps ?? point.dps)]);
    const average = chartData.reduce((sum, point) => sum + point[1], 0) / Math.max(1, chartData.length);
    const chartOption = {
      ...baseChartOption(),
      tooltip: {
        ...baseChartOption().tooltip,
        trigger: "axis",
        axisPointer: {type: "cross", lineStyle: {color: "#7897ad"}},
        formatter: (params) => {
          const point = params[0]?.value || [0, 0];
          return `${timed ? `${Number(point[0]).toFixed(1)} 秒` : `第 ${Number(point[0]) + 1} 个采样`}\n${seriesName} ${formatInteger(point[1])}`;
        },
      },
      legend: {top: 0, left: 0, data: [seriesName], textStyle: {color: "#c5d5df", fontWeight: 600}},
      grid: {left: 68, right: 116, top: 64, bottom: 42},
      xAxis: {
        type: "value", min: "dataMin", max: "dataMax",
        name: timed ? "战斗时间" : "采样顺序", nameTextStyle: {color: "#8195a3"},
        axisLabel: {color: "#8ea1ae", formatter: (value) => timed ? formatDuration(value) : `#${Math.round(value) + 1}`},
        axisLine: {lineStyle: {color: "#344452"}}, splitLine: {show: false},
      },
      yAxis: {
        type: "value", name: "DPS", nameTextStyle: {color: "#8195a3"},
        axisLabel: {color: "#8ea1ae", formatter: chartNumber},
        splitLine: {lineStyle: {color: "rgba(104,127,145,.16)"}},
      },
      dataZoom: [{type: "inside", filterMode: "none", zoomOnMouseWheel: "shift", moveOnMouseMove: true}],
      series: [{
        name: seriesName,
        type: "line",
        data: chartData,
        smooth: .16,
        showSymbol: chartData.length <= 45,
        symbolSize: 4,
        lineStyle: {color: "#61b6ff", width: 2},
        itemStyle: {color: "#61b6ff"},
        areaStyle: {color: {type: "linear", x: 0, y: 0, x2: 0, y2: 1, colorStops: [{offset: 0, color: "rgba(97,182,255,.34)"}, {offset: 1, color: "rgba(97,182,255,.02)"}]}},
        endLabel: {
          show: true, distance: 8, color: "#dceeff", fontSize: 10, fontWeight: 700,
          backgroundColor: "rgba(9,24,35,.92)", borderColor: "rgba(97,182,255,.58)",
          borderWidth: 1, borderRadius: 6, padding: [5, 8],
          formatter: ({value}) => `结束  ${chartNumber(value[1])}`,
        },
        labelLayout: {moveOverlap: "shiftY"},
        markPoint: {
          silent: true, symbol: "circle", symbolSize: 9,
          data: [{type: "max", name: "峰值"}],
          label: {
            show: true, position: "top", distance: 10, color: "#f4fbff",
            fontSize: 11, fontWeight: 700, backgroundColor: "rgba(9,24,35,.94)",
            borderColor: "rgba(97,182,255,.72)", borderWidth: 1, borderRadius: 7,
            padding: [6, 9], shadowBlur: 12, shadowColor: "rgba(42,145,220,.24)",
            formatter: ({value}) => `{title|峰值}  {value|${chartNumber(value)}}`,
            rich: {
              title: {color: "#7bc7ff", fontWeight: 800},
              value: {color: "#f4fbff", fontWeight: 700},
            },
          },
          itemStyle: {color: "#7bc7ff", borderColor: "#d8f0ff", borderWidth: 2},
        },
        markLine: {
          symbol: "none", silent: true,
          data: [{yAxis: average, name: "平均"}],
          lineStyle: {color: "rgba(109,211,231,.55)", type: "dashed"},
          label: {show: true, color: "#9edce8", formatter: `平均 ${chartNumber(average)}`, position: "insideEndTop"},
        },
      }],
    };
    section.appendChild(mountEChart("timeline-echart team-dps-echart", chartOption, `${seriesName} 时间曲线，图中标出峰值、平均值和结束值`));
    if (usesBossLoss) section.appendChild(element("p", "detail-note", "旧记录缺少完整团队逐次伤害，本曲线按 Boss 真实血量损失计算 10 秒滑动 DPS；它不包含小怪承受的伤害。"));
    else if (points.some((point) => point.source === "live_display_team_dps")) section.appendChild(element("p", "detail-note", "曲线记录采集时的团队显示值，并已通过最终团队 DPS 量级校验。"));
    if (!timed) section.appendChild(element("p", "detail-note", "旧上传记录缺少采样时间，曲线按采样顺序展示，无法确定每个点发生在第几秒。"));
    return section;
  }

  function compactLogRows(log) {
    if (!log || !Array.isArray(log.columns) || !Array.isArray(log.rows)) return [];
    return log.rows.map((values) => {
      if (!Array.isArray(values)) return values && typeof values === "object" ? values : {};
      return Object.fromEntries(log.columns.map((column, index) => [column, values[index]]));
    });
  }

  function bossHealthTimeline(encounter) {
    const section = element("section", "history-chart boss-health-chart");
    const log = encounter.data?.boss_hp_damage_samples;
    const samples = compactLogRows(log)
      .map((row) => ({
        time: Number(row.time_seconds), loss: Number(row.observed_boss_hp_loss),
        hp: row.current_hp == null ? NaN : Number(row.current_hp),
        maxHp: Number(row.max_hp || 0),
      }))
      .filter((row) => Number.isFinite(row.time) && Number.isFinite(row.loss))
      .sort((a, b) => a.time - b.time);
    const monster = encounter.data?.monster || {};
    const maxHp = Number(monster.max_hp || monster.observed_max_hp || 0);
    section.dataset.hasData = String(samples.length > 0);
    section.appendChild(sectionHeading("Boss 血量曲线", samples.length ? `${samples.length} 个真实采样点` : "未采集"));
    if (!samples.length) {
      section.appendChild(element("p", "detail-note", "采集端没有记录到本场 Boss 的真实血量变化，历史记录无法补造。"));
      return section;
    }
    const hasObservedHp = samples.every((sample) => Number.isFinite(sample.hp) && sample.hp >= 0);
    const canShowRemaining = hasObservedHp || (maxHp > 0 && Math.max(...samples.map((sample) => sample.loss)) <= maxHp * 1.02);
    const observedMaxima = new Set(samples.map((sample) => sample.maxHp));
    const referenceMaxHp = hasObservedHp ? (observedMaxima.size === 1 ? samples[0].maxHp : 0) : maxHp;
    const points = samples.map((sample) => ({
      ...sample,
      value: hasObservedHp ? sample.hp : canShowRemaining ? Math.max(0, maxHp - sample.loss) : sample.loss,
    }));
    const seriesName = canShowRemaining ? "Boss 剩余血量" : "Boss 累计血量损失";
    const chartData = points.map((point) => [point.time, point.value, point.loss, hasObservedHp ? point.maxHp : maxHp]);
    const first = chartData[0];
    const chartOption = {
      ...baseChartOption(),
      tooltip: {
        ...baseChartOption().tooltip,
        trigger: "axis",
        axisPointer: {type: "cross", lineStyle: {color: "#9f7784"}},
        formatter: (params) => {
          const value = params[0]?.value || [0, 0, 0];
          return canShowRemaining
            ? `${Number(value[0]).toFixed(1)} 秒\nBoss 血量 ${formatInteger(value[1])}${Number(value[3]) > 0 ? ` / ${formatInteger(value[3])}` : ""}\n累计损失 ${formatInteger(value[2])}`
            : `${Number(value[0]).toFixed(1)} 秒\n累计血量损失 ${formatInteger(value[2])}`;
        },
      },
      legend: {top: 0, left: 0, data: [seriesName], textStyle: {color: "#d9bdc5", fontWeight: 600}},
      grid: {left: 76, right: 120, top: 64, bottom: 42},
      xAxis: {
        type: "value", min: "dataMin", max: "dataMax", name: "战斗时间", nameTextStyle: {color: "#8f7e85"},
        axisLabel: {color: "#a18e95", formatter: formatDuration},
        axisLine: {lineStyle: {color: "#4a3840"}}, splitLine: {show: false},
      },
      yAxis: {
        type: "value", name: canShowRemaining ? "剩余血量" : "累计损失", nameTextStyle: {color: "#a08a92"},
        max: canShowRemaining ? Math.max(maxHp, ...samples.map((sample) => sample.maxHp), ...points.map((point) => point.value)) || undefined : undefined,
        axisLabel: {color: "#a18e95", formatter: chartNumber},
        splitLine: {lineStyle: {color: "rgba(126,91,105,.18)"}},
      },
      dataZoom: [{type: "inside", filterMode: "none", zoomOnMouseWheel: "shift", moveOnMouseMove: true}],
      series: [{
        name: seriesName,
        type: "line",
        encode: {x: 0, y: 1},
        data: chartData,
        smooth: .12,
        showSymbol: chartData.length <= 45,
        symbolSize: 4,
        lineStyle: {color: "#fa87a0", width: 2},
        itemStyle: {color: "#fa87a0"},
        areaStyle: {color: {type: "linear", x: 0, y: 0, x2: 0, y2: 1, colorStops: [{offset: 0, color: "rgba(250,135,160,.3)"}, {offset: 1, color: "rgba(250,135,160,.02)"}]}},
        endLabel: {
          show: true, distance: 8, color: "#ffe6ec", fontSize: 10, fontWeight: 700,
          backgroundColor: "rgba(35,12,21,.92)", borderColor: "rgba(250,135,160,.58)",
          borderWidth: 1, borderRadius: 6, padding: [5, 8],
          formatter: ({value}) => `结束  ${chartNumber(value[1])}`,
        },
        labelLayout: {moveOverlap: "shiftY"},
        markPoint: {
          silent: true, symbol: "circle", symbolSize: 9,
          data: [{name: "开场", coord: [first[0], first[1]], value: first[1]}],
          label: {
            show: true, position: "right", offset: [0, -18], distance: 9,
            color: "#fff4f7", fontSize: 11, fontWeight: 700,
            backgroundColor: "rgba(35,12,21,.94)", borderColor: "rgba(250,135,160,.72)",
            borderWidth: 1, borderRadius: 7, padding: [6, 9],
            shadowBlur: 12, shadowColor: "rgba(221,75,110,.22)",
            formatter: ({value}) => `{title|开场}  {value|${chartNumber(value)}}`,
            rich: {
              title: {color: "#ff9cb3", fontWeight: 800},
              value: {color: "#fff4f7", fontWeight: 700},
            },
          },
          itemStyle: {color: "#ff9cb3", borderColor: "#ffe0e8", borderWidth: 2},
        },
        markLine: canShowRemaining && referenceMaxHp > 0 ? {
          symbol: "none", silent: true,
          data: [{yAxis: referenceMaxHp * .5, name: "50% 血量"}],
          lineStyle: {color: "rgba(255,191,107,.62)", type: "dashed"},
          label: {show: true, color: "#ffd08e", formatter: `50% 血量 ${chartNumber(referenceMaxHp * .5)}`, position: "insideEndTop"},
        } : undefined,
      }],
    };
    section.appendChild(mountEChart("timeline-echart boss-hp-echart", chartOption, `${seriesName}曲线，图中显示开场值、结束值${canShowRemaining && referenceMaxHp > 0 ? "和 50% 血量线" : ""}`));
    return section;
  }

  function renderEncounterCurves(encounter) {
    const wrapper = element("section", "encounter-curves");
    const tabs = element("div", "curve-tabs");
    tabs.setAttribute("role", "tablist");
    tabs.setAttribute("aria-label", "团队战斗曲线");
    const body = element("div", "curve-tab-body");
    const definitions = [
      {key: "team-dps", label: "团队 DPS 时间曲线", panel: teamTimeline(encounter)},
      {key: "boss-hp", label: "Boss 血量曲线", panel: bossHealthTimeline(encounter)},
    ].map((item, index) => ({
      ...item,
      index,
      hasData: item.panel.dataset.hasData === "true",
    })).sort((left, right) => Number(right.hasData) - Number(left.hasData) || left.index - right.index);
    const hasAvailableCurve = definitions.some((item) => item.hasData);
    const preferred = definitions.find((item) => item.key === state.encounterCurveTab);
    const activeKey = preferred && (!hasAvailableCurve || preferred.hasData)
      ? preferred.key
      : definitions[0].key;
    const buttons = new Map();
    const panels = new Map();
    const select = (key) => {
      state.encounterCurveTab = key;
      buttons.forEach((tab, tabKey) => {
        const selected = tabKey === key;
        tab.classList.toggle("is-active", selected);
        tab.setAttribute("aria-selected", String(selected));
        tab.tabIndex = selected ? 0 : -1;
        panels.get(tabKey).hidden = !selected;
      });
      window.requestAnimationFrame(() => {
        panels.get(key)?.querySelectorAll(".echart").forEach((node) => {
          window.echarts?.getInstanceByDom(node)?.resize();
        });
      });
    };
    definitions.forEach(({key, label, panel, hasData}) => {
      const tab = button("curve-tab", label);
      tab.setAttribute("role", "tab");
      if (!hasData) {
        tab.classList.add("is-empty");
        tab.appendChild(element("span", "curve-tab-empty", "无数据"));
      }
      panel.classList.add("curve-tab-panel");
      tab.addEventListener("click", () => select(key));
      buttons.set(key, tab);
      panels.set(key, panel);
      tabs.appendChild(tab);
      body.appendChild(panel);
    });
    wrapper.append(tabs, body);
    select(activeKey);
    return wrapper;
  }

  function renderBossEventTable(bossDamage) {
    const details = element("details", "skill-events boss-events");
    const events = compactLogRows(bossDamage?.event_log).sort((a, b) => Number(a.time_ms) - Number(b.time_ms));
    details.appendChild(element("summary", "", `首领伤害时间轴 · ${events.length ? `${formatInteger(events.length)} 条` : "未采集"}`));
    if (!events.length) return details;
    const targetNames = new Map((bossDamage.targets || []).map((target) => [Number(target.actor_id), target.name || `成员 ${target.actor_id}`]));
    const sourceNames = new Map();
    (bossDamage.sources || []).forEach((source) => {
      sourceNames.set(Number(source.entity_id), source.name || "首领或机制");
      (source.entity_ids || []).forEach((id) => sourceNames.set(Number(id), source.name || "首领或机制"));
    });
    const body = element("div", "skill-event-body");
    let page = 0;
    const render = () => {
      body.replaceChildren();
      const table = element("table", "event-table");
      const head = element("thead"), header = element("tr");
      ["时间", "来源", "技能", "受击成员", "伤害"].forEach((label) => header.appendChild(element("th", "", label)));
      head.appendChild(header);
      const tbody = element("tbody");
      events.slice(page * 50, (page + 1) * 50).forEach((event) => {
        const row = element("tr"), skillCell = element("td"), identity = element("span", "event-skill");
        const skill = {skill_id: event.skill_id};
        identity.append(skillIcon(skill), element("span", "", skillDisplayName(skill)));
        skillCell.appendChild(identity);
        row.append(
          element("td", "", `${(Number(event.time_ms) / 1000).toFixed(2)} 秒`),
          element("td", "", sourceNames.get(Number(event.source_id)) || `来源 ${event.source_id || "--"}`),
          skillCell,
          element("td", "", targetNames.get(Number(event.target_id)) || `成员 ${event.target_id || "--"}`),
          element("td", "taken-text", formatInteger(event.damage)),
        );
        tbody.appendChild(row);
      });
      table.appendChild(tbody);
      body.appendChild(table);
      const pager = element("div", "history-pagination");
      const previous = button("portal-refresh", "上一页"), next = button("portal-refresh", "下一页");
      previous.disabled = page === 0;
      next.disabled = (page + 1) * 50 >= events.length;
      previous.addEventListener("click", () => { page--; render(); });
      next.addEventListener("click", () => { page++; render(); });
      pager.append(previous, element("span", "", `${page + 1} / ${Math.ceil(events.length / 50)}`), next);
      body.appendChild(pager);
    };
    render();
    details.appendChild(body);
    return details;
  }

  function renderBossDamage(encounter) {
    const section = element("section", "boss-damage-panel");
    const bossDamage = encounter.data?.boss_damage || {};
    const hasData = Number(bossDamage.observed_damage) > 0 || (bossDamage.sources || []).length || (bossDamage.skills || []).length;
    section.appendChild(sectionHeading("Boss 伤害", hasData ? ({complete: "数据完整", observed_partial: "已观测部分", conflict: "数据冲突"}[bossDamage.coverage] || "已记录") : "未采集"));
    if (!hasData) {
      section.appendChild(element("p", "detail-note", bossDamage.unavailable_reason === "training_dummy" ? "训练目标不会主动造成伤害。" : "本场没有已确认的首领或机制伤害事件。"));
      return section;
    }
    const metrics = element("div", "boss-metrics");
    [["已归类首领伤害", bossDamage.observed_damage], ["团队承伤", bossDamage.team_taken], ["归类比例", formatPercent(bossDamage.classification_ratio)], ["命中次数", bossDamage.hits], ["最高一击", bossDamage.max_hit]].forEach(([label, value]) => metrics.appendChild(metricCard(label, typeof value === "string" ? value : formatInteger(value))));
    section.appendChild(metrics);

    const sources = (Array.isArray(bossDamage.sources) ? bossDamage.sources : []).sort((a, b) => Number(b.damage || 0) - Number(a.damage || 0));
    section.appendChild(sectionHeading("首领与机制来源", sources.length ? `${sources.length} 个确认来源` : "未记录来源"));
    const sourceGrid = element("div", "boss-source-grid");
    sources.forEach((source) => {
      const card = element("article", "boss-source-card");
      card.append(
        element("strong", "", source.name || "首领或机制"),
        element("span", "", `${formatInteger(source.damage)} 伤害 · ${formatPercent(source.share)} · ${formatInteger(source.hits)} 次`),
      );
      const skills = element("div", "boss-source-skills");
      (source.skills || []).slice(0, 12).forEach((skill) => {
        const item = element("span", "event-skill");
        item.append(skillIcon(skill), element("span", "", `${skillDisplayName(skill)} ${formatInteger(skill.damage)}`));
        skills.appendChild(item);
      });
      card.appendChild(skills);
      sourceGrid.appendChild(card);
    });
    section.appendChild(sourceGrid);

    const skills = (Array.isArray(bossDamage.skills) ? bossDamage.skills : []).sort((a, b) => Number(b.damage || 0) - Number(a.damage || 0));
    section.appendChild(sectionHeading("首领技能构成", skills.length ? `${skills.length} 个技能` : "未记录技能"));
    const skillList = element("div", "boss-skill-list");
    skills.forEach((skill, index) => {
      const row = element("div", "boss-skill-row");
      const identity = element("div", "skill-identity");
      identity.append(skillIcon(skill), element("span", "skill-name", skillDisplayName(skill)));
      row.append(identity, element("b", "", formatInteger(skill.damage)), element("span", "", formatPercent(skill.share)), element("span", "", `${formatInteger(skill.hits)} 次`), element("span", "", `最高 ${formatInteger(skill.max_hit)}`));
      row.style.setProperty("--skill-color", SKILL_COLORS[index % SKILL_COLORS.length]);
      skillList.appendChild(row);
    });
    section.appendChild(skillList);

    const targets = (Array.isArray(bossDamage.targets) ? bossDamage.targets : []).sort((a, b) => Number(b.damage || 0) - Number(a.damage || 0));
    section.appendChild(sectionHeading("受击成员", targets.length ? `${targets.length} 名已记录成员` : "未记录"));
    const targetList = element("div", "boss-target-list");
    targets.forEach((target) => {
      const row = element("div", "boss-target-row");
      row.append(element("strong", "", target.name || `成员 ${target.actor_id || "--"}`), element("b", "", formatInteger(target.damage)), element("span", "", formatPercent(target.share)), element("span", "", `${formatInteger(target.hits)} 次 · 最高 ${formatInteger(target.max_hit)}`));
      targetList.appendChild(row);
    });
    section.append(targetList, renderBossEventTable(bossDamage));
    if (bossDamage.truncated) section.appendChild(element("p", "detail-note", "首领伤害事件超过上传上限，本页已明确标记为截断。"));
    return section;
  }

  function renderEncounterBody(encounter) {
    const fragment = document.createDocumentFragment();
    const hero = element("section", "encounter-hero");
    const backdrop = bossBannerName(encounter.stage_id, encounter.boss_name);
    if (backdrop) {
      hero.classList.add("has-backdrop");
      hero.style.setProperty(
        "--boss-backdrop",
        `url("${bossBannerUrl(backdrop)}")`,
      );
    }
    const heroImage = bossIcon(encounter.stage_id, "encounter-boss-icon", encounter.boss_name);
    if (!heroImage.hidden) hero.appendChild(heroImage);
    else hero.appendChild(element("div", "encounter-boss-icon"));
    const heroCopy = element("div");
    heroCopy.appendChild(element("h2", "encounter-name", encounter.boss_name || "未知首领"));
    const bossName = String(encounter.boss_name || "").trim();
    const location = [encounter.data?.dungeon_name, encounter.data?.stage_name]
      .map((value) => String(value || "").trim())
      .filter((value, index, values) => value && value !== bossName && values.indexOf(value) === index)
      .join(" · ");
    heroCopy.appendChild(element(
      "div",
      "encounter-subtitle",
      `${location ? `${location} · ` : ""}${formatDate(encounter.ended_at)} · ${Number(encounter.team_size) || 0} 人`,
    ));
    const [label, resultClass] = resultLabel(encounter.data?.result, encounter.data?.completion_confirmed);
    const context = element("div", "encounter-context");
    const difficultyKey = String(encounter.difficulty || "unknown").toLowerCase().replace(/[^a-z0-9_-]/g, "") || "unknown";
    const difficultyBadge = element("span", `encounter-context-item status-badge difficulty-badge difficulty-${difficultyKey}`);
    difficultyBadge.appendChild(element("strong", "encounter-context-value", performanceDifficultyLabel(encounter.difficulty || "unknown")));
    context.appendChild(difficultyBadge);
    const tomeLabel = brassTomeLabel(encounter.brass_tome_status);
    if (tomeLabel) {
      const tomeBadge = element("span", `encounter-context-item status-badge brass-${encounter.brass_tome_status}`);
      tomeBadge.appendChild(element("strong", "encounter-context-value", `黄铜书${tomeLabel}`));
      context.appendChild(tomeBadge);
    }
    const resultBadge = element("span", `encounter-context-item status-badge result-badge${resultClass}`);
    resultBadge.appendChild(element("strong", "result-badge-value", label));
    context.appendChild(resultBadge);
    const challengeIds = Array.isArray(encounter.brass_tome_challenge_ids)
      ? encounter.brass_tome_challenge_ids.filter((value) => Number(value) > 0)
      : [];
    if (challengeIds.length) {
      context.appendChild(element("span", "encounter-context-item", `挑战编号：${challengeIds.join("、")}`));
    }
    heroCopy.appendChild(context);
    hero.appendChild(heroCopy);
    fragment.appendChild(hero);
    const actions = element("div", "encounter-actions");
    const copy = button("portal-refresh", "复制战报链接");
    copy.addEventListener("click", async () => {
      try { await navigator.clipboard.writeText(window.location.href); copy.textContent = "已复制"; }
      catch (_error) {
        const input = element("input", "share-url");
        input.value = window.location.href;
        input.readOnly = true;
        actions.appendChild(input);
        input.select();
        copy.disabled = true;
        copy.textContent = "复制选中的地址";
      }
    });
    const back = element("a", "history-reset", "返回角色战斗记录 →");
    back.href = state.detailReturnRoute;
    actions.append(copy, back);
    fragment.appendChild(actions);
    const hasAi = (encounter.participants || []).some((participant) => participant.is_ai);
    const incompleteIdentity = (encounter.qualification_reasons || []).includes("PARTICIPANT_IDENTITY_INCOMPLETE");
    const included = encounter.statistics_status === "included" && encounter.ranking_status === "eligible" && !hasAi;
    const qualificationCopy = hasAi
      ? "本场包含人机成员，可查询战斗详情，不进入数据排行和职业分析。"
      : encounter.statistics_status === "included" && incompleteIdentity
        ? "本场有队友身份未被完整记录；战斗详情可以查看，但不进入数据排行和职业分析。"
      : included
        ? "本场已纳入有效统计与排行。"
        : encounter.statistics_status === "included"
          ? "本场已纳入有效统计，但不进入数据排行。"
          : `本场作为历史记录收录，未纳入正式统计：${qualificationText(encounter.qualification_reasons) || "数据未满足统计条件"}。`;
    fragment.appendChild(element("p", `qualification-note${included ? " is-included" : ""}`, qualificationCopy));

    const metrics = element("div", "metric-grid");
    const healing = encounterHealingTotals(encounter);
    const totalDamage = metricCard("团队总伤害", formatCompact(encounter.team_total_damage), "本场累计");
    const teamDps = metricCard("团队 DPS", formatInteger(encounter.data?.team_dps), "有效战斗时间");
    const teamHealing = metricCard("团队有效治疗", formatCompact(healing.healing), "本场累计");
    const teamHps = metricCard("团队 HPS", formatInteger(healing.hps), "有效治疗时间");
    totalDamage.classList.add("damage-metric");
    teamDps.classList.add("damage-metric");
    teamHealing.classList.add("healing-metric");
    teamHps.classList.add("healing-metric");
    metrics.append(totalDamage, teamDps, teamHealing, teamHps);
    metrics.appendChild(metricCard("战斗时长", formatDuration(encounter.duration_seconds), "分:秒"));
    const teamTaken = metricCard("团队承伤", formatCompact(encounter.data?.team_taken), "本场累计");
    teamTaken.classList.add("taken-metric");
    metrics.appendChild(teamTaken);
    metrics.appendChild(metricCard("Boss 最大血量", formatCompact(encounter.data?.monster?.max_hp || encounter.data?.monster?.observed_max_hp), "本场采集"));
    const deaths = metricCard("团队死亡次数", encounter.team_deaths === null || encounter.team_deaths === undefined
      ? encounter.recorded_deaths === null || encounter.recorded_deaths === undefined ? "未记录" : `${formatInteger(encounter.recorded_deaths)} 次已记录`
      : `${formatInteger(encounter.team_deaths)} 次`, `已记录 ${encounter.death_recorded_members || 0} / ${encounter.death_total_members || 0} 名成员`);
    deaths.classList.add("death-metric");
    metrics.appendChild(deaths);
    fragment.appendChild(metrics);
    fragment.appendChild(renderEncounterCurves(encounter));
    const tabs = element("div", "mode-tabs");
    [["dps", "伤害 DPS"], ["hps", "治疗 HPS"], ["dt", "承伤 DT"], ["boss_damage", "Boss 伤害"]].forEach(([mode, labelText]) => {
      const tab = button(`mode-tab${state.encounterMode === mode ? " is-active" : ""}`, labelText);
      tab.addEventListener("click", () => {
        state.encounterMode = mode;
        renderEncounterBodyIntoPage(encounter);
      });
      tabs.appendChild(tab);
    });
    fragment.appendChild(tabs);

    if (state.encounterMode === "boss_damage") {
      fragment.appendChild(renderBossDamage(encounter));
      return fragment;
    }

    const participantsSection = element("section");
    const skillsPanel = element("section", "skills-panel");
    const participants = Array.isArray(encounter.participants) ? encounter.participants : [];
    const durationSeconds = Number(encounter.duration_seconds) || 0;
    const sorted = [...participants].sort(
      (a, b) => (encounterMetric(state.encounterMode, b, durationSeconds) || 0)
        - (encounterMetric(state.encounterMode, a, durationSeconds) || 0),
    );
    participantsSection.appendChild(sectionHeading("团队成员", `${sorted.length} 名成员 · ${encounterMetricLabel(state.encounterMode)}`));
    const list = element("div", "records-list");
    list.classList.add("participants-list", `mode-${state.encounterMode}`);
    const memberHead = element("div", "participant-row participant-head");
    ["#", "角色 / 职业", "表现占比", encounterMetricLabel(state.encounterMode), "占比", "死亡次数"].forEach((label) => memberHead.appendChild(element("div", "", label)));
    list.appendChild(memberHead);
    const maxMetric = Math.max(
      1,
      ...sorted.map((participant) => encounterMetric(state.encounterMode, participant, durationSeconds) || 0),
    );
    let selectedRow = null;

    sorted.forEach((participant, index) => {
      const row = button("participant-row", "");
      row.dataset.slot = participant.slot;
      row.setAttribute("aria-label", `查看 ${participant.display_name || "未记录姓名"} 的技能数据`);
      row.appendChild(element("div", `rank-cell${index < 3 ? " is-top" : ""}`, index + 1));
      row.appendChild(participantIdentity(participant));
      const track = element("div", "metric-track");
      const fill = element("div", "metric-fill");
      fill.style.setProperty("--member-color", state.encounterMode === "hps" ? "#48d6b0" : state.encounterMode === "dt" ? "#ffbf6b" : professionColor(participant.profession_id));
      const metric = encounterMetric(state.encounterMode, participant, durationSeconds);
      fill.style.setProperty("--fill", `${Math.max(0, Math.min(100, (metric || 0) / maxMetric * 100))}%`);
      track.appendChild(fill);
      row.appendChild(track);
      row.appendChild(element("div", "number-cell primary", formatInteger(metric)));
      row.appendChild(element(
        "div",
        "number-cell",
        formatPercent(encounterShare(state.encounterMode, participant, participants, durationSeconds)),
      ));
      row.appendChild(element("div", "death-count", participant.stats?.deaths === undefined || participant.stats?.deaths === null ? "未记录" : `${formatInteger(participant.stats.deaths)} 次`));
      row.addEventListener("click", () => {
        selectedRow?.classList.remove("is-selected");
        row.classList.add("is-selected");
        selectedRow = row;
        state.encounterSlot = participant.slot;
        renderSkills(skillsPanel, participant, state.encounterMode);
      });
      list.appendChild(row);
      if (participant.slot === state.encounterSlot) selectedRow = row;
    });
    if (!selectedRow) selectedRow = list.querySelector(".participant-row[data-slot]");
    participantsSection.appendChild(list);
    fragment.appendChild(participantsSection);
    fragment.appendChild(skillsPanel);

    window.requestAnimationFrame(() => {
      if (selectedRow) selectedRow.click();
      else renderSkills(skillsPanel, null, state.encounterMode);
    });
    return fragment;
  }

  function renderEncounterBodyIntoPage(encounter) {
    disposeCharts(contentNode);
    contentNode.replaceChildren(renderEncounterBody(encounter));
  }

  async function renderEncounter(token, encounterId) {
    const id = String(encounterId || "");
    if (!/^enc_[A-Za-z0-9_-]{8,64}$/.test(id)) {
      setPage("BATTLE DETAIL", "战斗详情");
      showStatus("战斗记录地址无效", "请从角色搜索结果中重新打开。", false);
      return;
    }
    setPage("BATTLE DETAIL", "战斗详情");
    showStatus("正在读取战斗详情");
    await Promise.all([
      loadBaseData(),
      loadSkillNames(),
      loadECharts(),
      fetchJson(`${API_ROOT}/encounters/${encodeURIComponent(id)}`),
    ]).then(([, , , payload]) => {
      if (token !== state.renderToken) return;
      const encounter = payload.encounter;
      if (!encounter) throw new Error("encounter_not_found");
      titleNode.textContent = encounter.boss_name || "战斗详情";
      renderEncounterBodyIntoPage(encounter);
      document.title = `${encounter.boss_name || "战斗详情"} · 叨叨诡秘`;
    });
  }

  function parseRoute() {
    const raw = window.location.hash.slice(1);
    if (!raw) return { name: "home", params: new URLSearchParams() };
    const separator = raw.indexOf("?");
    const name = separator >= 0 ? raw.slice(0, separator) : raw;
    const query = separator >= 0 ? raw.slice(separator + 1) : "";
    return { name, params: new URLSearchParams(query) };
  }

  function setActiveNavigation(name) {
    navLinks.forEach((link) => {
      const active = link.getAttribute("href") === `#${name}`;
      link.classList.toggle("is-active", active);
      if (active) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
    const shareActive = name === "share";
    shareLink?.classList.toggle("is-active", shareActive);
    if (shareActive) shareLink?.setAttribute("aria-current", "page");
    else shareLink?.removeAttribute("aria-current");
  }

  async function renderRoute(force = false) {
    const route = parseRoute();
    const token = ++state.renderToken;
    const isHome = route.name === "home";
    if (route.name === "insights") {
      const requestedBoss = route.params.get("boss") || "";
      if (requestedBoss.startsWith("name:") || ["drill", "viscountess"].includes(requestedBoss)) {
        state.insightsFilters.boss = requestedBoss;
      }
      const simpleFilters = {
        category: "category",
        dungeon: "dungeon",
        difficulty: "difficulty",
        brass: "brassTome",
        metric: "metric",
        version: "gameVersion",
        sort: "sort",
      };
      Object.entries(simpleFilters).forEach(([parameter, field]) => {
        const value = route.params.get(parameter);
        if (value) state.insightsFilters[field] = value;
      });
      for (const [parameter, field] of [["minRating", "minRating"], ["maxRating", "maxRating"]]) {
        if (!route.params.has(parameter)) continue;
        const value = Number(route.params.get(parameter));
        if (Number.isFinite(value) && value >= 0 && value <= 1000000) {
          state.insightsFilters[field] = Math.round(value);
        }
      }
      state.insightsFilters.calibrated = route.params.get("calibrated") === "1";
    }
    scene.classList.toggle("portal-open", !isHome);
    portal.classList.toggle("is-visible", !isHome);
    portal.classList.toggle("is-insights", route.name === "insights");
    portal.setAttribute("aria-hidden", isHome ? "true" : "false");
    setActiveNavigation(route.name);
    if (isHome) {
      document.title = "叨叨诡秘 · DPS Logs";
      return;
    }

    document.title = "叨叨诡秘助手 · 战斗记录";
    refreshButton.hidden = route.name === "share" || route.name === "lab";
    try {
      if (force) await loadBaseData(true);
      if (route.name === "insights") await renderInsights(token);
      else if (route.name === "records") await renderRecords(token);
      else if (route.name === "history") {
        const query = String(route.params.get("q") || "").trim();
        window.location.replace(query ? `#search?${route.params}` : "#home");
      }
      else if (route.name === "search") await renderSearch(token, route.params.get("q"));
      else if (route.name === "share") renderShare();
      else if (route.name === "lab") renderLab();
      else if (route.name === "battle") await renderEncounter(token, route.params.get("id"));
      else window.location.hash = "";
    } catch (error) {
      if (token !== state.renderToken) return;
      console.error(error);
      showStatus("数据暂时无法加载", "请稍后重试。", true);
    }
  }

  searchForm.addEventListener("submit", (event) => {
    event.preventDefault();
    const query = searchInput.value.trim();
    if (!query) {
      searchInput.focus();
      return;
    }
    const normalized = normalizeRecentSearch(query);
    saveRecentSearch(normalized);
    renderRecentSearches();
    window.location.hash = `search?q=${encodeURIComponent(normalized)}`;
  });

  backButton.addEventListener("click", () => {
    window.location.hash = parseRoute().name === "battle" ? state.detailReturnRoute : "home";
  });
  refreshButton.addEventListener("click", () => renderRoute(true));
  window.addEventListener("hashchange", () => renderRoute(false));
  const uploadButton = document.getElementById("uploadStatusBtn");
  const uploadPopover = document.getElementById("uploadPopover");
  const setPopover = (open) => {
    uploadPopover.hidden = !open;
    uploadPopover.classList.toggle("open", open);
    uploadButton.setAttribute("aria-expanded", String(open));
  };
  uploadButton.addEventListener("click", () => setPopover(uploadPopover.hidden));
  document.addEventListener("click", (event) => { if (!event.target.closest(".upload-area")) setPopover(false); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !uploadPopover.hidden) { setPopover(false); uploadButton.focus(); } });
  fetchJson(`${API_ROOT}/statistics`).then(({statistics}) => {
    const count = formatInteger(statistics.history_encounters);
    document.getElementById("historySummary").textContent = `当前 ${count} 场历史战斗`;
    document.getElementById("uploadCount").textContent = `${count} 场`;
    document.getElementById("lastUpload").textContent = statistics.last_uploaded_at ? formatDate(statistics.last_uploaded_at) : "暂无上传";
  }).catch(() => {
    document.getElementById("historySummary").textContent = "暂时无法读取历史战斗场次";
    document.getElementById("uploadCount").textContent = "暂时无法读取";
    document.getElementById("lastUpload").textContent = "暂时无法读取";
  });
  renderRecentSearches();
  renderRoute(false);
})();
