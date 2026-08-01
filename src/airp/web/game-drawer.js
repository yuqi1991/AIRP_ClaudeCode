/* Integrated Project drawer for the game workspace. */
(function installAirpGameDrawer(global, document) {
  'use strict';

  var host = document.getElementById('studio-drawer-host');
  var panel = document.getElementById('game-drawer-panel') || document.getElementById('studio-drawer-panel');
  var toggle = document.getElementById('game-drawer-toggle');
  if (!host || !panel || !toggle) return;

  var model = {
    open: false,
    tab: 'card',
    projects: [],
    project: null,
    activeProjectId: null,
    search: '',
    worldbooks: [],
    worldbooksLoading: false,
    worldbooksLoaded: false,
    worldbooksError: '',
    worldbooksRequestId: 0,
    graphs: [],
    selectedGraph: null,
    stateRequestId: 0,
    status: '',
    diagnostics: null
  };

  function api(path) {
    return typeof global.apiUrl === 'function' ? global.apiUrl(path) : path;
  }

  function esc(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function request(path, options) {
    return fetch(api(path), options || {}).then(function(response) {
      return response.json().then(function(data) {
        if (!response.ok || data.ok === false) {
          var error = new Error(data.message || data.error || '请求失败');
          error.data = data;
          throw error;
        }
        return data;
      });
    });
  }

  function setStatus(message, error) {
    model.status = message || '';
    var status = panel.querySelector('[data-game-drawer-status]');
    if (status) {
      status.textContent = model.status;
      status.style.color = error ? 'var(--accent)' : '';
    }
  }

  function setOpen(open) {
    if (open && !host.hidden) {
      if (global.AIRPWorldbookDrawer && typeof global.AIRPWorldbookDrawer.close === 'function') global.AIRPWorldbookDrawer.close();
      if (global.AIRPStudioAgentsDrawer && typeof global.AIRPStudioAgentsDrawer.close === 'function') global.AIRPStudioAgentsDrawer.close();
    }
    model.open = !!open;
    var workspace = global.AIRPWorkspace;
    if (model.open) {
      if (workspace && typeof workspace.activateDrawerSurface === 'function') workspace.activateDrawerSurface('game');
      else {
        var studioPanel = document.getElementById('studio-drawer-panel');
        var worldbookMount = document.getElementById('worldbook-drawer-mount');
        if (studioPanel) studioPanel.hidden = true;
        if (worldbookMount) worldbookMount.hidden = true;
        panel.hidden = false;
      }
    } else {
      panel.hidden = true;
    }
    panel.classList.toggle('is-active', model.open);
    if (workspace && typeof workspace.setNavigationState === 'function') workspace.setNavigationState(model.open ? 'game' : null);
    if (workspace && workspace.drawerMotion) {
      if (model.open) workspace.drawerMotion.open(host);
      else workspace.drawerMotion.close(host);
    } else {
      host.hidden = !model.open;
      host.setAttribute('aria-hidden', model.open ? 'false' : 'true');
    }
    var worldbookToggle = document.getElementById('studio-worldbooks-toggle');
    var regexToggle = document.getElementById('studio-regex-toggle');
    if (worldbookToggle) worldbookToggle.setAttribute('aria-expanded', 'false');
    if (regexToggle) regexToggle.setAttribute('aria-expanded', 'false');
    if (workspace) {
      workspace.setState('studioDrawer', { open: model.open, view: model.open ? 'game' : null });
      workspace.emit('studio-drawer:' + (model.open ? 'opened' : 'closed'), { view: model.open ? 'game' : null });
      workspace.emit('studio:drawer-' + (model.open ? 'opened' : 'closed'), { view: model.open ? 'game' : null });
    }
    if (model.open) loadState();
  }

  function renderShell() {
    panel.innerHTML = '' +
      '<div class="airp-game-drawer">' +
        '<div class="airp-game-drawer-header">' +
          '<h2>游戏</h2>' +
          '<span class="airp-game-editor-note">当前工作区</span>' +
          '<button class="airp-game-drawer-close" type="button" data-game-drawer-close aria-label="关闭游戏抽屉" title="关闭">×</button>' +
        '</div>' +
        '<div class="airp-game-drawer-body">' +
          '<aside class="airp-game-list" aria-label="游戏列表">' +
            '<input type="search" data-game-search placeholder="搜索游戏" aria-label="搜索游戏">' +
            '<div class="airp-game-list-actions">' +
              '<button class="airp-game-button primary" type="button" data-game-import>导入</button>' +
              '<input type="file" data-game-import-file accept=".json,application/json" hidden>' +
            '</div>' +
            '<div data-game-list></div>' +
          '</aside>' +
          '<section class="airp-game-editor" aria-live="polite"><div class="airp-game-editor-inner" data-game-editor></div></section>' +
        '</div>' +
        '<div class="airp-game-toolbar"><span class="airp-game-status" data-game-drawer-status></span></div>' +
      '</div>';
    panel.querySelector('[data-game-drawer-close]').addEventListener('click', function() { setOpen(false); });
    panel.querySelector('[data-game-search]').addEventListener('input', function(event) {
      model.search = event.target.value || '';
      renderList();
    });
    panel.querySelector('[data-game-import]').addEventListener('click', function() {
      panel.querySelector('[data-game-import-file]').click();
    });
    panel.querySelector('[data-game-import-file]').addEventListener('change', importFile);
    renderList();
    renderEditor();
  }

  function renderList() {
    var list = panel.querySelector('[data-game-list]');
    if (!list) return;
    var needle = model.search.trim().toLowerCase();
    var items = model.projects.filter(function(project) {
      return !needle || String(project.name || project.id).toLowerCase().indexOf(needle) >= 0;
    });
    if (!items.length) {
      list.innerHTML = '<div class="airp-game-empty">暂无游戏，导入角色卡 JSON 开始。</div>';
      return;
    }
    list.innerHTML = items.map(function(project) {
      var active = project.id === model.activeProjectId;
      var session = project.last_session_id ? '存档 ' + project.last_session_id : '暂无存档';
      return '<button class="airp-game-list-item' + (active ? ' active' : '') + '" type="button" data-game-id="' + esc(project.id) + '">' +
        '<strong>' + esc(project.name || project.id) + '</strong>' +
        '<small>' + esc(session) + (active ? ' · 当前' : '') + '</small>' +
      '</button>';
    }).join('');
    Array.prototype.forEach.call(list.querySelectorAll('[data-game-id]'), function(button) {
      button.addEventListener('click', function() { selectProject(button.getAttribute('data-game-id')); });
    });
  }

  function renderEditor() {
    var editor = panel.querySelector('[data-game-editor]');
    if (!editor) return;
    var project = model.project;
    if (!project) {
      editor.innerHTML = '<div class="airp-game-empty">选择一个游戏，或从左侧导入新的 Project。</div>';
      return;
    }
    var tabs = [
      ['card', '角色卡'], ['openings', '开场'], ['worldbooks', '世界书绑定'], ['graph', '编排选择']
    ];
    var html = '<div class="airp-game-project-head"><div><h3>' + esc(project.name || project.id) + '</h3><small>' + esc(project.id) + '</small></div>' +
      '<button class="airp-game-button danger" type="button" data-game-delete>删除游戏</button></div>' +
      diagnosticMarkup() +
      '<div class="airp-game-tabs" role="tablist">' + tabs.map(function(tab) {
        return '<button class="airp-game-button" type="button" role="tab" aria-selected="' + (model.tab === tab[0] ? 'true' : 'false') + '" data-game-tab="' + tab[0] + '">' + tab[1] + '</button>';
      }).join('') + '</div>';
    html += '<div data-game-pane></div>';
    editor.innerHTML = html;
    Array.prototype.forEach.call(editor.querySelectorAll('[data-game-tab]'), function(button) {
      button.addEventListener('click', function() { model.tab = button.getAttribute('data-game-tab'); renderEditor(); if (model.tab === 'worldbooks') loadWorldbooks(); if (model.tab === 'graph') loadGraphs(); });
    });
    editor.querySelector('[data-game-delete]').addEventListener('click', deleteProject);
    var pane = editor.querySelector('[data-game-pane]');
    if (model.tab === 'card') renderCardPane(pane);
    if (model.tab === 'openings') renderOpeningsPane(pane);
    if (model.tab === 'worldbooks') renderWorldbooksPane(pane);
    if (model.tab === 'graph') renderGraphPane(pane);
  }

  function diagnosticMarkup() {
    var report = model.diagnostics;
    if (!report || !report.overall) return '';
    var overall = report.overall;
    var worldbook = report.worldbook || {};
    var status = overall.status === 'success' ? '导入完成' : overall.status === 'degraded' ? '导入完成 · 有降级项' : '导入失败';
    var counts = overall.counts || {};
    var details = worldbook.embedded
      ? '内嵌世界书: 已读取 ' + Number(worldbook.entries_seen || 0) + ' 条，导入 ' + Number(worldbook.entries_imported || 0) + ' 条，跳过 ' + Number(worldbook.entries_skipped || 0) + ' 条' + (worldbook.bound_to_project ? '，已绑定' : '')
      : '内嵌世界书: 未发现';
    return '<section class="airp-game-diagnostics ' + esc(overall.status) + '" aria-label="导入诊断摘要">' +
      '<strong>' + esc(status) + '</strong><span>' + esc(details) + '</span>' +
      '<small>提示 ' + Number(counts.info || 0) + ' · 警告 ' + Number(counts.warning || 0) + ' · 错误 ' + Number(counts.error || 0) + '</small>' +
      '</section>';
  }

  function field(label, id, value, full, tall) {
    return '<div class="airp-game-field' + (full ? ' full' : '') + '"><label for="' + id + '">' + label + '</label>' +
      (tall ? '<textarea class="tall" id="' + id + '">' + esc(value) + '</textarea>' : '<input id="' + id + '" value="' + esc(value) + '">') + '</div>';
  }

  function renderCardPane(pane) {
    var prompt = projectPrompt();
    pane.innerHTML = '<div class="airp-game-form-grid">' +
      field('名称', 'game-card-name', model.project.name) +
      field('头像路径', 'game-card-avatar', model.project.avatar) +
      field('角色描述', 'game-card-description', model.project.description, true, true) +
      field('性格', 'game-card-personality', model.project.personality, true, true) +
      field('场景', 'game-card-scenario', model.project.scenario, true, true) +
      field('System Prompt', 'game-card-system', prompt.system, true, true) +
      field('Post-history Instruction', 'game-card-post-history', prompt.post_history, true, true) +
      '</div><div class="airp-game-form-actions"><button class="airp-game-button primary" type="button" data-game-save-card>保存角色卡</button></div>';
    pane.querySelector('[data-game-save-card]').addEventListener('click', saveCard);
  }

  function projectPrompt() {
    return model.project.card_prompt && typeof model.project.card_prompt === 'object' ? model.project.card_prompt : { system: '', post_history: '' };
  }

  function renderOpeningsPane(pane) {
    var openings = Array.isArray(model.project.openings) ? model.project.openings : [];
    pane.innerHTML = '<div data-game-openings>' + openings.map(function(opening, index) {
      return '<div class="airp-game-opening-row" data-game-opening-row>' +
        '<div><input data-opening-label value="' + esc(opening.label || ('Opening ' + (index + 1))) + '">' +
        '<label class="airp-game-check-row"><input type="radio" name="game-default-opening" data-opening-default ' + (opening.is_default ? 'checked' : '') + '>默认开场</label></div>' +
        '<textarea data-opening-content>' + esc(opening.content || '') + '</textarea>' +
        '<button class="airp-game-icon-button" type="button" data-opening-remove aria-label="删除开场" title="删除">×</button>' +
      '</div>';
    }).join('') + '</div><div class="airp-game-form-actions"><button class="airp-game-button" type="button" data-opening-add>新增开场</button><button class="airp-game-button primary" type="button" data-game-save-openings>保存开场</button></div>';
    pane.querySelector('[data-opening-add]').addEventListener('click', function() {
      openings.push({ id: 'opening-' + (openings.length + 1), label: '新开场', content: '', is_default: !openings.length });
      model.project.openings = openings;
      renderEditor();
      model.tab = 'openings';
      renderEditor();
    });
    Array.prototype.forEach.call(pane.querySelectorAll('[data-opening-remove]'), function(button) {
      button.addEventListener('click', function() { button.closest('[data-game-opening-row]').remove(); });
    });
    pane.querySelector('[data-game-save-openings]').addEventListener('click', saveOpenings);
  }

  function readOpenings() {
    return Array.prototype.map.call(panel.querySelectorAll('[data-game-opening-row]'), function(row, index) {
      return {
        id: model.project.openings[index] && model.project.openings[index].id || 'opening-' + (index + 1),
        label: row.querySelector('[data-opening-label]').value.trim() || 'Opening ' + (index + 1),
        content: row.querySelector('[data-opening-content]').value,
        is_default: row.querySelector('[data-opening-default]').checked
      };
    });
  }

  function renderWorldbooksPane(pane) {
    var selected = Array.isArray(model.project.worldbook_ids) ? model.project.worldbook_ids : [];
    var listHtml = '<div class="airp-game-empty">正在读取世界书…</div>';
    if (model.worldbooksError) {
      listHtml = '<div class="airp-game-empty airp-game-error">读取世界书失败：' + esc(model.worldbooksError) +
        '<button class="airp-game-button" type="button" data-game-retry-worldbooks>重试</button></div>';
    } else if (model.worldbooksLoaded && !model.worldbooks.length) {
      listHtml = '<div class="airp-game-empty">暂无可用世界书，请先在“世界书”抽屉中新建或导入。</div>';
    } else if (model.worldbooksLoaded) {
      listHtml = model.worldbooks.map(function(book) {
        return '<label><input type="checkbox" value="' + esc(book.id) + '" ' + (selected.indexOf(book.id) >= 0 ? 'checked' : '') + '>' + esc(book.name || book.id) + '</label>';
      }).join('');
    }
    pane.innerHTML = '<div class="airp-game-checklist" data-game-worldbook-list>' + listHtml + '</div>' +
      '<div class="airp-game-form-actions"><button class="airp-game-button primary" type="button" data-game-save-worldbooks>保存绑定</button></div>';
    var retry = pane.querySelector('[data-game-retry-worldbooks]');
    if (retry) retry.addEventListener('click', loadWorldbooks);
    pane.querySelector('[data-game-save-worldbooks]').disabled = !model.worldbooksLoaded || !!model.worldbooksError;
    pane.querySelector('[data-game-save-worldbooks]').addEventListener('click', saveWorldbooks);
  }

  function renderGraphPane(pane) {
    pane.innerHTML = '<div class="airp-game-field"><label for="game-graph-select">当前 Graph</label><select id="game-graph-select"><option value="">未选择 Graph</option>' +
      model.graphs.map(function(graph) { return '<option value="' + esc(graph.id) + '" ' + (graph.id === model.selectedGraph ? 'selected' : '') + '>' + esc(graph.name || graph.id) + '</option>'; }).join('') +
      '</select></div><div class="airp-game-form-actions"><button class="airp-game-button primary" type="button" data-game-save-graph>保存 Graph 选择</button></div>';
    pane.querySelector('[data-game-save-graph]').addEventListener('click', saveGraph);
  }

  function projectPayload(extra) {
    var project = model.project;
    return Object.assign({
      id: project.id,
      name: project.name,
      avatar: project.avatar || '',
      description: project.description || '',
      personality: project.personality || '',
      scenario: project.scenario || '',
      card_prompt: project.card_prompt || { system: '', post_history: '' },
      openings: project.openings || [],
      variables: project.variables || {},
      assets: project.assets || [],
      worldbook_ids: project.worldbook_ids || []
    }, extra || {});
  }

  function saveProject(payload, message) {
    return request('/v1/studio/projects/' + encodeURIComponent(model.project.id), {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload)
    }).then(function(data) {
      model.project = data.project;
      model.projects = model.projects.map(function(item) { return item.id === data.project.id ? Object.assign({}, item, data.project) : item; });
      renderList();
      renderEditor();
      setStatus(message || '已保存');
    }).catch(function(error) { setStatus(error.message, true); });
  }

  function deleteProject() {
    if (!model.project || !global.confirm('从游戏列表删除“' + (model.project.name || model.project.id) + '”？此操作无法撤销。')) return;
    var projectId = model.project.id;
    setStatus('正在删除游戏…');
    return request('/v1/studio/projects/' + encodeURIComponent(projectId), { method: 'DELETE' }).then(function(data) {
      model.project = null;
      model.activeProjectId = data.active_project_id || null;
      model.projects = data.projects || model.projects.filter(function(item) { return item.id !== projectId; });
      renderList();
      renderEditor();
      setStatus('游戏已删除');
      if (global.AIRPWorkspace) global.AIRPWorkspace.emit('project:deleted', { project_id: projectId });
      return loadState();
    }).catch(function(error) { setStatus(error.message, true); });
  }

  function saveCard() {
    var prompt = projectPrompt();
    model.project.name = panel.querySelector('#game-card-name').value.trim();
    model.project.avatar = panel.querySelector('#game-card-avatar').value;
    model.project.description = panel.querySelector('#game-card-description').value;
    model.project.personality = panel.querySelector('#game-card-personality').value;
    model.project.scenario = panel.querySelector('#game-card-scenario').value;
    model.project.card_prompt = {
      system: panel.querySelector('#game-card-system').value,
      post_history: panel.querySelector('#game-card-post-history').value
    };
    return saveProject(projectPayload(), '角色卡已保存');
  }

  function saveOpenings() {
    var openings = readOpenings();
    if (openings.length && !openings.some(function(item) { return item.is_default; })) openings[0].is_default = true;
    if (openings.filter(function(item) { return item.is_default; }).length > 1) {
      var found = false;
      openings.forEach(function(item) { if (item.is_default && found) item.is_default = false; else if (item.is_default) found = true; });
    }
    return saveProject(projectPayload({ openings: openings }), '开场已保存');
  }

  function saveWorldbooks() {
    var ids = Array.prototype.map.call(panel.querySelectorAll('[data-game-worldbook-list] input:checked'), function(input) { return input.value; });
    return request('/v1/studio/projects/' + encodeURIComponent(model.project.id) + '/worldbooks', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: model.project.name, worldbook_ids: ids })
    }).then(function(data) {
      model.project = data.project;
      setStatus('世界书绑定已保存');
      renderEditor();
      loadWorldbooks();
    }).catch(function(error) { setStatus(error.message, true); });
  }

  function saveGraph() {
    var graphId = panel.querySelector('#game-graph-select').value || null;
    return request('/v1/session/runtime/graph', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ graph_id: graphId })
    }).then(function() {
      model.selectedGraph = graphId;
      setStatus('Graph 选择已保存');
      if (typeof global.loadActiveGraphPanel === 'function') global.loadActiveGraphPanel();
    }).catch(function(error) { setStatus(error.message, true); });
  }

  function loadWorldbooks() {
    var requestId = ++model.worldbooksRequestId;
    model.worldbooksLoading = true;
    model.worldbooksLoaded = false;
    model.worldbooksError = '';
    if (model.tab === 'worldbooks') renderEditor();
    return request('/v1/studio/worldbooks').then(function(data) {
      if (requestId !== model.worldbooksRequestId) return data;
      model.worldbooks = data.worldbooks || [];
      model.worldbooksLoading = false;
      model.worldbooksLoaded = true;
      if (model.tab === 'worldbooks') renderEditor();
      return data;
    }).catch(function(error) {
      if (requestId !== model.worldbooksRequestId) return null;
      model.worldbooksLoading = false;
      model.worldbooksLoaded = true;
      model.worldbooksError = error.message || '请求失败';
      if (model.tab === 'worldbooks') renderEditor();
      setStatus(error.message, true);
      return null;
    });
  }

  function loadGraphs() {
    Promise.all([request('/v1/studio/graphs'), request('/v1/session/runtime/graph')]).then(function(values) {
      model.graphs = values[0].graphs || [];
      model.selectedGraph = values[1].selected && values[1].selected.graph_id || null;
      if (model.tab === 'graph') renderEditor();
    }).catch(function(error) { setStatus(error.message, true); });
  }

  function loadProject(projectId) {
    return request('/v1/studio/projects/' + encodeURIComponent(projectId)).then(function(data) {
      model.project = data.project;
      renderEditor();
    });
  }

  function loadState() {
    var requestId = ++model.stateRequestId;
    return request('/v1/session/project').then(function(data) {
      if (requestId !== model.stateRequestId) return data;
      model.projects = data.projects || [];
      model.activeProjectId = data.active_project_id || null;
      renderList();
      if (model.activeProjectId) return loadProject(model.activeProjectId);
      model.project = null;
      renderEditor();
      var drawerState = global.AIRPWorkspace && global.AIRPWorkspace.state && global.AIRPWorkspace.state.studioDrawer;
      if (!model.projects.length && !model.open && !(drawerState && drawerState.open)) setOpen(true);
    }).catch(function(error) {
      if (requestId === model.stateRequestId) setStatus(error.message, true);
      return null;
    });
  }

  function waitForIdle() {
    var attempts = 0;
    function check() {
      return request('/v1/session/snapshot').then(function(snapshot) {
        if (!snapshot.pending && !((snapshot.current_task || {}).status && ['queued', 'leased', 'running', 'projection_pending'].indexOf(snapshot.current_task.status) >= 0)) return snapshot;
        attempts += 1;
        if (attempts > 60) throw new Error('取消生成超时，请稍后再试');
        return new Promise(function(resolve) { setTimeout(resolve, 100); }).then(check);
      });
    }
    return check();
  }

  function selectProject(projectId) {
    if (!projectId || projectId === model.activeProjectId) return loadProject(projectId);
    var busy = global.AIRPWorkspace && global.AIRPWorkspace.state.runtimeTask;
    setStatus('正在切换游戏…');
    return request('/v1/session/snapshot').then(function(snapshot) {
      return snapshot.current_task || busy;
    }).catch(function() { return busy; }).then(function(task) {
      var running = task && ['queued', 'leased', 'running', 'projection_pending'].indexOf(task.status) >= 0;
      if (!running) return null;
      if (!global.confirm('当前游戏正在生成，切换前取消本次生成？')) return { cancelled: true };
      return request('/v1/session/commands/cancel', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ task_id: task.task_id })
      }).then(function() { return waitForIdle(); });
    }).then(function(result) {
      if (result && result.cancelled) {
        setStatus('已取消切换');
        return null;
      }
      return request('/v1/session/project/switch', {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: projectId })
      });
    }).then(function(data) {
      if (!data) return;
      model.activeProjectId = data.active_project_id || projectId;
      model.projects = data.projects || model.projects;
      model.project = data.project || null;
      model.worldbooks = [];
      model.worldbooksLoading = false;
      model.worldbooksLoaded = false;
      model.worldbooksError = '';
      model.worldbooksRequestId += 1;
      model.tab = 'card';
      renderList();
      renderEditor();
      setStatus('已恢复目标游戏的最后存档');
      if (global.AIRPWorkspace) global.AIRPWorkspace.emit('project:switched', data);
      if (typeof global.applySessionsPayload === 'function') global.applySessionsPayload(data);
      if (typeof global.reloadData === 'function') global.reloadData();
      if (typeof global.loadSessions === 'function') global.loadSessions();
      if (typeof global.loadOpenings === 'function') global.loadOpenings();
      if (typeof global.hydrateRuntimeSnapshot === 'function') global.hydrateRuntimeSnapshot();
    }).catch(function(error) { setStatus(error.message, true); });
  }

  function importFile(event) {
    var file = event.target.files && event.target.files[0];
    event.target.value = '';
    if (!file) return;
    var reader = new FileReader();
    reader.onload = function() {
      try {
        var documentData = JSON.parse(reader.result);
        request('/v1/studio/projects/import', {
          method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ document: documentData })
        }).then(function(data) {
          model.diagnostics = data.diagnostics || null;
          var diagnosticStatus = model.diagnostics && model.diagnostics.overall && model.diagnostics.overall.status;
          setStatus(diagnosticStatus === 'degraded' ? '已导入游戏，但有降级项' : '已导入游戏');
          return loadState().then(function() { return selectProject(data.project.id); });
        }).catch(function(error) {
          model.diagnostics = error.data && error.data.diagnostics || null;
          setStatus(error.message, true);
          renderEditor();
        });
      } catch (error) {
        setStatus('只能导入有效的 JSON 角色卡文件', true);
      }
    };
    reader.readAsText(file);
  }

  toggle.addEventListener('click', function() { setOpen(!model.open); });
  document.addEventListener('keydown', function(event) { if (event.key === 'Escape' && model.open) setOpen(false); });
  document.addEventListener('DOMContentLoaded', function() {
    renderShell();
    loadState().then(function() { if (model.project) { loadWorldbooks(); loadGraphs(); } });
  });

  global.AIRPGameDrawer = Object.freeze({ open: function() { setOpen(true); }, close: function() { setOpen(false); }, refresh: loadState });
})(window, document);
