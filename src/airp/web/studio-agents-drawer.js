/* Integrated Agents and linear Graph editor for the game workspace. */
(function installStudioAgentsDrawer(global, document) {
  'use strict';

  var host = document.getElementById('studio-drawer-host');
  if (!host) return;

  var $ = function (id) { return document.getElementById(id); };
  var qa = function (selector) { return Array.prototype.slice.call(document.querySelectorAll(selector)); };
  var state = {
    view: 'agents',
    agentEditorView: 'instruction',
    agents: [],
    providers: [],
    regexCollections: [],
    selectedAgentId: null,
    selectedAgent: null,
    agentFilter: '',
    graphs: [],
    selectedGraphId: null,
    graphFilter: '',
    graphDraft: null,
    selectedNodeIndex: -1,
    selectedLoopId: null,
    runtimeGraphId: null,
    referencesLoaded: false
  };

  function contract() {
    return global.AIRPWorkspace || null;
  }

  function emit(type, detail) {
    var api = contract();
    if (api && typeof api.emit === 'function') api.emit(type, detail || null);
  }

  function patchWorkspace(values) {
    var api = contract();
    if (api && typeof api.patchState === 'function') api.patchState('studioDrawer', values);
  }

  function setNavigation(view) {
    var api = contract();
    if (api && typeof api.setNavigationState === 'function') api.setNavigationState(view);
  }

  function openHost() {
    var api = contract();
    if (api && api.drawerMotion) api.drawerMotion.open(host);
    else {
      host.hidden = false;
      host.setAttribute('aria-hidden', 'false');
    }
  }

  function closeHost() {
    var api = contract();
    if (api && api.drawerMotion) api.drawerMotion.close(host);
    else {
      host.hidden = true;
      host.setAttribute('aria-hidden', 'true');
    }
  }

  function setStatus(message, error) {
    var target = $('studio-drawer-status');
    if (!target) return;
    target.textContent = message || '';
    target.classList.toggle('is-error', !!error);
  }

  function jsonFetch(path, options) {
    return fetch(path, Object.assign({ headers: { Accept: 'application/json' } }, options || {}))
      .then(function (response) {
        return response.text().then(function (text) {
          var payload = {};
          try { payload = text ? JSON.parse(text) : {}; } catch (_) { payload = {}; }
          if (!response.ok) {
            var error = new Error(payload.message || payload.error || '请求失败');
            error.payload = payload;
            error.status = response.status;
            throw error;
          }
          return payload;
        });
      });
  }

  function setButtonDisabled(id, disabled) {
    var button = $(id);
    if (button) button.disabled = !!disabled;
  }

  function fillSelect(select, items, placeholder, selected) {
    if (!select) return;
    select.replaceChildren();
    var empty = document.createElement('option');
    empty.value = '';
    empty.textContent = placeholder;
    select.appendChild(empty);
    (items || []).forEach(function (item) {
      var option = document.createElement('option');
      option.value = item.id || item.agent_id || item.collection_id || '';
      option.textContent = item.name || option.value;
      select.appendChild(option);
    });
    select.value = selected || '';
  }

  function optionalNumber(value) {
    var text = String(value == null ? '' : value).trim();
    return text ? Number(text) : undefined;
  }

  function readJson(id, fallback, optional) {
    var text = ($(id) && $(id).value || '').trim();
    if (!text && optional) return undefined;
    if (!text) return fallback;
    try {
      return JSON.parse(text);
    } catch (_) {
      setStatus('高级参数或预览 JSON 无法解析', true);
      return null;
    }
  }

  function openDrawer(view) {
    var nextView = view || state.view || 'agents';
    if (global.AIRPGameDrawer && typeof global.AIRPGameDrawer.close === 'function') global.AIRPGameDrawer.close();
    if (global.AIRPWorldbookDrawer && typeof global.AIRPWorldbookDrawer.close === 'function') global.AIRPWorldbookDrawer.close();
    state.view = nextView;
    var api = contract();
    if (api && typeof api.activateDrawerSurface === 'function') api.activateDrawerSurface('studio');
    else if ($('studio-drawer-panel')) $('studio-drawer-panel').hidden = false;
    openHost();
    var toggle = $('studio-agents-toggle');
    setNavigation(nextView === 'orchestration' ? 'agents' : nextView);
    var modelToggle = $('studio-model-toggle');
    if (modelToggle) modelToggle.setAttribute('aria-expanded', nextView === 'model' ? 'true' : 'false');
    var worldbookToggle = $('studio-worldbooks-toggle');
    if (worldbookToggle) worldbookToggle.setAttribute('aria-expanded', 'false');
    var regexToggle = $('studio-regex-toggle');
    if (regexToggle) regexToggle.setAttribute('aria-expanded', nextView === 'regex-collections' ? 'true' : 'false');
    var modeToggle = $('studio-drawer-mode-toggle');
    if (modeToggle) {
      modeToggle.hidden = nextView !== 'agents' && nextView !== 'orchestration';
      modeToggle.setAttribute('aria-label', nextView === 'agents' ? '切换到编排' : '切换到 Agents');
      modeToggle.title = nextView === 'agents' ? '切换到编排' : '切换到 Agents';
    }
    var title = $('studio-drawer-title');
    if (title) title.textContent = nextView === 'orchestration' ? '编排' : (nextView === 'agents' ? 'Agents' : (nextView === 'model' ? '模型' : '正则集合'));
    patchWorkspace({ open: true, view: 'agents-orchestration:' + nextView });
    emit('studio:drawer-opened', { view: nextView });
    renderDrawerView();
    if (!state.referencesLoaded) loadReferences();
    if (!state.agents.length) loadAgents();
    if (nextView === 'orchestration' && !state.graphs.length) loadGraphs();
  }

  function closeDrawer() {
    closeHost();
    setNavigation(null);
    var modelToggle = $('studio-model-toggle');
    if (modelToggle) modelToggle.setAttribute('aria-expanded', 'false');
    var regexToggle = $('studio-regex-toggle');
    if (regexToggle) regexToggle.setAttribute('aria-expanded', 'false');
    var modeToggle = $('studio-drawer-mode-toggle');
    if (modeToggle) modeToggle.hidden = true;
    patchWorkspace({ open: false, view: null });
    emit('studio:drawer-closed');
  }

  function toggleDrawer() {
    if (host.hidden) openDrawer(state.view);
    else closeDrawer();
  }

  function renderDrawerView() {
    var studioPanel = $('studio-drawer-panel');
    if (!studioPanel) return;
    Array.prototype.slice.call(studioPanel.querySelectorAll('[data-studio-drawer-panel]')).forEach(function (panel) {
      var active = panel.getAttribute('data-studio-drawer-panel') === state.view;
      panel.classList.toggle('is-active', active);
      panel.hidden = !active;
    });
  }

  function renderAgentList() {
    var list = $('studio-agent-list');
    if (!list) return;
    var filter = state.agentFilter.toLowerCase();
    var agents = state.agents.filter(function (agent) {
      return !filter || [agent.name, agent.agent_id, agent.model_id].join(' ').toLowerCase().indexOf(filter) >= 0;
    });
    $('studio-agent-count').textContent = state.agents.length + ' 个 Agent';
    list.replaceChildren();
    if (!agents.length) {
      var empty = document.createElement('p');
      empty.className = 'studio-empty';
      empty.textContent = state.agents.length ? '没有匹配的 Agent。' : '还没有 Agent。';
      list.appendChild(empty);
      return;
    }
    agents.forEach(function (agent) {
      var row = document.createElement('button');
      row.type = 'button';
      row.className = 'studio-object-item' + (agent.agent_id === state.selectedAgentId ? ' is-active' : '');
      var title = document.createElement('span');
      title.className = 'studio-object-item-title';
      title.textContent = agent.name || agent.agent_id;
      var meta = document.createElement('span');
      meta.className = 'studio-object-item-meta';
      meta.textContent = agent.model_id || '服务商默认模型';
      row.append(title, meta);
      row.addEventListener('click', function () { selectAgent(agent.agent_id); });
      list.appendChild(row);
    });
  }

  function resetAgent() {
    state.selectedAgentId = null;
    state.selectedAgent = null;
    $('studio-agent-form').reset();
    $('studio-agent-instruction').value = '';
    $('studio-agent-advanced').value = '';
    $('studio-agent-id').textContent = '';
    $('studio-agent-selection').textContent = '未选择 Agent';
    setButtonDisabled('studio-agent-copy', true);
    setButtonDisabled('studio-agent-delete', true);
    setButtonDisabled('studio-agent-preview', true);
    $('studio-agent-preview-output').replaceChildren();
    renderAgentList();
  }

  function fillAgent(agent) {
    state.selectedAgentId = agent.agent_id;
    state.selectedAgent = agent;
    var generation = agent.generation || {};
    $('studio-agent-name').value = agent.name || '';
    $('studio-agent-instruction').value = agent.instruction || '';
    $('studio-agent-provider').value = agent.provider_profile_id || '';
    $('studio-agent-model').value = agent.model_id || '';
    $('studio-agent-temperature').value = generation.temperature == null ? '' : generation.temperature;
    $('studio-agent-max-tokens').value = generation.max_output_tokens == null ? '' : generation.max_output_tokens;
    $('studio-agent-tools').value = (agent.tool_allowlist || []).join(', ');
    $('studio-agent-regex').value = agent.regex_collection_id || '';
    $('studio-agent-advanced').value = Object.keys(agent.advanced || {}).length ? JSON.stringify(agent.advanced, null, 2) : '';
    $('studio-agent-id').textContent = agent.agent_id || '';
    $('studio-agent-selection').textContent = agent.name || agent.agent_id || '未选择 Agent';
    setButtonDisabled('studio-agent-copy', false);
    setButtonDisabled('studio-agent-delete', false);
    setButtonDisabled('studio-agent-preview', false);
    renderAgentList();
  }

  function selectAgent(agentId) {
    setStatus('读取 Agent…');
    return jsonFetch('/v1/studio/agents/' + encodeURIComponent(agentId)).then(function (data) {
      fillAgent(data.agent);
      setStatus('');
      emit('studio:agent-selected', data.agent);
      return data.agent;
    }).catch(function (error) {
      setStatus(error.message || '读取 Agent 失败', true);
    });
  }

  function loadReferences() {
    return Promise.all([
      jsonFetch('/v1/studio/providers').then(function (data) {
        state.providers = data.profiles || [];
        fillSelect($('studio-agent-provider'), state.providers, 'Runtime provider', state.selectedAgent && state.selectedAgent.provider_profile_id);
      }),
      jsonFetch('/v1/studio/regex-collections').then(function (data) {
        state.regexCollections = data.collections || [];
        fillSelect($('studio-agent-regex'), state.regexCollections, '不绑定正则集合', state.selectedAgent && state.selectedAgent.regex_collection_id);
      })
    ]).then(function () {
      state.referencesLoaded = true;
      fillAgentSelects();
      if (state.graphDraft) renderTopology();
    }).catch(function (error) {
      setStatus(error.message || '读取服务商或正则集合失败', true);
    });
  }

  function loadAgents() {
    return jsonFetch('/v1/studio/agents').then(function (data) {
      state.agents = data.agents || [];
      renderAgentList();
      fillAgentSelects();
      if (state.graphDraft) renderTopology();
      if (state.selectedAgentId) {
        var current = state.agents.find(function (item) { return item.agent_id === state.selectedAgentId; });
        if (current && !state.selectedAgent) fillAgent(current);
      } else if (state.agents.length) {
        return selectAgent(state.agents[0].agent_id);
      }
    }).catch(function (error) {
      setStatus(error.message || '读取 Agents 失败', true);
    });
  }

  function agentPayload() {
    var advanced = readJson('studio-agent-advanced', {});
    if (advanced === null || typeof advanced !== 'object' || Array.isArray(advanced)) {
      setStatus('高级参数必须是 JSON 对象', true);
      return null;
    }
    var generation = {};
    var temperature = optionalNumber($('studio-agent-temperature').value);
    var maxTokens = optionalNumber($('studio-agent-max-tokens').value);
    if (temperature !== undefined) generation.temperature = temperature;
    if (maxTokens !== undefined) generation.max_output_tokens = maxTokens;
    return {
      name: $('studio-agent-name').value.trim(),
      instruction: $('studio-agent-instruction').value,
      provider_profile_id: $('studio-agent-provider').value || null,
      model_id: $('studio-agent-model').value.trim() || null,
      generation: generation,
      advanced: advanced,
      tool_allowlist: $('studio-agent-tools').value.split(',').map(function (item) { return item.trim(); }).filter(Boolean),
      regex_collection_id: $('studio-agent-regex').value || null
    };
  }

  function saveAgent(event) {
    event.preventDefault();
    var payload = agentPayload();
    if (!payload || !payload.name) {
      setStatus('Agent 名称不能为空', true);
      return;
    }
    var endpoint = state.selectedAgentId ? '/v1/studio/agents/' + encodeURIComponent(state.selectedAgentId) : '/v1/studio/agents';
    setStatus('保存中…');
    jsonFetch(endpoint, {
      method: state.selectedAgentId ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (data) {
      fillAgent(data.agent);
      return loadAgents();
    }).then(function () {
      setStatus('Agent 已保存');
      emit('studio:agent-saved', state.selectedAgent);
    }).catch(function (error) {
      setStatus(error.message || '保存 Agent 失败', true);
    });
  }

  function copyAgent() {
    if (!state.selectedAgentId) return;
    setStatus('复制中…');
    jsonFetch('/v1/studio/agents/' + encodeURIComponent(state.selectedAgentId) + '/copy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: '{}'
    }).then(function (data) {
      fillAgent(data.agent);
      return loadAgents();
    }).then(function () { setStatus('Agent 已复制'); }).catch(function (error) { setStatus(error.message || '复制 Agent 失败', true); });
  }

  function deleteAgent() {
    if (!state.selectedAgentId || !global.confirm('删除当前 Agent？')) return;
    setStatus('删除中…');
    jsonFetch('/v1/studio/agents/' + encodeURIComponent(state.selectedAgentId), { method: 'DELETE' })
      .then(function () { resetAgent(); return loadAgents(); })
      .then(function () { setStatus('Agent 已删除'); })
      .catch(function (error) { setStatus(error.message || '删除 Agent 失败', true); });
  }

  function editorView(view) {
    state.agentEditorView = view;
    qa('[data-agent-editor-view]').forEach(function (button) {
      var active = button.getAttribute('data-agent-editor-view') === view;
      button.classList.toggle('is-active', active);
      button.setAttribute('aria-selected', active ? 'true' : 'false');
    });
    qa('[data-agent-editor-pane]').forEach(function (pane) {
      var active = pane.getAttribute('data-agent-editor-pane') === view;
      pane.classList.toggle('is-active', active);
      pane.hidden = !active;
    });
  }

  function previewJson(id, fallback, optional) {
    var value = readJson(id, fallback, optional);
    if (value === null) throw new Error('预览 JSON 无法解析');
    return value;
  }

  function withActiveProjectCard(context) {
    return jsonFetch('/v1/session/project').then(function (data) {
      var project = data && data.project;
      if (!project || typeof project !== 'object') return context;
      var prompt = project.card_prompt && typeof project.card_prompt === 'object' ? project.card_prompt : {};
      var openings = Array.isArray(project.openings) ? project.openings : [];
      var opening = openings.filter(function (item) { return item && item.is_default; })[0] || openings[0] || {};
      var cardFacts = {
        name: project.name || '',
        avatar: project.avatar || '',
        description: project.description || '',
        personality: project.personality || '',
        scenario: project.scenario || '',
        system_prompt: prompt.system || '',
        post_history_instructions: prompt.post_history || '',
        first_mes: opening.content || '',
        alternate_greetings: openings.filter(function (item) { return item && item !== opening; }).map(function (item) { return item.content || ''; })
      };
      var supplied = context && typeof context === 'object' ? context : {};
      var suppliedFacts = supplied.card_facts && typeof supplied.card_facts === 'object' ? supplied.card_facts : {};
      return Object.assign({}, supplied, { card_facts: Object.assign(cardFacts, suppliedFacts) });
    }).catch(function () {
      // Preview remains usable with manually supplied Runtime context when no game is active.
      return context;
    });
  }

  function compilePreview() {
    if (!state.selectedAgentId) return;
    var body;
    try {
      body = {
        project_input: $('studio-preview-input').value,
        context: previewJson('studio-preview-context', {}, false),
        handoff: previewJson('studio-preview-handoff', undefined, true),
        output_contract: previewJson('studio-preview-contract', undefined, true)
      };
    } catch (_) { return; }
    Object.keys(body).forEach(function (key) { if (body[key] === undefined) delete body[key]; });
    setStatus('编译提示词…');
    withActiveProjectCard(body.context).then(function (context) {
      body.context = context;
      return jsonFetch('/v1/studio/agents/' + encodeURIComponent(state.selectedAgentId) + '/prompt-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify(body)
      });
    }).then(function (data) {
      renderPreview(data.preview || {});
      setStatus('提示词预览已更新');
      emit('studio:agent-previewed', data.preview);
    }).catch(function (error) { setStatus(error.message || '编译提示词失败', true); });
  }

  function renderPreview(preview) {
    var target = $('studio-agent-preview-output');
    target.replaceChildren();
    var messages = preview.messages || [];
    if (!messages.length) {
      var empty = document.createElement('p');
      empty.className = 'studio-empty';
      empty.textContent = '预览没有返回消息。';
      target.appendChild(empty);
      return;
    }
    messages.forEach(function (message) {
      var item = document.createElement('article');
      item.className = 'studio-preview-message';
      var label = document.createElement('strong');
      var sourceLabels = { instruction: '指令', project_input: '游戏输入', handoff: '交接内容', tool_protocol: '工具协议', output_contract: '输出契约' };
      var roleLabels = { system: '系统', user: '用户', assistant: '助手' };
      label.textContent = (sourceLabels[message.source] || message.source || '运行时') + ' · ' + (roleLabels[message.role] || message.role || '消息');
      var content = document.createElement('span');
      content.textContent = message.content || '';
      item.append(label, content);
      target.appendChild(item);
    });
  }

  function renderGraphList() {
    var list = $('studio-graph-list');
    if (!list) return;
    var filter = state.graphFilter.toLowerCase();
    var graphs = state.graphs.filter(function (graph) {
      return !filter || [graph.name, graph.id, graph.graph_id].join(' ').toLowerCase().indexOf(filter) >= 0;
    });
    $('studio-graph-count').textContent = state.graphs.length + ' 个 Graph';
    list.replaceChildren();
    if (!graphs.length) {
      var empty = document.createElement('p');
      empty.className = 'studio-empty';
      empty.textContent = state.graphs.length ? '没有匹配的 Graph。' : '还没有 Graph。';
      list.appendChild(empty);
      return;
    }
    graphs.forEach(function (graph) {
      var id = graph.id || graph.graph_id;
      var row = document.createElement('button');
      row.type = 'button';
      row.className = 'studio-object-item' + (id === state.selectedGraphId ? ' is-active' : '');
      var title = document.createElement('span');
      title.className = 'studio-object-item-title';
      title.textContent = graph.name || id;
      var meta = document.createElement('span');
      meta.className = 'studio-object-item-meta';
      meta.textContent = (graph.nodes || []).length + ' 个节点' + (id === state.runtimeGraphId ? ' · 运行中' : '');
      row.append(title, meta);
      row.addEventListener('click', function () { selectGraph(id); });
      list.appendChild(row);
    });
  }

  function resetGraph() {
    state.selectedGraphId = null;
    state.graphDraft = { id: '', name: '', mode: 'handoff', nodes: [], loops: [], output_node_id: '' };
    state.selectedNodeIndex = -1;
    state.selectedLoopId = null;
    $('studio-graph-form').reset();
    $('studio-graph-id').value = '';
    $('studio-graph-name').value = '';
    $('studio-graph-selection').textContent = '新建编排图';
    $('studio-graph-title').textContent = '接力链';
    $('studio-graph-mode').value = 'handoff';
    setButtonDisabled('studio-graph-use', true);
    setButtonDisabled('studio-graph-copy', true);
    setButtonDisabled('studio-graph-delete', true);
    renderTopology();
    fillNodeSettings(null);
    renderGraphList();
  }

  function normalizeGraph(graph) {
    var copy = JSON.parse(JSON.stringify(graph || {}));
    copy.id = copy.id || copy.graph_id || '';
    copy.name = copy.name || copy.id || '';
    copy.nodes = Array.isArray(copy.nodes) ? copy.nodes : [];
    copy.nodes.forEach(function (node, index) {
      node.node_id = node.node_id || node.id || 'node-' + (index + 1);
      node.order = index;
      node.enabled = node.enabled !== false;
      node.handoff_prompt = typeof node.handoff_prompt === 'string' ? node.handoff_prompt : '';
    });
    copy.mode = copy.mode === 'handoff' ? 'handoff' : 'sequential';
    copy.loops = Array.isArray(copy.loops) ? copy.loops : [];
    copy.loops.forEach(function (loop, index) {
      loop.id = loop.id || loop.loop_id || 'loop-' + (index + 1);
      loop.mode = 'fixed';
      loop.iterations = Number.isInteger(loop.iterations) ? loop.iterations : Number(loop.count) || 1;
      if (typeof loop.exit_handoff_prompt !== 'string') delete loop.exit_handoff_prompt;
    });
    copy.output_node_id = copy.output_node_id || '';
    return copy;
  }

  function fillGraph(graph) {
    state.selectedGraphId = graph.id || graph.graph_id;
    state.graphDraft = normalizeGraph(graph);
    state.selectedNodeIndex = state.graphDraft.nodes.length ? 0 : -1;
    state.selectedLoopId = null;
    $('studio-graph-id').value = state.graphDraft.id || '';
    $('studio-graph-name').value = state.graphDraft.name || '';
    $('studio-graph-mode').value = state.graphDraft.mode;
    $('studio-graph-selection').textContent = state.graphDraft.name || state.graphDraft.id || '未选择 Graph';
    $('studio-graph-title').textContent = state.graphDraft.name || '接力链';
    setButtonDisabled('studio-graph-use', false);
    setButtonDisabled('studio-graph-copy', false);
    setButtonDisabled('studio-graph-delete', false);
    renderGraphList();
    renderTopology();
    fillNodeSettings(currentNode());
    fillLoopSettings(null);
  }

  function currentNode() {
    return state.graphDraft && state.graphDraft.nodes[state.selectedNodeIndex] || null;
  }

  function fillAgentSelects() {
    fillSelect($('studio-node-agent'), state.agents, '选择 Agent', currentNode() && currentNode().agent_id);
  }

  function fillNodeSettings(node) {
    $('studio-node-id').value = node ? node.node_id || '' : '';
    $('studio-node-label').value = node ? node.label || '' : '';
    $('studio-node-enabled').checked = !node || node.enabled !== false;
    $('studio-node-handoff').value = node ? node.handoff_prompt || '' : '';
    $('studio-node-handoff').disabled = !node || node.node_id === finalEnabledNodeId();
    fillAgentSelects();
    setButtonDisabled('studio-node-agent-open', !node || !node.agent_id);
    setButtonDisabled('studio-node-remove', !node);
    refreshOutputOptions();
    refreshLoopNodeOptions();
  }

  function refreshOutputOptions() {
    var select = $('studio-graph-output');
    if (!select) return;
    var nodes = state.graphDraft ? state.graphDraft.nodes : [];
    select.replaceChildren();
    var empty = document.createElement('option');
    empty.value = '';
    empty.textContent = '选择输出节点';
    select.appendChild(empty);
    nodes.forEach(function (node) {
      if (node.enabled === false) return;
      var option = document.createElement('option');
      option.value = node.node_id;
      option.textContent = (node.label || node.node_id) + (node.node_id === finalEnabledNodeId() ? ' · 最终节点' : '');
      option.disabled = node.node_id !== finalEnabledNodeId();
      select.appendChild(option);
    });
    select.value = state.graphDraft && state.graphDraft.output_node_id || '';
  }

  function finalEnabledNodeId() {
    if (!state.graphDraft) return '';
    var enabled = state.graphDraft.nodes.filter(function (node) { return node.enabled !== false; });
    return enabled.length ? enabled[enabled.length - 1].node_id : '';
  }

  function ensureValidOutput() {
    if (!state.graphDraft) return;
    var valid = state.graphDraft.nodes.some(function (node) { return node.node_id === state.graphDraft.output_node_id && node.enabled !== false; });
    var finalNode = finalEnabledNodeId();
    if (!valid || state.graphDraft.output_node_id === '' || state.graphDraft.output_node_id !== finalNode) state.graphDraft.output_node_id = finalNode;
  }

  function syncNodeFields() {
    var node = currentNode();
    if (!node) return;
    var oldNodeId = node.node_id;
    var nextNodeId = $('studio-node-id').value.trim() || oldNodeId;
    node.node_id = nextNodeId;
    node.label = $('studio-node-label').value.trim();
    node.agent_id = $('studio-node-agent').value;
    node.enabled = $('studio-node-enabled').checked;
    node.handoff_prompt = $('studio-node-handoff').value;
    node.order = state.selectedNodeIndex;
    if (nextNodeId !== oldNodeId) {
      state.graphDraft.loops.forEach(function (loop) {
        if (loop.start_node_id === oldNodeId) loop.start_node_id = nextNodeId;
        if (loop.end_node_id === oldNodeId) loop.end_node_id = nextNodeId;
      });
    }
    ensureValidOutput();
  }

  function currentLoop() {
    if (!state.graphDraft || !state.selectedLoopId) return null;
    return state.graphDraft.loops.find(function (loop) { return loop.id === state.selectedLoopId; }) || null;
  }

  function fillLoopNodeSelect(select, selected, placeholder) {
    if (!select) return;
    select.replaceChildren();
    var empty = document.createElement('option');
    empty.value = '';
    empty.textContent = placeholder;
    select.appendChild(empty);
    (state.graphDraft ? state.graphDraft.nodes : []).forEach(function (node) {
      if (node.enabled === false) return;
      var option = document.createElement('option');
      option.value = node.node_id;
      option.textContent = node.label || node.node_id;
      select.appendChild(option);
    });
    select.value = selected || '';
  }

  function refreshLoopNodeOptions() {
    var loop = currentLoop();
    var start = $('studio-loop-start');
    var end = $('studio-loop-end');
    fillLoopNodeSelect(start, loop ? loop.start_node_id : start.value, '选择起点');
    fillLoopNodeSelect(end, loop ? loop.end_node_id : end.value, '选择终点');
    renderLoopList();
  }

  function fillLoopSettings(loop) {
    state.selectedLoopId = loop && loop.id || null;
    fillLoopNodeSelect($('studio-loop-start'), loop && loop.start_node_id, '选择起点');
    fillLoopNodeSelect($('studio-loop-end'), loop && loop.end_node_id, '选择终点');
    $('studio-loop-iterations').value = loop ? loop.iterations : 2;
    $('studio-loop-exit-handoff').value = loop ? loop.exit_handoff_prompt || '' : '';
    $('studio-loop-save').textContent = loop ? '更新循环' : '添加循环';
    setButtonDisabled('studio-loop-remove', !loop);
    renderLoopList();
  }

  function loopPayloadFromFields() {
    var start = $('studio-loop-start').value;
    var end = $('studio-loop-end').value;
    var iterations = Number($('studio-loop-iterations').value);
    if (!start || !end) {
      setStatus('循环需要起点和终点', true);
      return null;
    }
    if (!Number.isInteger(iterations) || iterations < 1 || iterations > 100) {
      setStatus('循环次数必须在 1 到 100 之间', true);
      return null;
    }
    var loop = currentLoop() || { id: 'loop-' + (state.graphDraft.loops.length + 1) };
    loop.mode = 'fixed';
    loop.start_node_id = start;
    loop.end_node_id = end;
    loop.iterations = iterations;
    var exitPrompt = $('studio-loop-exit-handoff').value;
    if (exitPrompt) loop.exit_handoff_prompt = exitPrompt;
    else delete loop.exit_handoff_prompt;
    return loop;
  }

  function saveLoop() {
    if (!state.graphDraft) return;
    syncNodeFields();
    var loop = loopPayloadFromFields();
    if (!loop) return;
    var index = state.graphDraft.loops.findIndex(function (entry) { return entry.id === loop.id; });
    if (index < 0) state.graphDraft.loops.push(loop);
    else state.graphDraft.loops[index] = loop;
    state.graphDraft.mode = 'handoff';
    $('studio-graph-mode').value = 'handoff';
    state.selectedLoopId = loop.id;
    renderTopology();
    fillLoopSettings(loop);
    setStatus('固定循环已加入未保存的接力链');
  }

  function removeLoop() {
    if (!state.graphDraft || !state.selectedLoopId) return;
    state.graphDraft.loops = state.graphDraft.loops.filter(function (loop) { return loop.id !== state.selectedLoopId; });
    state.selectedLoopId = null;
    renderTopology();
    fillLoopSettings(null);
  }

  function renderLoopList() {
    var target = $('studio-loop-list');
    if (!target) return;
    target.replaceChildren();
    var loops = state.graphDraft && state.graphDraft.loops || [];
    if (!loops.length) {
      var empty = document.createElement('p');
      empty.className = 'studio-loop-empty';
      empty.textContent = '未配置固定循环。';
      target.appendChild(empty);
      return;
    }
    loops.forEach(function (loop) {
      var button = document.createElement('button');
      button.type = 'button';
      button.className = 'studio-loop-item' + (loop.id === state.selectedLoopId ? ' is-active' : '');
      button.textContent = (loop.start_node_id || '?') + ' → ' + (loop.end_node_id || '?') + ' · ' + loop.iterations + ' 次';
      button.addEventListener('click', function () { fillLoopSettings(loop); });
      target.appendChild(button);
    });
  }

  function loopsEndingAt(nodeId) {
    return state.graphDraft && state.graphDraft.loops.filter(function (loop) {
      return loop.end_node_id === nodeId;
    }) || [];
  }

  function renderTopology() {
    var target = $('studio-graph-topology');
    var empty = $('studio-graph-empty');
    if (!target || !state.graphDraft) return;
    ensureValidOutput();
    target.replaceChildren();
    var nodes = state.graphDraft.nodes;
    empty.hidden = nodes.length > 0;
    if (!nodes.length) return;
    nodes.forEach(function (node, index) {
      var card = document.createElement('article');
      card.className = 'studio-topology-node' + (index === state.selectedNodeIndex ? ' is-active' : '') + (node.enabled === false ? ' is-disabled' : '') + (node.node_id === state.graphDraft.output_node_id ? ' is-output' : '');
      card.addEventListener('click', function () {
        syncNodeFields();
        state.selectedNodeIndex = index;
        renderTopology();
        fillNodeSettings(node);
      });
      var head = document.createElement('div');
      head.className = 'studio-topology-node-head';
      var title = document.createElement('span');
      title.className = 'studio-topology-node-title';
      title.textContent = node.label || node.node_id;
      var status = document.createElement('span');
      status.className = 'studio-topology-node-state';
      status.textContent = node.enabled === false ? '停用' : (node.node_id === state.graphDraft.output_node_id ? '输出' : '启用');
      head.append(title, status);
      var meta = document.createElement('div');
      meta.className = 'studio-topology-node-meta';
      var agent = state.agents.find(function (item) { return item.agent_id === node.agent_id; });
      meta.textContent = (agent && agent.name || node.agent_id || '未配置 Agent') + ' · #' + (index + 1);
      card.appendChild(head);
      card.appendChild(meta);
      if (node.node_id === state.graphDraft.output_node_id) {
        var output = document.createElement('span');
        output.className = 'studio-output-label';
        output.textContent = '输出节点';
        card.appendChild(output);
      }
      var actions = document.createElement('div');
      actions.className = 'studio-topology-node-actions';
      [['↑', index === 0, -1], ['↓', index === nodes.length - 1, 1]].forEach(function (item) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'studio-order-button';
        button.textContent = item[0];
        button.title = item[2] < 0 ? '上移节点' : '下移节点';
        button.disabled = item[1];
        button.addEventListener('click', function (event) {
          event.stopPropagation();
          syncNodeFields();
          var next = index + item[2];
          var moved = state.graphDraft.nodes.splice(index, 1)[0];
          state.graphDraft.nodes.splice(next, 0, moved);
          state.graphDraft.nodes.forEach(function (entry, order) { entry.order = order; });
          state.selectedNodeIndex = next;
          ensureValidOutput();
          renderTopology();
          fillNodeSettings(currentNode());
        });
        actions.appendChild(button);
      });
      card.appendChild(actions);
      target.appendChild(card);

      loopsEndingAt(node.node_id).forEach(function (loop) {
        var marker = document.createElement('button');
        marker.type = 'button';
        marker.className = 'studio-loop-marker' + (loop.id === state.selectedLoopId ? ' is-active' : '');
        marker.textContent = '固定循环 · ' + loop.start_node_id + ' → ' + loop.end_node_id + ' · ' + loop.iterations + ' 次';
        marker.title = '编辑固定循环';
        marker.addEventListener('click', function (event) {
          event.stopPropagation();
          fillLoopSettings(loop);
        });
        target.appendChild(marker);
      });

      var next = nodes.slice(index + 1).find(function (candidate) { return candidate.enabled !== false; });
      if (next) {
        var edge = document.createElement('button');
        edge.type = 'button';
        edge.className = 'studio-handoff-edge' + (node.node_id === state.graphDraft.output_node_id ? ' is-output' : '');
        var edgeTitle = document.createElement('span');
        edgeTitle.className = 'studio-handoff-edge-title';
        edgeTitle.textContent = state.graphDraft.mode === 'handoff'
          ? '交接 → ' + (next.label || next.node_id)
          : '顺序传递 → ' + (next.label || next.node_id);
        var edgePrompt = document.createElement('span');
        edgePrompt.className = 'studio-handoff-edge-prompt';
        edgePrompt.textContent = state.graphDraft.mode === 'handoff'
          ? (node.handoff_prompt || '不加额外提示词，直接传递产物')
          : '兼容旧图：直接传递上游产物';
        edge.append(edgeTitle, edgePrompt);
        edge.addEventListener('click', function (event) {
          event.stopPropagation();
          state.selectedNodeIndex = index;
          renderTopology();
          fillNodeSettings(node);
          $('studio-node-handoff').focus();
        });
        target.appendChild(edge);
      }
    });
    refreshOutputOptions();
  }

  function selectGraph(graphId) {
    setStatus('读取编排图…');
    return jsonFetch('/v1/studio/graphs/' + encodeURIComponent(graphId)).then(function (data) {
      fillGraph(data.graph);
      setStatus('');
      emit('studio:graph-selected', data.graph);
      return data.graph;
    }).catch(function (error) { setStatus(error.message || '读取编排图失败', true); });
  }

  function loadGraphs() {
    return jsonFetch('/v1/studio/graphs').then(function (data) {
      state.graphs = data.graphs || [];
      renderGraphList();
      if (state.selectedGraphId) {
        var current = state.graphs.find(function (item) { return (item.id || item.graph_id) === state.selectedGraphId; });
        if (current && !state.graphDraft) return selectGraph(state.selectedGraphId);
      } else if (state.graphs.length) {
        return selectGraph(state.graphs[0].id || state.graphs[0].graph_id);
      }
    }).catch(function (error) { setStatus(error.message || '读取编排图列表失败', true); });
  }

  function loadRuntimeGraph() {
    return jsonFetch('/v1/session/runtime/graph').then(function (data) {
      state.runtimeGraphId = data.selected && data.selected.graph_id || null;
      $('studio-graph-runtime-state').textContent = state.runtimeGraphId ? '运行时：' + state.runtimeGraphId : '运行时未选择编排图';
      renderGraphList();
      emit('studio:runtime-graph-loaded', data);
    }).catch(function () {
      $('studio-graph-runtime-state').textContent = '运行时选择不可用';
    });
  }

  function addGraphNode() {
    if (!state.graphDraft) resetGraph();
    syncNodeFields();
    var index = state.graphDraft.nodes.length + 1;
    var firstAgent = state.agents[0];
    state.graphDraft.nodes.push({ node_id: 'node-' + index, label: '', agent_id: firstAgent ? firstAgent.agent_id : '', enabled: true, order: index - 1 });
    state.selectedNodeIndex = state.graphDraft.nodes.length - 1;
    ensureValidOutput();
    renderTopology();
    fillNodeSettings(currentNode());
  }

  function removeGraphNode() {
    if (!state.graphDraft || state.selectedNodeIndex < 0) return;
    var removed = state.graphDraft.nodes.splice(state.selectedNodeIndex, 1)[0];
    state.graphDraft.loops = state.graphDraft.loops.filter(function (loop) {
      return loop.start_node_id !== removed.node_id && loop.end_node_id !== removed.node_id;
    });
    if (currentLoop() === null) state.selectedLoopId = null;
    state.graphDraft.nodes.forEach(function (node, index) { node.order = index; });
    state.selectedNodeIndex = Math.min(state.selectedNodeIndex, state.graphDraft.nodes.length - 1);
    ensureValidOutput();
    renderTopology();
    fillNodeSettings(currentNode());
  }

  function graphPayload() {
    syncNodeFields();
    if (!state.graphDraft || !state.graphDraft.name.trim()) {
      setStatus('编排图名称不能为空', true);
      return null;
    }
    if (!state.graphDraft.nodes.length) {
      setStatus('编排图至少需要一个节点', true);
      return null;
    }
    ensureValidOutput();
    if (!state.graphDraft.output_node_id) {
      setStatus('请设置输出节点', true);
      return null;
    }
    state.graphDraft.nodes.forEach(function (node, index) { node.order = index; });
    return {
      id: state.selectedGraphId ? state.selectedGraphId : ($('studio-graph-id').value.trim() || undefined),
      name: state.graphDraft.name.trim(),
      mode: state.graphDraft.mode,
      nodes: state.graphDraft.nodes,
      loops: state.graphDraft.mode === 'handoff' ? state.graphDraft.loops : [],
      output_node_id: state.graphDraft.output_node_id
    };
  }

  function saveGraph(event) {
    event.preventDefault();
    if (state.graphDraft) state.graphDraft.name = $('studio-graph-name').value;
    var payload = graphPayload();
    if (!payload) return;
    var endpoint = state.selectedGraphId ? '/v1/studio/graphs/' + encodeURIComponent(state.selectedGraphId) : '/v1/studio/graphs';
    setStatus('保存中…');
    jsonFetch(endpoint, {
      method: state.selectedGraphId ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (data) {
      fillGraph(data.graph);
      return loadGraphs();
    }).then(function () {
      setStatus('编排图已保存');
      emit('studio:graph-saved', state.graphDraft);
    }).catch(function (error) { setStatus(error.message || '保存编排图失败', true); });
  }

  function useGraph() {
    if (!state.selectedGraphId) return;
    setStatus('更新运行时选择…');
    jsonFetch('/v1/session/runtime/graph', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ graph_id: state.selectedGraphId })
    }).then(function (data) {
      state.runtimeGraphId = state.selectedGraphId;
      $('studio-graph-runtime-state').textContent = '运行时：' + state.runtimeGraphId;
      renderGraphList();
      setStatus('运行时选择已保存');
      emit('studio:runtime-graph-selected', data);
    }).catch(function (error) { setStatus(error.message || '更新运行时选择失败', true); });
  }

  function copyGraph() {
    if (!state.selectedGraphId) return;
    setStatus('复制中…');
    jsonFetch('/v1/studio/graphs/' + encodeURIComponent(state.selectedGraphId) + '/copy', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: '{}'
    }).then(function (data) {
      fillGraph(data.graph);
      return loadGraphs();
    }).then(function () { setStatus('编排图已复制'); }).catch(function (error) { setStatus(error.message || '复制编排图失败', true); });
  }

  function deleteGraph() {
    if (!state.selectedGraphId || !global.confirm('删除当前编排图？')) return;
    setStatus('删除中…');
    jsonFetch('/v1/studio/graphs/' + encodeURIComponent(state.selectedGraphId), { method: 'DELETE' })
      .then(function () { resetGraph(); return loadGraphs(); })
      .then(function () { setStatus('编排图已删除'); })
      .catch(function (error) { setStatus(error.message || '删除编排图失败', true); });
  }

  function openNodeAgent() {
    var node = currentNode();
    if (!node || !node.agent_id) return;
    state.view = 'agents';
    renderDrawerView();
    openDrawer('agents');
    selectAgent(node.agent_id);
  }

  function bind() {
    $('studio-agents-toggle').addEventListener('click', function() {
      var current = global.AIRPWorkspace && global.AIRPWorkspace.state && global.AIRPWorkspace.state.studioDrawer;
      var currentView = current && current.view || '';
      var agentsView = currentView === 'agents-orchestration:agents';
      if (!host.hidden && agentsView) closeDrawer();
      else openDrawer('agents');
    });
    $('studio-drawer-close').addEventListener('click', closeDrawer);
    $('studio-drawer-mode-toggle').addEventListener('click', function () {
      openDrawer(state.view === 'agents' ? 'orchestration' : 'agents');
    });
    qa('[data-agent-editor-view]').forEach(function (button) {
      button.addEventListener('click', function () { editorView(button.getAttribute('data-agent-editor-view')); });
    });
    $('studio-agent-search').addEventListener('input', function (event) { state.agentFilter = event.target.value; renderAgentList(); });
    $('studio-graph-search').addEventListener('input', function (event) { state.graphFilter = event.target.value; renderGraphList(); });
    $('studio-agent-new').addEventListener('click', resetAgent);
    $('studio-agent-form').addEventListener('submit', saveAgent);
    $('studio-agent-copy').addEventListener('click', copyAgent);
    $('studio-agent-delete').addEventListener('click', deleteAgent);
    $('studio-agent-preview').addEventListener('click', compilePreview);
    $('studio-graph-new').addEventListener('click', resetGraph);
    $('studio-graph-add-node').addEventListener('click', addGraphNode);
    $('studio-graph-form').addEventListener('submit', saveGraph);
    $('studio-graph-use').addEventListener('click', useGraph);
    $('studio-graph-copy').addEventListener('click', copyGraph);
    $('studio-graph-delete').addEventListener('click', deleteGraph);
    $('studio-node-remove').addEventListener('click', removeGraphNode);
    $('studio-node-agent-open').addEventListener('click', openNodeAgent);
    $('studio-node-id').addEventListener('input', syncNodeFields);
    $('studio-node-label').addEventListener('input', syncNodeFields);
    $('studio-node-agent').addEventListener('change', syncNodeFields);
    $('studio-node-handoff').addEventListener('input', function () { syncNodeFields(); renderTopology(); });
    $('studio-node-enabled').addEventListener('change', function () { syncNodeFields(); renderTopology(); fillNodeSettings(currentNode()); });
    $('studio-graph-mode').addEventListener('change', function (event) {
      if (!state.graphDraft) return;
      state.graphDraft.mode = event.target.value === 'handoff' ? 'handoff' : 'sequential';
      renderTopology();
    });
    $('studio-loop-save').addEventListener('click', saveLoop);
    $('studio-loop-remove').addEventListener('click', removeLoop);
    $('studio-graph-output').addEventListener('change', function (event) { if (state.graphDraft) state.graphDraft.output_node_id = event.target.value; renderTopology(); });
    document.addEventListener('keydown', function (event) { if (event.key === 'Escape' && !host.hidden) closeDrawer(); });
  }

  bind();
  resetAgent();
  resetGraph();
  editorView('instruction');
  loadRuntimeGraph();
  global.AIRPStudioAgentsDrawer = Object.freeze({ open: openDrawer, close: closeDrawer, state: state });
})(window, document);
