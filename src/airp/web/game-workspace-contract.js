/*
 * AIRP game workspace contract.
 *
 * This is intentionally small: the existing game page still owns the
 * runtime behaviour, while future Studio and Monitor modules can consume a
 * stable set of regions, state slots, and lifecycle events without reaching
 * into private DOM details.
 */
(function installAirpWorkspaceContract(global, document) {
  'use strict';

  if (global.AIRPWorkspace) return;

  var regionSelectors = Object.freeze({
    app: '[data-airp-app="game-workspace"]',
    topbar: '[data-airp-region="topbar"]',
    navigation: '[data-airp-region="navigation"]',
    conversation: '[data-airp-region="conversation"]',
    composer: '[data-airp-region="composer"]',
    monitor: '[data-airp-region="monitor"]',
    studioDrawer: '[data-airp-region="studio-drawer"]',
    debugOverlay: '[data-airp-region="node-debug-overlay"]'
  });

  var slotSelectors = Object.freeze({
    conversation: '[data-airp-slot="conversation"]',
    composerInput: '[data-airp-slot="composer-input"]',
    monitorSession: '[data-airp-slot="monitor-session"]',
    monitorGeneration: '[data-airp-slot="monitor-generation"]',
    monitorGraph: '[data-airp-slot="monitor-graph"]',
    monitorRuntime: '[data-airp-slot="monitor-runtime"]',
    studioDrawerPanel: '[data-airp-slot="studio-drawer-panel"]',
    debugBody: '[data-airp-slot="debug-body"]'
  });

  var state = {
    activeGraph: null,
    runtimeTask: null,
    session: null,
    conversation: { content: null, stream: null },
    composer: { busy: false, value: '' },
    monitor: { sidebarOpen: false, trace: null },
    studioDrawer: { open: false, view: null },
    nodeDebug: { open: false, nodeRunId: null }
  };

  var events = new EventTarget();
  var drawerCloseTimers = new WeakMap();

  function drawerDuration(host) {
    if (!host || typeof getComputedStyle !== 'function') return 220;
    var value = parseFloat(getComputedStyle(host).transitionDuration || '0');
    return Number.isFinite(value) ? Math.max(0, value * 1000) : 220;
  }

  function drawerMotionOpen(host) {
    if (!host) return;
    var timer = drawerCloseTimers.get(host);
    if (timer) clearTimeout(timer);
    host.hidden = false;
    host.classList.remove('is-closing', 'is-open');
    host.classList.add('is-opening');
    host.setAttribute('aria-hidden', 'true');
    var frame = global.requestAnimationFrame || function (callback) { return global.setTimeout(callback, 0); };
    frame(function () {
      if (host.hidden || !host.classList.contains('is-opening')) return;
      host.classList.remove('is-opening');
      host.classList.add('is-open');
      host.setAttribute('aria-hidden', 'false');
    });
  }

  function drawerMotionClose(host) {
    if (!host) return;
    var timer = drawerCloseTimers.get(host);
    if (timer) clearTimeout(timer);
    host.hidden = false;
    host.classList.remove('is-opening', 'is-open');
    host.classList.add('is-closing');
    host.setAttribute('aria-hidden', 'true');
    var closeTimer = global.setTimeout(function () {
      if (!host.classList.contains('is-closing')) return;
      host.hidden = true;
      host.classList.remove('is-closing');
    }, drawerDuration(host));
    drawerCloseTimers.set(host, closeTimer);
  }

  function setNavigationState(view) {
    var normalized = String(view || '').split(':').pop();
    var selected = normalized === 'orchestration' ? 'agents' : normalized;
    var buttons = {
      game: '#game-drawer-toggle',
      worldbooks: '#studio-worldbooks-toggle',
      agents: '#studio-agents-toggle',
      regex: '#studio-regex-toggle',
      'regex-collections': '#studio-regex-toggle',
      model: '#studio-model-toggle'
    };
    Object.keys(buttons).forEach(function (key) {
      var button = document.querySelector(buttons[key]);
      if (button) button.setAttribute('aria-expanded', key === selected ? 'true' : 'false');
    });
  }

  function activateDrawerSurface(surface) {
    var surfaces = {
      studio: document.getElementById('studio-drawer-panel'),
      regex: document.getElementById('regex-drawer-panel'),
      game: document.getElementById('game-drawer-panel'),
      worldbooks: document.getElementById('worldbook-drawer-mount')
    };
    Object.keys(surfaces).forEach(function (name) {
      if (surfaces[name]) surfaces[name].hidden = name !== surface;
    });
    var host = region('studioDrawer');
    if (host) host.dataset.airpDrawerSurface = surface || '';
  }

  function emit(type, detail) {
    events.dispatchEvent(new CustomEvent(type, { detail: detail || null }));
  }

  function find(selector) {
    return document.querySelector(selector);
  }

  function region(name) {
    return regionSelectors[name] ? find(regionSelectors[name]) : null;
  }

  function slot(name) {
    return slotSelectors[name] ? find(slotSelectors[name]) : null;
  }

  function setState(name, value) {
    if (!Object.prototype.hasOwnProperty.call(state, name)) return;
    state[name] = value;
    emit('state:' + name, value);
  }

  function patchState(name, values) {
    if (!Object.prototype.hasOwnProperty.call(state, name) || !values) return;
    state[name] = Object.assign({}, state[name], values);
    emit('state:' + name, state[name]);
  }

  global.AIRPWorkspace = Object.freeze({
    version: 1,
    regions: regionSelectors,
    slots: slotSelectors,
    state: state,
    events: events,
    emit: emit,
    region: region,
    slot: slot,
    setState: setState,
    patchState: patchState,
    setNavigationState: setNavigationState,
    activateDrawerSurface: activateDrawerSurface,
    drawerMotion: Object.freeze({ open: drawerMotionOpen, close: drawerMotionClose })
  });
})(window, document);
