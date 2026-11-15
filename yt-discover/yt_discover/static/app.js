"use strict";

const state = {
  videos: [],
  status: null,
  busy: false,
  settingsDirty: false,
};

const el = (id) => document.getElementById(id);

function clear(node) {
  while (node && node.firstChild) node.removeChild(node.firstChild);
}

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key === "dataset") Object.assign(node.dataset, value);
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value);
    }
  }
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === "string" || typeof child === "number"
      ? document.createTextNode(String(child)) : child);
  }
  return node;
}

function notice(message, kind = "info") {
  const box = el("notice");
  clear(box);
  if (!message) { box.className = "notice"; return; }
  box.appendChild(h("span", { text: message }));
  const close = h("button", { type: "button", class: "notice-close", text: "×",
    onclick: () => notice("") });
  box.appendChild(close);
  box.className = `notice ${kind}`;
}

async function api(path, options = {}) {
  const init = { headers: {}, ...options };
  if (init.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.body);
  }
  const response = await fetch(path, init);
  let payload = null;
  try { payload = await response.json(); } catch (_err) { payload = null; }
  if (!response.ok) {
    const message = payload && payload.error ? payload.error
      : `Request failed (HTTP ${response.status})`;
    throw new Error(message);
  }
  return payload;
}

function videoIdOk(id) {
  return typeof id === "string" && /^[A-Za-z0-9_-]{11}$/.test(id);
}

function formatDuration(seconds) {
  if (typeof seconds !== "number" || !isFinite(seconds) || seconds <= 0) return "";
  const total = Math.round(seconds);
  const hrs = Math.floor(total / 3600);
  const mins = Math.floor((total % 3600) / 60);
  const secs = total % 60;
  if (hrs > 0) return `${hrs}:${String(mins).padStart(2, "0")}:${String(secs).padStart(2, "0")}`;
  return `${mins}:${String(secs).padStart(2, "0")}`;
}

function formatViews(views) {
  if (typeof views !== "number" || !isFinite(views)) return "";
  if (views >= 1e9) return `${(views / 1e9).toFixed(1)}B views`;
  if (views >= 1e6) return `${(views / 1e6).toFixed(1)}M views`;
  if (views >= 1e3) return `${(views / 1e3).toFixed(1)}K views`;
  return `${views} views`;
}

function formatAge(days) {
  if (typeof days !== "number" || !isFinite(days)) return "";
  if (days < 1) return "today";
  if (days < 30) return `${Math.floor(days)}d ago`;
  if (days < 365) return `${Math.floor(days / 30)}mo ago`;
  return `${(days / 365).toFixed(1)}y ago`;
}

function thumbnailUrl(video) {
  if (videoIdOk(video.video_id)) {
    return `https://i.ytimg.com/vi/${video.video_id}/mqdefault.jpg`;
  }
  return "";
}

async function loadStatus() {
  try {
    const data = await api("/api/status");
    state.status = data;
    renderTune(data);
    if (!data.has_key) {
      notice("No YouTube API key found. Add YOUTUBE_API_KEY to ~/.config/yt-discover/env (chmod 600) and restart.", "warn");
    }
  } catch (err) {
    notice(err.message, "error");
  }
}

function renderQuota(quota) {
  if (!quota) return;
  const percent = Math.round((quota.ratio || 0) * 100);
  const fill = el("quota-fill");
  if (fill) {
    fill.value = percent;
    fill.textContent = `${percent}%`;
  }
  const meter = el("quota-meter");
  if (meter) meter.className = quota.warn ? "meter warn" : "meter";
  el("quota-text").textContent =
    `${quota.units} / ${quota.units_limit} units · ` +
    `${quota.search_calls} / ${quota.search_calls_limit} search calls · ` +
    `${quota.videos_calls} videos.list calls · ${quota.day} (Pacific)`;
}

