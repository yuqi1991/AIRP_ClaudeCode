/* Safe startup diagnostics and initial Studio resource hints. */
(function installStartupDiagnostics(global, document) {
  'use strict';

  var report = null;
  var pending = null;
  var closeBound = false;

  function diagnosticsHost() {
    return document.getElementById('studio-startup-diagnostics');
  }

  function render(payload) {
    var notice = diagnosticsHost();
    var target = document.getElementById('studio-startup-diagnostics-message');
    if (!notice || !target) return;
    var diagnostics = payload && Array.isArray(payload.diagnostics) ? payload.diagnostics : [];
    target.replaceChildren();
    diagnostics.forEach(function (diagnostic) {
      if (!diagnostic || typeof diagnostic !== 'object') return;
      var row = document.createElement('span');
      row.className = 'studio-startup-diagnostic';
      if (typeof diagnostic.message === 'string' && diagnostic.message) {
        var message = document.createElement('span');
        message.textContent = diagnostic.message;
        row.appendChild(message);
      }
      if (typeof diagnostic.action === 'string' && diagnostic.action) {
        var action = document.createElement('span');
        action.textContent = diagnostic.action;
        row.appendChild(action);
      }
      if (typeof diagnostic.project_id === 'string' && diagnostic.project_id) {
        var project = document.createElement('small');
        project.textContent = 'Project: ' + diagnostic.project_id;
        row.appendChild(project);
      }
      if (row.childNodes.length) target.appendChild(row);
    });
    notice.dataset.status = payload && (payload.status === 'fatal' || payload.ok === false) ? 'fatal' : 'warning';
    notice.hidden = !target.childNodes.length;
  }

  function bindClose() {
    if (closeBound) return;
    closeBound = true;
    var close = document.getElementById('studio-startup-diagnostics-close');
    if (close) close.addEventListener('click', function () {
      var notice = diagnosticsHost();
      if (notice) notice.hidden = true;
    });
  }

  function init() {
    bindClose();
    if (pending) return pending;
    pending = fetch('/v1/studio/startup', { headers: { Accept: 'application/json' } })
      .then(function (response) {
        if (!response.ok) throw new Error('startup report unavailable');
        return response.json();
      })
      .then(function (payload) {
        report = payload && typeof payload === 'object' ? payload : null;
        render(report);
        return report;
      })
      .catch(function () {
        report = null;
        var notice = diagnosticsHost();
        if (notice) notice.hidden = true;
        return null;
      });
    return pending;
  }

  function initialResourceId(kind) {
    var ids = report && report.initial_resource_ids;
    var value = ids && ids[kind];
    return typeof value === 'string' && value ? value : null;
  }

  function preferredId(kind, items, idOf) {
    return init().then(function () {
      var candidates = Array.isArray(items) ? items : [];
      var hint = initialResourceId(kind);
      if (hint && candidates.some(function (item) { return idOf(item) === hint; })) return hint;
      return candidates.length ? idOf(candidates[0]) : null;
    });
  }

  global.AIRPStartupDiagnostics = Object.freeze({
    init: init,
    initialResourceId: initialResourceId,
    preferredId: preferredId
  });
})(window, document);
