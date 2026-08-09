/*
 * Regex Collection editor for the integrated game workspace.
 *
 * The workspace owns the shared drawer shell. Regex Collections owns a
 * dedicated surface inside that shell so its content cannot leak into the
 * Agents, orchestration, or Provider Profile view tree.
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
    '<div class="regex-drawer-view" id="regex-drawer-view" data-regex-drawer-view="regex-collections" hidden>',
      '<div class="regex-drawer-toolbar">',
        '<div class="regex-drawer-toolbar-title"><strong>正则集合</strong><span id="regex-drawer-count">已保存 0 个</span></div>',
        '<input class="regex-drawer-search" id="regex-drawer-library-search" type="search" placeholder="搜索正则集合" aria-label="搜索正则集合">',
        '<button class="regex-drawer-button primary" id="regex-drawer-new" type="button">新建</button>',
      '</div>',
      '<div class="regex-drawer-body">',
        '<aside class="regex-drawer-column regex-library-column" aria-label="正则集合库">',
          '<div class="regex-drawer-column-heading"><h2>集合库</h2><span class="regex-drawer-hint" id="regex-drawer-library-hint">可复用规则</span></div>',
          '<div class="regex-library-list" id="regex-drawer-list" aria-live="polite"></div>',
        '</aside>',
        '<main class="regex-drawer-column regex-editor-column" aria-label="正则集合编辑器">',
          '<div class="regex-editor-header">',
            '<div><div class="regex-drawer-kicker">集合定义</div><input class="regex-drawer-input regex-collection-name" id="regex-drawer-name" required autocomplete="off" placeholder="集合名称" aria-label="正则集合名称"></div>',
            '<div class="regex-editor-actions"><button class="regex-drawer-button" id="regex-drawer-copy" type="button" hidden>复制</button><button class="regex-drawer-button danger" id="regex-drawer-delete" type="button" hidden>删除</button></div>',
          '</div>',
          '<div class="regex-rule-toolbar"><div><h2>有序规则</h2><p class="regex-drawer-hint">JavaScript 正则表达式按从上到下的顺序执行。</p></div><button class="regex-drawer-button" id="regex-drawer-add-rule" type="button">添加规则</button></div>',
          '<div class="regex-rule-list" id="regex-drawer-rules"></div>',
          '<section class="regex-test-panel" aria-labelledby="regex-drawer-test-title">',
            '<div class="regex-rule-toolbar"><div><h2 id="regex-drawer-test-title">测试集合</h2><p class="regex-drawer-hint">无需保存，直接运行当前草稿。</p></div><button class="regex-drawer-button" id="regex-drawer-test" type="button">运行测试</button></div>',
            '<div class="regex-test-controls"><label>测试目标<select class="regex-drawer-select" id="regex-drawer-test-target" aria-label="测试目标"><option value="input">输入</option><option value="output" selected>输出</option></select></label><label class="regex-test-input-label">测试文本<textarea class="regex-drawer-textarea" id="regex-drawer-test-input" placeholder="在此粘贴示例文本"></textarea></label></div>',
            '<div class="regex-test-result-grid"><div><h3>结果</h3><pre class="regex-test-output" id="regex-drawer-test-output" aria-live="polite"></pre></div><div><h3>诊断</h3><div class="regex-diagnostics" id="regex-drawer-diagnostics" aria-live="polite"></div></div></div>',
          '</section>',
        '</main>',
        '<aside class="regex-drawer-column regex-binding-column" aria-label="Agent 正则集合绑定">',
          '<div class="regex-drawer-column-heading"><h2>Agent 绑定</h2><span class="regex-drawer-hint">每个 Agent 最多 1 个</span></div>',
          '<p class="regex-drawer-hint regex-binding-explanation">每个 Agent 最多选择一个正则集合。更改会通过 Agent API 立即保存。</p>',
          '<div class="regex-binding-list" id="regex-drawer-agent-bindings" aria-live="polite"></div>',
        '</aside>',
      '</div>',
      '<footer class="regex-drawer-footer">',
        '<div class="regex-drawer-notice" id="regex-drawer-notice" role="status" aria-live="polite"></div>',
        '<div class="regex-drawer-actions"><button class="regex-drawer-button primary" id="regex-drawer-save" type="button">保存集合</button></div>',
      '</footer>',
    '</div>'
  ].join('');
  var integratedHeader = [
    '<header class="studio-drawer-header regex-drawer-header">',
      '<div><p class="studio-drawer-kicker">工作室</p><h2>正则集合</h2></div>',
      '<button class="studio-icon-button" id="regex-drawer-close" type="button" aria-label="关闭正则集合抽屉" title="关闭">×</button>',
    '</header>'
  ].join('');

  function $(id) { return document.getElementById(id); }

  function clone(value) {
    return value === null || value === undefined ? value : JSON.parse(JSON.stringify(value));
  }

  function panel() {
    return integratedPanel() || legacyPanel() || document.getElementById('studio-drawer-panel');
  }

  function integratedPanel() {
    return document.getElementById('regex-drawer-panel');
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
      return (error.message || fallback) + '；仍被以下对象引用：' + payload.references.map(function(item) {
        return item.name || item.id || item.agent_id || '某个 Agent';
      }).join(', ') + '.';
    }
    return error && error.message ? error.message : fallback;
  }

  async function request(path, options) {
    var response = await fetch(path, Object.assign({ headers: { Accept: 'application/json' } }, options || {}));
    var payload = {};
    try { payload = await response.json(); } catch (_) { payload = {}; }
    if (!response.ok) {
      var error = new Error(payload.message || payload.error || '工作室请求失败');
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
    if (workspace && typeof workspace.setNavigationState === 'function') workspace.setNavigationState(view);
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
    tabs.setAttribute('aria-label', '工作室抽屉视图');
    [
      ['worldbooks', '世界书'],
      ['regex-collections', '正则集合']
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
    if (!$('regex-drawer-view')) {
      currentPanel.insertAdjacentHTML('beforeend', (integratedPanel() ? integratedHeader : '') + markup);
    }
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
      if (regexPanel) {
        regexPanel.hidden = !isRegex;
        regexPanel.classList.toggle('is-active', isRegex);
      }
      if (isRegex) {
        if (workspace && typeof workspace.activateDrawerSurface === 'function') workspace.activateDrawerSurface('regex');
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
    return { id: '', name: '规则 ' + (index + 1), enabled: true, target: 'both', pattern: '', flags: '', replacement: '' };
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
        name: value.name || ('规则 ' + (index + 1)),
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
    showNotice('新建正则集合');
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
    if (count) count.textContent = '已保存 ' + state.collections.length + ' 个';
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
      meta.textContent = ruleCount + ' 条规则';
      row.append(title, meta);
      row.addEventListener('click', function() { loadCollection(collection.id); });
      return row;
    });
    if (!rows.length) {
      var empty = document.createElement('div');
      empty.className = 'regex-drawer-empty';
      empty.textContent = state.libraryQuery ? '没有匹配的正则集合。' : '还没有正则集合。';
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
    title.textContent = rule.name || ('规则 ' + (index + 1));
    var enabledLabel = document.createElement('label');
    enabledLabel.className = 'regex-rule-enabled';
    var enabled = document.createElement('input');
    enabled.type = 'checkbox';
    enabled.checked = rule.enabled !== false;
    enabled.addEventListener('change', function() {
      rule.enabled = enabled.checked;
      card.classList.toggle('is-disabled', !rule.enabled);
    });
    enabledLabel.append(enabled, ' 启用');
    header.append(title, enabledLabel);

    var fields = document.createElement('div');
    fields.className = 'regex-rule-fields';
    var name = makeField('名称', rule.name, 'input', 'regex-rule-name');
    var target = makeField('目标', '', 'select', 'regex-rule-target');
    ['input', 'output', 'both'].forEach(function(optionValue) {
      var option = document.createElement('option');
      option.value = optionValue;
      option.textContent = { input: '输入', output: '输出', both: '输入和输出' }[optionValue];
      option.selected = optionValue === rule.target;
      target.control.appendChild(option);
    });
    var pattern = makeField('匹配表达式', rule.pattern, 'input', 'regex-rule-pattern');
    var flags = makeField('标志', rule.flags, 'input', 'regex-rule-flags');
    var replacement = makeField('替换内容', rule.replacement, 'textarea', 'regex-rule-replacement');
    [name, target, pattern, flags, replacement].forEach(function(field) { fields.appendChild(field.label); });
    name.control.addEventListener('input', function() { rule.name = name.control.value; title.textContent = rule.name || ('规则 ' + (index + 1)); });
    target.control.addEventListener('change', function() { rule.target = target.control.value; });
    pattern.control.addEventListener('input', function() { rule.pattern = pattern.control.value; });
    flags.control.addEventListener('input', function() { rule.flags = flags.control.value; });
    replacement.control.addEventListener('input', function() { rule.replacement = replacement.control.value; });

    var actions = document.createElement('div');
    actions.className = 'regex-rule-actions';
    [['上移', -1], ['下移', 1]].forEach(function(item) {
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
    remove.textContent = '移除';
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
      empty.textContent = '还没有规则。添加规则后即可开始处理文本。';
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
      output.textContent = '运行测试后可在此查看处理结果。';
      return;
    }
    if (result.error) {
      output.textContent = '测试失败。';
      var error = document.createElement('div');
      error.className = 'regex-diagnostic error';
      error.textContent = (result.error.code ? result.error.code + '：' : '') + (result.error.message || '正则测试失败');
      diagnostics.appendChild(error);
      return;
    }
    output.textContent = result.text === undefined ? (result.transformed || '') : result.text;
    var items = Array.isArray(result.rules) ? result.rules : (Array.isArray(result.diagnostics) ? result.diagnostics : []);
    if (!items.length) {
      var empty = document.createElement('div');
      empty.className = 'regex-drawer-hint';
      empty.textContent = '没有规则诊断信息。';
      diagnostics.appendChild(empty);
      return;
    }
    items.forEach(function(item, index) {
      var row = document.createElement('div');
      row.className = 'regex-diagnostic' + (item.error ? ' error' : '');
      var label = item.name || item.rule_id || ('规则 ' + (index + 1));
      var status = item.skipped ? '已跳过' : (item.changed ? '已替换' : (item.matched ? '已匹配' : '未匹配'));
      var targetLabel = { input: '输入', output: '输出', both: '输入和输出' }[item.target || 'output'];
      row.textContent = label + ' · ' + targetLabel + ' · ' + status;
      if (item.match_count) row.textContent += ' · 匹配 ' + item.match_count + ' 次';
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
      select.setAttribute('aria-label', (agent.name || agent.agent_id || 'Agent') + ' 的正则集合');
      var none = document.createElement('option');
      none.value = '';
      none.textContent = '不绑定集合';
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
        missing.textContent = current + '（已丢失）';
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
      empty.textContent = '没有找到 Agent 定义。';
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
    if (!nextId) {
      var startup = global.AIRPStartupDiagnostics;
      nextId = startup && typeof startup.preferredId === 'function'
        ? await startup.preferredId('regex', state.collections, function(collection) { return collection.id; })
        : (state.collections.length ? state.collections[0].id : null);
    }
    if (nextId && state.collections.some(function(collection) { return collection.id === nextId; })) {
      await loadCollection(nextId);
    } else if (state.collections.length) {
      await loadCollection(state.collections[0].id);
    } else {
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
      showNotice('已加载“' + payload.collection.name + '”。');
    } catch (error) {
      showNotice(errorMessage(error, '无法加载此正则集合。'), 'error');
    }
  }

  async function loadData() {
    if (state.loading) return;
    state.loading = true;
    try {
      await Promise.all([loadCollections(), loadAgents()]);
      state.ready = true;
    } catch (error) {
      showNotice(errorMessage(error, '无法加载正则集合。'), 'error');
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
      showNotice('正则集合名称不能为空。', 'error');
      if ($('regex-drawer-name')) $('regex-drawer-name').focus();
      return;
    }
    if (state.selectedId && Number.isInteger(state.collection.revision)) {
      payload.expected_revision = state.collection.revision;
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
      showNotice('正则集合已保存。', 'success');
    } catch (error) {
      var conflicts = global.AIRPStudioRevisionConflict;
      if (!(conflicts && conflicts.render($('regex-drawer-notice'), error, function (conflict) {
        return loadCollection(conflict.object_id).then(function () { showNotice('已 Reload 最新正则集合。', 'success'); });
      }))) showNotice(errorMessage(error, '无法保存此正则集合。'), 'error');
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
      showNotice('正则集合已复制。', 'success');
    } catch (error) {
      showNotice(errorMessage(error, '无法复制此正则集合。'), 'error');
    }
  }

  async function deleteCollection() {
    if (!state.selectedId || !global.confirm('删除此正则集合？')) return;
    try {
      await request(endpoint + '/' + encodeURIComponent(state.selectedId), { method: 'DELETE' });
      await loadCollections();
      showNotice('正则集合已删除。', 'success');
    } catch (error) {
      showNotice(errorMessage(error, '无法删除此正则集合。'), 'error');
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
      showNotice('测试完成。', 'success');
    } catch (error) {
      state.testResult = { error: error.payload || { message: error.message } };
      renderTestResult();
      showNotice(errorMessage(error, '正则测试失败。'), 'error');
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
        body: JSON.stringify({ regex_collection_id: value, expected_revision: agent.revision })
      });
      var updated = response.agent || response;
      Object.assign(agent, updated);
      showNotice(value ? 'Agent 绑定已保存。' : 'Agent 绑定已清除。', 'success');
    } catch (error) {
      select.value = agent.regex_collection_id || '';
      var conflicts = global.AIRPStudioRevisionConflict;
      if (!(conflicts && conflicts.render($('regex-drawer-notice'), error, function (conflict) {
        return request(conflict.reload_source).then(function (payload) {
          Object.assign(agent, payload.agent || payload);
          renderBindings();
          showNotice('已 Reload 最新 Agent 绑定。', 'success');
        });
      }))) showNotice(errorMessage(error, '无法更新 Agent 绑定。'), 'error');
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
    if ($('regex-drawer-close')) $('regex-drawer-close').addEventListener('click', function() {
      closeRegexDrawer();
    });
  }

  function onDrawerOpened(event) {
    if (!ensureMarkup()) return;
    bindControlsOnce();
    var view = event && event.detail && event.detail.view;
    if (integratedPanel()) {
      setView(view === 'regex-collections' ? 'regex-collections' : 'agents');
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
    if (integratedPanel()) {
      if (global.AIRPGameDrawer && typeof global.AIRPGameDrawer.close === 'function') global.AIRPGameDrawer.close();
      if (global.AIRPWorldbookDrawer && typeof global.AIRPWorldbookDrawer.close === 'function') global.AIRPWorldbookDrawer.close();
      if (global.AIRPStudioAgentsDrawer && typeof global.AIRPStudioAgentsDrawer.close === 'function') global.AIRPStudioAgentsDrawer.close();
      ensureMarkup();
      bindControlsOnce();
      if (workspace && workspace.drawerMotion) workspace.drawerMotion.open(host);
      else {
        host.hidden = false;
        host.setAttribute('aria-hidden', 'false');
      }
      setView('regex-collections');
      return Promise.resolve();
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

  function closeRegexDrawer() {
    if (workspace && workspace.drawerMotion) workspace.drawerMotion.close(host);
    else {
      host.hidden = true;
      host.setAttribute('aria-hidden', 'true');
    }
    if (workspace && typeof workspace.setNavigationState === 'function') workspace.setNavigationState(null);
    if (workspace && typeof workspace.patchState === 'function') workspace.patchState('studioDrawer', { open: false, view: null });
    if (workspace && typeof workspace.emit === 'function') workspace.emit('studio-drawer:closed', { view: 'regex-collections' });
  }

  ensureMarkup();
  bindControlsOnce();
  var regexToggle = $('studio-regex-toggle');
  if (regexToggle) {
    regexToggle.addEventListener('click', function () {
      var current = workspace && workspace.state && workspace.state.studioDrawer;
      var currentView = current && current.view ? String(current.view).split(':').pop() : '';
      if (current && current.open && currentView === 'regex-collections') {
        closeRegexDrawer();
      } else {
        openRegexDrawer();
      }
    });
  }
  if (!integratedPanel() && workspace && workspace.events && typeof workspace.events.addEventListener === 'function') {
    workspace.events.addEventListener('studio-drawer:opened', onDrawerOpened);
    workspace.events.addEventListener('studio:drawer-opened', onDrawerOpened);
  }
  global.AIRPRegexDrawer = Object.freeze({ open: openRegexDrawer, state: state, setView: setView });
})(window, document);
