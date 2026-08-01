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
    patchState: patchState
  });
})(window, document);