function renderTune(data) {
  if (!data) return;
  renderQuota(data.quota);
  renderDayparts(data);
  renderTopics(data.topics || [], data.intents || []);
  renderModifiers(data.modifiers || []);
  renderWords(data.words || []);
  renderStyles(data.styles || []);
  renderChannels(data.channels || []);
  renderTrials(data.trials || []);
  renderSettings(data.settings || {});
  renderLaneSliders(data.settings || {});
  const key = el("key-status");
  key.textContent = data.has_key
    ? `API key loaded. Quota day: ${data.quota.day}.`
    : "No API key. The app will serve only from the candidate pool.";
  const effective = data.lane_shares || {};
  el("lane-effective").textContent =
    `Effective shares — core ${Math.round(effective.core * 100)}%, ` +
    `neighbour ${Math.round(effective.neighbour * 100)}%, ` +
    `wildcard ${Math.round(effective.wildcard * 100)}%.`;
  const explore = el("explore-slider");
  if (data.settings && !state.settingsDirty) {
    explore.value = String(Math.round((data.settings.explore || 0) * 100));
    el("explore-value").textContent = `${explore.value}%`;
    el("filter-length").value = data.settings.duration_filter || "any";
    el("filter-published").value = data.settings.published_mode || "any";
    el("filter-gems").checked = Boolean(data.settings.hidden_gems);
    el("filter-count").value = String(data.settings.batch_size || 12);
  }
}

function formatIntentWeights(weights) {
  return Object.entries(weights || {})
    .map(([name, weight]) => `${name}:${weight}`).join(", ");
}

function parseIntentWeights(text) {
  const weights = {};
  for (const part of String(text || "").split(",")) {
    const trimmed = part.trim();
    if (!trimmed) continue;
    const [name, raw] = trimmed.split(":");
    if (!name || !name.trim()) continue;
    const value = Number(raw);
    weights[name.trim().toLowerCase()] = isFinite(value) ? value : 1;
  }
  return weights;
}

function renderDayparts(data) {
  const now = el("daypart-now");
  const hour = data.local_hour;
  now.textContent = data.active_daypart
    ? `Now: ${data.active_daypart} (local hour ${hour}). ` +
      `Core ${Math.round((data.lane_shares_now.core || 0) * 100)}% / ` +
      `neighbour ${Math.round((data.lane_shares_now.neighbour || 0) * 100)}% / ` +
      `wildcard ${Math.round((data.lane_shares_now.wildcard || 0) * 100)}%.`
    : `Now: no day part matches local hour ${hour}; using base lane shares.`;

  const enabledBox = el("dayparts-enabled");
  if (data.settings && !state.settingsDirty) {
    enabledBox.checked = Boolean(data.settings.dayparts_enabled);
  }

  const body = el("dayparts-table").querySelector("tbody");
  clear(body);
  for (const profile of data.dayparts || []) {
    const num = (value, step) => h("input", { type: "number", value: String(value),
      step: step || "1", class: "mini-number" });
    const start = num(profile.start_hour);
    const end = num(profile.end_hour);
    const core = num(profile.lane_core, "0.05");
    const neighbour = num(profile.lane_neighbour, "0.05");
    const wildcard = num(profile.lane_wildcard, "0.05");
    const weights = h("input", { type: "text", class: "mini-text",
      value: formatIntentWeights(profile.intent_weights) });
    const save = () => post("/api/dayparts", { action: "update",
      id: profile.id, fields: {
        start_hour: Number(start.value), end_hour: Number(end.value),
        lane_core: Number(core.value), lane_neighbour: Number(neighbour.value),
        lane_wildcard: Number(wildcard.value),
        intent_weights: parseIntentWeights(weights.value),
      } });
    const toggle = h("input", { type: "checkbox", checked: profile.enabled,
      onchange: () => post("/api/dayparts", { action: "toggle",
        id: profile.id, enabled: toggle.checked }) });
    const name = h("td", { class: profile.active ? "active-daypart" : "",
      text: profile.name });
    const row = h("tr", { class: profile.active ? "active-row" : "" }, name,
      h("td", null, start), h("td", null, end),
      h("td", null, core), h("td", null, neighbour), h("td", null, wildcard),
      h("td", null, weights), h("td", null, toggle),
      h("td", null,
        h("button", { type: "button", class: "mini", text: "Save",
          onclick: save }),
        h("button", { type: "button", class: "mini danger", text: "×",
          onclick: () => post("/api/dayparts", { action: "remove",
            id: profile.id }) })));
    body.appendChild(row);
  }
}

