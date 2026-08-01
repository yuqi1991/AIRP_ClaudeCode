/*
 * Regex Collection editor for the integrated game workspace.
 *
 * The Worldbook drawer owns the shared Studio host and its lifecycle event.
 * This module adds a second view without duplicating that host or changing
 * the Regex Collection/Graph Runtime implementation behind the HTTP API.
 */
(function installRegexDrawer(global, document) {
  'use strict';

  var host = document.getElementById('studio-drawer-host');
  if (!host) return;

  var endpoint = '/v1/studio/regex-collections';
  var agentEndpoint = '/v1/studio/agents';
  var state = {
    collections: [],
    selectedId: null,
    collection: null,
    libraryQuery: '',
    agents: [],
    testResult: null,
    ready: false,
    loading: false
  };
  var workspace = global.AIRPWorkspace || null;
  var markup = [
    '<div class="regex-drawer-view" id="regex-drawer-view" data-regex-drawer-view="regex-collections" data-studio-drawer-panel="regex-collections" hidden>',
      '<div class="regex-drawer-toolbar">',
        '<div class="regex-drawer-toolbar-title"><strong>Regex Collections</strong><span id="regex-drawer-count">0 saved</span></div>',
        '<input class="regex-drawer-search" id="regex-drawer-library-search" type="search" placeholder="Search collections" aria-label="Search Regex Collections">',
        '<button class="regex-drawer-button primary" id="regex-drawer-new" type="button">New</button>',
      '</div>',
      '<div class="regex-drawer-body">',
        '<aside class="regex-drawer-column regex-library-column" aria-label="Regex Collection library">',
          '<div class="regex-drawer-column-heading"><h2>Library</h2><span class="regex-drawer-hint" id="regex-drawer-library-hint">Reusable rules</span></div>',
          '<div class="regex-library-list" id="regex-drawer-list" aria-live="polite"></div>',
        '</aside>',
        '<main class="regex-drawer-column regex-editor-column" aria-label="Regex Collection editor">',
          '<div class="regex-editor-header">',
            '<div><div class="regex-drawer-kicker">Collection definition</div><input class="regex-drawer-input regex-collection-name" id="regex-drawer-name" required autocomplete="off" placeholder="Collection name" aria-label="Regex Collection name"></div>',
            '<div class="regex-editor-actions"><button class="regex-drawer-button" id="regex-drawer-copy" type="button" hidden>Copy</button><button class="regex-drawer-button danger" id="regex-drawer-delete" type="button" hidden>Delete</button></div>',
          '</div>',
          '<div class="regex-rule-toolbar"><div><h2>Ordered rules</h2><p class="regex-drawer-hint">JavaScript RegExp rules run from top to bottom.</p></div><button class="regex-drawer-button" id="regex-drawer-add-rule" type="button">Add rule</button></div>',
          '<div class="regex-rule-list" id="regex-drawer-rules"></div>',
          '<section class="regex-test-panel" aria-labelledby="regex-drawer-test-title">',
            '<div class="regex-rule-toolbar"><div><h2 id="regex-drawer-test-title">Test collection</h2><p class="regex-drawer-hint">Run the current draft without saving it first.</p></div><button class="regex-drawer-button" id="regex-drawer-test" type="button">Run test</button></div>',
            '<div class="regex-test-controls"><label>Test target<select class="regex-drawer-select" id="regex-drawer-test-target" aria-label="Test target"><option value="input">Input</option><option value="output" selected>Output</option></select></label><label class="regex-test-input-label">Test input<textarea class="regex-drawer-textarea" id="regex-drawer-test-input" placeholder="Paste sample text here"></textarea></label></div>',
            '<div class="regex-test-result-grid"><div><h3>Result</h3><pre class="regex-test-output" id="regex-drawer-test-output" aria-live="polite"></pre></div><div><h3>Diagnostics</h3><div class="regex-diagnostics" id="regex-drawer-diagnostics" aria-live="polite"></div></div></div>',
          '</section>',
        '</main>',
        '<aside class="regex-drawer-column regex-binding-column" aria-label="Agent Regex Collection bindings">',
          '<div class="regex-drawer-column-heading"><h2>Agent bindings</h2><span class="regex-drawer-hint">0 or 1 per Agent</span></div>',
          '<p class="regex-drawer-hint regex-binding-explanation">Choose at most one collection for each Agent. Changes save immediately through the existing Agent API.</p>',
          '<div class="regex-binding-list" id="regex-drawer-agent-bindings" aria-live="polite"></div>',
        '</aside>',
      '</div>',
      '<footer class="regex-drawer-footer">',
        '<div class="regex-drawer-notice" id="regex-drawer-notice" role="status" aria-live="polite"></div>',
        '<div class="regex-drawer-actions"><button class="regex-drawer-button primary" id="regex-drawer-save" type="button">Save Collection</button></div>',
      '</footer>',
    '</div>'
  ].join('');

  function $(id) { return document.getElementById(id); }

  function clone(value) {
    return value === null || value === undefined ? value : JSON.parse(JSON.stringify(value));
  }

  function panel() {
    return integratedPanel() || legacyPanel() || document.getElementById('studio-drawer-panel');
  }

  function integratedPanel() {
    var currentPanel = document.getElementById('studio-drawer-panel');
    return currentPanel && currentPanel.querySelector('[data-studio-drawer-panel="agents"]') ? currentPanel : null;
  }

  function legacyPanel() {
    return document.querySelector('.worldbook-drawer-panel');
  }

  function showNotice(message, kind) {
    var notice = $('regex-drawer-notice');
    if (!notice) return;
    notice.className = 'regex-drawer-notice' + (kind ? ' ' + kind : '');
    notice.textContent = message || '';
  }

  function errorMessage(error, fallback) {
    var payload = error && error.payload;
    if (payload && Array.isArray(payload.references) && payload.references.length) {
      return (error.message || fallback) + ' Referenced by ' + payload.references.map(function(item) {
        return item.name || item.id || item.agent_id || 'an Agent';
      }).join(', ') + '.';
    }
    return error && error.message ? error.message : fallback;
  }

  async function request(path, options) {
    var response = await fetch(path, Object.assign({ headers: { Accept: 'application/json' } }, options || {}));
    var payload = {};
    try { payload = await response.json(); } catch (_) { payload = {}; }
    if (!response.ok) {
      var error = new Error(payload.message || payload.error || 'Studio request failed');
      error.payload = payload;
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function setWorkspaceView(view) {
    if (workspace && typeof workspace.patchState === 'function') {
      workspace.patchState('studioDrawer', { open: true, view: view });
    }
  }

  function addTabs() {
    var currentPanel = panel();
    var integrated = integratedPanel();
    if (integrated) {
      return;
    }
    if (!currentPanel || currentPanel.querySelector('.regex-drawer-tabs')) return;
    var header = currentPanel.querySelector('.worldbook-drawer-header');
    var close = currentPanel.querySelector('#worldbook-drawer-close');
    if (!header || !close) return;
    var tabs = document.createElement('nav');
    tabs.className = 'regex-drawer-tabs';
    tabs.setAttribute('aria-label', 'Studio drawer views');
    [
      ['worldbooks', 'Worldbooks'],
      ['regex-collections', 'Regex Collections']
    ].forEach(function(item) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'regex-drawer-tab';
      button.dataset.regexDrawerView = item[0];
      button.textContent = item[1];
      button.addEventListener('click', function() { setView(item[0]); });
      tabs.appendChild(button);
    });
    header.insertBefore(tabs, close);
  }

  function ensureMarkup() {
    var currentPanel = panel();
    if (!currentPanel) return false;
    addTabs();
    if (!$('regex-drawer-view')) currentPanel.insertAdjacentHTML('beforeend', markup);
    return Boolean($('regex-drawer-view'));
  }

  function setView(view) {
    if (!ensureMarkup()) return;
    var regexView = $('regex-drawer-view');
    var currentPanel = panel();
    var isRegex = view === 'regex-collections';
    ['.worldbook-drawer-toolbar', '.worldbook-drawer-body', '.worldbook-drawer-footer'].forEach(function(selector) {
      var element = currentPanel.querySelector(selector);
      if (element) element.hidden = isRegex;
    });
    regexView.hidden = !isRegex;
    currentPanel.dataset.airpDrawerView = isRegex ? 'regex-collections' : 'worldbooks';
    currentPanel.setAttribute('aria-labelledby', isRegex ? 'regex-drawer-name' : 'worldbook-drawer-title');
    currentPanel.querySelectorAll('.regex-drawer-tab').forEach(function(tab) {
      var active = tab.dataset.regexDrawerView === (isRegex ? 'regex-collections' : 'worldbooks');
      tab.classList.toggle('active', active);
      if (active) tab.setAttribute('aria-current', 'page');
      else tab.removeAttribute('aria-current');
    });
    var integrated = integratedPanel();
    if (integrated) {
      var regexPanel = $('regex-drawer-view');
      integrated.querySelectorAll('[data-studio-drawer-panel]').forEach(function(viewPanel) {
        var active = viewPanel === regexPanel ? isRegex : (viewPanel.getAttribute('data-studio-drawer-panel') === view);
        viewPanel.hidden = !active;
        viewPanel.classList.toggle('is-active', active);
      });
      var mount = document.getElementById('worldbook-drawer-mount');
      if (mount) mount.hidden = view !== 'worldbooks';
      var title = document.getElementById('studio-drawer-title');
      if (title) title.textContent = isRegex ? 'Regex Collections' : (view === 'orchestration' ? 'Agents 与编排' : (view === 'model' ? '模型' : 'Agents 与编排'));
      var worldbook = legacyPanel();
      if (worldbook) worldbook.hidden = view !== 'worldbooks';
      if (isRegex) {
        setWorkspaceView('regex-collections');
        loadData();
      }
      return;
    }
    if (isRegex) {
      setWorkspaceView('regex-collections');
      loadData();
    } else {
      setWorkspaceView('worldbooks');
    }
  }

  function makeRule(index) {
    return { id: '', name: 'Rule ' + (index + 1), enabled: true, target: 'both', pattern: '', flags: '', replacement: '' };
  }

  function emptyCollection() {
    return { id: '', name: '', rules: [makeRule(0)] };
  }

  function normalizeCollection(collection) {
    var result = clone(collection) || emptyCollection();
    if (!Array.isArray(result.rules)) result.rules = [];
    result.rules = result.rules.map(function(rule, index) {
      var value = rule && typeof rule === 'object' ? rule : {};
      return {
        id: value.id || '',
        name: value.name || ('Rule ' + (index + 1)),
        enabled: value.enabled !== false,
        target: ['input', 'output', 'both'].indexOf(value.target) >= 0 ? value.target : 'both',
        pattern: value.pattern === undefined || value.pattern === null ? '' : String(value.pattern),
        flags: value.flags === undefined || value.flags === null ? '' : String(value.flags),
        replacement: value.replacement === undefined || value.replacement === null ? '' : String(value.replacement)
      };
    });
    return result;
  }

  function resetEditor() {
    state.selectedId = null;
    state.collection = emptyCollection();
    state.testResult = null;
    if ($('regex-drawer-library-search')) $('regex-drawer-library-search').value = '';
    renderAll();
    showNotice('New Regex Collection');
    if ($('regex-drawer-name')) $('regex-drawer-name').focus();
  }

  function fillEditor(collection) {
    state.selectedId = collection && collection.id ? collection.id : null;
    state.collection = normalizeCollection(collection);
    state.testResult = null;
    renderAll();
  }

  function visibleCollections() {
    var query = state.libraryQuery.trim().toLowerCase();
    return state.collections.filter(function(collection) {
      if (!query) return true;
      var haystack = [collection.id, collection.name].concat((collection.rules || []).map(function(rule) {
        return [rule.name, rule.target, rule.pattern, rule.replacement].join(' ');
      })).join(' ').toLowerCase();
      return haystack.indexOf(query) >= 0;
    });
  }

  function renderLibrary() {
    var target = $('regex-drawer-list');
    if (!target) return;
    var count = $('regex-drawer-count');
    if (count) count.textContent = state.collections.length + ' saved';
    var rows = visibleCollections().map(function(collection) {
      var row = document.createElement('button');
      row.type = 'button';
      row.className = 'regex-library-row' + (collection.id === state.selectedId ? ' selected' : '');
      row.dataset.regexCollectionId = collection.id;
      row.setAttribute('aria-current', collection.id === state.selectedId ? 'true' : 'false');
      var title = document.createElement('strong');
      title.className = 'regex-library-title';
      title.textContent = collection.name;
      var meta = document.createElement('span');
      meta.className = 'regex-library-meta';
      var ruleCount = Array.isArray(collection.rules) ? collection.rules.length : 0;
      meta.textContent = ruleCount + ' rule' + (ruleCount === 1 ? '' : 's');
      row.append(title, meta);
      row.addEventListener('click', function() { loadCollection(collection.id); });
      return row;
    });
    if (!rows.length) {
      var empty = document.createElement('div');
      empty.className = 'regex-drawer-empty';
      empty.textContent = state.libraryQuery ? 'No matching collections.' : 'No Regex Collections yet.';
      rows.push(empty);
    }
    target.replaceChildren.apply(target, rows);
  }

  function makeField(labelText, value, type, className) {
    var label = document.createElement('label');
    label.className = className || '';
    var text = document.createElement('span');
    text.textContent = labelText;
    var control = document.createElement(type || 'input');
    control.className = 'regex-drawer-control';
    if (value !== undefined && value !== null) control.value = value;
    label.append(text, control);
    return { label: label, control: control };
  }

  function renderRuleCard(rule, index) {
    var card = document.createElement('section');
    card.className = 'regex-rule-card' + (rule.enabled ? '' : ' is-disabled');
    card.dataset.regexRuleIndex = String(index);
    var header = document.createElement('div');
    header.className = 'regex-rule-card-header';
    var title = document.createElement('strong');
    title.textContent = rule.name || ('Rule ' + (index + 1));
    var enabledLabel = document.createElement('label');
    enabledLabel.className = 'regex-rule-enabled';
    var enabled = document.createElement('input');
    enabled.type = 'checkbox';
    enabled.checked = rule.enabled !== false;
    enabled.addEventListener('change', function() {
      rule.enabled = enabled.checked;
      card.classList.toggle('is-disabled', !rule.enabled);
    });
    enabledLabel.append(enabled, ' Enabled');
    header.append(title, enabledLabel);

    var fields = document.createElement('div');
    fields.className = 'regex-rule-fields';
    var name = makeField('Name', rule.name, 'input', 'regex-rule-name');
    var target = makeField('Target', '', 'select', 'regex-rule-target');
    ['input', 'output', 'both'].forEach(function(optionValue) {
      var option = document.createElement('option');
      option.value = optionValue;
      option.textContent = optionValue;
      option.selected = optionValue === rule.target;
      target.control.appendChild(option);
    });
    var pattern = makeField('Pattern', rule.pattern, 'input', 'regex-rule-pattern');
    var flags = makeField('Flags', rule.flags, 'input', 'regex-rule-flags');
    var replacement = makeField('Replacement', rule.replacement, 'textarea', 'regex-rule-replacement');
    [name, target, pattern, flags, replacement].forEach(function(field) { fields.appendChild(field.label); });
    name.control.addEventListener('input', function() { rule.name = name.control.value; title.textContent = rule.name || ('Rule ' + (index + 1)); });
    target.control.addEventListener('change', function() { rule.target = target.control.value; });
    pattern.control.addEventListener('input', function() { rule.pattern = pattern.control.value; });
    flags.control.addEventListener('input', function() { rule.flags = flags.control.value; });
    replacement.control.addEventListener('input', function() { rule.replacement = replacement.control.value; });

    var actions = document.createElement('div');
    actions.className = 'regex-rule-actions';
    [['Move up', -1], ['Move down', 1]].forEach(function(item) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'regex-drawer-button';
      button.textContent = item[0];
      button.disabled = item[1] < 0 ? index === 0 : index === state.collection.rules.length - 1;
      button.addEventListener('click', function() {
        var next = index + item[1];
        if (next < 0 || next >= state.collection.rules.length) return;
        var moved = state.collection.rules.splice(index, 1)[0];
        state.collection.rules.splice(next, 0, moved);
        renderRules();
      });
      actions.appendChild(button);
    });
    var remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'regex-drawer-button danger';
    remove.textContent = 'Remove';
    remove.addEventListener('click', function() {
      state.collection.rules.splice(index, 1);
      renderRules();
    });
    actions.appendChild(remove);
    card.append(header, fields, actions);
    return card;
  }

  function renderRules() {
    var target = $('regex-drawer-rules');
    if (!target) return;
    var rules = state.collection && Array.isArray(state.collection.rules) ? state.collection.rules : [];
    var cards = rules.map(renderRuleCard);
    if (!cards.length) {
      var empty = document.createElement('div');
      empty.className = 'regex-drawer-empty';
      empty.textContent = 'No rules. Add a rule to begin transforming text.';
      cards.push(empty);
    }
    target.replaceChildren.apply(target, cards);
  }

  function renderEditorActions() {
    var hasCollection = Boolean(state.collection);
    var saved = Boolean(state.selectedId);
    if ($('regex-drawer-name')) $('regex-drawer-name').value = hasCollection ? state.collection.name || '' : '';
    if ($('regex-drawer-copy')) $('regex-drawer-copy').hidden = !saved;
    if ($('regex-drawer-delete')) $('regex-drawer-delete').hidden = !saved;
    if ($('regex-drawer-save')) $('regex-drawer-save').disabled = !hasCollection;
  }

  function renderTestResult() {
    var output = $('regex-drawer-test-output');
    var diagnostics = $('regex-drawer-diagnostics');
    if (!output || !diagnostics) return;
    output.textContent = '';
    diagnostics.replaceChildren();
    var result = state.testResult;
    if (!result) {
      output.textContent = 'Run a test to inspect transformed text.';
      return;
    }
    if (result.error) {
      output.textContent = 'Test failed.';
      var error = document.createElement('div');
      error.className = 'regex-diagnostic error';
      error.textContent = (result.error.code ? result.error.code + ': ' : '') + (result.error.message || 'Regex test failed');
      diagnostics.appendChild(error);
      return;
    }
    output.textContent = result.text === undefined ? (result.transformed || '') : result.text;
    var items = Array.isArray(result.rules) ? result.rules : (Array.isArray(result.diagnostics) ? result.diagnostics : []);
    if (!items.length) {
      var empty = document.createElement('div');
      empty.className = 'regex-drawer-hint';
      empty.textContent = 'No rule diagnostics.';
      diagnostics.appendChild(empty);
      return;
    }
    items.forEach(function(item, index) {
      var row = document.createElement('div');
      row.className = 'regex-diagnostic' + (item.error ? ' error' : '');
      var label = item.name || item.rule_id || ('Rule ' + (index + 1));
      var status = item.skipped ? 'skipped' : (item.changed ? 'changed' : (item.matched ? 'matched' : 'no match'));
      row.textContent = label + ' · ' + (item.target || 'output') + ' · ' + status;
      if (item.match_count) row.textContent += ' · ' + item.match_count + ' match' + (item.match_count === 1 ? '' : 'es');
      diagnostics.appendChild(row);
    });
  }

  function renderBindings() {
    var target = $('regex-drawer-agent-bindings');
    if (!target) return;
    var collections = state.collections.slice();
    var rows = state.agents.map(function(agent) {
      var row = document.createElement('div');
      row.className = 'regex-binding-row';
      var identity = document.createElement('div');
      identity.className = 'regex-binding-identity';
      var name = document.createElement('strong');
      name.textContent = agent.name || agent.agent_id || agent.id;
      var id = document.createElement('small');
      id.textContent = agent.agent_id || agent.id || '';
      identity.append(name, id);
      var select = document.createElement('select');
      select.className = 'regex-drawer-select';
      select.dataset.agentId = agent.agent_id || agent.id || '';
      select.setAttribute('aria-label', 'Regex Collection for ' + (agent.name || agent.agent_id || 'Agent'));
      var none = document.createElement('option');
      none.value = '';
      none.textContent = 'No collection';
      select.appendChild(none);
      collections.forEach(function(collection) {
        var option = document.createElement('option');
        option.value = collection.id;
        option.textContent = collection.name;
        select.appendChild(option);
      });
      var current = agent.regex_collection_id || '';
      if (current && !collections.some(function(collection) { return collection.id === current; })) {
        var missing = document.createElement('option');
        missing.value = current;
        missing.textContent = current + ' (missing)';
        select.appendChild(missing);
      }
      select.value = current;
      select.addEventListener('change', function() { updateAgentBinding(agent, select); });
      row.append(identity, select);
      return row;
    });
    if (!rows.length) {
      var empty = document.createElement('div');
      empty.className = 'regex-drawer-empty';
      empty.textContent = 'No Agent Definitions found.';
      rows.push(empty);
    }
    target.replaceChildren.apply(target, rows);
  }

  function renderAll() {
    renderLibrary();
    renderEditorActions();
    renderRules();
    renderTestResult();
    renderBindings();
  }

  async function loadCollections(selectId) {
    var payload = await request(endpoint);
    state.collections = Array.isArray(payload.collections) ? payload.collections : [];
    renderLibrary();
    var nextId = selectId || state.selectedId;
    if (nextId && state.collections.some(function(collection) { return collection.id === nextId; })) {
      await loadCollection(nextId);
    } else if (!state.selectedId || !state.collections.some(function(collection) { return collection.id === state.selectedId; })) {
      resetEditor();
    }
    renderBindings();
  }

  async function loadAgents() {
    var payload = await request(agentEndpoint);
    state.agents = Array.isArray(payload.agents) ? payload.agents : [];
    renderBindings();
  }

  async function loadCollection(id) {
    if (!id) return;
    try {
      var payload = await request(endpoint + '/' + encodeURIComponent(id));
      fillEditor(payload.collection);
      showNotice('Loaded ' + payload.collection.name + '.');
    } catch (error) {
      showNotice(errorMessage(error, 'Could not load this Regex Collection.'), 'error');
    }
  }

  async function loadData() {
    if (state.loading) return;
    state.loading = true;
    try {
      await Promise.all([loadCollections(), loadAgents()]);
      state.ready = true;
    } catch (error) {
      showNotice(errorMessage(error, 'Could not load Regex Collections.'), 'error');
    } finally {
      state.loading = false;
    }
  }

  function editorPayload() {
    if (!state.collection) return null;
    return {
      name: String(($('regex-drawer-name') && $('regex-drawer-name').value) || '').trim(),
      rules: state.collection.rules.map(function(rule) {
        var value = {
          name: String(rule.name || '').trim(),
          enabled: rule.enabled !== false,
          target: rule.target,
          pattern: String(rule.pattern || ''),
          flags: String(rule.flags || ''),
          replacement: String(rule.replacement || '')
        };
        if (rule.id) value.id = rule.id;
        return value;
      })
    };
  }

  async function saveCollection() {
    var payload = editorPayload();
    if (!payload || !payload.name) {
      showNotice('Regex Collection name is required.', 'error');
      if ($('regex-drawer-name')) $('regex-drawer-name').focus();
      return;
    }
    var button = $('regex-drawer-save');
    button.disabled = true;
    try {
      var path = state.selectedId ? endpoint + '/' + encodeURIComponent(state.selectedId) : endpoint;
      var response = await request(path, {
        method: state.selectedId ? 'PUT' : 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      await loadCollections(response.collection.id);
      showNotice('Regex Collection saved.', 'success');
    } catch (error) {
      showNotice(errorMessage(error, 'Could not save this Regex Collection.'), 'error');
    } finally {
      button.disabled = false;
    }
  }

  async function copyCollection() {
    if (!state.selectedId) return;
    try {
      var payload = await request(endpoint + '/' + encodeURIComponent(state.selectedId) + '/copy', {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: '{}'
      });
      await loadCollections(payload.collection.id);
      showNotice('Regex Collection copied.', 'success');
    } catch (error) {
      showNotice(errorMessage(error, 'Could not copy this Regex Collection.'), 'error');
    }
  }

  async function deleteCollection() {
    if (!state.selectedId || !global.confirm('Delete this Regex Collection?')) return;
    try {
      await request(endpoint + '/' + encodeURIComponent(state.selectedId), { method: 'DELETE' });
      await loadCollections();
      showNotice('Regex Collection deleted.', 'success');
    } catch (error) {
      showNotice(errorMessage(error, 'Could not delete this Regex Collection.'), 'error');
    }
  }

  async function testCollection() {
    var payload = editorPayload();
    if (!payload) return;
    var target = $('regex-drawer-test-target').value;
    var text = $('regex-drawer-test-input').value;
    var button = $('regex-drawer-test');
    button.disabled = true;
    try {
      var id = state.selectedId || 'preview';
      var response = await request(endpoint + '/' + encodeURIComponent(id) + '/test', {
        method: 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ collection: payload, text: text, target: target })
      });
      state.testResult = response.result || null;
      renderTestResult();
      showNotice('Test completed.', 'success');
    } catch (error) {
      state.testResult = { error: error.payload || { message: error.message } };
      renderTestResult();
      showNotice(errorMessage(error, 'Regex test failed.'), 'error');
    } finally {
      button.disabled = false;
    }
  }

  async function updateAgentBinding(agent, select) {
    var agentId = agent.agent_id || agent.id;
    if (!agentId) return;
    var value = select.value || null;
    select.disabled = true;
    try {
      var response = await request(agentEndpoint + '/' + encodeURIComponent(agentId), {
        method: 'PUT',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ regex_collection_id: value })
      });
      var updated = response.agent || response;
      agent.regex_collection_id = updated.regex_collection_id || null;
      showNotice(value ? 'Agent binding saved.' : 'Agent binding cleared.', 'success');
    } catch (error) {
      select.value = agent.regex_collection_id || '';
      showNotice(errorMessage(error, 'Could not update the Agent binding.'), 'error');
    } finally {
      select.disabled = false;
    }
  }

  function bindControls() {
    if ($('regex-drawer-library-search')) $('regex-drawer-library-search').addEventListener('input', function(event) {
      state.libraryQuery = event.target.value;
      renderLibrary();
    });
    if ($('regex-drawer-new')) $('regex-drawer-new').addEventListener('click', resetEditor);
    if ($('regex-drawer-add-rule')) $('regex-drawer-add-rule').addEventListener('click', function() {
      if (!state.collection) resetEditor();
      state.collection.rules.push(makeRule(state.collection.rules.length));
      renderRules();
    });
    if ($('regex-drawer-save')) $('regex-drawer-save').addEventListener('click', saveCollection);
    if ($('regex-drawer-copy')) $('regex-drawer-copy').addEventListener('click', copyCollection);
    if ($('regex-drawer-delete')) $('regex-drawer-delete').addEventListener('click', deleteCollection);
    if ($('regex-drawer-test')) $('regex-drawer-test').addEventListener('click', testCollection);
  }

  function onDrawerOpened(event) {
    if (!ensureMarkup()) return;
    bindControlsOnce();
    var view = event && event.detail && event.detail.view;
    if (integratedPanel()) {
      setView(view === 'regex-collections' ? 'regex-collections' : (view || 'agents'));
    } else {
      setView(view === 'regex-collections' ? 'regex-collections' : 'worldbooks');
    }
  }

  var controlsBound = false;
  function bindControlsOnce() {
    if (controlsBound || !$('regex-drawer-view')) return;
    controlsBound = true;
    bindControls();
  }

  function openRegexDrawer() {
    var regexToggle = $('studio-regex-toggle');
    if (regexToggle) regexToggle.setAttribute('aria-expanded', 'true');
    if (integratedPanel() && global.AIRPStudioAgentsDrawer && typeof global.AIRPStudioAgentsDrawer.open === 'function') {
      return Promise.resolve(global.AIRPStudioAgentsDrawer.open('regex-collections')).then(function() {
        ensureMarkup();
        bindControlsOnce();
        setView('regex-collections');
      });
    }
    if (global.AIRPWorldbookDrawer && typeof global.AIRPWorldbookDrawer.open === 'function') {
      return Promise.resolve(global.AIRPWorldbookDrawer.open()).then(function() {
        ensureMarkup();
        bindControlsOnce();
        setView('regex-collections');
      });
    }
    ensureMarkup();
    bindControlsOnce();
    setView('regex-collections');
    host.hidden = false;
    host.setAttribute('aria-hidden', 'false');
    return Promise.resolve();
  }

  ensureMarkup();
  bindControlsOnce();
  var regexToggle = $('studio-regex-toggle');
  if (regexToggle) {
    regexToggle.addEventListener('click', function () {
      var current = workspace && workspace.state && workspace.state.studioDrawer;
      var currentView = current && current.view ? String(current.view).split(':').pop() : '';
      if (current && current.open && currentView === 'regex-collections') {
        if (global.AIRPStudioAgentsDrawer && typeof global.AIRPStudioAgentsDrawer.close === 'function') global.AIRPStudioAgentsDrawer.close();
        else if (global.AIRPWorldbookDrawer && typeof global.AIRPWorldbookDrawer.close === 'function') global.AIRPWorldbookDrawer.close();
      } else {
        openRegexDrawer();
      }
    });
  }
  if (workspace && workspace.events && typeof workspace.events.addEventListener === 'function') {
    workspace.events.addEventListener('studio-drawer:opened', onDrawerOpened);
    workspace.events.addEventListener('studio:drawer-opened', onDrawerOpened);
  }
  global.AIRPRegexDrawer = Object.freeze({ open: openRegexDrawer, state: state, setView: setView });
})(window, document);
