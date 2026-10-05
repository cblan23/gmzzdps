(() => {
  "use strict";

  const scene = document.getElementById("scene");
  const searchForm = document.querySelector(".search");
  const searchInput = searchForm?.querySelector("input");
  const navLinks = Array.from(document.querySelectorAll(".nav a"));
  const shareLink = document.querySelector(".share-link");
  const API_ROOT = "/api/v1/dps/public";
  const PROFESSIONS = {
    1200001: "歌颂者",
    1200002: "观众",
    1200003: "占卜家",
    1200004: "仲裁人",
    1200005: "学徒",
    1200006: "战士",
    1200007: "窥秘人",
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
    catalog: null,
    skillNames: null,
    iconCatalog: null,
    basePromise: null,
    skillNamesPromise: null,
    renderToken: 0,
    insightsRequestToken: 0,
    insightsSelectedProfession: 0,
    insightsInitialized: false,
    insightsFilters: {
      boss: "drill",
      difficulty: "all",
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
    detailReturnRoute: "#history",
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
      state.catalog = null;
      state.basePromise = null;
    }
    if (state.statistics && state.leaderboards && state.bossCatalog && state.catalog) return;
    if (state.basePromise) return state.basePromise;
    state.basePromise = Promise.all([
      fetchJson(`${API_ROOT}/statistics`),
      fetchJson(`${API_ROOT}/leaderboards?limit=500`),
      fetchJson("assets/bosses/boss_icon_sources.json"),
      fetchJson(`${API_ROOT}/catalog`),
      fetchJson("assets/icon_catalog.json"),
    ])
      .then(([statistics, leaderboards, bossCatalog, catalog, iconCatalog]) => {
        state.iconCatalog = iconCatalog;
        state.statistics = statistics.statistics || {};
        state.leaderboards = Array.isArray(leaderboards.leaderboards)
          ? leaderboards.leaderboards
          : [];
        state.bossCatalog = bossCatalog || {};
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

  function formatDate(value) {
    const seconds = Number(value);
    if (!Number.isFinite(seconds) || seconds <= 0) return "--";
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    }).format(new Date(seconds * 1000));
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

  function professionIcon(id) {
    const image = element("img", "profession-icon");
    image.src = Object.hasOwn(PROFESSIONS, Number(id)) ? `assets/professions/${Number(id)}.png` : "assets/skills/0.png";
    image.alt = professionName(id);
    image.loading = "lazy";
    image.addEventListener("error", () => { image.src = "assets/skills/0.png"; }, { once: true });
    return image;
  }

  function bossIconName(stageId) {
    const stages = state.bossCatalog?.stages;
    const stage = stages && stages[String(Number(stageId) || 0)];
    return stage && typeof stage.icon === "string" ? stage.icon : "";
  }

  function bossIcon(stageId, className = "boss-icon") {
    const image = element("img", className);
    const filename = bossIconName(stageId);
    if (!filename) {
      image.hidden = true;
      return image;
    }
    image.src = `assets/bosses/${encodeURIComponent(filename)}`;
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
    const image = bossIcon(row.stage_id);
    if (!image.hidden) cell.appendChild(image);
    const copy = element("div");
    copy.appendChild(element("div", "boss-name", row.boss_name || "未知首领"));
    copy.appendChild(element("div", "boss-stage", `关卡 ${Number(row.stage_id) || "--"}`));
    cell.appendChild(copy);
    return cell;
  }

  function openEncounter(encounterId) {
    const id = String(encounterId || "");
    if (!/^enc_[A-Za-z0-9_-]{8,64}$/.test(id)) return;
    state.detailReturnRoute = window.location.hash || "#history";
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
      record.appendChild(bossCell(row));
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
      unknown: "未确认",
    }[String(status || "unknown").toLowerCase()] || "未确认";
  }

  function performanceValue(value, metric) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    return metric === "boss_damage" ? formatCompact(number) : formatInteger(number);
  }

  async function fetchPerformance(filters) {
    const params = new URLSearchParams({
      boss: filters.boss,
      difficulty: filters.difficulty,
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

  function fillPerformanceDetail(container, group, performance) {
    container.replaceChildren();
    if (!group) {
      container.hidden = true;
      return;
    }
    container.hidden = false;
    const metric = performance.selection?.metric || "dps";
    const identity = element("div", "performance-detail-identity");
    identity.appendChild(element("strong", "", `${professionName(group.profession_id)} · 当前区间`));
    identity.appendChild(element("span", "", `${formatInteger(group.sample_count)} 场有效样本`));
    container.appendChild(identity);

    const quantiles = element("div", "performance-detail-quantiles");
    [
      ["P10", group.p10],
      ["P25", group.p25],
      ["P50 中位", group.p50],
      ["P75", group.p75],
      ["P90", group.p90],
      ["最佳", group.best],
    ].forEach(([labelText, value]) => {
      const item = element("div", "performance-detail-value");
      item.appendChild(element("span", "", labelText));
      item.appendChild(element("b", "", performanceValue(value, metric)));
      quantiles.appendChild(item);
    });
    container.appendChild(quantiles);

    if (group.mine) {
      const mine = element("div", "performance-mine-detail");
      const mineTitle = group.mine.demo ? "示例位置" : "我的位置";
      mine.appendChild(element(
        "strong",
        "",
        `${mineTitle} · ${performanceValue(group.mine.value, metric)} · P${Math.round(Number(group.mine.percentile) || 0)}`,
      ));
      mine.appendChild(element(
        "span",
        "",
        `超过 ${Math.round(Number(group.mine.exceeds_percent) || 0)}% 同职业记录`,
      ));
      const gap75 = Number(group.mine.gap_to_p75 || 0);
      const gap90 = Number(group.mine.gap_to_p90 || 0);
      mine.appendChild(element(
        "span",
        "",
        `${gap75 > 0 ? "距" : "领先"} P75 ${performanceValue(Math.abs(gap75), metric)} · ${gap90 > 0 ? "距" : "领先"} P90 ${performanceValue(Math.abs(gap90), metric)}`,
      ));
      container.appendChild(mine);
    }

    const bestEncounter = String(group.best_record?.encounter_id || "");
    const open = button(
      "performance-detail-open",
      bestEncounter
        ? "查看真实优秀战斗记录 →"
        : performance.preview
          ? "演示数据不提供战斗记录"
          : "真实记录开放后可查看",
    );
    open.disabled = !/^enc_[A-Za-z0-9_-]{8,64}$/.test(bestEncounter);
    if (!open.disabled) open.addEventListener("click", () => openEncounter(bestEncounter));
    container.appendChild(open);
  }

  function renderPerformancePage(performance, routeToken) {
    const filters = state.insightsFilters;
    const selection = performance.selection || {};
    const availability = performance.availability || {};
    const groups = Array.isArray(performance.groups) ? performance.groups : [];
    const fragment = document.createDocumentFragment();

    const overview = element("div", "performance-overview");
    const overviewCopy = element("div");
    const status = element(
      "span",
      `performance-source${performance.preview ? " is-preview" : ""}`,
      performance.preview
        ? performance.preview_kind === "demo" ? "效果预览" : "预览数据"
        : "真实上传统计",
    );
    overviewCopy.appendChild(status);
    overviewCopy.appendChild(element(
      "span",
      "",
      `${selection.dungeon_name || "--"} · ${selection.boss_name || "--"}`,
    ));
    overview.appendChild(overviewCopy);
    const overviewMeta = element("div", "performance-overview-meta");
    overviewMeta.appendChild(element(
      "div",
      "performance-update",
      `有效样本 ${formatInteger(performance.total_samples)} · ${formatInteger(performance.total_encounters)} 场战斗 · 更新 ${formatDate(performance.updated_at)}`,
    ));
    overview.appendChild(overviewMeta);
    fragment.appendChild(overview);

    const updateFilters = (patch) => {
      Object.assign(state.insightsFilters, patch);
      state.insightsSelectedProfession = 0;
      renderInsights(routeToken).catch(() => {
        if (routeToken === state.renderToken) showStatus("统计暂时无法加载", "请稍后重试。", true);
      });
    };

    const modeBar = element("div", "performance-modebar");
    modeBar.appendChild(performanceSegments([
      { value: "raw", label: "原始表现" },
      { value: "calibrated", label: "校准表现", title: "降低非凡评分差异对 DPS 对比的影响" },
    ], filters.calibrated ? "calibrated" : "raw", (value) => {
      updateFilters({ calibrated: value === "calibrated" });
    }));
    const allGroupsSingleSample = groups.length > 0
      && groups.every((group) => Number(group.sample_count) < 2);
    const modeNote = filters.calibrated && !Number(performance.calibration?.applied_groups)
      ? "当前样本不足，校准结果暂与原始表现一致"
      : allGroupsSingleSample
        ? "样本积累中，当前分位点会暂时重合"
        : "点击任一职业查看完整分位";
    modeBar.appendChild(element("div", "performance-mode-note", modeNote));
    fragment.appendChild(modeBar);

    const layout = element("div", "performance-layout");
    const primary = element("section", "performance-primary");
    const chartCard = element("section", "performance-card performance-chart-card");
    const chartHead = element("div", "performance-chart-head");
    const chartCopy = element("div");
    chartCopy.appendChild(element("h2", "", "当前筛选 · 职业表现分布"));
    chartCopy.appendChild(element(
      "p",
      "",
      `${performanceDifficultyLabel(filters.difficulty)} · ${performanceMetricLabel(filters.metric)} · 非凡评分 ${formatInteger(filters.minRating)}–${formatInteger(filters.maxRating)}`,
    ));
    chartHead.appendChild(chartCopy);
    chartHead.appendChild(performanceSegments([
      { value: "dps", label: "DPS" },
      { value: "boss_damage", label: "首领伤害" },
    ], filters.metric, (value) => updateFilters({ metric: value })));
    chartCard.appendChild(chartHead);

    if (groups.length) {
      const minimumValue = Math.min(...groups.map((row) => Number(row.p10) || 0));
      const maximumValue = Math.max(...groups.map((row) => Number(row.best) || 0));
      const padding = Math.max(1, (maximumValue - minimumValue) * 0.08);
      const axisMin = Math.max(0, minimumValue - padding);
      const axisMax = Math.max(axisMin + 1, maximumValue + padding);
      const chartScroller = element("div", "performance-chart-scroll");
      const chartInner = element("div", "performance-chart-inner");
      const axis = element("div", "performance-axis");
      axis.appendChild(element("div", "", "职业 / 有效样本"));
      const scale = element("div", "performance-axis-scale");
      for (let index = 0; index < 6; index += 1) {
        scale.appendChild(element(
          "span",
          "",
          performanceValue(axisMin + (axisMax - axisMin) * index / 5, filters.metric),
        ));
      }
      axis.appendChild(scale);
      axis.appendChild(element("div", "performance-axis-end", "中位"));
      chartInner.appendChild(axis);

      const rows = element("div", "performance-rows");
      let selectedGroup = groups.find(
        (row) => Number(row.profession_id) === Number(state.insightsSelectedProfession),
      ) || groups[0];
      state.insightsSelectedProfession = Number(selectedGroup.profession_id);
      const rowNodes = [];
      groups.forEach((group) => {
        const row = button(
          `performance-row${group === selectedGroup ? " is-selected" : ""}`,
          "",
          `查看${professionName(group.profession_id)}分位详情`,
        );
        const identity = element("div", "performance-profession");
        const icon = professionIcon(group.profession_id);
        icon.className = "performance-profession-icon";
        identity.appendChild(icon);
        const identityCopy = element("div");
        identityCopy.appendChild(element("b", "", professionName(group.profession_id)));
        identityCopy.appendChild(element("small", "", `${formatInteger(group.sample_count)} 场有效样本`));
        identity.appendChild(identityCopy);
        row.appendChild(identity);

        const plot = element("div", "performance-plot");
        const p10 = performancePosition(group.p10, axisMin, axisMax);
        const p25 = performancePosition(group.p25, axisMin, axisMax);
        const p50 = performancePosition(group.p50, axisMin, axisMax);
        const p75 = performancePosition(group.p75, axisMin, axisMax);
        const p90 = performancePosition(group.p90, axisMin, axisMax);
        const best = performancePosition(group.best, axisMin, axisMax);
        const whisker = element("span", "performance-whisker");
        whisker.style.left = `${p10}%`;
        whisker.style.width = `${Math.max(0.25, p90 - p10)}%`;
        const box = element("span", "performance-box");
        box.style.left = `${p25}%`;
        box.style.width = `${Math.max(0.35, p75 - p25)}%`;
        const median = element("span", "performance-median");
        median.style.left = `${p50}%`;
        const bestMarker = element("span", "performance-best");
        bestMarker.style.left = `${best}%`;
        plot.append(whisker, box, median, bestMarker);
        if (group.mine) {
          const mine = element(
            "span",
            `performance-mine${group.mine.demo ? " is-demo" : ""}`,
            `${group.mine.demo ? "示例位置" : "我的位置"} P${Math.round(Number(group.mine.percentile) || 0)}`,
          );
          mine.style.left = `${performancePosition(group.mine.value, axisMin, axisMax)}%`;
          plot.appendChild(mine);
        }
        plot.title = [
          `P10 ${performanceValue(group.p10, filters.metric)}`,
          `P25 ${performanceValue(group.p25, filters.metric)}`,
          `P50 ${performanceValue(group.p50, filters.metric)}`,
          `P75 ${performanceValue(group.p75, filters.metric)}`,
          `P90 ${performanceValue(group.p90, filters.metric)}`,
          `最佳 ${performanceValue(group.best, filters.metric)}`,
        ].join(" · ");
        row.appendChild(plot);

        const medianValue = element("div", "performance-median-value");
        medianValue.appendChild(element("strong", "", performanceValue(group.p50, filters.metric)));
        medianValue.appendChild(element("small", "", "P50"));
        row.appendChild(medianValue);
        rows.appendChild(row);
        rowNodes.push([row, group]);
      });
      chartInner.appendChild(rows);

      const legend = element("div", "performance-legend");
      [
        ["performance-legend-box", "P25–P75 主要区间"],
        ["performance-legend-line", "P50 中位"],
        ["performance-legend-best", "最佳有效记录"],
        ["performance-legend-whisker", "须线：P10–P90"],
      ].forEach(([className, labelText]) => {
        const item = element("span");
        item.appendChild(element("i", className));
        item.append(labelText);
        legend.appendChild(item);
      });
      chartInner.appendChild(legend);
      chartScroller.appendChild(chartInner);
      chartCard.appendChild(chartScroller);
      primary.appendChild(chartCard);

      const detail = element("section", "performance-detail");
      fillPerformanceDetail(detail, selectedGroup, performance);
      rowNodes.forEach(([row, group]) => {
        row.addEventListener("click", () => {
          rowNodes.forEach(([node]) => node.classList.remove("is-selected"));
          row.classList.add("is-selected");
          state.insightsSelectedProfession = Number(group.profession_id);
          fillPerformanceDetail(detail, group, performance);
        });
      });
      primary.appendChild(detail);
    } else {
      const empty = element("div", "performance-empty");
      empty.appendChild(element("strong", "", "当前条件下暂无有效样本"));
      empty.appendChild(element("span", "", "可调整评分范围、难度或游戏版本后重试。"));
      chartCard.appendChild(empty);
      primary.appendChild(chartCard);
    }
    layout.appendChild(primary);

    const filter = element("aside", "performance-card performance-filter");
    const filterHead = element("div", "performance-filter-head");
    filterHead.appendChild(element("strong", "", "筛选条件"));
    filterHead.appendChild(element("span", "", "FILTERS"));
    filter.appendChild(filterHead);
    const bossOptions = (state.catalog?.bosses || []).map((boss) => ({
      value: `name:${boss.name}`, label: `${boss.name} · ${boss.records} 场历史`,
    }));
    if (!bossOptions.some((boss) => boss.value === filters.boss)) {
      bossOptions.unshift({value: filters.boss, label: selection.boss_name || "钻头"});
    }
    filter.appendChild(performanceFilterField("Boss", performanceSelect(
      bossOptions, filters.boss, (value) => updateFilters({
        boss: value, difficulty: "all", gameVersion: "all", minRating: 0, maxRating: 200000,
      }), "Boss",
    )));
    filter.appendChild(performanceFilterField("难度", performanceSegments([
      { value: "all", label: "全部" },
      { value: "normal", label: "普通" },
      { value: "hard", label: "困难" },
      { value: "nightmare", label: "噩梦" },
    ], filters.difficulty, (value) => updateFilters({ difficulty: value }))));
    filter.appendChild(performanceFilterField("统计类型", performanceSegments([
      { value: "dps", label: "DPS" },
      { value: "boss_damage", label: "首领伤害" },
    ], filters.metric, (value) => updateFilters({ metric: value }))));
    filter.appendChild(performanceFilterField("评分依据", performanceSegments([
      { value: "extraordinary", label: "非凡评分" },
      {
        value: "equipment",
        label: "装备评分",
        disabled: !availability.equipment_rating,
        title: availability.equipment_rating ? "" : "现有上传记录尚未包含装备评分",
      },
    ], filters.ratingBasis, (value) => updateFilters({ ratingBasis: value }))));

    const range = element("div", "performance-range-control");
    const rangeLabels = element("div", "performance-range-labels");
    const minimumLabel = element("span", "", formatInteger(filters.minRating));
    const maximumLabel = element("span", "", formatInteger(filters.maxRating));
    rangeLabels.append(minimumLabel, maximumLabel);
    range.appendChild(rangeLabels);
    const slider = element("div", "performance-range-slider");
    const rangeMaximum = Math.max(
      120000,
      Math.ceil(Number(availability.rating_max || filters.maxRating || 0) / 10000) * 10000,
    );
    const lower = document.createElement("input");
    const upper = document.createElement("input");
    [lower, upper].forEach((input) => {
      input.type = "range";
      input.min = "0";
      input.max = String(rangeMaximum);
      input.step = "1000";
      input.setAttribute("aria-label", input === lower ? "最低评分" : "最高评分");
    });
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
    lower.addEventListener("change", () => updateFilters({
      minRating: Number(lower.value),
      maxRating: Number(upper.value),
    }));
    upper.addEventListener("change", () => updateFilters({
      minRating: Number(lower.value),
      maxRating: Number(upper.value),
    }));
    slider.append(lower, upper);
    range.appendChild(slider);
    updateRangeVisual();
    filter.appendChild(performanceFilterField("评分范围", range));

    const rules = element("div", "performance-rules");
    ["仅完整战斗", "仅通关记录", "排除异常样本"].forEach((labelText) => {
      const item = element("span");
      item.appendChild(element("i", "", "✓"));
      item.append(labelText);
      rules.appendChild(item);
    });
    filter.appendChild(performanceFilterField("数据条件", rules));
    const versionOptions = [{ value: "all", label: "全部版本" }];
    (availability.game_versions || []).forEach((value) => {
      versionOptions.push({
        value,
        label: value === "unknown" ? "未标记版本" : value === "preview" ? "预览版本" : value,
      });
    });
    filter.appendChild(performanceFilterField("游戏版本", performanceSelect(
      versionOptions,
      filters.gameVersion,
      (value) => updateFilters({ gameVersion: value }),
      "游戏版本",
    )));
    filter.appendChild(performanceFilterField("排序方式", performanceSegments([
      { value: "p10", label: "P10" },
      { value: "p25", label: "P25" },
      { value: "p50", label: "中位" },
      { value: "p75", label: "P75" },
      { value: "p90", label: "P90" },
      { value: "best", label: "最佳" },
      { value: "sample_count", label: "样本" },
    ], filters.sort, (value) => updateFilters({ sort: value }))));
    const note = element("div", "performance-filter-note");
    note.appendChild(element("strong", "", "统计说明"));
    note.appendChild(element(
      "p",
      "",
      "P10 / P25 / P50 / P75 / P90 来自相同条件下的真实有效样本；重复、不完整和异常战斗不进入正式统计。",
    ));
    filter.appendChild(note);
    layout.appendChild(filter);
    fragment.appendChild(layout);
    contentNode.replaceChildren(fragment);
  }

  async function renderInsights(token) {
    setPage(
      "DATA INSIGHTS / 副本表现",
      "副本表现",
      "从真实上传记录中观察同一 Boss、难度与评分区间下的职业表现分布。",
    );
    showStatus("正在计算职业表现分布");
    const requestToken = ++state.insightsRequestToken;
    await loadBaseData();
    if (token !== state.renderToken) return;
    if (!state.insightsInitialized && !parseRoute().params.get("boss")) {
      const available = (state.catalog?.bosses || []).find((boss) => Number(boss.included) > 0);
      if (available) state.insightsFilters.boss = `name:${available.name}`;
    }
    state.insightsInitialized = true;
    const performance = await fetchPerformance(state.insightsFilters);
    if (token !== state.renderToken || requestToken !== state.insightsRequestToken) return;
    renderPerformancePage(performance, token);
  }

  async function renderRecords(token) {
    setPage("DATA RANKINGS", "数据排行", "名次按首领、关卡与职业分别计算；仅完整、通过验证的战斗进入排行。其他记录可在历史战斗中浏览。");
    showStatus("正在读取巅峰记录");
    await loadBaseData();
    if (token !== state.renderToken) return;

    const params = parseRoute().params;
    const rows = [...(state.leaderboards || [])].filter((row) =>
      (!params.get("boss") || row.boss_name === params.get("boss"))
      && (!params.get("profession") || String(row.profession_id) === params.get("profession"))
    ).sort((a, b) => {
      const dps = Number(b.dps || 0) - Number(a.dps || 0);
      return dps || Number(b.ended_at || 0) - Number(a.ended_at || 0);
    });
    const fragment = document.createDocumentFragment();
    fragment.appendChild(historyFilters(params, true));
    fragment.appendChild(sectionHeading("伤害 DPS 排行", rows.length ? `共 ${rows.length} 条` : "暂无记录"));
    if (rows.length) {
      fragment.appendChild(recordList(rows));
    } else {
      const empty = element("div", "portal-status");
      empty.appendChild(element("span", "", "当前还没有符合排行条件的战斗"));
      fragment.appendChild(empty);
    }
    contentNode.replaceChildren(fragment);
  }

  async function renderSearch(token, query) {
    await renderHistory(token, String(query || "").trim().slice(0, 48));
  }

  function historyFilters(params, ranking = false) {
    const form = element("form", "history-filters");
    const field = (labelText, name, options) => {
      const label = element("label", "history-field");
      label.appendChild(element("span", "", labelText));
      const control = element(options ? "select" : "input");
      control.name = name;
      if (options) options.forEach(([value, text]) => {
        const option = element("option", "", text);
        option.value = value;
        control.appendChild(option);
      });
      else control.type = name === "from" || name === "to" ? "date" : "search";
      control.value = params.get(name) || "";
      if (name === "q") { control.placeholder = "角色名 / 公开昵称"; control.maxLength = 48; }
      label.appendChild(control);
      form.appendChild(label);
      if (options || control.type === "date") control.addEventListener("change", () => form.requestSubmit());
    };
    if (!ranking) field("角色查询", "q");
    field("Boss", "boss", [["", "全部 Boss"], ...(state.catalog?.bosses || []).map((boss) => [boss.name, `${boss.name} (${boss.records})`])]);
    field("职业", "profession", [["", "全部职业"], ...Object.entries(PROFESSIONS)]);
    if (!ranking) {
      field("数据条件", "eligibility", [["", "全部历史"], ["included", "有效统计"], ["not_eligible", "未纳入统计"]]);
      field("难度", "difficulty", [["", "全部难度"], ...(state.catalog?.difficulties || []).map((value) => [value, performanceDifficultyLabel(value)])]);
      field("开始日期", "from");
      field("结束日期", "to");
    }
    const submit = button("portal-refresh", "查询");
    submit.type = "submit";
    const clear = element("a", "history-reset", "清空筛选");
    clear.href = ranking ? "#records" : "#history";
    form.append(submit, clear);
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      const values = new URLSearchParams();
      new FormData(form).forEach((value, name) => { if (String(value).trim()) values.set(name, String(value).trim()); });
      const next = `${ranking ? "#records" : "#history"}${values.size ? `?${values}` : ""}`;
      if (next === window.location.hash) renderRoute(true);
      else window.location.hash = next;
    });
    return form;
  }

  function qualificationText(reasons) {
    const labels = {
      ENCOUNTER_NOT_COMPLETED: "未确认通关", PARTICIPANT_IDENTITY_INCOMPLETE: "角色身份不完整",
      TEAM_TOTAL_MISMATCH: "团队总量不一致", CAPTURE_INCOMPLETE: "采集不完整",
      BOSS_UNSUPPORTED: "首领未纳入统计", DIFFICULTY_INVALID: "关卡信息不完整",
      CLIENT_VERSION_UNSUPPORTED: "客户端版本未支持", GAME_VERSION_UNSUPPORTED: "游戏版本未支持",
      DURATION_INVALID: "战斗时长异常", KEY_DATA_MISSING: "关键数据缺失",
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
      boss.querySelector(".boss-stage").textContent = `${performanceDifficultyLabel(row.difficulty || "unknown")} · 黄铜书${brassTomeLabel(row.brass_tome_status)} · ${result}`;
      boss.classList.add(className.trim() || "is-complete");
      record.append(boss, identityCell(row));
      const totals = element("div", "history-number");
      totals.append(element("b", "", formatInteger(row.team_dps)), element("small", "", `${formatCompact(row.team_total_damage)} 总伤害`));
      const time = element("div", "history-number");
      time.append(element("b", "", formatDuration(row.duration_seconds)), element("small", "", `${row.team_size} 人`), element("small", "death-count", deathSummaryText(row)));
      const info = element("div", "history-info");
      info.appendChild(element("span", "", formatDate(row.ended_at)));
      const status = element("small", row.statistics_status === "included" ? "history-included" : "history-partial",
        row.statistics_status === "included" ? "有效统计" : qualificationText(row.qualification_reasons) || "未纳入统计");
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
      window.location.hash = `history?${params}`;
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
      ["查询与分享", "自动上传完成后进入“历史战斗”，或搜索角色名称。打开详情可查看已捕获模块并复制战报链接；缺少的模块会明确标注覆盖状态。"],
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
      detail += ` · 非凡评分 ${formatInteger(rating)}`;
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

  const SKILL_COLORS = ["#61b6ff", "#bd95ff", "#48d6b0", "#ffbf6b", "#fa87b1", "#6dd3e7", "#8cca73", "#f39069"];

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

  function svgNode(parent, name, attrs = {}, text) {
    const node = document.createElementNS("http://www.w3.org/2000/svg", name);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, String(value)));
    if (text !== undefined) node.textContent = text;
    parent.appendChild(node);
    return node;
  }

  function renderEventTable(events, className = "skill-events", metadata = {}) {
    const details = element("details", className);
    const total = Number(metadata.total);
    const countText = events.length
      ? `${formatInteger(events.length)} 条${Number.isFinite(total) && total > events.length ? ` / 共 ${formatInteger(total)} 条` : ""}`
      : "本场未上传";
    details.appendChild(element("summary", "", `逐次打击记录 · ${countText}`));
    if (!events.length) {
      details.appendChild(element("p", "detail-note", "本场未采集逐次事件，仍可查看已收录的技能汇总。"));
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

  function renderCriticalLuck(stats) {
    const section = element("section", "luck-panel");
    section.appendChild(sectionHeading("暴击运气", "暴击落点估计"));
    const model = stats.critical_luck;
    if (!model) {
      section.appendChild(element("p", "detail-note", "需要至少 8 次带明确暴击标记的伤害事件。本场样本不足，暂不计算运气。"));
      return section;
    }
    const verdict = element("div", "luck-verdict");
    verdict.append(element("strong", "", model.verdict), element("span", "", `百分位 ${formatPercent(model.percentile)}`));
    section.appendChild(verdict);
    const svg = svgNode(section, "svg", {viewBox: "0 0 800 190", role: "img", "aria-label": `暴击运气分布：${model.verdict}，百分位 ${formatPercent(model.percentile)}`});
    const x = (z) => 50 + (z + 3.5) / 7 * 700;
    const y = (z) => 140 - Math.exp(-z * z / 2) * 105;
    const coords = Array.from({length: 101}, (_, i) => { const z = -3.5 + i * .07; return `${x(z)},${y(z)}`; });
    svgNode(svg, "polygon", {points: `50,140 ${coords.join(" ")} 750,140`, class: "luck-area"});
    svgNode(svg, "polyline", {points: coords.join(" "), class: "luck-line"});
    [-3, -2, -1, 0, 1, 2, 3].forEach((z) => svgNode(svg, "text", {x: x(z), y: 164, "text-anchor": "middle"}, `${z > 0 ? "+" : ""}${z}σ`));
    svgNode(svg, "line", {x1: x(0), x2: x(0), y1: 25, y2: 140, class: "luck-expected"});
    const observedZ = Math.max(-3.5, Math.min(3.5, Number(model.z_score)));
    svgNode(svg, "line", {x1: x(observedZ), x2: x(observedZ), y1: 25, y2: 140, class: "luck-observed"});
    svgNode(svg, "circle", {cx: x(observedZ), cy: y(observedZ), r: 5, class: "luck-dot"});
    const legend = element("div", "chart-legend");
    legend.append(element("span", "expected-legend", "期望位置"), element("span", "observed-legend", "本场实测"));
    section.appendChild(legend);
    const metrics = element("div", "luck-metrics");
    [["实测伤害", model.observed], ["期望伤害", model.expected], ["运气增减伤害", model.extra]].forEach(([label, value]) => {
      metrics.appendChild(metricCard(label, `${label === "运气增减伤害" && value > 0 ? "+" : ""}${formatInteger(value)}`));
    });
    section.append(metrics, element("p", "detail-note", `${formatInteger(model.known_hits)} 次已知打击 · ${formatInteger(model.critical_hits)} 次暴击 · 伤害覆盖 ${formatPercent(model.coverage)}。固定每个技能的实测暴击次数，估计暴击落在不同伤害打击时的分布；曲线使用正态近似，并按本场总伤害折算。该结果不代表面板暴击率或未来战斗表现。`));
    return section;
  }

  function renderTargetDistribution(targets, totalDamage) {
    const section = element("section", "detail-module target-panel");
    const rows = (Array.isArray(targets) ? targets : [])
      .filter((target) => Number(target.damage) > 0 || target.name)
      .sort((a, b) => Number(b.damage || 0) - Number(a.damage || 0));
    section.appendChild(sectionHeading("目标分布", rows.length ? `${rows.length} 个已记录目标` : "未采集"));
    if (!rows.length) {
      section.appendChild(element("p", "detail-note", "本场没有该成员的可靠目标分布；不会按总伤害推测目标。"));
      return section;
    }
    const total = Number(totalDamage) || rows.reduce((sum, target) => sum + Number(target.damage || 0), 0);
    const list = element("div", "target-list");
    rows.forEach((target) => {
      const damage = Number(target.damage || 0);
      const share = Number.isFinite(Number(target.share)) ? Number(target.share) : damage / Math.max(1, total);
      const row = element("div", "target-row");
      const identity = element("div", "target-identity");
      identity.append(
        element("strong", "", target.name || `目标 ${target.entity_id || target.template_id || "--"}`),
        element("span", "", target.kind || (target.template_id ? `模板 ${target.template_id}` : "已记录目标")),
      );
      const track = element("div", "target-track");
      const fill = element("span");
      fill.style.setProperty("--target-share", `${Math.max(0, Math.min(100, share * 100))}%`);
      track.appendChild(fill);
      row.append(identity, track, element("b", "", formatInteger(damage)), element("span", "target-share", formatPercent(share)));
      list.appendChild(row);
    });
    section.appendChild(list);
    return section;
  }

  function renderOpeningSequence(stats) {
    const section = element("section", "detail-module opening-panel");
    const rows = (Array.isArray(stats.opening_sequence) ? [...stats.opening_sequence] : [])
      .sort((a, b) => Number(a.time_ms) - Number(b.time_ms) || Number(a.sequence) - Number(b.sequence));
    const source = stats.opening_sequence_source || rows[0]?.source;
    const sourceLabel = source === "successful_cast"
      ? "真实成功施法"
      : source === "damage_hit"
        ? "真实伤害命中顺序"
        : "来源未标记";
    section.appendChild(sectionHeading("起手序列", rows.length ? `${rows.length} 条 · ${sourceLabel}` : "未采集"));
    if (!rows.length) {
      section.appendChild(element("p", "detail-note", "本场没有该成员的施法或命中时间记录。"));
      return section;
    }
    const list = element("ol", "opening-list");
    rows.forEach((entry) => {
      const item = element("li", "opening-item");
      const skill = {skill_id: entry.skill_id};
      item.appendChild(skillIcon(skill));
      const copy = element("div");
      copy.append(
        element("strong", "", skillDisplayName(skill)),
        element("span", "", `${(Number(entry.time_ms) / 1000).toFixed(2)} 秒${entry.target_name ? ` · ${entry.target_name}` : ""}${Number(entry.damage) > 0 ? ` · ${formatInteger(entry.damage)} 伤害` : ""}`),
      );
      item.appendChild(copy);
      list.appendChild(item);
    });
    section.appendChild(list);
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
      metricCard("非凡评分", formatInteger(snapshot.extraordinary_rating)));
    section.appendChild(metrics);
    if (snapshot.captured_at) section.appendChild(element("p", "detail-note", `快照采集时间：${snapshot.captured_at}`));
    const grid = element("div", "equipment-grid");
    const properties = (rows, container) => {
      (Array.isArray(rows) ? rows : []).forEach((prop) => {
        const label = prop.name || state.iconCatalog?.attributes?.[prop.key] || prop.key || "未知属性";
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
      body.appendChild(element("p", "detail-note", `装备编号 ${item.item_id} · 物品评分 ${formatInteger(item.item_score)} · 强化评分 ${formatInteger(item.enhance_score)}${item.score_complete === false ? " · 评分不完整" : ""}`));
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
        row.appendChild(element("strong", "", `${affix.name || "未知词条"}${Number(affix.score) > 0 ? ` · ${formatInteger(affix.score)} 分` : ""}`));
        properties(affix.properties, row);
        body.appendChild(row);
      });
      if (item.special_affix) {
        const special = element("div", "equipment-special");
        special.appendChild(element("strong", "", item.special_affix.name || "特殊词条"));
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
      attributes.forEach(([key, value]) => { const row = element("div"); row.append(element("span", "", state.iconCatalog?.attributes?.[key] || key), element("b", "", formatInteger(value))); list.appendChild(row); });
      section.appendChild(list);
    }
    for (const [key, label] of [["gems", "宝石"], ["sets", "套装"]]) {
      const rows = snapshot[key];
      if (Array.isArray(rows) && rows.length) {
        section.appendChild(sectionHeading(label));
        rows.forEach((row) => { const item = element("div", "equipment-affix"); item.appendChild(element("strong", "", row.name || `${label} ${row.item_id}`)); properties(row.properties, item); section.appendChild(item); });
      } else section.appendChild(element("p", "detail-note", `${label}：未记录`));
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
    const highest = Math.max(...bins.map((bin) => bin.normal + bin.critical + bin.unknown), 1);
    container.appendChild(sectionHeading("单次伤害分布", `${damages.length} 条伤害事件`));
    const svg = svgNode(container, "svg", {viewBox: "0 0 700 190", role: "img", "aria-label": "单次伤害分布：蓝色普通，紫色暴击，灰色未知"});
    bins.forEach((bin, i) => {
      let y = 150;
      for (const key of ["normal", "critical", "unknown"]) {
        const height = bin[key] / highest * 125;
        y -= height;
        const bar = svgNode(svg, "rect", {x: 50 + i * 60, y, width: 45, height, class: `hit-bar-${key}`});
        svgNode(bar, "title", {}, `${formatInteger(min + i / 10 * (max - min))} 至 ${formatInteger(min + (i + 1) / 10 * (max - min))}：${bin[key]} 次`);
      }
    });
    svgNode(svg, "text", {x: 50, y: 180}, formatInteger(min));
    svgNode(svg, "text", {x: 635, y: 180, "text-anchor": "end"}, formatInteger(max));
    const legend = element("div", "chart-legend");
    legend.append(element("span", "normal-legend", "普通"), element("span", "crit-legend", "暴击"), element("span", "unknown-legend", "标记未知"));
    container.appendChild(legend);
  }

  function renderSkills(panel, participant, mode) {
    panel.replaceChildren();
    const stats = participant?.stats || {};
    const overview = element("div", "participant-overview");
    overview.appendChild(sectionHeading(participant ? `${participant.display_name} · 个人数据` : "个人数据"));
    if (participant && participant.public_mode !== "unrecorded") {
      const history = element("a", "history-reset", "查看该角色的历史 →");
      history.href = `#history?q=${encodeURIComponent(participant.display_name)}`;
      overview.appendChild(history);
    }
    const metrics = element("div", "participant-metrics");
    for (const [label, value] of [["总伤害", participant?.damage], ["有效治疗", stats.effective_healing], ["承伤", stats.taken], ["死亡次数", stats.deaths], ["死亡时长", stats.death_duration_seconds], ["非凡评分", stats.extraordinary_rating]]) {
      const card = metricCard(label, value === undefined || value === null ? "未记录" : label === "死亡时长" ? `${Number(value).toFixed(1)} 秒` : formatInteger(value));
      card.classList.add(label === "死亡次数" ? "death-metric" : label === "有效治疗" ? "healing-metric" : label === "承伤" ? "taken-metric" : "damage-metric");
      metrics.appendChild(card);
    }
    overview.appendChild(metrics);
    const timeline = Array.isArray(stats.skill_timeline) ? [...stats.skill_timeline].sort((a, b) => a.time_ms - b.time_ms) : [];
    overview.append(
      renderTargetDistribution(stats.targets, participant?.damage),
      renderCriticalLuck(stats),
      renderOpeningSequence(stats),
      renderEventTable(timeline, "skill-events", {
        total: stats.skill_timeline_total,
        truncated: stats.skill_timeline_truncated,
      }),
    );
    panel.appendChild(overview);
    panel.appendChild(sectionHeading(mode === "hps" ? "治疗技能构成" : "技能构成", mode === "hps"
      ? "按有效治疗排序 · 点击技能展开详情"
      : `暴击 ${formatPercent(stats.critical_rate)} · 穿刺 ${formatPercent(stats.penetration_rate)} · 点击技能展开详情`));
    const key = mode === "hps" ? "effective_healing" : "damage";
    const skills = (Array.isArray(stats.skills) ? stats.skills : []).map((skill) => ({...skill, metricValue: Number(skill[key] || 0)})).filter((skill) => skill.metricValue > 0).sort((a, b) => b.metricValue - a.metricValue);
    if (mode === "dt" || !skills.length) {
      panel.appendChild(element("p", "detail-note", mode === "dt" ? "当前记录没有可靠的逐技能承伤归属。" : "该成员未上传此项逐技能数据。"));
    } else {
      const total = mode === "hps" ? Number(stats.effective_healing) || skills.reduce((sum, skill) => sum + skill.metricValue, 0) : Number(participant.damage) || skills.reduce((sum, skill) => sum + skill.metricValue, 0);
      const composition = element("div", "skill-composition"), legend = element("div", "skill-legend");
      const head = element("div", "skill-row skill-head");
      ["技能 / 展开详情", mode === "hps" ? "有效治疗" : "伤害", "占比", mode === "hps" ? "技能次数" : "记录次数", mode === "hps" ? "过量治疗" : "最高一击"].forEach((label) => head.appendChild(element("div", "", label)));
      const list = element("div", "skill-list");
      skills.forEach((skill, index) => {
        const color = SKILL_COLORS[index % SKILL_COLORS.length];
        const detail = element("details", "skill-detail");
        detail.dataset.skillId = skill.skill_id;
        detail.style.setProperty("--skill-color", color);
        const summary = element("summary", "skill-row");
        const identity = element("div", "skill-identity"), copy = element("div");
        copy.append(element("span", "skill-name", skillDisplayName(skill, mode)), element("span", "skill-share-track"));
        copy.lastChild.style.setProperty("--share", `${Math.min(100, skill.metricValue / Math.max(1, total) * 100)}%`);
        identity.append(skillIcon(skill), copy);
        const shownCount = mode === "hps" ? skill.healing_skill_count ?? skill.healing_events : skill.hits;
        const lastMetric = mode === "hps" ? skill.overhealing : skill.max_hit;
        summary.append(identity, element("div", "number-cell", formatInteger(skill.metricValue)), element("div", "number-cell", formatPercent(skill.metricValue / Math.max(1, total))), element("div", "number-cell", shownCount !== null && shownCount !== undefined ? formatInteger(shownCount) : "未记录"), element("div", "number-cell", lastMetric !== null && lastMetric !== undefined ? formatInteger(lastMetric) : "--"));
        detail.appendChild(summary);
        const body = element("div", "skill-detail-body");
        const events = timeline.filter((event) => Number(event.skill_id) === Number(skill.skill_id));
        const sampled = events.reduce((sum, event) => sum + Number(event.damage || 0), 0);
        const knownCrit = events.filter((event) => typeof event.critical === "boolean" && Number(event.damage) > 0);
        const knownPierce = events.filter((event) => typeof event.penetrating === "boolean" && Number(event.damage) > 0);
        const critRate = skill.critical_rate ?? (knownCrit.length ? knownCrit.filter((event) => event.critical).length / knownCrit.length : null);
        const pierceRate = skill.penetration_rate ?? (knownPierce.length ? knownPierce.filter((event) => event.penetrating).length / knownPierce.length : null);
        const cards = element("div", "skill-detail-metrics");
        const castCount = skill.count_semantics === "server_skill_count" || skill.count_semantics === "casts";
        const damageEvents = events.filter((event) => Number(event.damage) > 0);
        const average = mode === "dps" && damageEvents.length ? sampled / damageEvents.length : null;
        const maxHit = mode === "dps" ? Number(skill.max_hit) || (damageEvents.length ? Math.max(...damageEvents.map((event) => Number(event.damage))) : null) : null;
        if (mode === "hps") {
          [["有效治疗", skill.effective_healing], ["总治疗", skill.total_healing], ["过量治疗", skill.overhealing], ["技能次数", skill.healing_skill_count ?? skill.healing_events]].forEach(([label, value]) => cards.appendChild(metricCard(label, value === null || value === undefined ? "未记录" : formatInteger(value))));
        } else {
          [["记录次数", Number(skill.hits) || null], ["平均每次打击", average], ["最高一击", maxHit]].forEach(([label, value]) => cards.appendChild(metricCard(label, formatInteger(value))));
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
        const segment = button("composition-segment", "", `${skillDisplayName(skill, mode)} · ${formatPercent(skill.metricValue / Math.max(1, total))}`);
        segment.style.background = color;
        segment.style.flexGrow = skill.metricValue;
        const open = () => { detail.open = true; detail.scrollIntoView({block: "nearest", behavior: "smooth"}); };
        segment.addEventListener("click", open);
        composition.appendChild(segment);
        const label = button("skill-legend-item", skillDisplayName(skill, mode));
        label.style.setProperty("--skill-color", color);
        label.addEventListener("click", open);
        legend.appendChild(label);
      });
      const missing = Math.max(0, total - skills.reduce((sum, skill) => sum + skill.metricValue, 0));
      if (missing) { const segment = element("div", "composition-unclassified"); segment.style.flexGrow = missing; segment.title = `未归类 ${formatInteger(missing)}`; composition.appendChild(segment); legend.appendChild(element("span", "detail-note", `未归类 ${formatPercent(missing / total)}`)); }
      panel.append(composition, legend, head, list);
    }
    panel.appendChild(renderEquipment(stats.equipment_snapshot));
  }


  function resultLabel(value, confirmed = true) {
    if (value === "defeated" && !confirmed) return ["击败 · 未确认结算", " is-interrupted"];
    if (value === "defeated") return ["胜利", ""];
    if (value === "failed") return ["失败", " is-failed"];
    if (value === "interrupted") return ["中断", " is-interrupted"];
    return ["未确认", " is-interrupted"];
  }

  function teamTimeline(encounter) {
    const source = encounter.data?.team_dps_timeline;
    const points = (Array.isArray(source) ? source : [])
      .filter((point) => Number.isFinite(Number(point.time)) && Number.isFinite(Number(point.team_dps ?? point.dps)))
      .sort((a, b) => Number(a.time) - Number(b.time));
    const section = element("section", "history-chart");
    const timed = points.some((point) => Number(point.time) > 0);
    section.appendChild(sectionHeading(timed ? "团队 DPS 时间曲线" : "团队 DPS 采样曲线", points.length ? `${formatInteger(points.length)} 个采样点` : "未上传"));
    if (!points.length) { section.appendChild(element("p", "detail-note", "本场未上传时间曲线，可继续查看成员和技能汇总。")); return section; }
    const width = 1000, height = 210, left = 70, bottom = 175, right = 975, top = 15;
    const maxTime = timed ? Math.max(1, ...points.map((point) => Number(point.time))) : Math.max(1, points.length - 1);
    const position = (point, index) => timed ? Number(point.time) : index;
    const maxDps = Math.max(1, ...points.map((point) => Number(point.team_dps ?? point.dps)));
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
    svg.setAttribute("role", "img");
    svg.setAttribute("aria-label", "团队 DPS 曲线，左右方向键查看采样值");
    svg.setAttribute("tabindex", "0");
    const node = (name, attrs, text) => {
      const child = document.createElementNS(svg.namespaceURI, name);
      Object.entries(attrs).forEach(([key, value]) => child.setAttribute(key, value));
      if (text !== undefined) child.textContent = text;
      svg.appendChild(child);
      return child;
    };
    [0, .5, 1].forEach((fraction) => {
      const y = bottom - fraction * (bottom - top);
      node("line", {x1: left, x2: right, y1: y, y2: y, class: "chart-grid"});
      node("text", {x: left - 10, y: y + 4, "text-anchor": "end"}, formatCompact(maxDps * fraction));
      node("text", {x: left + fraction * (right - left), y: 200, "text-anchor": "middle"}, timed ? formatDuration(maxTime * fraction) : `第 ${Math.round(maxTime * fraction) + 1} 个`);
    });
    const definitions = svgNode(svg, "defs");
    const gradient = svgNode(definitions, "linearGradient", {id: "team-dps-gradient", x1: 0, y1: 0, x2: 0, y2: 1});
    svgNode(gradient, "stop", {offset: "0%", "stop-color": "#61b6ff", "stop-opacity": ".32"});
    svgNode(gradient, "stop", {offset: "100%", "stop-color": "#61b6ff", "stop-opacity": ".02"});
    const curve = points.map((point, index) => `${left + position(point, index) / maxTime * (right - left)},${bottom - Number(point.team_dps ?? point.dps) / maxDps * (bottom - top)}`).join(" ");
    node("polygon", {points: `${left},${bottom} ${curve} ${right},${bottom}`, fill: "url(#team-dps-gradient)"});
    node("polyline", {points: curve, class: "chart-line"});
    const crosshair = node("line", {y1: top, y2: bottom, class: "chart-crosshair"});
    const marker = node("circle", {r: 4, class: "chart-marker"});
    const readout = element("p", "chart-readout");
    let selected = points.length - 1;
    const show = () => {
      const point = points[selected];
      const dps = Number(point.team_dps ?? point.dps);
      marker.setAttribute("cx", left + position(point, selected) / maxTime * (right - left));
      marker.setAttribute("cy", bottom - dps / maxDps * (bottom - top));
      crosshair.setAttribute("x1", marker.getAttribute("cx"));
      crosshair.setAttribute("x2", marker.getAttribute("cx"));
      readout.textContent = `${timed ? `${Number(point.time).toFixed(1)} 秒` : `第 ${selected + 1} 个采样`} · 团队 DPS ${formatInteger(dps)}`;
    };
    svg.addEventListener("pointermove", (event) => {
      const bounds = svg.getBoundingClientRect();
      const time = ((event.clientX - bounds.left) / bounds.width * width - left) / (right - left) * maxTime;
      selected = points.reduce((best, point, index) => Math.abs(position(point, index) - time) < Math.abs(position(points[best], best) - time) ? index : best, 0);
      show();
    });
    svg.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      selected = Math.max(0, Math.min(points.length - 1, selected + (event.key === "ArrowRight" ? 1 : -1)));
      show();
    });
    show();
    const legend = element("div", "chart-legend");
    legend.append(element("span", "normal-legend", "团队伤害 DPS"), element("span", "", "悬停或左右方向键查看采样"));
    section.append(legend, svg, readout);
    if (points.some((point) => point.source === "live_display_team_dps")) section.appendChild(element("p", "detail-note", "曲线记录采集时的团队显示值；结算补全成员后，可能与最终团队 DPS 不同。"));
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
      .map((row) => ({time: Number(row.time_seconds), loss: Number(row.observed_boss_hp_loss)}))
      .filter((row) => Number.isFinite(row.time) && Number.isFinite(row.loss))
      .sort((a, b) => a.time - b.time);
    const monster = encounter.data?.monster || {};
    const maxHp = Number(monster.max_hp || monster.observed_max_hp || 0);
    section.appendChild(sectionHeading("Boss 血量曲线", samples.length ? `${samples.length} 个真实采样点` : "未采集"));
    if (!samples.length) {
      section.appendChild(element("p", "detail-note", "本场未上传 Boss 血量采样。"));
      return section;
    }
    const points = samples.map((sample) => ({
      ...sample,
      value: maxHp > 0 ? Math.max(0, maxHp - sample.loss) : sample.loss,
    }));
    const width = 1000, height = 210, left = 82, bottom = 175, right = 975, top = 15;
    const maxTime = Math.max(1, ...points.map((point) => point.time));
    const maxValue = maxHp > 0 ? maxHp : Math.max(1, ...points.map((point) => point.value));
    const svg = svgNode(section, "svg", {viewBox: `0 0 ${width} ${height}`, role: "img", tabindex: "0", "aria-label": "Boss 血量曲线，左右方向键查看采样值"});
    [0, .5, 1].forEach((fraction) => {
      const y = bottom - fraction * (bottom - top);
      svgNode(svg, "line", {x1: left, x2: right, y1: y, y2: y, class: "chart-grid"});
      svgNode(svg, "text", {x: left - 10, y: y + 4, "text-anchor": "end"}, maxHp > 0 ? formatCompact(maxValue * fraction) : formatCompact(maxValue * fraction));
      svgNode(svg, "text", {x: left + fraction * (right - left), y: 200, "text-anchor": "middle"}, formatDuration(maxTime * fraction));
    });
    const definitions = svgNode(svg, "defs");
    const gradient = svgNode(definitions, "linearGradient", {id: "boss-hp-gradient", x1: 0, y1: 0, x2: 0, y2: 1});
    svgNode(gradient, "stop", {offset: "0%", "stop-color": "#fa87a0", "stop-opacity": ".28"});
    svgNode(gradient, "stop", {offset: "100%", "stop-color": "#fa87a0", "stop-opacity": ".02"});
    const coordinate = (point) => `${left + point.time / maxTime * (right - left)},${bottom - point.value / maxValue * (bottom - top)}`;
    const curve = points.map(coordinate).join(" ");
    svgNode(svg, "polygon", {points: `${left},${bottom} ${curve} ${right},${bottom}`, fill: "url(#boss-hp-gradient)"});
    svgNode(svg, "polyline", {points: curve, class: "boss-hp-line"});
    const crosshair = svgNode(svg, "line", {y1: top, y2: bottom, class: "chart-crosshair"});
    const marker = svgNode(svg, "circle", {r: 4, class: "boss-hp-marker"});
    const readout = element("p", "chart-readout boss-hp-readout");
    let selected = points.length - 1;
    const show = () => {
      const point = points[selected];
      const [x, y] = coordinate(point).split(",");
      marker.setAttribute("cx", x);
      marker.setAttribute("cy", y);
      crosshair.setAttribute("x1", x);
      crosshair.setAttribute("x2", x);
      readout.textContent = maxHp > 0
        ? `${point.time.toFixed(1)} 秒 · Boss 血量 ${formatInteger(point.value)} / ${formatInteger(maxHp)} · 已损失 ${formatInteger(point.loss)}`
        : `${point.time.toFixed(1)} 秒 · 已观测血量损失 ${formatInteger(point.loss)}`;
    };
    svg.addEventListener("pointermove", (event) => {
      const bounds = svg.getBoundingClientRect();
      const time = ((event.clientX - bounds.left) / bounds.width * width - left) / (right - left) * maxTime;
      selected = points.reduce((best, point, index) => Math.abs(point.time - time) < Math.abs(points[best].time - time) ? index : best, 0);
      show();
    });
    svg.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      event.preventDefault();
      selected = Math.max(0, Math.min(points.length - 1, selected + (event.key === "ArrowRight" ? 1 : -1)));
      show();
    });
    show();
    const legend = element("div", "chart-legend");
    legend.append(element("span", "boss-hp-legend", maxHp > 0 ? `Boss 最大血量 ${formatInteger(maxHp)}` : "已观测血量损失"), element("span", "", "悬停或左右方向键查看采样"));
    section.append(legend, readout);
    return section;
  }

  function renderModuleCoverage(encounter) {
    const coverage = encounter.data?.module_coverage;
    if (!coverage || typeof coverage !== "object") return null;
    const total = Number(coverage.total_members) || encounter.participants?.length || 0;
    const details = element("details", "coverage-panel");
    details.appendChild(element("summary", "", `数据模块覆盖 · ${coverage.details_complete ? "本场所列模块完整" : "按实际捕获展示"}`));
    const grid = element("div", "coverage-grid");
    [
      ["DPS", "dps_members"], ["HPS", "hps_members"], ["承伤", "dt_members"],
      ["死亡", "death_members"], ["技能汇总", "skill_members"], ["目标分布", "target_members"],
      ["装备快照", "equipment_members"], ["命中时间轴", "skill_timeline_members"], ["起手序列", "opening_sequence_members"],
    ].forEach(([label, key]) => {
      const value = Number(coverage[key]) || 0;
      const item = element("div", `coverage-item${value === total && total ? " is-complete" : value ? " is-partial" : " is-missing"}`);
      item.append(element("span", "", label), element("b", "", `${value} / ${total}`));
      grid.appendChild(item);
    });
    [
      ["Boss 伤害", "boss_damage"], ["Boss 血量", "boss_hp_timeline"],
      ["Boss 最大血量", "boss_max_hp"], ["副本难度", "difficulty"],
      ["黄铜书状态", "brass_tome"],
    ].forEach(([label, key]) => {
      const available = Boolean(coverage[key]);
      const item = element("div", `coverage-item${available ? " is-complete" : " is-missing"}`);
      item.append(element("span", "", label), element("b", "", available ? "已记录" : "未记录"));
      grid.appendChild(item);
    });
    details.append(grid, element("p", "detail-note", `成功施法起手 ${formatInteger(coverage.successful_cast_opening_members || 0)} 人；伤害命中起手 ${formatInteger(coverage.damage_hit_opening_members || 0)} 人。缺少队友逐次事件时不会生成推测时间点。`));
    return details;
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
    const heroImage = bossIcon(encounter.stage_id, "encounter-boss-icon");
    if (!heroImage.hidden) hero.appendChild(heroImage);
    else hero.appendChild(element("div", "encounter-boss-icon"));
    const heroCopy = element("div");
    heroCopy.appendChild(element("h2", "encounter-name", encounter.boss_name || "未知首领"));
    const location = [encounter.data?.dungeon_name, encounter.data?.stage_name]
      .filter((value, index, values) => value && values.indexOf(value) === index)
      .join(" · ");
    heroCopy.appendChild(element(
      "div",
      "encounter-subtitle",
      `${location ? `${location} · ` : ""}${performanceDifficultyLabel(encounter.difficulty || "unknown")} · 关卡 ${Number(encounter.stage_id) || "--"} · ${formatDate(encounter.ended_at)} · ${Number(encounter.team_size) || 0} 人`,
    ));
    const context = element("div", "encounter-context");
    context.append(
      element("span", "encounter-context-item", `副本难度：${performanceDifficultyLabel(encounter.difficulty || "unknown")}`),
      element("span", `encounter-context-item brass-${encounter.brass_tome_status || "unknown"}`, `黄铜书：${brassTomeLabel(encounter.brass_tome_status)}`),
    );
    const challengeIds = Array.isArray(encounter.brass_tome_challenge_ids)
      ? encounter.brass_tome_challenge_ids.filter((value) => Number(value) > 0)
      : [];
    if (challengeIds.length) {
      context.appendChild(element("span", "encounter-context-item", `挑战编号：${challengeIds.join("、")}`));
    }
    heroCopy.appendChild(context);
    hero.appendChild(heroCopy);
    const [label, resultClass] = resultLabel(encounter.data?.result, encounter.data?.completion_confirmed);
    hero.appendChild(element("div", `result-badge${resultClass}`, label));
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
    const back = element("a", "history-reset", "返回历史战斗 →");
    back.href = state.detailReturnRoute;
    actions.append(copy, back);
    fragment.appendChild(actions);
    const included = encounter.statistics_status === "included";
    fragment.appendChild(element("p", `qualification-note${included ? " is-included" : ""}`,
      included ? "本场已纳入有效统计与排行。" : `本场作为历史记录收录，未纳入正式统计：${qualificationText(encounter.qualification_reasons) || "数据未满足统计条件"}。`));

    const metrics = element("div", "metric-grid");
    metrics.appendChild(metricCard("团队总伤害", formatCompact(encounter.team_total_damage), "本场累计"));
    metrics.appendChild(metricCard("团队 DPS", formatInteger(encounter.data?.team_dps), "有效战斗时间"));
    metrics.appendChild(metricCard("团队有效治疗", formatCompact(encounter.data?.team_effective_healing), "本场累计"));
    metrics.appendChild(metricCard("团队 HPS", formatInteger(encounter.data?.team_hps), "有效治疗时间"));
    metrics.appendChild(metricCard("战斗时长", formatDuration(encounter.duration_seconds), "分:秒"));
    metrics.appendChild(metricCard("团队承伤", formatCompact(encounter.data?.team_taken), "本场累计"));
    metrics.appendChild(metricCard("Boss 最大血量", formatCompact(encounter.data?.monster?.max_hp || encounter.data?.monster?.observed_max_hp), "本场采集"));
    const deaths = metricCard("团队死亡次数", encounter.team_deaths === null || encounter.team_deaths === undefined
      ? encounter.recorded_deaths === null || encounter.recorded_deaths === undefined ? "未记录" : `${formatInteger(encounter.recorded_deaths)} 次已记录`
      : `${formatInteger(encounter.team_deaths)} 次`, `已记录 ${encounter.death_recorded_members || 0} / ${encounter.death_total_members || 0} 名成员`);
    deaths.classList.add("death-metric");
    metrics.appendChild(deaths);
    fragment.appendChild(metrics);
    fragment.appendChild(teamTimeline(encounter));
    fragment.appendChild(bossHealthTimeline(encounter));
    const coverage = renderModuleCoverage(encounter);
    if (coverage) fragment.appendChild(coverage);

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
      fill.style.setProperty("--member-color", state.encounterMode === "hps" ? "#48d6b0" : state.encounterMode === "dt" ? "#ffbf6b" : SKILL_COLORS[(Number(participant.profession_id) || index) % SKILL_COLORS.length]);
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
    contentNode.replaceChildren(renderEncounterBody(encounter));
  }

  async function renderEncounter(token, encounterId) {
    const id = String(encounterId || "");
    if (!/^enc_[A-Za-z0-9_-]{8,64}$/.test(id)) {
      setPage("BATTLE DETAIL", "战斗详情");
      showStatus("战斗记录地址无效", "请从历史战斗或搜索结果中重新打开。", false);
      return;
    }
    setPage("BATTLE DETAIL", "战斗详情");
    showStatus("正在读取战斗详情");
    await Promise.all([
      loadBaseData(),
      loadSkillNames(),
      fetchJson(`${API_ROOT}/encounters/${encodeURIComponent(id)}`),
    ]).then(([, , payload]) => {
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
    const requestedInsightsBoss = route.name === "insights" ? route.params.get("boss") : "";
    if (requestedInsightsBoss && (requestedInsightsBoss.startsWith("name:") || ["drill", "viscountess"].includes(requestedInsightsBoss))) {
      state.insightsFilters.boss = requestedInsightsBoss;
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
    refreshButton.hidden = route.name === "share";
    try {
      if (force) await loadBaseData(true);
      if (route.name === "insights") await renderInsights(token);
      else if (route.name === "records") await renderRecords(token);
      else if (route.name === "history") await renderHistory(token);
      else if (route.name === "search") await renderSearch(token, route.params.get("q"));
      else if (route.name === "share") renderShare();
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
    window.location.hash = `search?q=${encodeURIComponent(query.slice(0, 48))}`;
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
  renderRoute(false);
})();
