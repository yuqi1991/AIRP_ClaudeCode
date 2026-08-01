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
      setStatus('Advanced JSON 或预览 JSON 无法解析', true);
      return null;
    }
  }

  function openDrawer(view) {
    var nextView = view || state.view || 'agents';
    if (global.AIRPGameDrawer && typeof global.AIRPGameDrawer.close === 'function') global.AIRPGameDrawer.close();
    if (global.AIRPWorldbookDrawer && typeof global.AIRPWorldbookDrawer.close === 'function') global.AIRPWorldbookDrawer.close();
    state.view = nextView;
    host.hidden = false;
    host.setAttribute('aria-hidden', 'false');
    var toggle = $('studio-agents-toggle');
    if (toggle) toggle.setAttribute('aria-expanded', 'true');
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
    if (title) title.textContent = nextView === 'orchestration' ? '编排' : (nextView === 'agents' ? 'Agents' : (nextView === 'model' ? '模型' : 'Regex Collections'));
    patchWorkspace({ open: true, view: 'agents-orchestration:' + nextView });
    emit('studio:drawer-opened', { view: nextView });
    renderDrawerView();
    if (!state.referencesLoaded) loadReferences();
    if (!state.agents.length) loadAgents();
    if (nextView === 'orchestration' && !state.graphs.length) loadGraphs();
  }

  function closeDrawer() {
    host.hidden = true;
    host.setAttribute('aria-hidden', 'true');
    var toggle = $('studio-agents-toggle');
    if (toggle) toggle.setAttribute('aria-expanded', 'false');
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
    qa('[data-studio-drawer-panel]').forEach(function (panel) {
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
      meta.textContent = agent.model_id || 'Provider default';
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
        fillSelect($('studio-agent-regex'), state.regexCollections, '不绑定 Regex Collection', state.selectedAgent && state.selectedAgent.regex_collection_id);
      })
    ]).then(function () {
      state.referencesLoaded = true;
      fillAgentSelects();
      if (state.graphDraft) renderTopology();
    }).catch(function (error) {
      setStatus(error.message || '读取 Provider 或 Regex Collection 失败', true);
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
      setStatus('Advanced JSON 必须是 JSON object', true);
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
    setStatus('编译 Prompt…');
    jsonFetch('/v1/studio/agents/' + encodeURIComponent(state.selectedAgentId) + '/prompt-preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(body)
    }).then(function (data) {
      renderPreview(data.preview || {});
      setStatus('Prompt 预览已更新');
      emit('studio:agent-previewed', data.preview);
    }).catch(function (error) { setStatus(error.message || '编译 Prompt 失败', true); });
  }

  function renderPreview(preview) {
    var target = $('studio-agent-preview-output');
    target.replaceChildren();
    var messages = preview.messages || [];
    if (!messages.length) {
      var empty = document.createElement('p');
      empty.className = 'studio-empty';
      empty.textContent = '预览没有返回 messages。';
      target.appendChild(empty);
      return;
    }
    messages.forEach(function (message) {
      var item = document.createElement('article');
      item.className = 'studio-preview-message';
      var label = document.createElement('strong');
      label.textContent = (message.source || 'runtime') + ' · ' + (message.role || 'message');
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
      meta.textContent = (graph.nodes || []).length + ' 个节点' + (id === state.runtimeGraphId ? ' · Runtime' : '');
      row.append(title, meta);
      row.addEventListener('click', function () { selectGraph(id); });
      list.appendChild(row);
    });
  }

  function resetGraph() {
    state.selectedGraphId = null;
    state.graphDraft = { id: '', name: '', nodes: [], output_node_id: '' };
    state.selectedNodeIndex = -1;
    $('studio-graph-form').reset();
    $('studio-graph-id').value = '';
    $('studio-graph-name').value = '';
    $('studio-graph-selection').textContent = '新建 Graph';
    $('studio-graph-title').textContent = 'Linear topology';
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
    });
    copy.output_node_id = copy.output_node_id || '';
    return copy;
  }

  function fillGraph(graph) {
    state.selectedGraphId = graph.id || graph.graph_id;
    state.graphDraft = normalizeGraph(graph);
    state.selectedNodeIndex = state.graphDraft.nodes.length ? 0 : -1;
    $('studio-graph-id').value = state.graphDraft.id || '';
    $('studio-graph-name').value = state.graphDraft.name || '';
    $('studio-graph-selection').textContent = state.graphDraft.name || state.graphDraft.id || '未选择 Graph';
    $('studio-graph-title').textContent = state.graphDraft.name || 'Linear topology';
    setButtonDisabled('studio-graph-use', false);
    setButtonDisabled('studio-graph-copy', false);
    setButtonDisabled('studio-graph-delete', false);
    renderGraphList();
    renderTopology();
    fillNodeSettings(currentNode());
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
    fillAgentSelects();
    setButtonDisabled('studio-node-agent-open', !node || !node.agent_id);
    setButtonDisabled('studio-node-remove', !node);
    refreshOutputOptions();
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
      option.textContent = (node.label || node.node_id) + (node.node_id === finalEnabledNodeId() ? ' · final' : '');
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
    node.node_id = $('studio-node-id').value.trim() || node.node_id;
    node.label = $('studio-node-label').value.trim();
    node.agent_id = $('studio-node-agent').value;
    node.enabled = $('studio-node-enabled').checked;
    node.order = state.selectedNodeIndex;
    ensureValidOutput();
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
      status.textContent = node.enabled === false ? '停用' : (node.node_id === state.graphDraft.output_node_id ? 'Output' : '启用');
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
        output.textContent = 'Output node';
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
    });
    refreshOutputOptions();
  }

  function selectGraph(graphId) {
    setStatus('读取 Graph…');
    return jsonFetch('/v1/studio/graphs/' + encodeURIComponent(graphId)).then(function (data) {
      fillGraph(data.graph);
      setStatus('');
      emit('studio:graph-selected', data.graph);
      return data.graph;
    }).catch(function (error) { setStatus(error.message || '读取 Graph 失败', true); });
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
    }).catch(function (error) { setStatus(error.message || '读取 Graphs 失败', true); });
  }

  function loadRuntimeGraph() {
    return jsonFetch('/v1/session/runtime/graph').then(function (data) {
      state.runtimeGraphId = data.selected && data.selected.graph_id || null;
      $('studio-graph-runtime-state').textContent = state.runtimeGraphId ? 'Runtime: ' + state.runtimeGraphId : 'Runtime 未选择 Graph';
      renderGraphList();
      emit('studio:runtime-graph-loaded', data);
    }).catch(function () {
      $('studio-graph-runtime-state').textContent = 'Runtime 选择不可用';
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
    state.graphDraft.nodes.splice(state.selectedNodeIndex, 1);
    state.graphDraft.nodes.forEach(function (node, index) { node.order = index; });
    state.selectedNodeIndex = Math.min(state.selectedNodeIndex, state.graphDraft.nodes.length - 1);
    ensureValidOutput();
    renderTopology();
    fillNodeSettings(currentNode());
  }

  function graphPayload() {
    syncNodeFields();
    if (!state.graphDraft || !state.graphDraft.name.trim()) {
      setStatus('Graph 名称不能为空', true);
      return null;
    }
    if (!state.graphDraft.nodes.length) {
      setStatus('Graph 至少需要一个节点', true);
      return null;
    }
    ensureValidOutput();
    if (!state.graphDraft.output_node_id) {
      setStatus('请设置 Output node', true);
      return null;
    }
    state.graphDraft.nodes.forEach(function (node, index) { node.order = index; });
    return {
      id: state.selectedGraphId ? state.selectedGraphId : ($('studio-graph-id').value.trim() || undefined),
      name: state.graphDraft.name.trim(),
      nodes: state.graphDraft.nodes,
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
      setStatus('Graph 已保存');
      emit('studio:graph-saved', state.graphDraft);
    }).catch(function (error) { setStatus(error.message || '保存 Graph 失败', true); });
  }

  function useGraph() {
    if (!state.selectedGraphId) return;
    setStatus('更新 Runtime 选择…');
    jsonFetch('/v1/session/runtime/graph', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify({ graph_id: state.selectedGraphId })
    }).then(function (data) {
      state.runtimeGraphId = state.selectedGraphId;
      $('studio-graph-runtime-state').textContent = 'Runtime: ' + state.runtimeGraphId;
      renderGraphList();
      setStatus('Runtime 选择已保存');
      emit('studio:runtime-graph-selected', data);
    }).catch(function (error) { setStatus(error.message || '更新 Runtime 选择失败', true); });
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
    }).then(function () { setStatus('Graph 已复制'); }).catch(function (error) { setStatus(error.message || '复制 Graph 失败', true); });
  }

  function deleteGraph() {
    if (!state.selectedGraphId || !global.confirm('删除当前 Graph？')) return;
    setStatus('删除中…');
    jsonFetch('/v1/studio/graphs/' + encodeURIComponent(state.selectedGraphId), { method: 'DELETE' })
      .then(function () { resetGraph(); return loadGraphs(); })
      .then(function () { setStatus('Graph 已删除'); })
      .catch(function (error) { setStatus(error.message || '删除 Graph 失败', true); });
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
    $('studio-node-enabled').addEventListener('change', function () { syncNodeFields(); renderTopology(); fillNodeSettings(currentNode()); });
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