function renderTopics(topics, intents) {
  const body = el("topics-table").querySelector("tbody");
  clear(body);
  const known = Array.from(new Set(["learning", "fun", ...(intents || [])]));
  for (const topic of topics) {
    const toggle = h("input", { type: "checkbox", checked: topic.enabled,
      onchange: () => post("/api/topics", { action: "toggle", id: topic.id,
        enabled: toggle.checked }) });
    const remove = h("button", { type: "button", class: "mini danger", text: "×",
      title: "Remove topic",
      onclick: () => post("/api/topics", { action: "remove", id: topic.id }) });
    const lane = h("span", { class: `lane lane-${topic.lane}`, text: topic.lane });
    const intent = h("select", { class: "mini-select",
      onchange: () => post("/api/topics", { action: "intent", id: topic.id,
        intent: intent.value }) });
    const values = known.includes(topic.intent)
      ? known : [...known, topic.intent];
    for (const name of values) {
      intent.appendChild(h("option", { value: name, text: name,
        selected: name === topic.intent ? "selected" : null }));
    }
    body.appendChild(h("tr", null,
      h("td", { text: topic.text }),
      h("td", null, lane),
      h("td", null, intent),
      h("td", null, toggle),
      h("td", { text: Number(topic.score || 0).toFixed(2) }),
      h("td", { text: String(topic.searches || 0) }),
      h("td", { text: String(topic.plays || 0) }),
      h("td", { text: String(topic.rejects || 0) }),
      h("td", null, remove)));
  }
}

function renderModifiers(modifiers) {
  const list = el("modifiers-list");
  clear(list);
  for (const modifier of modifiers) {
    list.appendChild(h("li", null,
      h("span", { text: modifier.text }),
      h("small", { text: ` score ${Number(modifier.score || 0).toFixed(2)} · ` +
        `${modifier.searches} searches · ${modifier.plays} plays` }),
      h("button", { type: "button", class: "mini danger", text: "×",
        onclick: () => post("/api/modifiers", { action: "remove", id: modifier.id }) })));
  }
}

function renderWords(words) {
  const list = el("words-list");
  clear(list);
  for (const word of words) {
    const evidence = `neg ${word.distinct_negatives} distinct, ` +
      `${Math.round((word.negative_share || 0) * 100)}% negative`;
    const action = word.blocked
      ? h("button", { type: "button", class: "mini", text: "Unblock",
          onclick: () => post("/api/words", { action: "unblock", word: word.word }) })
      : h("button", { type: "button", class: "mini", text: "Block",
          onclick: () => post("/api/words", { action: "block", word: word.word }) });
    list.appendChild(h("li", null,
      h("span", { text: word.word }),
      h("small", { text: `${word.source}${word.auto ? " auto" : ""}` +
        `${word.protected ? " protected" : ""} · ${evidence}` }),
      action,
      h("button", { type: "button", class: "mini danger", text: "×",
        onclick: () => post("/api/words", { action: "remove", word: word.word }) })));
  }
}

function renderStyles(styles) {
  const list = el("styles-list");
  clear(list);
  for (const style of styles) {
    const label = style.kind === "regex"
      ? `regex: ${style.pattern}` : style.kind;
    const evidence = `neg ${style.distinct_negatives} distinct, ` +
      `${Math.round((style.negative_share || 0) * 100)}% negative`;
    list.appendChild(h("li", null,
      h("span", { text: label }),
      h("small", { text: `${style.enabled ? "active" : "off"}` +
        `${style.auto ? " auto" : ""} · ${evidence}` }),
      h("button", { type: "button", class: "mini", text: style.enabled ? "Disable" : "Enable",
        onclick: () => post("/api/styles", { action: "toggle", id: style.id,
          enabled: !style.enabled }) }),
      h("button", { type: "button", class: "mini danger", text: "×",
        onclick: () => post("/api/styles", { action: "remove", id: style.id }) })));
  }
}

