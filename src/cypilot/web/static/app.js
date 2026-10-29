// SPDX-License-Identifier: Apache-2.0
// cypilot web UI (Alpine.js and uPlot, vendored, no build step). It uses the REST API and the
// WebSocket of cypilot.web only; the WebSocket renews this client's control lease every 100 ms.
'use strict';

const SEVERITIES = ['trace', 'debug', 'info', 'notice', 'warning', 'error', 'critical', 'alert'];
const COLORS = ['#4e79a7', '#f28e2b', '#e15759', '#76b7b2', '#59a14f', '#edc948', '#b07aa1', '#ff9da7'];
const CLIENT_ID = 'web-' + Math.random().toString(16).slice(2, 10);
const RENEW_MS = 100;
const BAD_CHIPS = ['offline', 'E-stop latched', 'tripped', 'fault', 'blocked'];

function loadSaved(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; }
}
function save(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* not stored */ }
}

document.documentElement.dataset.theme = loadSaved('cypilot.theme', 'dark');

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Cypilot-Client': CLIENT_ID },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = data.detail ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : res.statusText;
    throw new ApiError(res.status, detail);
  }
  return data;
}

// uPlot instances, sample buffers and plot markers stay outside Alpine's reactivity.
const plots = {};
const buffers = {};
const markers = [];  // {at, text}: wall-clock seconds, as the subject timestamps
const MAX_MARKERS = 200;
let plotEnd = 0;  // the wall-clock time of x = 0 in the plot

function drawMarkers(u) {
  const ctx = u.ctx;
  const [x0, x1] = [u.scales.x.min, u.scales.x.max];
  ctx.save();
  ctx.strokeStyle = 'rgba(224, 122, 0, .8)';
  ctx.fillStyle = 'rgba(224, 122, 0, 1)';
  ctx.font = `${11 * devicePixelRatio}px system-ui, sans-serif`;
  ctx.lineWidth = devicePixelRatio;
  for (const m of markers) {
    const x = m.at - plotEnd;
    if (x < x0 || x > x1) continue;
    const px = Math.round(u.valToPos(x, 'x', true));
    ctx.beginPath();
    ctx.moveTo(px, u.bbox.top);
    ctx.lineTo(px, u.bbox.top + u.bbox.height);
    ctx.stroke();
    ctx.fillText(m.text, px + 3 * devicePixelRatio, u.bbox.top + 12 * devicePixelRatio);
  }
  ctx.restore();
}

function makePlot(el, labels, height) {
  const width = Math.max(280, el.clientWidth || 800);
  const opts = {
    width, height,
    scales: { x: { time: false } },
    series: [{ label: 't [s]' }, ...labels.map((l, i) => ({ label: l, stroke: COLORS[i % COLORS.length], width: 1.5, spanGaps: true, points: { show: false } }))],
    axes: [0, 1].map(() => ({ stroke: '#888', grid: { stroke: 'rgba(128,128,128,.2)' }, ticks: { stroke: 'rgba(128,128,128,.3)' } })),
    legend: { live: true },
    hooks: { draw: [drawMarkers] },
  };
  el.innerHTML = '';
  return new uPlot(opts, [[], ...labels.map(() => [])], el);
}

function download(name, text, type) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type }));
  a.download = name;
  a.click();
}

function stamp() { return new Date().toISOString().replace(/[:.]/g, '-'); }

