(() => {
  "use strict";

  const scene = document.getElementById("scene");
  const searchForm = document.querySelector(".search");
  const searchInput = searchForm?.querySelector("input");
  const navLinks = Array.from(document.querySelectorAll(".nav a"));
  const shareLink = document.querySelector(".share-link");
  const API_ROOT = "/api/v1/dps/public";
  const PROFESSIONS = {
    1200001: "太阳",
    1200002: "观众",
    1200003: "愚者",
    1200004: "审判",
    1200005: "门",
    1200006: "黄昏",
    1200007: "隐者",
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
    ])
      .then(([statistics, leaderboards, bossCatalog, catalog]) => {
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
    return PROFESSIONS[Number(id)] || "未知途径";
  }

  function professionIcon(id) {
    const image = element("img", "profession-icon");
    image.src = `assets/professions/${Number(id) || 0}.png`;
    image.alt = professionName(id);
    image.loading = "lazy";
    image.addEventListener("error", () => image.remove(), { once: true });
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
    copy.appendChild(element("div", "identity-name", row.display_name || "匿名玩家"));
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
      unknown: "未标记难度",
    }[difficulty] || difficulty || "全部难度";
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
    setPage("BOSS RANKINGS", "Boss 排行", "名次按首领、关卡与职业分别计算；仅完整、通过验证的战斗进入排行。其他记录可在历史战斗中浏览。");
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
      "浏览已收录的战斗与原始表现。匿名记录不支持按名称搜索，未纳入排行的历史仍可查看详情。");
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
    ["Boss / 结果", "公开玩家", "团队 DPS / 总伤害", "时长 / 人数", "时间 / 数据状态", ""].forEach((label) => head.appendChild(element("div", "", label)));
    list.appendChild(head);
    for (const row of history.records || []) {
      const record = element("article", "history-row");
      const boss = bossCell(row);
      const [result, className] = resultLabel(row.result, row.completion_confirmed);
      boss.querySelector(".boss-stage").textContent = `${performanceDifficultyLabel(row.difficulty || "unknown")} · ${result}`;
      boss.classList.add(className.trim() || "is-complete");
      record.append(boss, identityCell(row));
      const totals = element("div", "history-number");
      totals.append(element("b", "", formatInteger(row.team_dps)), element("small", "", `${formatCompact(row.team_total_damage)} 总伤害`));
      const time = element("div", "history-number");
      time.append(element("b", "", formatDuration(row.duration_seconds)), element("small", "", `${row.team_size} 人`));
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
      empty.appendChild(element("span", "", "没有符合条件的战斗。可清空筛选，或检查角色是否以公开名称上传。"));
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
    setPage("UPLOAD GUIDE", "如何上传战斗记录", "通过叨叨诡秘客户端上传；网页不直接读取游戏或本机记录。");
    const flow = element("div", "share-flow");
    [
      ["完成一场 Boss 战斗", "登录客户端并保持采集运行。已启用自动上传时，胜利记录会自动同步；伤害木桩不进入上传。"],
      ["上传已有记录", "打开客户端“战斗记录”，点击该场记录右侧的“上传”。失败时可在同一位置重试；未上传的本地记录不会出现在网站。"],
      ["确认公开方式", "选择匿名、公开角色名称或上传昵称。匿名记录仍可浏览战报，其他成员保留各自的公开设置。"],
      ["查询与分享", "上传完成后进入“历史战斗”，或搜索公开名称。打开详情可复制战报链接；完整度不足的历史会展示原因，不进入正式排行。"],
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
    const link = element("a", "portal-refresh guide-download", "下载 Windows 客户端 ↗");
    link.href = "https://github.com/cblan23/gmzzdps/releases/tag/v0.3.5-r1";
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    contentNode.appendChild(link);
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
    copy.appendChild(element("div", "identity-name", participant.display_name || "匿名玩家"));
    let detail = participant.is_ai ? "人机" : professionName(participant.profession_id);
    const rating = Number(participant.stats?.extraordinary_rating);
    if (!participant.is_ai && Number.isFinite(rating) && rating > 0) {
      detail += ` · 非凡评分 ${formatInteger(rating)}`;
    }
    copy.appendChild(element("div", "identity-kind", detail));
    cell.appendChild(copy);
    return cell;
  }

  function skillDisplayName(skill) {
    const skillId = Number(skill?.skill_id);
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

  function renderSkills(panel, participant, mode) {
    panel.replaceChildren();
    const stats = participant?.stats || {};
    const overview = element("div", "participant-overview");
    overview.appendChild(sectionHeading(participant ? `${participant.display_name} · 个人数据` : "个人数据"));
    if (participant?.public_mode !== "anonymous") {
      const history = element("a", "history-reset", "查看该公开名称的历史 →");
      history.href = `#history?q=${encodeURIComponent(participant.display_name)}`;
      overview.appendChild(history);
    }
    const metrics = element("div", "participant-metrics");
    for (const [label, value] of [["总伤害", participant?.damage], ["有效治疗", stats.effective_healing],
      ["承伤", stats.taken], ["死亡次数", stats.deaths], ["死亡时长", stats.death_duration_seconds], ["非凡评分", stats.extraordinary_rating]]) {
      metrics.appendChild(metricCard(label, label === "死亡时长"
        ? value !== undefined ? `${Number(value).toFixed(1)} 秒` : "--" : formatInteger(value)));
    }
    overview.appendChild(metrics);
    const details = element("details", "skill-events");
    const timeline = Array.isArray(stats.skill_timeline) ? [...stats.skill_timeline].sort((a, b) => a.time_ms - b.time_ms) : [];
    details.appendChild(element("summary", "", `逐次打击记录 · ${timeline.length ? `${formatInteger(timeline.length)} 条` : "本场未上传"}`));
    if (timeline.length) {
      const body = element("div", "skill-event-body");
      let page = 0;
      const render = () => {
        body.replaceChildren();
        const table = element("table", "event-table");
        const header = element("tr");
        ["时间", "技能", "伤害", "类型"].forEach((text) => header.appendChild(element("th", "", text)));
        const thead = element("thead");
        thead.appendChild(header);
        table.appendChild(thead);
        const tbody = element("tbody");
        timeline.slice(page * 50, (page + 1) * 50).forEach((event) => {
          const row = element("tr");
          [`${(Number(event.time_ms) / 1000).toFixed(2)} 秒`, skillDisplayName(event), formatInteger(event.damage),
            [event.critical ? "暴击" : "", event.penetrating ? "穿刺" : ""].filter(Boolean).join(" · ") || "普通"].forEach((text) => row.appendChild(element("td", "", text)));
          tbody.appendChild(row);
        });
        table.appendChild(tbody);
        body.appendChild(table);
        const pager = element("div", "history-pagination");
        const previous = button("portal-refresh", "上一页");
        previous.disabled = page === 0;
        previous.addEventListener("click", () => { page--; render(); });
        const next = button("portal-refresh", "下一页");
        next.disabled = (page + 1) * 50 >= timeline.length;
        next.addEventListener("click", () => { page++; render(); });
        pager.append(previous, element("span", "", `${page + 1} / ${Math.ceil(timeline.length / 50)}`), next);
        body.appendChild(pager);
      };
      render();
      details.appendChild(body);
    } else details.appendChild(element("p", "", "没有逐次事件不代表没有造成伤害；下方技能汇总仍使用已收录的数据。"));
    overview.appendChild(details);
    panel.appendChild(overview);
    const skills = Array.isArray(stats.skills) ? stats.skills : [];
    panel.appendChild(sectionHeading(
      participant ? `${participant.display_name || "匿名玩家"} · 技能构成` : "技能构成",
      participant && mode === "dps"
        ? `暴击 ${formatPercent(stats.critical_rate)} · 穿刺 ${formatPercent(stats.penetration_rate)}`
        : "",
    ));

    if (mode === "dt") {
      const status = element("div", "portal-status");
      status.appendChild(element("span", "", "当前公开记录没有可靠的技能承伤归属"));
      panel.appendChild(status);
      return;
    }

    const valueKey = mode === "hps" ? "effective_healing" : "damage";
    const usable = skills
      .map((skill) => ({ ...skill, metricValue: Number(skill[valueKey] || 0) }))
      .filter((skill) => skill.metricValue > 0)
      .sort((a, b) => b.metricValue - a.metricValue);
    if (!usable.length) {
      const status = element("div", "portal-status");
      status.appendChild(element("span", "", "该成员没有可公开的逐技能数据"));
      panel.appendChild(status);
      return;
    }

    const total = usable.reduce((sum, skill) => sum + skill.metricValue, 0) || 1;
    const head = element("div", "skill-row skill-head");
    ["技能", mode === "hps" ? "有效治疗" : "伤害", "占比", "打击次数", "最高一击"].forEach((label) => {
      head.appendChild(element("div", "", label));
    });
    panel.appendChild(head);
    usable.forEach((skill) => {
      const row = element("div", "skill-row");
      const skillName = element("div", "skill-name", skillDisplayName(skill));
      const skillId = Number(skill.skill_id);
      if (Number.isSafeInteger(skillId) && skillId > 0) {
        skillName.title = `技能编号 ${skillId}`;
      }
      row.appendChild(skillName);
      row.appendChild(element("div", "number-cell", formatInteger(skill.metricValue)));
      row.appendChild(element("div", "number-cell", formatPercent(skill.metricValue / total)));
      row.appendChild(element("div", "number-cell", formatInteger(skill.hits)));
      row.appendChild(element("div", "number-cell", Number(skill.max_hit) > 0 ? formatInteger(skill.max_hit) : "--"));
      panel.appendChild(row);
    });
    if (Number(stats.unclassified_damage) > 0) {
      panel.appendChild(element("p", "detail-note", `另有 ${formatInteger(stats.unclassified_damage)} 伤害未归类到具体技能。`));
    }
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
    node("polyline", {points: points.map((point, index) => `${left + position(point, index) / maxTime * (right - left)},${bottom - Number(point.team_dps ?? point.dps) / maxDps * (bottom - top)}`).join(" "), class: "chart-line"});
    const marker = node("circle", {r: 4, class: "chart-marker"});
    const readout = element("p", "chart-readout");
    let selected = points.length - 1;
    const show = () => {
      const point = points[selected];
      const dps = Number(point.team_dps ?? point.dps);
      marker.setAttribute("cx", left + position(point, selected) / maxTime * (right - left));
      marker.setAttribute("cy", bottom - dps / maxDps * (bottom - top));
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
    section.append(svg, readout);
    if (!timed) section.appendChild(element("p", "detail-note", "旧上传记录缺少采样时间，曲线按采样顺序展示，无法确定每个点发生在第几秒。"));
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
    heroCopy.appendChild(element(
      "div",
      "encounter-subtitle",
      `${performanceDifficultyLabel(encounter.difficulty || "unknown")} · 关卡 ${Number(encounter.stage_id) || "--"} · ${formatDate(encounter.ended_at)} · ${Number(encounter.team_size) || 0} 人`,
    ));
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
    metrics.appendChild(metricCard("战斗时长", formatDuration(encounter.duration_seconds), "分:秒"));
    metrics.appendChild(metricCard("团队承伤", formatCompact(encounter.data?.team_taken), "本场累计"));
    fragment.appendChild(metrics);
    fragment.appendChild(teamTimeline(encounter));

    const tabs = element("div", "mode-tabs");
    [["dps", "伤害 DPS"], ["hps", "治疗 HPS"], ["dt", "承伤 DT"]].forEach(([mode, labelText]) => {
      const tab = button(`mode-tab${state.encounterMode === mode ? " is-active" : ""}`, labelText);
      tab.addEventListener("click", () => {
        state.encounterMode = mode;
        renderEncounterBodyIntoPage(encounter);
      });
      tabs.appendChild(tab);
    });
    fragment.appendChild(tabs);

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
    const maxMetric = Math.max(
      1,
      ...sorted.map((participant) => encounterMetric(state.encounterMode, participant, durationSeconds) || 0),
    );
    let selectedRow = null;

    sorted.forEach((participant, index) => {
      const row = button("participant-row", "");
      row.dataset.slot = participant.slot;
      row.setAttribute("aria-label", `查看 ${participant.display_name || "匿名玩家"} 的技能数据`);
      row.appendChild(element("div", `rank-cell${index < 3 ? " is-top" : ""}`, index + 1));
      row.appendChild(participantIdentity(participant));
      const track = element("div", "metric-track");
      const fill = element("div", "metric-fill");
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
    if (!selectedRow) selectedRow = list.querySelector(".participant-row");
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
  document.getElementById("manualUploadBtn").addEventListener("click", () => setPopover(false));
  fetchJson(`${API_ROOT}/statistics`).then(({statistics}) => {
    const count = formatInteger(statistics.history_encounters);
    document.getElementById("historySummary").textContent = `浏览 ${count} 场历史战斗 →`;
    document.getElementById("qualifiedSummary").textContent = `${formatInteger(statistics.encounters)} 场有效统计`;
    document.getElementById("uploadCount").textContent = `${count} 场`;
    document.getElementById("lastUpload").textContent = statistics.last_uploaded_at ? formatDate(statistics.last_uploaded_at) : "暂无上传";
  }).catch(() => {
    document.getElementById("qualifiedSummary").textContent = "统计暂时无法读取";
    document.getElementById("uploadCount").textContent = "暂时无法读取";
    document.getElementById("lastUpload").textContent = "暂时无法读取";
  });
  renderRoute(false);
})();