function renderChannels(channels) {
  const list = el("channels-list");
  clear(list);
  if (!channels.length) {
    list.appendChild(h("li", { class: "muted", text: "None blocked." }));
    return;
  }
  for (const channel of channels) {
    list.appendChild(h("li", null,
      h("span", { text: channel.channel_title || channel.channel_id }),
      h("button", { type: "button", class: "mini", text: "Unblock",
        onclick: () => post("/api/channels", { action: "unblock",
          channel_id: channel.channel_id }) })));
  }
}

function renderTrials(trials) {
  const active = el("trials-list");
  const retired = el("retired-list");
  clear(active);
  clear(retired);
  let activeCount = 0;
  for (const trial of trials) {
    const line = h("li", null,
      h("span", { text: trial.text }),
      h("small", { text: ` ${trial.searches_used}/${trial.budget} searches · ` +
        `${trial.impressions} impressions · ${trial.clicks} clicks` }));
    if (trial.status === "active") {
      activeCount += 1;
      line.appendChild(h("button", { type: "button", class: "mini danger", text: "×",
        onclick: () => post("/api/trials", { action: "remove",
          topic_id: trial.topic_id }) }));
      active.appendChild(line);
    } else if (trial.status === "promoted") {
      line.appendChild(h("small", { text: " promoted to neighbour" }));
      line.appendChild(h("button", { type: "button", class: "mini danger", text: "×",
        onclick: () => post("/api/trials", { action: "remove",
          topic_id: trial.topic_id }) }));
      active.appendChild(line);
    } else {
      line.appendChild(h("button", { type: "button", class: "mini", text: "Revive",
        onclick: () => post("/api/trials", { action: "revive",
          topic_id: trial.topic_id }) }));
      retired.appendChild(line);
    }
  }
  if (!activeCount) active.appendChild(h("li", { class: "muted", text: "None active." }));
  if (!retired.firstChild) retired.appendChild(h("li", { class: "muted", text: "None retired." }));
}

const SETTING_LABELS = {
  half_life_days: "Half-life (days)",
  batch_size: "Batch size",
  max_per_channel: "Max per channel / batch",
  min_duration_seconds: "Min duration (s)",
  duration_short_seconds: "Short/long cutoff (s)",
  hidden_gems_min_views: "Gems min views",
  hidden_gems_max_views: "Gems max views",
  pool_expiry_days: "Pool expiry (days)",
  modifier_probability: "Modifier probability",
  weight_play: "Weight: play",
  weight_more_like_this: "Weight: more like this",
  weight_not_this: "Weight: not this",
  weight_enough_topic: "Weight: enough topic",
  weight_impression_miss: "Weight: ignored impression",
  impression_miss_after_hours: "Ignored after (hours)",
  word_min_distinct_negatives: "Word: min distinct negatives",
  word_negative_share: "Word: negative share",
  max_auto_blocked_words: "Max auto-blocked words",
  style_caps_ratio: "Caps ratio threshold",
  style_emoji_min: "Emoji count threshold",
  style_min_distinct_negatives: "Style: min distinct negatives",
  style_negative_share: "Style: negative share",
  max_auto_style_filters: "Max auto style filters",
  max_trial_topics: "Max trial topics",
  trial_budget_searches: "Trial search budget",
  trial_min_impressions: "Trial min impressions",
  trial_retire_click_rate: "Trial retire click rate",
  exploration_floor: "Exploration floor",
  topic_ceiling: "Per-topic ceiling",
  max_searches_per_batch: "Max searches / batch",
  lane_core_share: "Lane share: core",
  lane_neighbour_share: "Lane share: neighbour",
  lane_wildcard_share: "Lane share: wildcard",
  quota_warn_ratio: "Quota warning ratio",
  safe_search: "Safe search",
  relevance_language: "Relevance language",
};