window.cypilot = function () {
  return {
    clientId: CLIENT_ID,
    views: [
      { id: 'bus', label: 'Bus' }, { id: 'nodes', label: 'Nodes' }, { id: 'registers', label: 'Registers' },
      { id: 'control', label: 'Control' }, { id: 'plots', label: 'Plots' }, { id: 'scope', label: 'Scope' },
      { id: 'sim', label: 'Simulation' }, { id: 'runs', label: 'Runs' }, { id: 'expert', label: 'Monitor' },
      { id: 'log', label: 'Log' }, { id: 'wifi', label: 'Wi-Fi' },
    ],
    view: 'nodes',
    ws: null,
    wsOpen: false,
    status: null,
    auth: { required: false, ok: true, pin: '' },
    toast: '',
    toastBad: false,
    theme: document.documentElement.dataset.theme,
    busy: '',
    busForm: {
      source: 'adapter', interfaces: [''], mtu: '', bitrate: '', data_bitrate: '', local_node_id: 127, pnp: '',
      demo_devices: 1, ...loadSaved('cypilot.bus', {}),
    },
    adapters: [],
    adaptersSearched: false,
    nodes: [],
    selected: null,
    descriptions: { available: false, registers: {} },
    registers: [],
    registerFilter: '',
    loadingRegisters: false,
    open: {},
    target: null,
    targetError: '',
    homing: null,
    setpoint: { mode: '', values: [0, 0], ramp: 0.5 },
    allowRestart: false,
    plotSources: [],
    plotSourceKey: '',
    channels: [],
    subscriptions: [],
    windowSeconds: 10,
    plotPaused: false,
    monitorForm: { subject_id: null, type: '' },
    pubForm: { subject_id: null, type: '', rate_hz: 10, lease_s: 1, value: '{}' },
    publications: [],
    scope: {
      infos: {}, picked: {}, trigger_node: 0, shared: null, loading: false, periodSet: false,
      period: 100, samples: 200, pretrigger: 20, mode: 'immediate', source: 0, level: 0, band: 0,
      busy: false, state: '', result: null,
    },
    sim: { status: null, error: '', cycles: 16000 },
    startup: { state: null, error: '', vdc: '', current: '' },
    log: [],
    logLevel: 'info',
    logNode: '',
    runs: { available: false, active: null, space: null, list: [], selected: [], open: null, detail: null },
    replay: null,
    wifi: { available: false, status: null, known: [], access_point: null, networks: [], scanning: false,
            ssid: '', password: '', apSsid: '', apKey: '' },

    async init() {
      window.addEventListener('keydown', (e) => {
        if (e.key !== 'Escape') return;
        e.preventDefault();
        if (e.shiftKey) this.estop(); else this.stopAll();
      });
      setInterval(() => { if (this.wsOpen) this.ws.send(JSON.stringify({ type: 'renew' })); }, RENEW_MS);
      setInterval(() => { if (this.status?.connected && this.view === 'sim' && this.selected !== null) this.loadSim(); }, 1000);
      await this.checkAuth();
    },

    async checkAuth() {
      try {
        const a = await api('GET', '/api/auth');
        this.auth.required = a.required;
        this.auth.ok = a.ok;
      } catch (e) { this.notify(e); }
      if (this.auth.ok) await this.start();
    },

    async login() {
      try {
        await api('POST', '/api/login', { pin: this.auth.pin });
        this.auth.pin = '';
        this.auth.ok = true;
        await this.start();
      } catch (e) { this.notify(e); }
    },

    async start() {
      this.connectWs();
      try {
        await this.setStatus(await api('GET', '/api/status'));
        if (!this.status.connected) { this.view = 'bus'; this.searchAdapters(); }
        this.log = await api('GET', '/api/log').catch(() => []);
        await this.loadRuns();
        await this.loadWifi();
      } catch (e) { this.fail(e); }
    },

    fail(e) {
      if (e instanceof ApiError && e.status === 401) { this.auth.ok = false; return; }
      this.notify(e);
    },

    connectWs() {
      if (this.ws) this.ws.onclose = null;
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      this.ws = new WebSocket(`${proto}://${location.host}/ws?client=${CLIENT_ID}`);
      this.ws.onopen = () => { this.wsOpen = true; };
      this.ws.onclose = (ev) => {
        this.wsOpen = false;
        if (ev.code === 4401) { this.auth.ok = false; return; }
        setTimeout(() => this.connectWs(), 1000);
      };
      this.ws.onmessage = (ev) => this.onEvent(JSON.parse(ev.data));
    },

    async setStatus(status) {
      const old = this.status;
      this.status = status;
      const newConnection = status.connected && old?.connection !== status.connection;
      if (!status.connected || newConnection) {
        this.nodes = []; this.registers = []; this.target = null; this.publications = [];
        this.plotSources = []; this.dropAllChannels(); this.scope.infos = {}; this.scope.picked = {}; this.sim.status = null;
      }
      if (newConnection) {
        try {
          this.nodes = await api('GET', '/api/nodes');
          this.publications = (await api('GET', '/api/publishers')).map(p => ({ ...p, edit: JSON.stringify(p.value) }));
          if (this.selected !== null && !this.node()) this.selected = null;
          if (this.selected === null && this.nodes.length) this.selectNode(this.nodes[0].node_id);
        } catch (e) { this.fail(e); }
      }
    },

    mark(e) {
      const text = {
        control: () => `control ${e.reason}`, stop: () => 'STOP ALL', estop: () => 'E-STOP',
        register: () => `${e.node_id} ${e.name}`, command: () => `${e.node_id} command ${e.command}`,
      }[e.kind];
      if (!text || e.at === undefined) return;
      markers.push({ at: e.at, text: text() });
      if (markers.length > MAX_MARKERS) markers.splice(0, markers.length - MAX_MARKERS);
    },

    onEvent(e) {
      this.mark(e);
      switch (e.kind) {
        case 'hello': this.setStatus(e.status); break;
        case 'status': this.setStatus(e.status); break;
        case 'nodes':
          this.nodes = e.nodes;
          if (this.selected === null && this.nodes.length) this.selectNode(this.nodes[0].node_id);
          break;
        case 'control': {
          const wasMine = this.isOperator();
          if (this.status) this.status.control = { operator: e.operator, engaged: e.engaged };
          if (wasMine && !this.isOperator() && e.reason !== 'released') this.notify(`Control lost: ${e.reason}`, true);
          break;
        }
        case 'stop': this.notify(`STOP ALL: SLEEP to ${e.nodes.join(', ') || 'no drive'}`); break;
        case 'estop': this.notify(`E-STOP sent to ${Object.keys(e.nodes).join(', ') || 'no node'}`, true); break;
        case 'log':
          this.log.push(e);
          if (this.log.length > 5000) this.log.splice(0, 1000);
          break;
        case 'subject': this.onSubject(e); break;
        case 'publisher': this.onPublisher(e); break;
        case 'capture': this.scope.state = e.state; break;
        case 'run': this.onRun(e); break;
        case 'wifi': this.loadWifi(); break;
        case 'startup': this.onStartup(e); break;
        case 'homing': if (e.node_id === this.selected) this.homing = e.state; break;
      }
    },

    notify(e, bad = false) {
      this.toast = String(e.message ?? e);
      this.toastBad = bad || e instanceof Error;
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => { this.toast = ''; }, 6000);
    },

    toggleTheme() {
      this.theme = this.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = this.theme;
      save('cypilot.theme', this.theme);
    },

    show(view) {
      this.view = view;
      this.$nextTick(() => this.onView());
    },

    onView() {
      if (this.selected === null) return;
      if (this.view === 'registers' && !this.registers.length) this.loadRegisters();
      if (this.view === 'control' && !this.target) this.loadTarget();
      if (this.view === 'control') this.loadStartup();
      if (this.view === 'plots' && !this.plotSources.length) this.loadSources();
      if (this.view === 'scope' && !Object.keys(this.scope.infos).length) this.loadScope();
      if (this.view === 'sim') { this.loadSim(); if (!this.registers.length) this.loadRegisters(); }
      if (this.view === 'plots') this.redraw();
      if (this.view === 'runs') this.loadRuns();
    },

    // --- Stops: open to every client ---------------------------------------------

    async stopAll() {
      try { await api('POST', '/api/stop'); } catch (e) { this.fail(e); }
    },
    async estop() {
      try { await api('POST', '/api/estop'); } catch (e) { this.fail(e); }
    },

    // --- Control lease ------------------------------------------------------------

    isOperator() { return this.status?.control?.operator === CLIENT_ID; },
    operatorText() {
      const op = this.status?.control?.operator;
      if (!op) return 'nobody has control';
      return op === CLIENT_ID ? 'you have control' : `${op} has control`;
    },
    async takeControl(force = false) {
      try {
        this.status.control = await api('POST', '/api/control/take', { force });
      } catch (e) {
        if (e instanceof ApiError && e.status === 403 && !force && confirm(`${e.message}?`)) return this.takeControl(true);
        this.fail(e);
      }
    },
    async releaseControl() {
      try { this.status.control = await api('POST', '/api/control/release'); } catch (e) { this.fail(e); }
    },

    // --- Bus ----------------------------------------------------------------------

    async searchAdapters() {
      this.busy = 'searching adapters ...';
      try { this.adapters = await api('GET', '/api/adapters'); } catch (e) { this.fail(e); }
      this.busy = '';
      this.adaptersSearched = true;
      const f = this.busForm;
      if (this.adapters.length && !f.interfaces.some(i => i.trim())) f.interfaces = [this.adapters[0].interface];
    },
    useAdapter(a) {
      const f = this.busForm;
      if (f.interfaces.includes(a.interface)) return;
      const empty = f.interfaces.findIndex(i => !i.trim());
      if (empty >= 0) f.interfaces[empty] = a.interface; else f.interfaces.push(a.interface);
    },
    async connectBus() {
      const f = this.busForm;
      save('cypilot.bus', f);
      const demo = f.source === 'demo';
      const bitrate = f.bitrate === '' ? null : [Number(f.bitrate), Number(f.data_bitrate || f.bitrate)];
      this.busy = 'connecting ...';
      try {
        await api('POST', '/api/connect', {
          interfaces: demo ? [] : f.interfaces.map(i => i.trim()).filter(Boolean),
          local_node_id: Number(f.local_node_id), mtu: f.mtu === '' ? null : Number(f.mtu), bitrate,
          pnp: f.pnp.trim() || null, demo_devices: demo ? Number(f.demo_devices) : 0,
        });
        this.show('nodes');
      } catch (e) { this.fail(e); }
      this.busy = '';
    },
    async disconnectBus() {
      this.busy = 'disconnecting ...';
      try { await api('POST', '/api/disconnect'); } catch (e) { this.fail(e); }
      this.busy = '';
    },

    // --- Nodes --------------------------------------------------------------------

    node() { return this.nodes.find(n => n.node_id === this.selected); },
    nodeLabel(n) { return `${n.node_id} ${n.name || '(no name)'}`; },
    unimocNodes() { return this.nodes.filter(n => n.unimoc); },
    chipClass(chip) {
      if (BAD_CHIPS.includes(chip)) return 'bad';
      if (chip === 'engaged') return 'on';
      if (chip === 'settings pending' || chip === 'local control') return 'warn';
      return '';
    },
    selectNode(id) {
      if (id === null || id === '' || id === undefined) return;
      id = Number(id);
      if (id === this.selected) return;
      this.selected = id;
      this.registers = []; this.target = null; this.targetError = ''; this.homing = null; this.plotSources = [];
      this.sim.status = null; this.sim.error = '';
      this.startup = { state: null, error: '', vdc: '', current: '' };
      this.$nextTick(() => this.onView());
    },
    async command(name) {
      try {
        const res = await api('POST', `/api/nodes/${this.selected}/command`, { command: name });
        this.notify(`${name}: ${res.status}`, !res.ok);
      } catch (e) { this.fail(e); }
    },

    // --- Registers ----------------------------------------------------------------

    async loadDescriptions() {
      if (this.descriptions.available || this._descriptionsLoaded) return;
      this._descriptionsLoaded = true;
      try { this.descriptions = await api('GET', '/api/descriptions'); } catch (e) { this.fail(e); }
    },
    async loadRegisters() {
      if (this.selected === null) return;
      this.loadingRegisters = true;
      this.loadDescriptions();
      const node = this.selected;
      try {
        const regs = await api('GET', `/api/nodes/${node}/registers`);
        if (node === this.selected) this.registers = regs.map(r => ({ ...r, edit: r.text }));
      } catch (e) { this.fail(e); }
      this.loadingRegisters = false;
    },
    describe(name) { return this.descriptions.registers?.[name] ?? null; },
    groupOf(name) {
      const parts = name.split('.');
      return parts.length > 2 ? parts.slice(0, 2).join('.') : parts[0];
    },
    registerGroups() {
      const f = this.registerFilter.toLowerCase();
      const groups = {};
      for (const r of this.registers) {
        const help = this.describe(r.name)?.help ?? '';
        if (f && !r.name.toLowerCase().includes(f) && !help.toLowerCase().includes(f)) continue;
        (groups[this.groupOf(r.name)] ??= []).push(r);
      }
      return Object.entries(groups).sort(([a], [b]) => a.localeCompare(b)).map(([name, regs]) => ({ name, regs }));
    },
    isOpen(group) { return this.registerFilter !== '' || (this.open[group] ?? !group.startsWith('uavcan')); },
    limits(d) {
      if (!d || (d.min === null && d.max === null)) return '';
      return `${d.min ?? ''} .. ${d.max ?? ''}`;
    },
    async writeRegister(r) {
      try {
        const res = await api('PUT', `/api/nodes/${this.selected}/registers/${encodeURIComponent(r.name)}`, { value: r.edit });
        Object.assign(r, res.register, { edit: res.register.text });
        if (!res.accepted) this.notify(`${r.name}: the node kept ${r.edit}`, true);
      } catch (e) { this.fail(e); }
    },
    async readRegister(r) {
      try {
        const res = await api('GET', `/api/nodes/${this.selected}/registers/${encodeURIComponent(r.name)}`);
        Object.assign(r, res, { edit: res.text });
      } catch (e) { this.fail(e); }
    },
    dirty(r) { return r.edit !== r.text; },
    exportRegisters() {
      const data = this.registers.map(({ edit, ...r }) => r);
      download(`cypilot_node${this.selected}_registers_${stamp()}.json`, JSON.stringify(data, null, 2), 'application/json');
    },

    // --- Control ------------------------------------------------------------------

    async loadTarget() {
      if (this.selected === null) return;
      this.targetError = '';
      const node = this.selected;
      try {
        const target = await api('GET', `/api/nodes/${node}/target`);
        if (node !== this.selected) return;
        this.target = target;
        this.homing = target.homing === null ? null : (await api('GET', `/api/nodes/${node}/homing`)).state;
        const modes = Object.keys(target.setpoints);
        if (!modes.includes(this.setpoint.mode)) this.setpoint.mode = modes[0] ?? '';
      } catch (e) {
        this.target = null;
        this.targetError = e.message;
      }
    },
    engaged() { return this.node()?.chips.includes('engaged') ?? false; },
    components() { return this.target?.setpoints[this.setpoint.mode]?.components ?? 0; },
    componentLabels() {
      return { current: ['i_d [A]', 'i_q [A]'], torque: ['torque [N m]'], speed: ['speed [rad/s]'], position: ['position [rad]'] }[this.setpoint.mode] ?? [];
    },
    async enable() {
      this.busy = 'enabling ...';
      try { await api('POST', `/api/nodes/${this.selected}/enable`); } catch (e) { this.fail(e); }
      this.busy = '';
    },
    async disable() {
      try { await api('POST', `/api/nodes/${this.selected}/disable`, { ramp: Number(this.setpoint.ramp) }); } catch (e) { this.fail(e); }
    },
    async sendSetpoint(zero = false) {
      const values = this.setpoint.values.slice(0, this.components()).map(v => (zero ? 0 : Number(v)));
      if (zero) this.setpoint.values = this.setpoint.values.map(() => 0);
      try {
        await api('POST', `/api/nodes/${this.selected}/setpoint`, { mode: this.setpoint.mode, values, ramp: Number(this.setpoint.ramp) });
      } catch (e) { this.fail(e); }
    },
    homingShown() { return this.target?.homing != null && this.setpoint.mode === 'position'; },
    async startHoming() {
      if (!confirm('Home the drive? The shaft runs at the homing speed until it stalls against its stop; that stop becomes position 0.')) return;
      try {
        this.homing = (await api('POST', `/api/nodes/${this.selected}/homing`)).state;
      } catch (e) { this.fail(e); }
    },
    // --- Startup aid (UNIMOC hardware bring-up) --------------------------------------

    async loadStartup() {
      if (this.selected === null || !this.node()?.unimoc) { this.startup.state = null; return; }
      const node = this.selected;
      try {
        const state = await api('GET', `/api/nodes/${node}/startup`);
        if (node === this.selected) { this.startup.state = state; this.startup.error = ''; }
      } catch (e) {
        if (node === this.selected) { this.startup.state = null; this.startup.error = e.message; }
      }
    },
    onStartup(e) {
      // The status message of the drive, on every change (cypilot.core.startup).
      if (!e.state || e.node_id !== this.selected || !this.startup.state) return;
      const done = e.state.step_name === 'DONE' && this.startup.state.step_name !== 'DONE';
      this.startup.state = { ...e.state, calibration: done ? {} : (this.startup.state.calibration ?? {}) };
      if (done) this.loadStartup();
    },
    async startupAction(action) {
      const s = this.startup.state;
      const body = { action };
      if (action === 'start' && !confirm('Start the hardware startup aid? Phase 1 runs WITHOUT a motor: disconnect it first. The drive releases its outputs.')) return;
      if (action === 'next' && s?.phase_2_next) {
        if (!confirm('Phase 2 puts voltage on the motor. Is the motor connected (U, V, W and PE) and the shaft free?')) return;
        body.motor_connected = true;
      }
      if (s?.reading === 'vdc' && this.startup.vdc !== '') body.ext_vdc = Number(this.startup.vdc);
      if (s?.reading === 'current' && this.startup.current !== '') body.ext_current = Number(this.startup.current);
      try {
        this.startup.state = { ...(await api('POST', `/api/nodes/${this.selected}/startup`, body)), calibration: {} };
        if (action !== 'external') this.startup.vdc = this.startup.current = '';
      } catch (e) { this.fail(e); }
      await this.loadStartup();
    },
    startupSteps() {
      return Object.entries(this.startup.state?.passed ?? {}).filter(([name]) => !['DONE', 'FAULT'].includes(name));
    },

    async recover() {
      this.busy = 'recovering ...';
      try {
        const res = await api('POST', `/api/nodes/${this.selected}/recover`, { restart: this.allowRestart });
        this.notify(res.steps.join('; '), !res.ready);
      } catch (e) { this.fail(e); }
      this.busy = '';
    },

    // --- Plots --------------------------------------------------------------------

    async loadSources() {
      if (this.selected === null) return;
      const node = this.selected;
      try {
        const found = await api('GET', `/api/nodes/${node}/sources`);
        if (node === this.selected) {
          this.plotSources = found;
          if (!found.some(s => s.key === this.plotSourceKey)) this.plotSourceKey = found[0]?.key ?? '';
        }
      } catch (e) { this.fail(e); }
    },
    async subscribe(subjectId, type, label) {
      const s = await api('POST', '/api/subjects', { subject_id: subjectId, type });
      if (!this.subscriptions.find(x => x.id === s.id)) {
        this.subscriptions.push({ ...s, label, fields: [], last: null, sources: [] });
        buffers[s.id] = { t: [], v: {} };
      }
      return this.subscriptions.find(x => x.id === s.id);
    },
    async addSource() {
      if (this.replay) this.leaveReplay();
      const source = this.plotSources.find(s => s.key === this.plotSourceKey);
      if (!source) return;
      try { await this.subscribe(source.subject, source.type_name, `${source.node_id}/${source.port}`); } catch (e) { this.fail(e); }
    },
    async dropSubscription(s) {
      if (!String(s.id).startsWith('replay')) await api('DELETE', `/api/subjects/${s.id}`).catch(e => this.fail(e));
      this.subscriptions = this.subscriptions.filter(x => x.id !== s.id);
      this.channels = this.channels.filter(c => c.sub !== s.id);
      delete buffers[s.id];
      this.rebuildPlot();
    },
    dropAllChannels() {
      this.subscriptions = []; this.channels = []; markers.length = 0;
      for (const k of Object.keys(buffers)) delete buffers[k];
      if (plots.main) { plots.main.destroy(); delete plots.main; }
    },
    hasChannel(s, field) { return this.channels.some(c => c.sub === s.id && c.field === field); },
    toggleChannel(s, field) {
      if (this.hasChannel(s, field)) this.channels = this.channels.filter(c => !(c.sub === s.id && c.field === field));
      else this.channels.push({ sub: s.id, field, label: `${s.label} ${field}` });
      this.rebuildPlot();
    },
    onSubject(e) {
      const s = this.subscriptions.find(x => x.id === e.id);
      if (!s || this.replay) return;
      s.count += e.t.length;
      s.last = e.last;
      s.sources = [...new Set([...s.sources, ...e.source.filter(x => x !== null)])];
      const buf = buffers[s.id];
      for (const k of Object.keys(e.fields)) {
        if (!(k in buf.v)) {
          buf.v[k] = new Array(buf.t.length).fill(null);
          s.fields.push(k);
          if (typeof e.fields[k][0] === 'number' && this.channels.filter(c => c.sub === s.id).length < 3 && this.view === 'plots') {
            this.channels.push({ sub: s.id, field: k, label: `${s.label} ${k}` });
            this.rebuildPlot();
          }
        }
      }
      for (let i = 0; i < e.t.length; i++) {
        buf.t.push(e.t[i]);
        for (const k of Object.keys(buf.v)) buf.v[k].push(e.fields[k] ? e.fields[k][i] : null);
      }
      const cutoff = buf.t[buf.t.length - 1] - Math.max(this.windowSeconds, 1) * 3;
      let drop = 0;
      while (drop < buf.t.length && buf.t[drop] < cutoff) drop++;
      if (drop) { buf.t.splice(0, drop); for (const k of Object.keys(buf.v)) buf.v[k].splice(0, drop); }
      if (!this.plotPaused) this.redraw();
    },
    rebuildPlot() {
      if (plots.main) { plots.main.destroy(); delete plots.main; }
      this.$nextTick(() => this.redraw());
    },
    plotData() {
      // One time axis for channels of several subjects: uPlot.join aligns them (null where a subject has no sample).
      const bySub = {};
      for (const c of this.channels) (bySub[c.sub] ??= []).push(c);
      const tables = [];
      const order = [];
      for (const [sub, chans] of Object.entries(bySub)) {
        const buf = buffers[sub];
        if (!buf) continue;
        tables.push([buf.t, ...chans.map(c => buf.v[c.field] ?? [])]);
        order.push(...chans);
      }
      if (!tables.length) return { order, data: [[]] };
      const data = tables.length === 1 ? tables[0] : uPlot.join(tables);
      const t = data[0];
      const end = t.length ? t[t.length - 1] : 0;
      plotEnd = end;
      let start = 0;
      while (start < t.length && t[start] < end - this.windowSeconds) start++;
      return { order, data: data.map(column => Array.from(column).slice(start)).map((col, i) => (i === 0 ? col.map(x => x - end) : col)) };
    },
    redraw() {
      const el = document.getElementById('plot-main');
      if (!el || this.view !== 'plots') return;
      const { order, data } = this.plotData();
      if (!order.length) { if (plots.main) { plots.main.destroy(); delete plots.main; } el.innerHTML = ''; return; }
      if (!plots.main || plots.main.series.length !== order.length + 1) {
        if (plots.main) plots.main.destroy();
        plots.main = makePlot(el, order.map(c => c.label), window.innerHeight < 700 ? 240 : 360);
      }
      plots.main.setData(data);
    },
    exportPlot() {
      const { order, data } = this.plotData();
      if (!order.length) return;
      const header = ['t_s', ...order.map(c => c.label.replace(/[ ,]/g, '_'))].join(',');
      const rows = data[0].map((t, i) => [t, ...data.slice(1).map(col => col[i] ?? '')].join(','));
      download(`cypilot_plot_${stamp()}.csv`, header + '\n' + rows.join('\n') + '\n', 'text/csv');
    },

    // --- Monitor and publish (expert) -----------------------------------------

    async monitor() {
      const f = this.monitorForm;
      try { await this.subscribe(Number(f.subject_id), f.type.trim(), `${f.subject_id}`); } catch (e) { this.fail(e); }
    },
    async loadTemplate() {
      if (!this.pubForm.type.trim()) return;
      try {
        const t = await api('GET', `/api/types/${encodeURIComponent(this.pubForm.type.trim())}`);
        this.pubForm.type = t.type;
        this.pubForm.value = JSON.stringify(t.default, null, 2);
      } catch (e) { this.fail(e); }
    },
    async startPublish() {
      try {
        const f = this.pubForm;
        await api('POST', '/api/publishers', {
          subject_id: Number(f.subject_id), type: f.type.trim(), value: JSON.parse(f.value),
          rate_hz: Number(f.rate_hz), lease_s: Number(f.lease_s),
        });
      } catch (e) { this.fail(e); }
    },
    async updatePublish(p) {
      try { Object.assign(p, await api('PUT', `/api/publishers/${p.id}`, JSON.parse(p.edit))); } catch (e) { this.fail(e); }
    },
    async stopPublish(p) { await api('DELETE', `/api/publishers/${p.id}`).catch(e => this.fail(e)); },
    onPublisher(e) {
      if (e.action === 'stopped') {
        this.publications = this.publications.filter(p => p.id !== e.id);
        if (e.reason !== 'stopped') this.notify(`Publication on ${e.subject_id} stopped: ${e.reason}`);
      } else if (!this.publications.find(p => p.id === e.id)) {
        this.publications.push({ ...e, edit: JSON.stringify(e.value) });
      }
    },

    // --- Scope (cymon captures) -------------------------------------------------

    async loadScope() {
      const sc = this.scope;
      sc.loading = true;
      const infos = {};
      for (const n of this.nodes) {
        try { infos[n.node_id] = await api('GET', `/api/nodes/${n.node_id}/scope?refresh=true`); } catch { /* no cymon */ }
      }
      sc.infos = infos;
      for (const id of Object.keys(sc.picked)) if (!infos[id]) delete sc.picked[id];
      if (!infos[sc.trigger_node]) sc.trigger_node = Number(Object.keys(infos)[0] ?? 0);
      const tick = infos[sc.trigger_node]?.buffer.tick_period_us;
      if (tick > 0 && !sc.periodSet) sc.period = tick;
      sc.loading = false;
      await this.checkTrigger();
    },
    scopeNodes() { return Object.keys(this.scope.infos).map(Number).sort((a, b) => a - b); },
    picked(node) { return this.scope.picked[node] ?? []; },
    participants() { return this.scopeNodes().filter(n => this.picked(n).length); },
    togglePick(node, id) {
      const ch = this.picked(node);
      this.scope.picked[node] = ch.includes(id) ? ch.filter(x => x !== id) : [...ch, id];
      this.checkTrigger();
    },
    maxSamples() {
      const nodes = this.participants();
      if (!nodes.length) return 0;
      return Math.min(...nodes.map(n => Math.floor(this.scope.infos[n].buffer.max_frames / this.picked(n).length)));
    },
    async checkTrigger() {
      const nodes = this.participants();
      this.scope.shared = null;
      if (nodes.length < 2) return;
      try { this.scope.shared = await api('GET', `/api/scope/trigger?nodes=${nodes.join(',')}`); } catch (e) { this.fail(e); }
    },
    async setupTrigger() {
      const nodes = this.participants();
      if (!confirm(`Set the shared trigger subject on nodes ${nodes.join(', ')}, store and restart them?`)) return;
      this.busy = 'setting up the shared trigger ...';
      try {
        await api('POST', '/api/scope/trigger', { nodes });
        await new Promise(resolve => setTimeout(resolve, 3000));  // the nodes restart
        await this.loadScope();
      } catch (e) { this.fail(e); }
      this.busy = '';
    },
    canCapture() {
      const nodes = this.participants();
      return nodes.length > 0 && nodes.includes(Number(this.scope.trigger_node)) && (nodes.length === 1 || this.scope.shared?.shared);
    },
    async capture() {
      const sc = this.scope;
      sc.busy = true;
      sc.state = 'setting up';
      try {
        const nodes = {};
        for (const n of this.participants()) nodes[n] = this.picked(n);
        const res = await api('POST', '/api/scope/capture', {
          nodes, trigger_node: Number(sc.trigger_node), sample_period_us: Number(sc.period), num_samples: Number(sc.samples),
          pretrigger_samples: Number(sc.pretrigger), trigger_mode: sc.mode, trigger_source: Number(sc.source),
          trigger_level: Number(sc.level), hysteresis_band: Number(sc.band),
        });
        sc.result = { ...res, picked: JSON.parse(JSON.stringify(nodes)) };
        const first = res.captures[res.trigger_node];
        sc.state = `${first.t.length} samples at ${first.sample_period_us} µs on ${Object.keys(res.captures).length} node(s)`;
        this.$nextTick(() => this.drawScope());
      } catch (e) { this.fail(e); sc.state = 'error'; }
      sc.busy = false;
    },
    async disarm() {
      for (const n of this.participants()) await api('POST', `/api/nodes/${n}/scope/disarm`).catch(e => this.fail(e));
    },
    scopeLabels() {
      const labels = [];
      for (const [node, capture] of Object.entries(this.scope.result.captures)) {
        const vars = this.scope.infos[node]?.variables ?? [];
        capture.channels.forEach((_, i) => {
          const id = this.scope.result.picked[node][i];
          const v = vars.find(x => x.id === id);
          labels.push(`${node} ${v ? v.name + (v.unit ? ' [' + v.unit + ']' : '') : '#' + id}`);
        });
      }
      return labels;
    },
    scopeData() {
      // Each node on its own trigger sample (t = 0); uPlot.join puts the nodes on one time axis.
      const tables = Object.values(this.scope.result.captures).map(c => [c.t, ...c.channels]);
      return tables.length === 1 ? tables[0] : uPlot.join(tables);
    },
    drawScope() {
      const el = document.getElementById('scope-plot');
      if (!el || !this.scope.result) return;
      if (plots.scope) plots.scope.destroy();
      plots.scope = makePlot(el, this.scopeLabels(), window.innerHeight < 700 ? 260 : 420);
      plots.scope.setData(this.scopeData());
    },
    exportScope() {
      const data = this.scopeData();
      const header = ['t_s', ...this.scopeLabels().map(l => l.replace(/[ ,]/g, '_'))].join(',');
      const rows = Array.from(data[0]).map((t, i) => [t, ...data.slice(1).map(col => col[i] ?? '')].join(','));
      const nodes = Object.keys(this.scope.result.captures).join('-');
      download(`cypilot_scope_nodes${nodes}_${stamp()}.csv`, header + '\n' + rows.join('\n') + '\n', 'text/csv');
    },

    // --- Simulation ---------------------------------------------------------------

    async loadSim() {
      const node = this.selected;
      try {
        const status = await api('GET', `/api/nodes/${node}/sim`);
        if (node === this.selected) { this.sim.status = status; this.sim.error = ''; }
      } catch (e) {
        if (node === this.selected) { this.sim.status = null; this.sim.error = e.message; }
      }
    },
    async simAction(action) {
      try {
        this.sim.status = await api('POST', `/api/nodes/${this.selected}/sim`, { action, cycles: Number(this.sim.cycles) });
      } catch (e) { this.fail(e); }
    },
    plantInputs() {
      return this.registers.filter(r => r.name.startsWith('unimoc.sim.') && r.mutable
        && !['unimoc.sim.paused', 'unimoc.sim.advance'].includes(r.name));
    },

    // --- Runs: started and stopped only by the user --------------------------------

    async loadRuns() {
      try {
        const state = await api('GET', '/api/runs');
        Object.assign(this.runs, { available: state.available, active: state.active, space: state.space ?? null, list: state.runs });
        this.runs.selected = this.runs.selected.filter(id => state.runs.some(r => r.id === id));
      } catch (e) { this.fail(e); }
    },
    onRun(e) {
      if (e.action === 'recording' || e.action === 'started') this.runs.active = e.run;
      if (e.action === 'stopped' || e.action === 'disk full') this.runs.active = null;
      if (e.action === 'disk full') this.notify('The disk is nearly full: the run stopped.', true);
      if (e.action !== 'recording') this.loadRuns();
    },
    async startRun() {
      const name = prompt('Name of the run (optional)', '');
      if (name === null) return;
      try { this.runs.active = await api('POST', '/api/runs/start', { name }); } catch (e) { this.fail(e); }
    },
    async stopRun() {
      try { await api('POST', '/api/runs/stop'); this.runs.active = null; await this.loadRuns(); } catch (e) { this.fail(e); }
    },
    async noteRun() {
      const text = prompt('Note on the time axis of the run', '');
      if (!text) return;
      try { await api('POST', '/api/runs/mark', { text }); } catch (e) { this.fail(e); }
    },
    async openRun(run) {
      if (this.runs.open === run.id) { this.runs.open = null; this.runs.detail = null; return; }
      this.runs.open = run.id;
      this.runs.detail = null;
      try { this.runs.detail = await api('GET', `/api/runs/${run.id}`); } catch (e) { this.fail(e); }
    },
    async saveRun(detail) {
      try {
        await api('PATCH', `/api/runs/${detail.id}`, { name: detail.name, notes: detail.notes });
        await this.loadRuns();
        this.notify('Saved.');
      } catch (e) { this.fail(e); }
    },
    async deleteRuns(ids) {
      if (!ids.length) return;
      const names = ids.map(id => this.runs.list.find(r => r.id === id)?.name || id).join(', ');
      if (!confirm(`Delete ${ids.length === 1 ? 'the run' : ids.length + ' runs'} ${names}? This cannot be undone.`)) return;
      for (const id of ids) {
        try { await api('DELETE', `/api/runs/${id}`); } catch (e) { this.fail(e); }
      }
      if (ids.includes(this.runs.open)) { this.runs.open = null; this.runs.detail = null; }
      await this.loadRuns();
    },
    toggleRun(id) {
      const sel = this.runs.selected;
      this.runs.selected = sel.includes(id) ? sel.filter(x => x !== id) : [...sel, id];
    },
    async replayRun(run) {
      try {
        const data = await api('GET', `/api/runs/${run.id}/replay`);
        for (const s of this.subscriptions) {
          if (!String(s.id).startsWith('replay')) api('DELETE', `/api/subjects/${s.id}`).catch(() => {});
        }
        this.dropAllChannels();
        this.replay = { id: run.id, name: run.name || run.id };
        let index = 0;
        let start = Infinity, end = -Infinity;
        for (const src of data.sources) {
          const id = `replay${index++}`;
          const fields = Object.keys(src.fields);
          this.subscriptions.push({ id, subject_id: src.subject, type: src.type, count: src.t.length, label: src.key, fields, last: null, sources: [src.node_id] });
          buffers[id] = { t: src.t, v: src.fields };
          for (const f of fields.slice(0, 3)) this.channels.push({ sub: id, field: f, label: `${src.key} ${f}` });
          if (src.t.length) { start = Math.min(start, src.t[0]); end = Math.max(end, src.t[src.t.length - 1]); }
        }
        for (const m of data.markers) markers.push(m);
        if (Number.isFinite(start)) this.windowSeconds = Math.max(1, Math.ceil(end - start));
        this.show('plots');
        this.rebuildPlot();
      } catch (e) { this.fail(e); }
    },
    leaveReplay() {
      this.replay = null;
      this.dropAllChannels();
      this.windowSeconds = 10;
      this.rebuildPlot();
    },
    fmtSize(bytes) {
      if (bytes === null || bytes === undefined) return '';
      const units = ['B', 'kB', 'MB', 'GB', 'TB'];
      let v = bytes, i = 0;
      while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
      return `${i ? v.toFixed(1) : v} ${units[i]}`;
    },
    fmtDuration(s) {
      if (s === null || s === undefined) return '';
      s = Math.round(s);
      const h = Math.floor(s / 3600), m = Math.floor(s / 60) % 60;
      return (h ? `${h}:${String(m).padStart(2, '0')}` : `${m}`) + `:${String(s % 60).padStart(2, '0')}`;
    },
    fmtDate(t) { return new Date(t * 1000).toLocaleString(undefined, { hour12: false }); },

    // --- Wi-Fi of the Pi (serve --wifi) --------------------------------------------

    visibleViews() { return this.views.filter(v => v.id !== 'wifi' || this.wifi.available); },
    async loadWifi() {
      try {
        const state = await api('GET', '/api/wifi');
        this.wifi.available = state.available;
        if (!state.available) return;
        Object.assign(this.wifi, { status: state.status, known: state.known, access_point: state.access_point });
        if (!this.wifi.apSsid) this.wifi.apSsid = state.access_point?.ssid ?? '';
      } catch (e) { this.fail(e); }
    },
    async scanWifi() {
      this.wifi.scanning = true;
      try { this.wifi.networks = await api('GET', '/api/wifi/scan'); } catch (e) { this.fail(e); }
      this.wifi.scanning = false;
    },
    pickNetwork(n) { this.wifi.ssid = n.ssid; this.wifi.password = ''; },
    async joinWifi() {
      const w = this.wifi;
      const warning = w.status?.access_point
        ? ` Devices on the access point lose cypilot: join "${w.ssid}" with them too and open http://cypilot.local/.` : '';
      if (!confirm(`Join the Wi-Fi "${w.ssid}"?${warning} If it fails, the access point opens again.`)) return;
      this.busy = `joining ${w.ssid} ...`;
      try {
        const status = await api('POST', '/api/wifi/connect', { ssid: w.ssid, password: w.password || null });
        this.notify(`Joined ${status.connection}: ${status.addresses.join(', ')}`);
        w.password = '';
      } catch (e) { this.fail(e); }
      this.busy = '';
      await this.loadWifi();
    },
    async forgetWifi(name) {
      if (!confirm(`Forget the Wi-Fi "${name}"? The Pi no longer joins it by itself.`)) return;
      try { await api('DELETE', `/api/wifi/known/${encodeURIComponent(name)}`); } catch (e) { this.fail(e); }
      await this.loadWifi();
    },
    async saveAccessPoint() {
      const w = this.wifi;
      try {
        await api('PUT', '/api/wifi/access-point', { ssid: w.apSsid || null, key: w.apKey || null });
        w.apKey = '';
        this.notify('Saved: the access point uses it the next time it opens.');
      } catch (e) { this.fail(e); }
      await this.loadWifi();
    },
    async startAccessPoint() {
      if (!confirm('Open the access point now? Devices on the current Wi-Fi lose cypilot until they join it.')) return;
      this.busy = 'opening the access point ...';
      try { await api('POST', '/api/wifi/access-point/start'); } catch (e) { this.fail(e); }
      this.busy = '';
      await this.loadWifi();
    },

    // --- Log ----------------------------------------------------------------------

    filteredLog() {
      const min = SEVERITIES.indexOf(this.logLevel);
      return this.log.filter(d => SEVERITIES.indexOf(d.severity) >= min && (this.logNode === '' || d.node_id === Number(this.logNode)))
        .slice(-1000).reverse();
    },

    // --- Formatting ---------------------------------------------------------------

    fmtValue(v) {
      if (v === null || v === undefined) return '';
      return Array.isArray(v) ? v.map(x => (typeof x === 'number' && !Number.isInteger(x) ? +x.toPrecision(7) : x)).join(', ') : String(v);
    },
    fmtNum(v) { return typeof v === 'number' ? String(+v.toPrecision(5)) : String(v ?? ''); },
    fmtUptime(s) {
      if (s === undefined || s === null) return '';
      const d = Math.floor(s / 86400), h = Math.floor(s / 3600) % 24, m = Math.floor(s / 60) % 60;
      return (d ? `${d}d ` : '') + `${h}:${String(m).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
    },
    fmtTime(t) {
      return new Date(t * 1000).toLocaleTimeString(undefined, { hour12: false }) + '.' + String(Math.floor((t % 1) * 1000)).padStart(3, '0');
    },
  };
};
