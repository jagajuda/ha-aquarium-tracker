// Aquarium Tracker sidebar panel (no build step, plain web component)

const LABEL_COLORS = {
  dose: "#e67e22",
  measurement: "#3498db",
  water_change: "#16a085",
  fertilize: "#27ae60",
  note: "#7f8c8d",
  system: "#8e44ad",
};
const EXTRA_COLORS = ["#c0392b", "#d4ac0d", "#2e86c1", "#a04000", "#1e8449", "#6c3483"];
const PHASE_NAMES = {
  start: "Başlangıç",
  ammonia: "Amonyak fazı",
  nitrite: "Nitrit fazı",
  verify: "Doğrulama",
  complete: "Tamamlandı",
};
const MODE_NAMES = { cycling: "Cycle", maintenance: "Bakım" };
const STATUS_NAMES = { upcoming: "Yaklaşıyor", due: "Zamanı geldi", snoozed: "Ertelendi", inactive: "Pasif" };
const USER_TYPES = ["measurement", "dose", "water_change", "fertilize", "maintenance", "note"];
const ZERO_BASED = new Set(["NH4", "NO2", "NO3", "PO4", "Fe", "Cu"]);
const PARAM_COLORS = {
  NH4: "#d62728", NO2: "#9467bd", NO3: "#1f77b4", PO4: "#b8a200", Fe: "#8c564b",
  Cu: "#17becf", pH: "#e377c2", KH: "#393b79", GH: "#7f7f7f", temp: "#637939",
};
const MARKER_TYPES = ["dose", "water_change", "fertilize"];
const MARKER_SHORT = { dose: "Doz", water_change: "SD", fertilize: "G" };

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pad = (n) => String(n).padStart(2, "0");
const toLocalInput = (d) =>
  `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
const fmtDT = (iso) =>
  new Date(iso).toLocaleString("tr-TR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
const fmtDay = (d) => d.toLocaleDateString("tr-TR", { weekday: "short", day: "2-digit", month: "long", year: "numeric" });
// iOS Turkish keyboards only offer a decimal comma
const num = (v) => (typeof v === "string" ? v.trim().replace(",", ".") : v);
const fmtNum = (v) => (v === null || v === undefined || v === "" ? "–" : Number(v).toLocaleString("tr-TR", { maximumFractionDigits: 3 }));
function ago(iso) {
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  if (m < 1) return "şimdi";
  if (m < 60) return `${m} dk önce`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} sa önce`;
  return `${Math.round(h / 24)} gün önce`;
}
function until(iso) {
  const m = Math.round((new Date(iso).getTime() - Date.now()) / 60000);
  if (m <= 0) return ago(iso);
  if (m < 60) return `${m} dk sonra`;
  const h = Math.round(m / 60);
  if (h < 48) return `${h} sa sonra`;
  return `${Math.round(h / 24)} gün sonra`;
}
function store(key, val) {
  try {
    if (val === undefined) return JSON.parse(localStorage.getItem(`aqt_${key}`));
    localStorage.setItem(`aqt_${key}`, JSON.stringify(val));
  } catch (e) {
    return null;
  }
  return null;
}

class AquariumTrackerPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this.s = {
      tab: "dashboard",
      tank: store("tank"),
      snap: null,
      events: [],
      hidden: new Set(store("hidden") || []),
      range: store("range") || 30,
      form: null,
      audit: null,
      error: null,
      toast: null,
      loading: true,
    };
    this._built = false;
  }

  set hass(h) {
    const first = !this._hass;
    this._hass = h;
    if (this._menu) this._menu.hass = h;
    if (first) this._init();
  }
  set narrow(n) {
    this._narrow = n;
    if (this._menu) this._menu.narrow = n;
  }
  set panel(p) {
    this._panel = p;
  }

  connectedCallback() {
    if (this._hass && !this._unsub) this._subscribe();
  }
  disconnectedCallback() {
    if (this._unsub) {
      this._unsub.then((u) => u && u()).catch(() => {});
      this._unsub = null;
    }
  }

  async _init() {
    this._build();
    this._subscribe();
    await this._refresh();
    const q = new URLSearchParams(window.location.search);
    if (q.get("tank")) {
      this._openForm(q.get("tank"), q.get("task"));
      history.replaceState(null, "", window.location.pathname);
    }
  }

  _subscribe() {
    try {
      this._unsub = this._hass.connection.subscribeMessage(() => this._scheduleRefresh(), {
        type: "aquarium_tracker/subscribe",
      });
      this._unsub.catch(() => (this._unsub = null));
    } catch (e) {
      this._unsub = null;
    }
  }
  _scheduleRefresh() {
    clearTimeout(this._rt);
    this._rt = setTimeout(() => this._refresh(), 300);
  }

  async _refresh() {
    try {
      const [snap, events] = await Promise.all([
        this._hass.callWS({ type: "aquarium_tracker/snapshot" }),
        this._hass.callWS({ type: "aquarium_tracker/events" }),
      ]);
      this.s.snap = snap;
      this.s.events = events;
      this.s.error = null;
      if (!snap.tanks.find((t) => t.key === this.s.tank)) this.s.tank = snap.tanks[0]?.key;
    } catch (e) {
      this.s.error = e.message || String(e);
    }
    this.s.loading = false;
    this._render();
  }

  // ------------------------------------------------------------ helpers
  tank(key = this.s.tank) {
    return this.s.snap?.tanks.find((t) => t.key === key);
  }
  labelColor(key) {
    if (LABEL_COLORS[key]) return LABEL_COLORS[key];
    let h = 0;
    for (const c of key) h = (h * 31 + c.charCodeAt(0)) >>> 0;
    return EXTRA_COLORS[h % EXTRA_COLORS.length];
  }
  labelName(key) {
    return this.s.snap?.labels.find((l) => l.key === key)?.name || key;
  }
  unit(p) {
    return this.s.snap?.parameters[p]?.unit || "";
  }
  _toast(msg, err = false) {
    this.s.toast = { msg, err };
    this._render();
    clearTimeout(this._tt);
    this._tt = setTimeout(() => {
      this.s.toast = null;
      this._render();
    }, 3000);
  }
  async _ws(msg, okText) {
    try {
      const r = await this._hass.callWS(msg);
      if (okText) this._toast(okText);
      await this._refresh();
      return r;
    } catch (e) {
      this._toast(e.message || String(e), true);
      return undefined;
    }
  }

  // ------------------------------------------------------------ form
  _newForm(tankKey, type, task) {
    const t = this.tank(tankKey);
    const f = {
      id: null,
      tank: tankKey,
      type,
      ts: toLocalInput(new Date()),
      tsTouched: false,
      note: "",
      solution: t?.stock_solutions[0]?.key || "",
      volume_ml: "",
      params: [],
      values: {},
      percent: t?.water_change_default_percent ?? "",
      doses: {},
      action: task?.action || "",
    };
    if (type === "measurement") {
      if (task?.params) f.params = [...task.params];
      else if (t?.mode === "cycling" && t.cycle) f.params = [...t.cycle.params];
    }
    if (type === "fertilize") for (const fz of t?.fertilizers || []) f.doses[fz.key] = fz.default_dose ?? "";
    return f;
  }
  _openForm(tankKey, taskKey) {
    const t = this.tank(tankKey);
    if (!t) return;
    this.s.tank = tankKey;
    const task = t.tasks.find((x) => x.task === taskKey);
    this.s.form = this._newForm(tankKey, task?.event || "measurement", task);
    this.s.audit = null;
    this.s.tab = "log";
    this._render();
  }
  _editEvent(id) {
    const ev = this.s.events.find((e) => e.id === id);
    if (!ev) return;
    const f = this._newForm(ev.tank, ev.type);
    Object.assign(f, { id: ev.id, ts: toLocalInput(new Date(ev.ts)), tsTouched: true, note: ev.note || "" });
    const p = ev.payload || {};
    if (ev.type === "measurement") {
      f.values = Object.fromEntries(Object.entries(p.values || {}).map(([k, v]) => [k, String(v)]));
      f.params = Object.keys(p.values || {});
    } else if (ev.type === "dose") {
      f.solution = p.solution;
      f.volume_ml = String(p.volume_ml ?? "");
    } else if (ev.type === "water_change") {
      f.percent = p.percent ?? "";
    } else if (ev.type === "fertilize") {
      f.doses = Object.fromEntries(Object.keys(f.doses).map((k) => [k, p.doses?.[k] ?? ""]));
    } else if (ev.type === "maintenance") {
      f.action = p.action || "";
    }
    this.s.form = f;
    this.s.tank = ev.tank;
    this.s.audit = null;
    this.s.tab = "log";
    this._render();
    this._hass
      .callWS({ type: "aquarium_tracker/audit", event_id: id })
      .then((a) => {
        this.s.audit = a;
        this._render();
      })
      .catch(() => {});
  }
  _payload(f) {
    switch (f.type) {
      case "measurement":
        return { values: Object.fromEntries(f.params.map((p) => [p, num(f.values[p] ?? "")])) };
      case "dose":
        return { solution: f.solution, volume_ml: num(f.volume_ml) };
      case "water_change":
        return { percent: num(f.percent) };
      case "fertilize":
        return { doses: Object.fromEntries(Object.entries(f.doses).map(([k, v]) => [k, num(v)])) };
      case "maintenance":
        return { action: f.action };
      default:
        return {};
    }
  }
  async _save() {
    const f = this.s.form;
    if (f.type === "measurement" && f.params.some((p) => (f.values[p] ?? "") === "")) {
      this._toast("Seçili parametrelerin hepsine değer gir (ya da seçimi kaldır)", true);
      return;
    }
    if (f.type === "maintenance" && !f.action) {
      this._toast("Bakım işi seç", true);
      return;
    }
    const ts = f.tsTouched ? new Date(f.ts).toISOString() : null;
    const msg = f.id
      ? { type: "aquarium_tracker/update_event", event_id: f.id, payload: this._payload(f), note: f.note, ts }
      : { type: "aquarium_tracker/add_event", tank: f.tank, event_type: f.type, payload: this._payload(f), note: f.note, ts };
    const r = await this._ws(msg, f.id ? "Güncellendi" : "Kaydedildi");
    if (r !== undefined) {
      this.s.form = f.id ? null : this._newForm(f.tank, f.type);
      if (f.id) this.s.tab = "dashboard";
      this._render();
    }
  }
  async _delete(id) {
    if (!confirm("Bu kayıt silinsin mi?")) return;
    await this._ws({ type: "aquarium_tracker/delete_event", event_id: id }, "Silindi");
    this.s.form = null;
    this.s.tab = "dashboard";
    this._render();
  }

  // ------------------------------------------------------------ DOM
  _build() {
    this.shadowRoot.innerHTML = `<style>${CSS}</style>
      <div class="toolbar"><span id="menu"></span><div class="title">Akvaryum</div></div>
      <div id="tabs" class="tabs"></div>
      <div id="main" class="main"></div>
      <div id="toast"></div>`;
    this._menu = document.createElement("ha-menu-button");
    this._menu.hass = this._hass;
    this._menu.narrow = this._narrow;
    this.shadowRoot.getElementById("menu").appendChild(this._menu);
    const root = this.shadowRoot;
    root.addEventListener("click", (e) => this._onClick(e));
    root.addEventListener("input", (e) => this._onInput(e));
    root.addEventListener("change", (e) => this._onChange(e));
  }

  _onClick(e) {
    const el = e.target.closest("[data-act]");
    if (!el) return;
    const { act, v, w } = el.dataset;
    const s = this.s;
    switch (act) {
      case "tab":
        s.tab = v;
        if (v === "log" && !s.form) s.form = this._newForm(s.tank, "measurement");
        break;
      case "tank":
        s.tank = v;
        store("tank", v);
        s.point = null;
        if (s.form && !s.form.id) s.form = this._newForm(v, s.form.type);
        break;
      case "label":
        s.hidden.has(v) ? s.hidden.delete(v) : s.hidden.add(v);
        store("hidden", [...s.hidden]);
        break;
      case "cparam":
        this._toggleChart("params", v);
        break;
      case "cmarker":
        this._toggleChart("markers", v);
        break;
      case "point": {
        const p = this.s.point;
        this.s.point = p && p.id === Number(v) && p.p === w ? null : { id: Number(v), p: w };
        break;
      }
      case "range":
        s.range = Number(v);
        s.point = null;
        store("range", s.range);
        break;
      case "type":
        if (!s.form.id) s.form = this._newForm(s.form.tank, v);
        break;
      case "action":
        s.form.action = v;
        break;
      case "param": {
        const ps = s.form.params;
        s.form.params = ps.includes(v) ? ps.filter((p) => p !== v) : [...ps, v];
        break;
      }
      case "save":
        this._save();
        return;
      case "cancel":
        s.form = this._newForm(s.tank, "measurement");
        s.tab = "dashboard";
        break;
      case "delete":
        this._delete(Number(v));
        return;
      case "edit":
        this._editEvent(Number(v));
        return;
      case "open-task":
        this._openForm(v, w);
        return;
      case "snooze":
        this._ws({ type: "aquarium_tracker/snooze", tank: v, task: w }, "Ertelendi");
        return;
      case "mode": {
        const t = this.tank(v);
        if (confirm(`${t.name}: ${MODE_NAMES[w]} moduna geçilsin mi?`))
          this._ws({ type: "aquarium_tracker/set_mode", tank: v, mode: w }, "Mod değişti");
        return;
      }
      case "reload":
        this._ws({ type: "aquarium_tracker/reload" }, "Config yeniden yüklendi");
        return;
      default:
        return;
    }
    this._render();
  }

  _onInput(e) {
    const el = e.target;
    const f = this.s.form;
    if (!f || !el.dataset.f) return;
    const k = el.dataset.f;
    if (k === "value") f.values[el.dataset.p] = el.value;
    else if (k === "dose") f.doses[el.dataset.p] = el.value;
    else {
      f[k] = el.value;
      if (k === "ts") f.tsTouched = true;
    }
    if (k === "volume_ml" || k === "solution") this._updateDoseCalc();
  }
  _onChange(e) {
    if (e.target.dataset.f === "solution") {
      this.s.form.solution = e.target.value;
      this._updateDoseCalc();
    }
  }
  _updateDoseCalc() {
    const span = this.shadowRoot.getElementById("dosecalc");
    if (!span) return;
    const f = this.s.form;
    const t = this.tank(f.tank);
    const sol = t?.stock_solutions.find((x) => x.key === f.solution);
    const v = parseFloat(num(String(f.volume_ml)));
    span.textContent = sol && v > 0 ? `≈ +${fmtNum((v * sol.nh4_mg_per_ml) / t.net_volume_l)} mg/L NH4` : "";
  }

  _render() {
    if (!this._built) {
      if (!this.shadowRoot.getElementById("main")) return;
      this._built = true;
    }
    const s = this.s;
    const tabs = [
      ["dashboard", "Panel"],
      ["log", "Kayıt"],
      ["tasks", "Görevler"],
    ];
    this.shadowRoot.getElementById("tabs").innerHTML = tabs
      .map(([k, n]) => `<button class="tab ${s.tab === k ? "on" : ""}" data-act="tab" data-v="${k}">${n}</button>`)
      .join("");
    const main = this.shadowRoot.getElementById("main");
    if (s.loading) main.innerHTML = `<div class="card">Yükleniyor…</div>`;
    else if (s.error && !s.snap)
      main.innerHTML = `<div class="card err">Aquarium Tracker yüklenemedi: ${esc(s.error)}<br><br>
        tanks.yaml'ı kontrol et, sonra <button class="btn" data-act="reload">Config'i yeniden yükle</button></div>`;
    else if (s.tab === "dashboard") main.innerHTML = this._renderDashboard();
    else if (s.tab === "log") main.innerHTML = this._renderForm();
    else main.innerHTML = this._renderTasks();
    this._updateDoseCalc();
    this.shadowRoot.getElementById("toast").innerHTML = s.toast
      ? `<div class="toast ${s.toast.err ? "err" : ""}">${esc(s.toast.msg)}</div>`
      : "";
  }

  _tankChips() {
    return `<div class="chips">${this.s.snap.tanks
      .map(
        (t) =>
          `<button class="chip ${t.key === this.s.tank ? "on" : ""}" data-act="tank" data-v="${t.key}">${esc(t.name)}</button>`
      )
      .join("")}</div>`;
  }

  // ------------------------------------------------------------ dashboard
  _renderDashboard() {
    const t = this.tank();
    if (!t) return "";
    const s = this.s;
    const latest = t.parameters
      .filter((p) => t.latest[p])
      .map(
        (p) => `<div class="val"><div class="vk">${esc(p)}</div>
          <div class="vv">${fmtNum(t.latest[p].value)} <span class="u">${esc(this.unit(p))}</span></div>
          ${p === "NH4" && t.latest[p].NH3_free !== undefined ? `<div class="vs">NH3 ${fmtNum(t.latest[p].NH3_free)}</div>` : ""}
          <div class="vs">${ago(t.latest[p].ts)}</div></div>`
      )
      .join("");
    const cyc =
      t.cycle && t.mode === "cycling"
        ? `<div class="row"><span class="badge" style="background:${LABEL_COLORS.dose}">${PHASE_NAMES[t.cycle.phase]}</span>
           <span>Doğrulanan doz: <b>${t.cycle.passes}/${t.cycle.required}</b></span>
           <span class="muted">Sıradaki ölçüm: ${t.cycle.params.join(" + ")}</span></div>`
        : "";
    const otherMode = t.mode === "cycling" ? "maintenance" : "cycling";
    const next = t.tasks
      .filter((x) => x.active && x.due)
      .sort((a, b) => a.due.localeCompare(b.due))
      .map((x) => this._taskRow(x, false))
      .join("");

    const tankEvents = s.events.filter((e) => e.tank === t.key);
    const startMs = s.range ? Date.now() - s.range * 86400000 : Math.min(...tankEvents.map((e) => Date.parse(e.ts)), Date.now() - 86400000);
    const inRange = tankEvents.filter((e) => Date.parse(e.ts) >= startMs);
    const cfg = this._chartCfg(t);

    // chart: parameter chips (only parameters that have data) + marker chips
    const measured = new Set(tankEvents.filter((e) => e.type === "measurement").flatMap((e) => Object.keys(e.payload.values || {})));
    const paramChips = t.parameters
      .filter((p) => measured.has(p))
      .map((p) => {
        const on = cfg.params.includes(p);
        return `<button class="chip lbl ${on ? "on" : ""}" style="--c:${this.paramColor(p)}" data-act="cparam" data-v="${p}">${esc(p)}</button>`;
      })
      .join("");
    const markerChips = MARKER_TYPES.filter(
      (k) => (k !== "dose" || t.stock_solutions.length) && (k !== "fertilize" || t.fertilizers.length)
    )
      .map((k) => {
        const on = cfg.markers.includes(k);
        return `<button class="chip lbl ${on ? "on" : ""}" style="--c:${this.labelColor(k)}" data-act="cmarker" data-v="${k}">${esc(this.labelName(k))}</button>`;
      })
      .join("");

    // list: label chips filter only the list
    const labelsPresent = new Set(tankEvents.map((e) => e.label));
    const listChips = s.snap.labels
      .filter((l) => labelsPresent.has(l.key) || (USER_TYPES.includes(l.key) && l.key !== "maintenance"))
      .map((l) => {
        const off = s.hidden.has(l.key);
        return `<button class="chip lbl ${off ? "" : "on"}" style="--c:${this.labelColor(l.key)}" data-act="label" data-v="${l.key}">${esc(l.name)}</button>`;
      })
      .join("");
    const listed = inRange.filter((e) => !s.hidden.has(e.label));
    const ranges = [
      [7, "7 g"],
      [30, "30 g"],
      [90, "90 g"],
      [0, "Tümü"],
    ]
      .map(([v, n]) => `<button class="chip ${s.range === v ? "on" : ""}" data-act="range" data-v="${v}">${n}</button>`)
      .join("");

    return `${this._tankChips()}
      <div class="card">
        <div class="row between"><div><b>${esc(t.name)}</b> <span class="muted">${fmtNum(t.net_volume_l)} L</span></div>
          <span class="badge">${MODE_NAMES[t.mode]}</span></div>
        ${cyc}
        ${latest ? `<div class="vals">${latest}</div>` : `<div class="muted">Henüz ölçüm yok</div>`}
        ${next ? `<div class="sub">Görevler</div>${next}` : ""}
        <div class="row end"><button class="btn ghost" data-act="mode" data-v="${t.key}" data-w="${otherMode}">${MODE_NAMES[otherMode]} moduna geç</button></div>
      </div>
      <div class="card">
        <div class="chips">${ranges}</div>
        ${paramChips ? `<div class="sub">Parametreler</div><div class="chips">${paramChips}</div>` : ""}
        ${markerChips ? `<div class="sub">Grafikte göster</div><div class="chips">${markerChips}</div>` : ""}
        ${this._renderChart(t, inRange, startMs, cfg)}
        ${this._renderStrip(inRange, startMs)}
      </div>
      <div class="card">
        <div class="sub" style="margin-top:0">Listede göster</div><div class="chips">${listChips}</div>
        ${this._renderTimeline(listed)}
      </div>`;
  }

  // Chart selection per tank and mode, remembered on this device
  _chartCfg(t) {
    const key = `chart_${t.key}_${t.mode}`;
    const saved = store(key);
    if (saved && Array.isArray(saved.params) && Array.isArray(saved.markers)) return saved;
    const params = (t.mode === "cycling" ? ["NH4", "NO2"] : ["NO3", "PO4"]).filter((p) => t.parameters.includes(p));
    const markers = t.mode === "cycling" ? ["water_change", "dose"] : ["water_change"];
    return { params, markers };
  }
  _toggleChart(kind, v) {
    const t = this.tank();
    const cfg = this._chartCfg(t);
    const arr = cfg[kind];
    cfg[kind] = arr.includes(v) ? arr.filter((x) => x !== v) : [...arr, v];
    store(`chart_${t.key}_${t.mode}`, cfg);
    this.s.point = null;
  }
  paramColor(p) {
    return PARAM_COLORS[p] || this.labelColor(`p_${p}`);
  }

  // One chart. One parameter: real axis. Several: each normalised to its own range.
  _renderChart(t, events, startMs, cfg) {
    const meas = events.filter((e) => e.type === "measurement").sort((a, b) => a.ts.localeCompare(b.ts));
    const series = cfg.params
      .map((p) => ({
        p,
        pts: meas
          .filter((e) => e.payload.values && e.payload.values[p] !== undefined)
          .map((e) => ({ x: Date.parse(e.ts), y: Number(e.payload.values[p]), id: e.id, ts: e.ts })),
      }))
      .filter((sr) => sr.pts.length);
    if (!meas.length) return `<div class="muted pad">Bu aralıkta ölçüm yok</div>`;
    if (!series.length) return `<div class="muted pad">Grafikte görmek için parametre seç</div>`;

    const W = 400, H = 210, L = series.length === 1 ? 38 : 10, R = 10, T = 18, B = 22;
    const x0 = Math.min(startMs, ...series.flatMap((sr) => sr.pts.map((q) => q.x)));
    const x1 = Date.now();
    const sx = (x) => L + ((x - x0) / Math.max(x1 - x0, 1)) * (W - L - R);
    const domain = (p, ys) => {
      if (ZERO_BASED.has(p)) return [0, Math.max(...ys) * 1.15 || 1];
      const lo = Math.min(...ys), hi = Math.max(...ys), pd = Math.max((hi - lo) * 0.15, 0.3);
      return [lo - pd, hi + pd];
    };
    const syN = (f) => T + (1 - f) * (H - T - B); // f in 0..1
    const single = series.length === 1;

    let grid = "";
    if (single) {
      const [y0, y1] = domain(series[0].p, series[0].pts.map((q) => q.y));
      grid = [0, 0.5, 1]
        .map((f) => `<line x1="${L}" x2="${W - R}" y1="${syN(f)}" y2="${syN(f)}" class="grid"/>
          <text x="${L - 4}" y="${syN(f) + 3}" class="axis" text-anchor="end">${fmtNum(+(y0 + f * (y1 - y0)).toFixed(2))}</text>`)
        .join("");
    } else {
      grid = [0, 0.5, 1].map((f) => `<line x1="${L}" x2="${W - R}" y1="${syN(f)}" y2="${syN(f)}" class="grid"/>`).join("");
    }

    // vertical markers with a short label, skipping labels that would overlap
    let lastLabelX = -1e9;
    const mk = events
      .filter((e) => cfg.markers.includes(e.type) && Date.parse(e.ts) >= x0)
      .sort((a, b) => a.ts.localeCompare(b.ts))
      .map((m) => {
        const x = sx(Date.parse(m.ts));
        const c = this.labelColor(m.label);
        let txt = "";
        if (x - lastLabelX > 26) {
          txt = `<text x="${x}" y="${T - 5}" class="mkt" fill="${c}" text-anchor="middle">${MARKER_SHORT[m.type]}</text>`;
          lastLabelX = x;
        }
        return `<line x1="${x}" x2="${x}" y1="${T}" y2="${H - B}" stroke="${c}" class="mk"/>${txt}`;
      })
      .join("");

    const lines = series
      .map((sr) => {
        const [y0, y1] = domain(sr.p, sr.pts.map((q) => q.y));
        const sy = (y) => syN((y - y0) / (y1 - y0));
        const c = this.paramColor(sr.p);
        const sel = this.s.point;
        const poly = sr.pts.length > 1
          ? `<polyline points="${sr.pts.map((q) => `${sx(q.x).toFixed(1)},${sy(q.y).toFixed(1)}`).join(" ")}" class="ln" stroke="${c}"/>`
          : "";
        const dots = sr.pts
          .map((q) => {
            const on = sel && sel.id === q.id && sel.p === sr.p;
            return `<circle cx="${sx(q.x).toFixed(1)}" cy="${sy(q.y).toFixed(1)}" r="${on ? 6 : 4}" fill="${c}" class="dot"/>
              <circle cx="${sx(q.x).toFixed(1)}" cy="${sy(q.y).toFixed(1)}" r="12" class="hit" data-act="point" data-v="${q.id}" data-w="${sr.p}"/>`;
          })
          .join("");
        return poly + dots;
      })
      .join("");

    const d = (ms) => new Date(ms).toLocaleDateString("tr-TR", { day: "2-digit", month: "short" });
    let info = `<div class="muted small">${single ? "" : "Normalize: her parametre kendi aralığında · "}Değeri görmek için noktaya dokun</div>`;
    const sel = this.s.point;
    if (sel) {
      const ev = this.s.events.find((e) => e.id === sel.id);
      if (ev) {
        const vals = Object.entries(ev.payload.values || {})
          .filter(([k]) => cfg.params.includes(k))
          .map(([k, v]) => `<span style="color:${this.paramColor(k)}"><b>${esc(k)}</b> ${fmtNum(v)} ${esc(this.unit(k))}</span>`)
          .join(" · ");
        info = `<div class="ptinfo">${fmtDT(ev.ts)} · ${vals} <button class="btn ghost sm" data-act="edit" data-v="${ev.id}">Düzenle</button></div>`;
      }
    }
    return `<svg class="chart1" viewBox="0 0 ${W} ${H}">${grid}${mk}${lines}
        <text x="${L}" y="${H - 6}" class="axis">${d(x0)}</text>
        <text x="${(L + W - R) / 2}" y="${H - 6}" class="axis" text-anchor="middle">${d((x0 + x1) / 2)}</text>
        <text x="${W - R}" y="${H - 6}" class="axis" text-anchor="end">${d(x1)}</text></svg>${info}`;
  }

  // One row per action label; every event is a dot on a shared time axis.
  _renderStrip(events, startMs) {
    const acts = events.filter((e) => e.type !== "system");
    if (!acts.length) return "";
    const x0 = Math.min(startMs, ...acts.map((e) => Date.parse(e.ts)));
    const x1 = Date.now();
    const pos = (ts) => (((Date.parse(ts) - x0) / Math.max(x1 - x0, 1)) * 100).toFixed(2);
    const order = this.s.snap.labels.map((l) => l.key);
    const labels = [...new Set(acts.map((e) => e.label))].sort((a, b) => order.indexOf(a) - order.indexOf(b));
    const rows = labels
      .map((lb) => {
        const dots = acts
          .filter((e) => e.label === lb)
          .map((e) => {
            const edit = USER_TYPES.includes(e.type) ? `data-act="edit" data-v="${e.id}"` : "";
            return `<span class="sdot" style="left:${pos(e.ts)}%" ${edit} title="${esc(fmtDT(e.ts) + " · " + this._summary(e))}"></span>`;
          })
          .join("");
        return `<div class="srow" style="--c:${this.labelColor(lb)}"><span class="slabel">${esc(this.labelName(lb))}</span>
          <span class="strack">${dots}</span></div>`;
      })
      .join("");
    const d = (ms) => new Date(ms).toLocaleDateString("tr-TR", { day: "2-digit", month: "short" });
    return `<div class="sub">Zaman çizelgesi</div><div class="strip">${rows}
      <div class="saxis"><span>${d(x0)}</span><span>${d((x0 + x1) / 2)}</span><span>${d(x1)}</span></div></div>`;
  }

  _summary(e) {
    const p = e.payload || {};
    const t = this.tank(e.tank);
    switch (e.type) {
      case "dose":
        return `${p.solution_name || p.solution}: ${fmtNum(p.volume_ml)} mL → +${fmtNum(p.nh4_mg_l)} mg/L NH4`;
      case "measurement": {
        const vals = Object.entries(p.values || {})
          .map(([k, v]) => `${k} ${fmtNum(v)}${this.unit(k) ? " " + this.unit(k) : ""}`)
          .join(" · ");
        const nh3 = p.derived?.NH3_free !== undefined ? ` (serbest NH3 ${fmtNum(p.derived.NH3_free)} mg/L)` : "";
        return vals + nh3;
      }
      case "water_change":
        return p.percent ? `%${fmtNum(p.percent)} (${fmtNum(p.volume_l)} L)` : "Su değiştirildi";
      case "maintenance":
        return "Yapıldı";
      case "fertilize":
        return Object.entries(p.doses || {})
          .map(([k, v]) => `${t?.fertilizers.find((f) => f.key === k)?.name || k} ${fmtNum(v)} ${p.units?.[k] || ""}`)
          .join(" · ");
      case "entity":
        return `${p.name || p.entity_id}: ${p.from ?? "?"} → ${p.to}`;
      case "system":
        if (p.kind === "mode") return `Mod: ${MODE_NAMES[p.from] || p.from} → ${MODE_NAMES[p.to] || p.to}`;
        if (p.kind === "cycle_complete") return `Cycle tamamlandı (${p.passes} ardışık doz)`;
        return p.kind || "";
      default:
        return "";
    }
  }

  _renderTimeline(events) {
    if (!events.length) return `<div class="muted">Kayıt yok</div>`;
    const sorted = [...events].sort((a, b) => b.ts.localeCompare(a.ts) || b.id - a.id);
    let day = "";
    const rows = [];
    for (const e of sorted) {
      const d = new Date(e.ts);
      const dk = d.toDateString();
      if (dk !== day) {
        day = dk;
        rows.push(`<div class="day">${fmtDay(d)}</div>`);
      }
      const editable = USER_TYPES.includes(e.type);
      rows.push(`<div class="ev" ${editable ? `data-act="edit" data-v="${e.id}"` : ""}>
        <span class="tm">${pad(d.getHours())}:${pad(d.getMinutes())}</span>
        <span class="lb" style="--c:${this.labelColor(e.label)}">${esc(this.labelName(e.label))}</span>
        <span class="tx">${esc(this._summary(e))}${e.note ? `<span class="note">${esc(e.note)}</span>` : ""}</span>
        ${!editable ? `<button class="x" data-act="delete" data-v="${e.id}" title="Sil">×</button>` : ""}
      </div>`);
    }
    return `<div class="timeline">${rows.join("")}</div>`;
  }

  // ------------------------------------------------------------ form view
  _renderForm() {
    const s = this.s;
    if (!s.form) s.form = this._newForm(s.tank, "measurement");
    const f = s.form;
    const t = this.tank(f.tank);
    if (!t) return "";
    const types = USER_TYPES.filter(
      (k) =>
        (k !== "dose" || t.stock_solutions.length) &&
        (k !== "fertilize" || t.fertilizers.length) &&
        (k !== "maintenance" || t.maintenance_actions.length)
    );
    const typeChips = types
      .map(
        (k) =>
          `<button class="chip lbl ${f.type === k ? "on" : ""}" style="--c:${this.labelColor(k)}" data-act="type" data-v="${k}" ${
            f.id && f.type !== k ? "disabled" : ""
          }>${esc(this.labelName(k))}</button>`
      )
      .join("");
    let fields = "";
    if (f.type === "measurement") {
      const chips = t.parameters
        .map((p) => `<button class="chip ${f.params.includes(p) ? "on" : ""}" data-act="param" data-v="${p}">${esc(p)}</button>`)
        .join("");
      const inputs = f.params
        .map(
          (p) => `<label class="fld"><span>${esc(p)} <span class="muted">${esc(this.unit(p))}</span></span>
            <input type="text" inputmode="decimal" data-f="value" data-p="${p}" value="${esc(f.values[p] ?? "")}"></label>`
        )
        .join("");
      fields = `<div class="sub">Parametreler</div><div class="chips">${chips}</div><div class="grid2">${inputs}</div>`;
    } else if (f.type === "dose") {
      const opts = t.stock_solutions
        .map((x) => `<option value="${x.key}" ${x.key === f.solution ? "selected" : ""}>${esc(x.name)}</option>`)
        .join("");
      fields = `<label class="fld"><span>Solüsyon</span><select data-f="solution">${opts}</select></label>
        <label class="fld"><span>Hacim (mL)</span><input type="text" inputmode="decimal" data-f="volume_ml" value="${esc(f.volume_ml)}"></label>
        <div class="calc" id="dosecalc"></div>`;
    } else if (f.type === "water_change") {
      fields = `<label class="fld"><span>Değişim (%) <span class="muted">opsiyonel · ${fmtNum(t.net_volume_l)} L tank</span></span>
        <input type="text" inputmode="decimal" data-f="percent" value="${esc(f.percent)}"></label>`;
    } else if (f.type === "maintenance") {
      fields = `<div class="sub">Yapılan iş</div><div class="chips">${t.maintenance_actions
        .map(
          (a) =>
            `<button class="chip lbl ${f.action === a.key ? "on" : ""}" style="--c:${this.labelColor(a.key)}" data-act="action" data-v="${a.key}">${esc(a.name)}</button>`
        )
        .join("")}</div>`;
    } else if (f.type === "fertilize") {
      fields = `<div class="grid2">${t.fertilizers
        .map(
          (fz) => `<label class="fld"><span>${esc(fz.name)} <span class="muted">${esc(fz.unit)}</span></span>
          <input type="text" inputmode="decimal" data-f="dose" data-p="${fz.key}" value="${esc(f.doses[fz.key] ?? "")}"></label>`
        )
        .join("")}</div>`;
    }
    const audit =
      f.id && s.audit && s.audit.length > 1
        ? `<div class="sub">Değişiklik geçmişi</div>${s.audit
            .map((a) => `<div class="muted small">${fmtDT(a.at)} · ${{ create: "oluşturuldu", update: "düzenlendi", delete: "silindi" }[a.action]}${
              a.action === "update" ? ` (önceki: ${esc(this._summary(a.old))})` : ""
            }</div>`)
            .join("")}`
        : "";
    return `${f.id ? "" : this._tankChips()}
      <div class="card">
        ${f.id ? `<div class="row between"><b>Kaydı düzenle · ${esc(t.name)}</b></div>` : ""}
        <div class="chips">${typeChips}</div>
        ${fields}
        <label class="fld"><span>Zaman</span><input type="datetime-local" data-f="ts" value="${esc(f.ts)}"></label>
        <label class="fld"><span>Not</span><textarea rows="2" data-f="note">${esc(f.note)}</textarea></label>
        <div class="row end">
          ${f.id ? `<button class="btn danger" data-act="delete" data-v="${f.id}">Sil</button>` : ""}
          ${f.id ? `<button class="btn ghost" data-act="cancel">Vazgeç</button>` : ""}
          <button class="btn" data-act="save">${f.id ? "Güncelle" : "Kaydet"}</button>
        </div>
        ${audit}
      </div>`;
  }

  // ------------------------------------------------------------ tasks view
  _taskRow(x, showTank) {
    const t = this.tank(x.tank);
    const st = x.status;
    const params = x.params?.length ? ` · ${x.params.join(", ")}` : x.action_name ? ` · ${x.action_name}` : "";
    return `<div class="task ${st}">
      <div class="tinfo"><b>${showTank ? esc(t.name) + " · " : ""}${esc(x.title)}</b>
        <div class="muted small">${x.due ? `${fmtDT(x.due)} (${until(x.due)})` : ""}${esc(params)}</div></div>
      <span class="badge st-${st}">${STATUS_NAMES[st]}</span>
      ${x.active ? `<div class="tact">
        ${st === "due" ? `<button class="btn ghost sm" data-act="snooze" data-v="${x.tank}" data-w="${x.task}">Ertele</button>` : ""}
        <button class="btn sm" data-act="open-task" data-v="${x.tank}" data-w="${x.task}">Kaydet</button></div>` : ""}
    </div>`;
  }
  _renderTasks() {
    const all = this.s.snap.tanks.flatMap((t) => t.tasks);
    const order = { due: 0, snoozed: 1, upcoming: 2, inactive: 3 };
    all.sort((a, b) => order[a.status] - order[b.status] || String(a.due).localeCompare(String(b.due)));
    return `<div class="card">${all.length ? all.map((x) => this._taskRow(x, true)).join("") : `<div class="muted">Görev yok</div>`}</div>
      <div class="card"><div class="row between"><span class="muted small">Ayarlar: /config/aquarium_tracker/tanks.yaml</span>
        <button class="btn ghost sm" data-act="reload">Config'i yeniden yükle</button></div></div>`;
  }
}