function renderLaneSliders(settings) {
  const box = el("lane-sliders");
  clear(box);
  const lanes = [
    ["lane_core_share", "core"],
    ["lane_neighbour_share", "neighbour"],
    ["lane_wildcard_share", "wildcard"],
  ];
  for (const [key, label] of lanes) {
    const slider = h("input", { type: "range", min: "0", max: "100",
      step: "5", value: String(Math.round((settings[key] || 0) * 100)) });
    const value = h("span", { text: `${slider.value}%` });
    slider.addEventListener("input", () => {
      value.textContent = `${slider.value}%`;
    });
    slider.addEventListener("change", () => {
      post("/api/settings", { settings: { [key]: Number(slider.value) / 100 } });
    });
    box.appendChild(h("label", null,
      h("span", { text: label }), slider, value));
  }
}

function renderSettings(settings) {
  if (state.settingsDirty) return;
  const box = el("settings-list");
  clear(box);
  for (const [key, label] of Object.entries(SETTING_LABELS)) {
    if (!(key in settings)) continue;
    const value = settings[key];
    const input = h("input", { type: typeof value === "number" ? "number" : "text",
      step: typeof value === "number" && !Number.isInteger(value) ? "0.01" : "1",
      value: String(value) });
    input.dataset.settingKey = key;
    input.addEventListener("input", () => { state.settingsDirty = true; });
    box.appendChild(h("label", { class: "setting" }, h("span", { text: label }), input));
  }
  box.appendChild(h("p", { class: "hint",
    text: "Saving reloads the engine values; no restart needed." }));
}

function renderFeed(videos) {
  const feed = el("feed");
  clear(feed);
  state.videos = videos;
  el("empty").classList.toggle("hidden", videos.length > 0);
  videos.forEach((video, index) => feed.appendChild(renderCard(video, index)));
}

function renderCard(video, index) {
  const card = h("article", { class: "card" });
  const thumbButton = h("button", { type: "button", class: "thumb",
    title: "Play here",
    onclick: () => playVideo(video) });
  const image = h("img", { alt: "", loading: "lazy" });
  const url = thumbnailUrl(video);
  if (url) image.src = url;
  image.addEventListener("error", () => { image.style.visibility = "hidden"; });
  thumbButton.appendChild(image);
  const duration = formatDuration(video.duration_seconds);
  if (duration) thumbButton.appendChild(h("span", { class: "duration", text: duration }));
  card.appendChild(thumbButton);

  const body = h("div", { class: "card-body" });
  const title = h("h3", { class: "title", text: video.title || "(untitled)" });
  title.addEventListener("click", () => playVideo(video));
  body.appendChild(title);
  body.appendChild(h("p", { class: "meta",
    text: [video.channel_title, formatViews(video.views), formatAge(video.age_days)]
      .filter(Boolean).join(" · ") }));
  const lane = String(video.lane || "");
  const tag = h("span", { class: `lane lane-${lane}`,
    text: `${video.topic || "unknown"} · ${lane || "?"}` });
  body.appendChild(h("p", { class: "tags-line" }, tag));

  const actions = h("div", { class: "actions" });
  const actionButton = (label, kind, extra) => h("button", {
    type: "button", class: "mini", text: label,
    onclick: () => sendEvent(video, kind, extra) });
  actions.appendChild(actionButton("More like this", "more_like_this"));
  actions.appendChild(actionButton("Not this", "not_this"));
  actions.appendChild(actionButton("Block channel", "block_channel"));
  actions.appendChild(actionButton("Enough of topic", "enough_of_this_topic"));
  body.appendChild(actions);
  card.appendChild(body);
  card.dataset.index = String(index);
  return card;
}

