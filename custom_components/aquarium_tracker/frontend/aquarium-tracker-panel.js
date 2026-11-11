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

const esc = (v) =>
  String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const pad = (n) => String(n).padStart(2, "0");
const toLocalInput = (d) =>
  `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
const fmtDT = (iso) =>
  new Date(iso).toLocaleString("tr-TR", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
const fmtDay = (d) => d.toLocaleDateString("tr-TR", { weekday: "short", day: "2-digit", month: "long", year: "numeric" });
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
        return { values: Object.fromEntries(f.params.map((p) => [p, f.values[p] ?? ""])) };
      case "dose":
        return { solution: f.solution, volume_ml: f.volume_ml };
      case "water_change":
        return { percent: f.percent };
      case "fertilize":
        return { doses: f.doses };
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
        if (s.form && !s.form.id) s.form = this._newForm(v, s.form.type);
        break;
      case "label":
        s.hidden.has(v) ? s.hidden.delete(v) : s.hidden.add(v);
        store("hidden", [...s.hidden]);
        break;
      case "range":
        s.range = Number(v);
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
    const v = parseFloat(String(f.volume_ml).replace(",", "."));
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
    const visible = inRange.filter((e) => !s.hidden.has(e.label));
    const labelsPresent = new Set(tankEvents.map((e) => e.label));
    const labelChips = s.snap.labels
      .filter((l) => labelsPresent.has(l.key) || (USER_TYPES.includes(l.key) && l.key !== "maintenance"))
      .map((l) => {
        const off = s.hidden.has(l.key);
        return `<button class="chip lbl ${off ? "" : "on"}" style="--c:${this.labelColor(l.key)}" data-act="label" data-v="${l.key}">${esc(l.name)}</button>`;
      })
      .join("");
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
        <div class="chips">${labelChips}</div>
        <div class="chips">${ranges}</div>
        ${this._renderCharts(t, visible, startMs)}
      </div>
      <div class="card">${this._renderTimeline(visible)}</div>`;
  }

  _renderCharts(t, events, startMs) {
    if (this.s.hidden.has("measurement")) return "";
    const meas = events.filter((e) => e.type === "measurement").sort((a, b) => a.ts.localeCompare(b.ts));
    const markers = events.filter((e) => ["dose", "water_change", "fertilize"].includes(e.type));
    const out = [];
    for (const p of t.parameters) {
      const pts = meas
        .filter((e) => e.payload.values && e.payload.values[p] !== undefined)
        .map((e) => ({ x: Date.parse(e.ts), y: Number(e.payload.values[p]), id: e.id }));
      if (!pts.length) continue;
      out.push(this._chart(p, pts, markers, startMs));
    }
    return out.length ? `<div class="charts">${out.join("")}</div>` : `<div class="muted pad">Bu aralıkta ölçüm yok</div>`;
  }

  _chart(p, pts, markers, startMs) {
    const W = 640,
      H = 170,
      L = 40,
      R = 10,
      T = 12,
      B = 22;
    const x0 = Math.min(startMs, ...pts.map((q) => q.x));
    const x1 = Date.now();
    const ys = pts.map((q) => q.y);
    let y0, y1;
    if (ZERO_BASED.has(p)) {
      y0 = 0;
      y1 = Math.max(...ys) * 1.15 || 1;
    } else {
      const lo = Math.min(...ys),
        hi = Math.max(...ys),
        padv = Math.max((hi - lo) * 0.15, 0.3);
      y0 = lo - padv;
      y1 = hi + padv;
    }
    const sx = (x) => L + ((x - x0) / Math.max(x1 - x0, 1)) * (W - L - R);
    const sy = (y) => T + (1 - (y - y0) / (y1 - y0)) * (H - T - B);
    const grid = [0, 0.5, 1]
      .map((f) => {
        const yv = y0 + f * (y1 - y0);
        return `<line x1="${L}" x2="${W - R}" y1="${sy(yv)}" y2="${sy(yv)}" class="grid"/>
          <text x="${L - 4}" y="${sy(yv) + 4}" class="axis" text-anchor="end">${fmtNum(+yv.toFixed(2))}</text>`;
      })
      .join("");
    const mk = markers
      .filter((m) => Date.parse(m.ts) >= x0)
      .map((m) => {
        const x = sx(Date.parse(m.ts));
        return `<line x1="${x}" x2="${x}" y1="${T}" y2="${H - B}" stroke="${this.labelColor(m.label)}" class="mk"><title>${esc(
          this.labelName(m.label)
        )} · ${fmtDT(m.ts)} · ${esc(this._summary(m))}</title></line>`;
      })
      .join("");
    const line = pts.map((q) => `${sx(q.x).toFixed(1)},${sy(q.y).toFixed(1)}`).join(" ");
    const dots = pts
      .map(
        (q) =>
          `<circle cx="${sx(q.x)}" cy="${sy(q.y)}" r="4" class="dot" data-act="edit" data-v="${q.id}"><title>${p} ${fmtNum(q.y)} · ${fmtDT(
            new Date(q.x).toISOString()
          )}</title></circle>`
      )
      .join("");
    const d0 = new Date(x0).toLocaleDateString("tr-TR", { day: "2-digit", month: "short" });
    const d1 = new Date(x1).toLocaleDateString("tr-TR", { day: "2-digit", month: "short" });
    return `<div class="chart"><div class="ct">${esc(p)} <span class="muted">${esc(this.unit(p))}</span></div>
      <svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">${grid}${mk}
        <polyline points="${line}" class="ln"/>${dots}
        <text x="${L}" y="${H - 6}" class="axis">${d0}</text>
        <text x="${W - R}" y="${H - 6}" class="axis" text-anchor="end">${d1}</text></svg></div>`;
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
.charts { display:grid; grid-template-columns:repeat(auto-fill,minmax(280px,1fr)); gap:12px; }
.chart svg { width:100%; height:150px; display:block; }
.ct { font-size:13px; font-weight:500; margin-bottom:2px; }
.grid { stroke:var(--divider-color); stroke-width:1; }
.axis { font-size:11px; fill:var(--secondary-text-color); }
.ln { fill:none; stroke:${LABEL_COLORS.measurement}; stroke-width:2; vector-effect:non-scaling-stroke; }
.dot { fill:${LABEL_COLORS.measurement}; cursor:pointer; }
.mk { stroke-width:1.5; stroke-dasharray:4 3; opacity:.8; vector-effect:non-scaling-stroke; }
.timeline .day { font-size:12px; color:var(--secondary-text-color); margin:14px 0 4px; font-weight:500; }
.timeline .day:first-child { margin-top:0; }
.ev { display:flex; align-items:flex-start; gap:8px; padding:7px 4px; border-radius:6px; cursor:pointer; }
.ev:hover { background:var(--secondary-background-color); }
.tm { font-size:13px; color:var(--secondary-text-color); width:40px; flex-shrink:0; padding-top:2px; }
.lb { font-size:11px; color:#fff; background:var(--c); border-radius:8px; padding:3px 7px; flex-shrink:0; }
.tx { font-size:14px; flex:1; } .note { display:block; font-size:12px; color:var(--secondary-text-color); font-style:italic; }
.x { border:0; background:transparent; color:var(--secondary-text-color); font-size:18px; cursor:pointer; }
.toast { position:fixed; left:50%; bottom:24px; transform:translateX(-50%); background:#323232; color:#fff; padding:10px 18px;
  border-radius:8px; font-size:14px; z-index:10; } .toast.err { background:var(--error-color); }
`;

customElements.define("aquarium-tracker-panel", AquariumTrackerPanel);