const CSS = `
:host { display:block; min-height:100vh; background:var(--primary-background-color); color:var(--primary-text-color);
  font-family:var(--paper-font-body1_-_font-family, Roboto, sans-serif); }
.toolbar { display:flex; align-items:center; height:56px; padding:0 8px; background:var(--app-header-background-color, var(--primary-color));
  color:var(--app-header-text-color, #fff); }
.title { font-size:20px; margin-left:8px; }
.tabs { display:flex; gap:4px; padding:8px 12px 0; background:var(--app-header-background-color, var(--primary-color)); }
.tab { flex:1; max-width:160px; border:0; background:transparent; color:var(--app-header-text-color,#fff); opacity:.7;
  padding:10px; font-size:15px; border-bottom:3px solid transparent; cursor:pointer; }
.tab.on { opacity:1; border-bottom-color:var(--app-header-text-color,#fff); }
.main { max-width:900px; margin:0 auto; padding:12px; padding-bottom:80px; }
.card { background:var(--card-background-color); border-radius:12px; padding:14px; margin-bottom:12px;
  box-shadow:var(--ha-card-box-shadow, 0 1px 3px rgba(0,0,0,.15)); }
.card.err, .err { color:var(--error-color); }
.row { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin:6px 0; }
.between { justify-content:space-between; } .end { justify-content:flex-end; }
.muted { color:var(--secondary-text-color); } .small { font-size:12px; } .pad { padding:8px 0; }
.sub { font-size:12px; text-transform:uppercase; letter-spacing:.05em; color:var(--secondary-text-color); margin:14px 0 6px; }
.chips { display:flex; gap:6px; flex-wrap:wrap; margin:6px 0 10px; }
.chip { border:1px solid var(--divider-color); background:transparent; color:var(--primary-text-color); border-radius:16px;
  padding:6px 12px; font-size:13px; cursor:pointer; }
.chip.on { background:var(--primary-color); border-color:var(--primary-color); color:var(--text-primary-color,#fff); }
.chip.lbl { border-color:var(--c); color:var(--c); opacity:.55; }
.chip.lbl.on { background:var(--c); color:#fff; opacity:1; }
.chip:disabled { opacity:.2; }
.badge { font-size:12px; padding:3px 8px; border-radius:10px; background:var(--secondary-background-color); color:var(--primary-text-color); }
.badge[style] { color:#fff; }
.st-due { background:var(--error-color); color:#fff; } .st-snoozed { background:var(--warning-color); color:#fff; }
.vals { display:grid; grid-template-columns:repeat(auto-fill,minmax(92px,1fr)); gap:8px; margin-top:10px; }
.val { background:var(--secondary-background-color); border-radius:8px; padding:8px; }
.vk { font-size:12px; color:var(--secondary-text-color); } .vv { font-size:18px; font-weight:500; } .u { font-size:11px; color:var(--secondary-text-color); }
.vs { font-size:11px; color:var(--secondary-text-color); }
.task { display:flex; align-items:center; gap:8px; padding:8px 0; border-bottom:1px solid var(--divider-color); flex-wrap:wrap; }
.task:last-child { border-bottom:0; } .tinfo { flex:1; min-width:160px; } .tact { display:flex; gap:6px; }
.btn { border:0; border-radius:8px; padding:9px 16px; background:var(--primary-color); color:var(--text-primary-color,#fff); font-size:14px; cursor:pointer; }
.btn.sm { padding:6px 10px; font-size:13px; }
.btn.ghost { background:transparent; color:var(--primary-color); border:1px solid var(--primary-color); }
.btn.danger { background:var(--error-color); }
.fld { display:flex; flex-direction:column; gap:4px; margin:10px 0; font-size:13px; }
.fld input, .fld select, .fld textarea { font-size:16px; padding:9px; border-radius:8px; border:1px solid var(--divider-color);
  background:var(--secondary-background-color); color:var(--primary-text-color); font-family:inherit; }
.grid2 { display:grid; grid-template-columns:repeat(auto-fill,minmax(120px,1fr)); gap:0 10px; }
.calc { font-size:14px; color:var(--primary-color); min-height:18px; }
.grid { stroke:var(--divider-color); stroke-width:1; }
.axis { font-size:11px; fill:var(--secondary-text-color); }
.chart1 { width:100%; height:auto; display:block; margin-top:4px; }
.ln { fill:none; stroke-width:2; }
.dot { pointer-events:none; }
.hit { fill:transparent; cursor:pointer; }
.mk { stroke-width:1.2; stroke-dasharray:4 3; opacity:.85; }
.mkt { font-size:9px; font-weight:600; }
.ptinfo { display:flex; flex-wrap:wrap; align-items:center; gap:6px; font-size:13px; margin-top:4px; }
.timeline .day { font-size:12px; color:var(--secondary-text-color); margin:14px 0 4px; font-weight:500; }
.timeline .day:first-child { margin-top:0; }
.ev { display:flex; align-items:flex-start; gap:8px; padding:7px 4px; border-radius:6px; cursor:pointer; }
.ev:hover { background:var(--secondary-background-color); }
.tm { font-size:13px; color:var(--secondary-text-color); width:40px; flex-shrink:0; padding-top:2px; }
.lb { font-size:11px; color:#fff; background:var(--c); border-radius:8px; padding:3px 7px; flex-shrink:0; }
.tx { font-size:14px; flex:1; } .note { display:block; font-size:12px; color:var(--secondary-text-color); font-style:italic; }
.x { border:0; background:transparent; color:var(--secondary-text-color); font-size:18px; cursor:pointer; }
.strip { margin-top:4px; }
.srow { display:flex; align-items:center; height:28px; }
.slabel { width:104px; flex-shrink:0; font-size:11px; color:var(--secondary-text-color); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; padding-right:6px; }
.strack { flex:1; position:relative; height:100%; margin-right:8px; }
.strack::before { content:""; position:absolute; left:0; right:0; top:50%; border-top:1px dashed var(--divider-color); }
.sdot { position:absolute; top:50%; width:12px; height:12px; border-radius:50%; transform:translate(-50%,-50%);
  background:var(--c); border:2px solid var(--card-background-color); cursor:pointer; box-sizing:border-box; }
.saxis { display:flex; justify-content:space-between; margin:2px 8px 0 104px; font-size:11px; color:var(--secondary-text-color); }
.toast { position:fixed; left:50%; bottom:24px; transform:translateX(-50%); background:#323232; color:#fff; padding:10px 18px;
  border-radius:8px; font-size:14px; z-index:10; } .toast.err { background:var(--error-color); }
`;

customElements.define("aquarium-tracker-panel", AquariumTrackerPanel);