function playVideo(video) {
  if (!videoIdOk(video.video_id)) {
    notice("This video id is invalid; not playable.", "error");
    return;
  }
  const wrap = el("player-wrap");
  wrap.classList.remove("hidden");
  const iframe = el("player");
  iframe.src = `https://www.youtube-nocookie.com/embed/${video.video_id}` +
    "?autoplay=1&rel=0&modestbranding=1";
  el("player-title").textContent = video.title || "";
  el("player-fallback").href = `https://www.youtube.com/watch?v=${video.video_id}`;
  sendEvent(video, "play", true);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

async function sendEvent(video, kind, silent) {
  try {
    const payload = await api("/api/event", {
      method: "POST",
      body: {
        kind,
        video_id: video.video_id,
        topic: video.topic || "",
        channel_id: video.channel_id || "",
        channel_title: video.channel_title || "",
      },
    });
    if (!silent) {
      const changed = payload.changed || {};
      const parts = [];
      if (changed.blocked_words && changed.blocked_words.length) {
        parts.push(`blocked words: ${changed.blocked_words.join(", ")}`);
      }
      if (changed.style_filters && changed.style_filters.length) {
        parts.push(`new style filters: ${changed.style_filters.length}`);
      }
      if (changed.retired_trials && changed.retired_trials.length) {
        parts.push(`retired trials: ${changed.retired_trials.join(", ")}`);
      }
      notice(parts.length ? `Learned: ${parts.join("; ")}` : "Noted.");
      loadStatus();
    }
  } catch (err) {
    notice(err.message, "error");
  }
}

async function post(path, body) {
  try {
    const result = await api(path, { method: "POST", body });
    await loadStatus();
    return result;
  } catch (err) {
    notice(err.message, "error");
    return null;
  }
}

async function discover(mode) {
  if (state.busy) return;
  state.busy = true;
  const button = el("discover");
  button.disabled = true;
  try {
    const count = Number(el("filter-count").value) || 12;
    const payload = await api("/api/feed", {
      method: "POST",
      body: { count, mode: mode || "normal" },
    });
    renderFeed(payload.videos || []);
    renderQuota(payload.quota);
    const messages = payload.messages || [];
    if (payload.filters_warning) messages.push(payload.filters_warning);
    if (messages.length) notice(messages.join(" "), "warn");
    else notice("");
    await loadStatus();
  } catch (err) {
    notice(err.message, "error");
  } finally {
    state.busy = false;
    button.disabled = false;
  }
}

function applyFilterSettings() {
  const settings = {
    duration_filter: el("filter-length").value,
    published_mode: el("filter-published").value,
    hidden_gems: el("filter-gems").checked,
    batch_size: Number(el("filter-count").value) || 12,
  };
  return post("/api/settings", { settings });
}

function wire() {
  el("discover").addEventListener("click", () => discover("normal"));
  el("surprise").addEventListener("click", () => discover("wildcard"));
  el("tune-toggle").addEventListener("click", (event) => {
    const panel = el("tune");
    const hidden = panel.classList.toggle("hidden");
    event.currentTarget.setAttribute("aria-expanded", String(!hidden));
  });
  el("topic-add").addEventListener("click", () => {
    const input = el("topic-input");
    const text = input.value.trim();
    if (!text) return;
    post("/api/topics", { action: "add", text,
      lane: el("topic-lane").value,
      intent: el("topic-intent").value }).then(() => { input.value = ""; });
  });
  el("topic-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") el("topic-add").click();
  });
  el("modifier-add").addEventListener("click", () => {
    const input = el("modifier-input");
    if (!input.value.trim()) return;
    post("/api/modifiers", { action: "add", text: input.value.trim() })
      .then(() => { input.value = ""; });
  });
  el("word-add").addEventListener("click", () => {
    const input = el("word-input");
    if (!input.value.trim()) return;
    post("/api/words", { action: "add", word: input.value.trim() })
      .then(() => { input.value = ""; });
  });
  el("style-add").addEventListener("click", () => {
    const kind = el("style-kind").value;
    const pattern = el("style-pattern").value;
    post("/api/styles", { action: "add", kind,
      pattern: kind === "regex" ? pattern : "" })
      .then(() => { el("style-pattern").value = ""; });
  });
  el("dayparts-enabled").addEventListener("change", (event) => {
    post("/api/settings", { settings: { dayparts_enabled: event.target.checked } });
  });
  el("daypart-add").addEventListener("click", () => {
    const name = window.prompt("Day part name (e.g. Morning reading)");
    if (!name) return;
    post("/api/dayparts", { action: "add", daypart: {
      name, start_hour: 0, end_hour: 6, lane_core: 0.4,
      lane_neighbour: 0.4, lane_wildcard: 0.2,
      intent_weights: { learning: 1, fun: 1 } } });
  });
  el("seed-reload").addEventListener("click", async () => {
    try {
      const result = await api("/api/seed", { method: "POST", body: {} });
      if (!result.ok) { notice(result.reason || "Seed reload failed.", "warn"); return; }
      const added = Object.entries(result.added || {})
        .map(([table, n]) => `${n} ${table}`).join(", ");
      notice(added ? `Seed reloaded: ${added}.` : "Seed reloaded; nothing new.");
      await loadStatus();
    } catch (err) { notice(err.message, "error"); }
  });
  el("explore-slider").addEventListener("input", (event) => {
    el("explore-value").textContent = `${Number(event.target.value)}%`;
  });
  el("explore-slider").addEventListener("change", (event) => {
    const value = Number(event.target.value);
    post("/api/settings", { settings: { explore: value / 100 } });
  });
  for (const id of ["filter-length", "filter-published", "filter-gems", "filter-count"]) {
    el(id).addEventListener("change", applyFilterSettings);
  }
  el("settings-save").addEventListener("click", () => {
    const settings = {};
    for (const input of el("settings-list").querySelectorAll("input")) {
      const key = input.dataset.settingKey;
      if (!key) continue;
      const value = input.type === "number" ? Number(input.value) : input.value;
      if (input.type === "number" && !isFinite(value)) continue;
      settings[key] = value;
    }
    state.settingsDirty = false;
    post("/api/settings", { settings });
  });
  el("learn-now").addEventListener("click", async () => {
    try {
      const result = await api("/api/learn", { method: "POST", body: {} });
      notice(`Learning done. Words: ${(result.blocked_words || []).length}, ` +
        `styles: ${(result.style_filters || []).length}, ` +
        `trials added: ${(result.trial_topics || []).length}, ` +
        `retired: ${(result.retired_trials || []).length}.`);
      await loadStatus();
    } catch (err) { notice(err.message, "error"); }
  });
  el("reset").addEventListener("click", () => {
    if (!window.confirm("Clear seen history, pool, events and counters? Topics and settings stay.")) return;
    post("/api/reset", {}).then(() => {
      renderFeed([]);
      notice("History reset.");
    });
  });
  el("export").addEventListener("click", async () => {
    try {
      const data = await api("/api/export");
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = "yt-discover-export.json";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(link.href);
      notice("Exported.");
    } catch (err) { notice(err.message, "error"); }
  });
  el("import").addEventListener("click", () => el("import-file").click());
  el("import-file").addEventListener("change", async (event) => {
    const file = event.target.files && event.target.files[0];
    if (!file) return;
    try {
      const text = await file.text();
      const parsed = JSON.parse(text);
      await api("/api/import", { method: "POST", body: { data: parsed } });
      notice("Imported.");
      await loadStatus();
    } catch (err) { notice(`Import failed: ${err.message}`, "error"); }
    event.target.value = "";
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "n" && !event.ctrlKey && !event.metaKey && !event.altKey) {
      const tag = (event.target.tagName || "").toLowerCase();
      if (tag === "input" || tag === "textarea" || tag === "select") return;
      event.preventDefault();
      discover("normal");
    }
  });
}

wire();
loadStatus().then(() => {
  const quoted = new URLSearchParams(window.location.search);
  if (quoted.get("autostart") === "1") discover("normal");
});
