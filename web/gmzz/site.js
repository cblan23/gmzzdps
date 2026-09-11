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
  const INSIGHTS_PREVIEW_DATA = {
    drill: {
      bossName: "钻头",
      dungeonName: "记忆的传承",
      encounterCount: 46,
      groups: [
        [1200003, 78, 15100, 18400, 22600, 26400, 30100, 35800],
        [1200006, 69, 14700, 17900, 21900, 25700, 29400, 34900],
        [1200005, 57, 13900, 17100, 21100, 24800, 28300, 33700],
        [1200001, 51, 13200, 16300, 20200, 23800, 27200, 32400],
        [1200007, 47, 12700, 15700, 19500, 23100, 26400, 31500],
        [1200004, 42, 12000, 15100, 18800, 22300, 25500, 30400],
        [1200002, 36, 2800, 4100, 5900, 7900, 9800, 12600],
      ],
    },
    viscountess: {
      bossName: "子爵夫人",
      dungeonName: "五月庄园·城堡",
      encounterCount: 32,
      groups: [
        [1200006, 62, 14200, 17800, 22200, 25800, 29100, 34400],
        [1200003, 58, 13600, 17100, 21400, 24900, 28200, 33100],
        [1200005, 44, 12800, 16200, 20500, 24100, 27400, 32200],
        [1200001, 39, 12100, 15600, 19800, 23200, 26500, 30900],
        [1200007, 37, 11800, 15100, 19100, 22500, 25700, 30100],
        [1200004, 31, 11000, 14500, 18400, 21800, 24900, 29200],
        [1200002, 28, 2400, 3600, 5200, 7100, 8900, 11800],
      ],
    },
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
    skillNames: null,
    basePromise: null,
    skillNamesPromise: null,
    renderToken: 0,
    insightsRequestToken: 0,
    insightsSelectedProfession: 0,
    insightsFilters: {
      boss: "drill",
      difficulty: "normal",
      metric: "dps",
      ratingBasis: "extraordinary",
      minRating: 0,
      maxRating: 120000,
      gameVersion: "all",
      sort: "p50",
      calibrated: false,
      demo: false,
    },
    encounterMode: "dps",
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
      state.basePromise = null;
    }
    if (state.statistics && state.leaderboards && state.bossCatalog) return;
    if (state.basePromise) return state.basePromise;
    state.basePromise = Promise.all([
      fetchJson(`${API_ROOT}/statistics`),
      fetchJson(`${API_ROOT}/leaderboards?limit=500`),
      fetchJson("assets/bosses/boss_icon_sources.json"),
    ])
      .then(([statistics, leaderboards, bossCatalog]) => {
        state.statistics = statistics.statistics || {};
        state.leaderboards = Array.isArray(leaderboards.leaderboards)
          ? leaderboards.leaderboards
          : [];
        state.bossCatalog = bossCatalog || {};
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
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(number);
  }

  function formatCompact(value) {
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

  function rememberedViewerProfileId() {
    try {
      const value = String(window.sessionStorage.getItem("gmzz.viewerProfileId") || "");
      return /^prf_[A-Za-z0-9_-]{8,64}$/.test(value) ? value : "";
    } catch (_error) {
      return "";
    }
  }

  function rememberViewerProfileId(profileId) {
    const value = String(profileId || "");
    if (!/^prf_[A-Za-z0-9_-]{8,64}$/.test(value)) return;
    try {
      window.sessionStorage.setItem("gmzz.viewerProfileId", value);
    } catch (_error) {
      // Private browsing can disable storage; the public page still works.
    }
  }

  function openEncounter(encounterId, profileId = "") {
    const id = String(encounterId || "");
    if (!/^enc_[A-Za-z0-9_-]{8,64}$/.test(id)) return;
    rememberViewerProfileId(profileId);
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
    }[difficulty] || difficulty || "全部难度";
  }

  function performanceValue(value, metric) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "--";
    return metric === "boss_damage" ? formatCompact(number) : formatInteger(number);
  }

  function previewPerformance(serverValue, filters) {
    const previewConfig = INSIGHTS_PREVIEW_DATA[filters.boss]
      || INSIGHTS_PREVIEW_DATA.viscountess;
    const difficultyScale = {
      all: 1,
      normal: 1,
      hard: 1.14,
      nightmare: 1.27,
    }[filters.difficulty] || 1;
    const ratingMidpoint = (Number(filters.minRating) + Number(filters.maxRating)) / 2;
    const ratingScale = Math.max(0.76, Math.min(1.18, 0.76 + ratingMidpoint / 285000));
    const metricScale = filters.metric === "boss_damage" ? 305 : 1;
    const calibratedFactors = {
      1200001: 1.015,
      1200002: 1.08,
      1200003: 0.985,
      1200004: 1.025,
      1200005: 1.0,
      1200006: 0.96,
      1200007: 1.01,
    };
    const width = Math.max(10000, Number(filters.maxRating) - Number(filters.minRating));
    const sampleScale = Math.max(0.22, Math.min(1, width / 120000));
    const groups = previewConfig.groups.map((source, index) => {
      const [professionId, samples, p10, p25, p50, p75, p90, best] = source;
      const calibrationScale = filters.calibrated ? calibratedFactors[professionId] : 1;
      const scale = difficultyScale * ratingScale * metricScale * calibrationScale;
      const values = [p10, p25, p50, p75, p90, best].map((value) => value * scale);
      const mineValue = index === 2 ? values[2] + (values[3] - values[2]) * 0.48 : 0;
      return {
        profession_id: professionId,
        sample_count: Math.max(6, Math.round(samples * sampleScale)),
        p10: values[0],
        p25: values[1],
        p50: values[2],
        p75: values[3],
        p90: values[4],
        best: values[5],
        target_rating: ratingMidpoint,
        calibration_applied: filters.calibrated,
        best_record: null,
        mine: index === 2 ? {
          value: mineValue,
          raw_value: mineValue,
          percentile: 68,
          exceeds_percent: 67,
          gap_to_p75: values[3] - mineValue,
          gap_to_p90: values[4] - mineValue,
          demo: true,
        } : null,
      };
    });
    const sortKey = filters.sort || "p50";
    groups.sort((a, b) => Number(b[sortKey] || 0) - Number(a[sortKey] || 0));
    return {
      ...(serverValue || {}),
      source: "preview",
      preview: true,
      preview_kind: filters.demo ? "demo" : "upcoming",
      selection: {
        boss: filters.boss,
        boss_name: previewConfig.bossName,
        dungeon_name: previewConfig.dungeonName,
        difficulty: filters.difficulty,
        metric: filters.metric,
        rating_basis: filters.ratingBasis,
        min_rating: filters.minRating,
        max_rating: filters.maxRating,
        game_version: filters.gameVersion,
        sort: filters.sort,
        calibrated: filters.calibrated,
      },
      availability: {
        difficulties: ["normal", "hard", "nightmare"],
        game_versions: ["preview"],
        extraordinary_rating: true,
        equipment_rating: true,
        rating_min: 0,
        rating_max: 120000,
      },
      total_samples: groups.reduce((sum, row) => sum + row.sample_count, 0),
      total_encounters: Math.max(4, Math.round(previewConfig.encounterCount * sampleScale)),
      updated_at: Date.now() / 1000,
      groups,
      calibration: {
        requested: filters.calibrated,
        applied_groups: filters.calibrated ? groups.length : 0,
        minimum_samples_per_profession: 4,
      },
    };
  }

  async function fetchPerformance(filters) {
    if (filters.demo) return previewPerformance(null, filters);
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
    const profileId = rememberedViewerProfileId();
    if (profileId) params.set("profile_id", profileId);
    const payload = await fetchJson(`${API_ROOT}/performance?${params}`);
    const performance = payload.performance || {};
    if (filters.boss === "viscountess" && !(performance.groups || []).length) {
      return previewPerformance(performance, filters);
    }
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
    if (filters.boss === "drill") {
      const previewToggle = element(
        "a",
        "performance-preview-toggle",
        filters.demo ? "返回真实数据" : "查看完整效果",
      );
      previewToggle.href = filters.demo
        ? "#insights?boss=drill"
        : "#insights?boss=drill&demo=1";
      overviewMeta.appendChild(previewToggle);
    }
    overview.appendChild(overviewMeta);
    fragment.appendChild(overview);

    const updateFilters = (patch) => {
      Object.assign(state.insightsFilters, patch);
      state.insightsSelectedProfession = 0;
      renderInsights(routeToken);
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
    const bossOptions = [
      { value: "drill", label: "钻头" },
      { value: "viscountess", label: "子爵夫人 · 预览" },
    ];
    filter.appendChild(performanceFilterField("副本", performanceSelect([
      { value: "drill", label: "记忆的传承" },
      { value: "viscountess", label: "五月庄园·城堡" },
    ], filters.boss, (value) => updateFilters({
      boss: value,
      difficulty: "normal",
      ratingBasis: "extraordinary",
      demo: false,
      gameVersion: "all",
      minRating: 0,
      maxRating: 120000,
    }), "副本")));
    filter.appendChild(performanceFilterField("Boss", performanceSelect(
      bossOptions,
      filters.boss,
      (value) => updateFilters({
        boss: value,
        difficulty: "normal",
        ratingBasis: "extraordinary",
        demo: false,
        gameVersion: "all",
        minRating: 0,
        maxRating: 120000,
      }),
      "Boss",
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
    if (performance.preview) {
      note.appendChild(element(
        "p",
        "performance-preview-note",
        performance.preview_kind === "demo"
          ? "当前为前端演示样本，仅用于查看完整图表效果，不写入数据库，也不进入真实统计。"
          : "子爵夫人尚未产生正式样本，当前数据仅用于查看页面效果；产生有效上传后会自动切换为真实统计。",
      ));
    }
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
    let performance;
    try {
      performance = await fetchPerformance(state.insightsFilters);
    } catch (error) {
      if (state.insightsFilters.boss !== "viscountess") throw error;
      performance = previewPerformance(null, state.insightsFilters);
    }
    if (token !== state.renderToken || requestToken !== state.insightsRequestToken) return;
    renderPerformancePage(performance, token);
  }

  async function renderRecords(token) {
    setPage("PEAK RECORDS", "巅峰记录", "榜单按首领、关卡与职业分别计算名次；仅展示符合公开与排行条件的上传记录。");
    showStatus("正在读取巅峰记录");
    await loadBaseData();
    if (token !== state.renderToken) return;

    const rows = [...(state.leaderboards || [])].sort((a, b) => {
      const dps = Number(b.dps || 0) - Number(a.dps || 0);
      return dps || Number(b.ended_at || 0) - Number(a.ended_at || 0);
    });
    const fragment = document.createDocumentFragment();
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
    const cleaned = String(query || "").trim().slice(0, 48);
    setPage("BATTLE SEARCH", "战斗记录搜索");
    const summary = element("p", "search-summary");
    summary.append("正在搜索：");
    summary.appendChild(element("strong", "", cleaned || "--"));
    contentNode.appendChild(summary);
    showStatus("正在搜索公开记录");
    await loadBaseData();
    if (token !== state.renderToken) return;

    const needle = cleaned.toLocaleLowerCase("zh-CN");
    const rows = (state.leaderboards || [])
      .filter((row) => {
        if (!needle || row.public_mode === "anonymous") return false;
        return String(row.display_name || "").toLocaleLowerCase("zh-CN").includes(needle);
      })
      .sort((a, b) => Number(b.ended_at || 0) - Number(a.ended_at || 0));

    const fragment = document.createDocumentFragment();
    const nextSummary = element("p", "search-summary");
    nextSummary.append("搜索 ");
    nextSummary.appendChild(element("strong", "", cleaned || "--"));
    nextSummary.append(`，找到 ${rows.length} 条公开记录`);
    fragment.appendChild(nextSummary);
    if (rows.length) {
      fragment.appendChild(recordList(rows, { sequentialRank: true }));
    } else {
      const empty = element("div", "portal-status");
      const wrap = element("div");
      wrap.appendChild(element("strong", "", "没有找到公开战斗记录"));
      wrap.appendChild(element("span", "", "请检查角色名称或上传昵称，匿名记录无法通过名称搜索。"));
      empty.appendChild(wrap);
      fragment.appendChild(empty);
    }
    contentNode.replaceChildren(fragment);
  }

  function renderShare() {
    setPage("SHARE BATTLE", "如何分享战斗记录", "战斗记录由玩家主动选择上传；未上传的本地记录不会出现在网站中。");
    const flow = element("div", "share-flow");
    [
      ["完成一场战斗", "在客户端的“战斗记录”中选择需要分享的记录。"],
      ["点击上传", "在该场记录的操作区域点击“上传”，首次使用时按提示创建上传身份。"],
      ["确认公开方式", "选择匿名、角色名称或上传昵称后确认；网站只展示你选择公开的信息。"],
      ["搜索与分享", "上传完成后，可在首页输入角色名称或昵称打开公开记录并分享页面地址。"],
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
      return duration > 0 ? effectiveHealing / duration : 0;
    }
    if (mode === "dt") return Number(participant.taken || participant.stats?.taken || 0);
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
      (sum, row) => sum + encounterMetric(mode, row, durationSeconds),
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
  }

  function resultLabel(value) {
    if (value === "defeated") return ["胜利", ""];
    if (value === "failed") return ["失败", " is-failed"];
    return ["中断", " is-interrupted"];
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
      `关卡 ${Number(encounter.stage_id) || "--"} · ${formatDate(encounter.ended_at)} · ${Number(encounter.team_size) || 0} 人`,
    ));
    hero.appendChild(heroCopy);
    const [label, resultClass] = resultLabel(encounter.data?.result);
    hero.appendChild(element("div", `result-badge${resultClass}`, label));
    fragment.appendChild(hero);

    const metrics = element("div", "metric-grid");
    metrics.appendChild(metricCard("团队总伤害", formatCompact(encounter.team_total_damage), "本场累计"));
    metrics.appendChild(metricCard("团队 DPS", formatInteger(encounter.data?.team_dps), "有效战斗时间"));
    metrics.appendChild(metricCard("战斗时长", formatDuration(encounter.duration_seconds), "分:秒"));
    metrics.appendChild(metricCard("团队承伤", formatCompact(encounter.data?.team_taken), "本场累计"));
    fragment.appendChild(metrics);

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
      (a, b) => encounterMetric(state.encounterMode, b, durationSeconds)
        - encounterMetric(state.encounterMode, a, durationSeconds),
    );
    participantsSection.appendChild(sectionHeading("团队成员", `${sorted.length} 名成员 · ${encounterMetricLabel(state.encounterMode)}`));
    const list = element("div", "records-list");
    const maxMetric = Math.max(
      1,
      ...sorted.map((participant) => encounterMetric(state.encounterMode, participant, durationSeconds)),
    );
    let selectedRow = null;

    sorted.forEach((participant, index) => {
      const row = button("participant-row", "");
      row.setAttribute("aria-label", `查看 ${participant.display_name || "匿名玩家"} 的技能数据`);
      row.appendChild(element("div", `rank-cell${index < 3 ? " is-top" : ""}`, index + 1));
      row.appendChild(participantIdentity(participant));
      const track = element("div", "metric-track");
      const fill = element("div", "metric-fill");
      const metric = encounterMetric(state.encounterMode, participant, durationSeconds);
      fill.style.setProperty("--fill", `${Math.max(0, Math.min(100, metric / maxMetric * 100))}%`);
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
        renderSkills(skillsPanel, participant, state.encounterMode);
      });
      list.appendChild(row);
      if (index === 0) selectedRow = row;
    });
    participantsSection.appendChild(list);
    fragment.appendChild(participantsSection);
    fragment.appendChild(skillsPanel);

    window.requestAnimationFrame(() => {
      if (selectedRow) selectedRow.classList.add("is-selected");
      renderSkills(skillsPanel, sorted[0], state.encounterMode);
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
      showStatus("战斗记录地址无效", "请从搜索结果或巅峰记录中重新打开。", false);
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
    const isHome = route.name === "home";
    const requestedInsightsBoss = route.name === "insights" ? route.params.get("boss") : "";
    if (["drill", "viscountess"].includes(requestedInsightsBoss)) {
      state.insightsFilters.boss = requestedInsightsBoss;
    }
    if (route.name === "insights") {
      state.insightsFilters.demo = state.insightsFilters.boss === "drill"
        && route.params.get("demo") === "1";
    }
    scene.classList.toggle("portal-open", !isHome);
    portal.classList.toggle("is-visible", !isHome);
    portal.classList.toggle("is-insights", route.name === "insights");
    portal.setAttribute("aria-hidden", isHome ? "true" : "false");
    setActiveNavigation(route.name);
    if (isHome) {
      document.title = "记录每一场战斗";
      return;
    }

    const token = ++state.renderToken;
    document.title = "叨叨诡秘 · 战斗记录";
    refreshButton.hidden = route.name === "share" || route.name === "battle";
    try {
      if (force) await loadBaseData(true);
      if (route.name === "insights") await renderInsights(token);
      else if (route.name === "records") await renderRecords(token);
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
    window.location.hash = "";
  });
  refreshButton.addEventListener("click", () => renderRoute(true));
  window.addEventListener("hashchange", () => renderRoute(false));
  renderRoute(false);
})();
