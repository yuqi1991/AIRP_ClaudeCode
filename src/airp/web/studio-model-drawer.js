/* Integrated Provider Profile editor for the game workspace. */
(function installStudioModelDrawer(global, document) {
  'use strict';

  var host = document.getElementById('studio-drawer-host');
  if (!host || !document.getElementById('studio-model-view')) return;

  var endpoint = '/v1/studio/providers';
  var $ = function (id) { return document.getElementById(id); };
  var state = {
    profiles: [],
    selectedId: null,
    filter: '',
    loading: false,
    loaded: false
  };

  function drawer() {
    return global.AIRPStudioAgentsDrawer || null;
  }

  function api() {
    return global.AIRPWorkspace || null;
  }

  function setNavigation(view) {
    var workspace = api();
    if (workspace && typeof workspace.setNavigationState === 'function') workspace.setNavigationState(view);
  }

  function emit(type, detail) {
    var workspace = api();
    if (workspace && typeof workspace.emit === 'function') workspace.emit(type, detail || null);
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

  function setActionDisabled(id, disabled) {
    var button = $(id);
    if (button) button.disabled = !!disabled;
  }

  function setNotice(message, error) {
    var notice = $('studio-provider-notice');
    if (!notice) return;
    notice.textContent = message || '';
    notice.classList.toggle('is-error', !!error);
  }

  function setConnection(message, error) {
    var target = $('studio-provider-connection');
    if (!target) return;
    target.textContent = message || '';
    target.classList.toggle('is-error', !!error);
  }

  function errorMessage(error, fallback) {
    var payload = error && error.payload || {};
    var message = error && error.message || fallback;
    if (Array.isArray(payload.references) && payload.references.length) {
      message += ' · ' + payload.references.map(function (reference) {
        return (reference.type || 'reference') + ': ' + (reference.name || reference.id || '');
      }).join(', ');
    }
    return message;
  }

  function setHeader(model) {
    var title = $('studio-drawer-title');
    if (!title) return;
    if (model) {
      title.textContent = '模型';
      return;
    }
    var agentsState = global.AIRPStudioAgentsDrawer && global.AIRPStudioAgentsDrawer.state;
    title.textContent = agentsState && agentsState.view === 'orchestration' ? '编排' : 'Agents';
  }

  function renderModelView() {
    var active = !host.hidden;
    var modelToggle = $('studio-model-toggle');
    var agentsToggle = $('studio-agents-toggle');
    if (modelToggle) modelToggle.setAttribute('aria-expanded', active ? 'true' : 'false');
    if (agentsToggle) agentsToggle.setAttribute('aria-expanded', 'false');
    var studioPanel = $('studio-drawer-panel');
    if (!studioPanel) return;
    studioPanel.querySelectorAll('[data-studio-drawer-panel]').forEach(function (panel) {
      var isActive = panel.getAttribute('data-studio-drawer-panel') === 'model';
      panel.classList.toggle('is-active', isActive);
      panel.hidden = !isActive;
    });
    if (active) setHeader(true);
  }

  function openModel() {
    var current = drawer();
    if (current && !host.hidden && current.state.view === 'model') {
      current.close();
      return;
    }
    if (current) {
      current.open('model');
    } else {
      var workspace = api();
      if (workspace && workspace.drawerMotion) workspace.drawerMotion.open(host);
      else {
        host.hidden = false;
        host.setAttribute('aria-hidden', 'false');
      }
    }
    setNavigation('model');
    renderModelView();
    if (!state.loaded) loadProfiles();
  }

  function renderCatalog(modelIds) {
    var target = $('studio-provider-catalog');
    if (!target) return;
    target.replaceChildren();
    (modelIds || []).forEach(function (modelId) {
      var tag = document.createElement('span');
      tag.className = 'studio-provider-model';
      tag.textContent = modelId;
      target.appendChild(tag);
    });
  }

  function renderList() {
    var list = $('studio-provider-list');
    if (!list) return;
    var filter = state.filter.toLowerCase();
    var profiles = state.profiles.filter(function (profile) {
      return !filter || [profile.name, profile.id, profile.base_url, profile.api_format].join(' ').toLowerCase().indexOf(filter) >= 0;
    });
    $('studio-provider-count').textContent = state.profiles.length + ' 个服务商配置';
    list.replaceChildren();
    if (!profiles.length) {
      var empty = document.createElement('p');
      empty.className = 'studio-empty';
      empty.textContent = state.profiles.length ? '没有匹配的服务商。' : '还没有服务商配置。';
      list.appendChild(empty);
      return;
    }
    profiles.forEach(function (profile) {
      var row = document.createElement('button');
      row.type = 'button';
      row.className = 'studio-object-item' + (profile.id === state.selectedId ? ' is-active' : '');
      var title = document.createElement('span');
      title.className = 'studio-object-item-title';
      title.textContent = profile.name || profile.id;
      var meta = document.createElement('span');
      meta.className = 'studio-object-item-meta';
      meta.textContent = (profile.enabled ? '已启用' : '已停用') + ' · ' + profile.api_format;
      var key = document.createElement('span');
      key.className = 'studio-object-item-meta';
      key.textContent = profile.key_configured ? '已配置密钥 · ' + (profile.model_ids || []).length + ' 个模型' : '未配置密钥 · ' + (profile.model_ids || []).length + ' 个模型';
      row.append(title, meta, key);
      row.addEventListener('click', function () { loadProfile(profile.id); });
      list.appendChild(row);
    });
  }

  function keyStatus(configured) {
    var target = $('studio-provider-key-status');
    if (!target) return;
    target.classList.toggle('is-configured', !!configured);
    var text = target.querySelector('span:last-child');
    if (text) text.textContent = configured ? '已配置' : '未配置';
  }

  function fillProfile(profile) {
    state.selectedId = profile.id;
    $('studio-provider-title').textContent = '编辑 ' + (profile.name || profile.id);
    $('studio-provider-selection').textContent = profile.id || '';
    $('studio-provider-name').value = profile.name || '';
    $('studio-provider-base-url').value = profile.base_url || '';
    $('studio-provider-format').value = profile.api_format || 'chat_completions';
    $('studio-provider-enabled').checked = profile.enabled !== false;
    $('studio-provider-api-key').value = '';
    $('studio-provider-models').value = (profile.model_ids || []).join('\n');
    keyStatus(profile.key_configured);
    renderCatalog(profile.model_ids || []);
    setActionDisabled('studio-provider-test', false);
    setActionDisabled('studio-provider-refresh', false);
    setActionDisabled('studio-provider-delete-key', !profile.key_configured);
    setActionDisabled('studio-provider-delete', false);
    renderList();
  }

  function resetForm() {
    state.selectedId = null;
    $('studio-provider-form').reset();
    $('studio-provider-enabled').checked = true;
    $('studio-provider-title').textContent = '新建服务商配置';
    $('studio-provider-selection').textContent = '未选择服务商';
    $('studio-provider-api-key').value = '';
    $('studio-provider-models').value = '';
    keyStatus(false);
    renderCatalog([]);
    setConnection('');
    setNotice('');
    setActionDisabled('studio-provider-test', true);
    setActionDisabled('studio-provider-refresh', true);
    setActionDisabled('studio-provider-delete-key', true);
    setActionDisabled('studio-provider-delete', true);
    renderList();
  }

  function loadProfile(id) {
    setConnection('读取中…');
    return jsonFetch(endpoint + '/' + encodeURIComponent(id)).then(function (data) {
      fillProfile(data.profile);
      setConnection('');
      setNotice('');
      emit('studio:provider-selected', data.profile);
      return data.profile;
    }).catch(function (error) {
      setConnection(errorMessage(error, '读取服务商失败'), true);
    });
  }

  function loadProfiles() {
    state.loading = true;
    return jsonFetch(endpoint).then(function (data) {
      state.profiles = data.profiles || [];
      state.loaded = true;
      renderList();
      if (state.selectedId) {
        var current = state.profiles.find(function (profile) { return profile.id === state.selectedId; });
        if (!current) resetForm();
      } else if (state.profiles.length) {
        return loadProfile(state.profiles[0].id);
      }
    }).catch(function (error) {
      setNotice(errorMessage(error, '读取服务商配置失败'), true);
    }).then(function () {
      state.loading = false;
    });
  }

  function profilePayload() {
    var payload = {
      name: $('studio-provider-name').value.trim(),
      base_url: $('studio-provider-base-url').value.trim(),
      api_format: $('studio-provider-format').value,
      enabled: $('studio-provider-enabled').checked,
      model_ids: $('studio-provider-models').value.split(/\r?\n/).map(function (value) { return value.trim(); }).filter(Boolean)
    };
    var apiKey = $('studio-provider-api-key').value.trim();
    if (apiKey) payload.api_key = apiKey;
    return payload;
  }

  function saveProfile(event) {
    event.preventDefault();
    var payload = profilePayload();
    if (!payload.name || !payload.base_url) {
      setNotice('名称和 Base URL 不能为空', true);
      return;
    }
    var id = state.selectedId;
    var url = id ? endpoint + '/' + encodeURIComponent(id) : endpoint;
    setConnection('保存中…');
    jsonFetch(url, {
      method: id ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (data) {
      fillProfile(data.profile);
      return loadProfiles().then(function () { fillProfile(data.profile); return data; });
    }).then(function (data) {
      setConnection('');
      var discovery = data.model_discovery;
      if (discovery && discovery.ok === false) {
        setNotice('服务商已保存，但模型发现失败：' + (discovery.error && discovery.error.message || '请稍后重试'), true);
      } else {
        setNotice('服务商已保存', false);
      }
      emit('studio:provider-saved', data.profile);
    }).catch(function (error) {
      setConnection('');
      setNotice(errorMessage(error, '保存服务商失败'), true);
    });
  }

  function testConnection() {
    if (!state.selectedId) return;
    setConnection('测试连接中…');
    setNotice('');
    jsonFetch(endpoint + '/' + encodeURIComponent(state.selectedId) + '/test', { method: 'POST' })
      .then(function (data) {
        var modelIds = data.model_ids || [];
        renderCatalog(modelIds);
        setConnection('连接成功 · ' + modelIds.length + ' 个模型');
        setNotice('连接测试成功；结果尚未写入目录。');
        emit('studio:provider-tested', data);
      }).catch(function (error) {
        setConnection(errorMessage(error, '连接测试失败'), true);
        setNotice(errorMessage(error, '连接测试失败'), true);
      });
  }

  function refreshModels() {
    if (!state.selectedId) return;
    setConnection('刷新模型中…');
    setNotice('');
    jsonFetch(endpoint + '/' + encodeURIComponent(state.selectedId) + '/models/refresh', { method: 'POST' })
      .then(function (data) {
        fillProfile(data.profile);
        return loadProfiles().then(function () { fillProfile(data.profile); return data; });
      }).then(function (data) {
        setConnection('模型目录已刷新');
        setNotice('已保存最新模型目录。');
        emit('studio:provider-models-refreshed', data.profile);
      }).catch(function (error) {
        setConnection(errorMessage(error, '刷新模型失败'), true);
        setNotice(errorMessage(error, '刷新模型失败'), true);
      });
  }

  function deleteKey() {
    if (!state.selectedId || !global.confirm('删除当前服务商的 API 密钥？')) return;
    setConnection('删除密钥中…');
    jsonFetch(endpoint + '/' + encodeURIComponent(state.selectedId) + '/secret', { method: 'DELETE' })
      .then(function (data) {
        fillProfile(data.profile);
        return loadProfiles().then(function () { fillProfile(data.profile); });
      }).then(function () {
        setConnection('');
        setNotice('API 密钥已删除');
        emit('studio:provider-secret-deleted', { id: state.selectedId });
      }).catch(function (error) {
        setConnection('');
        setNotice(errorMessage(error, '删除 API 密钥失败'), true);
      });
  }

  function deleteProfile() {
    if (!state.selectedId || !global.confirm('删除当前服务商配置？')) return;
    setConnection('删除服务商中…');
    jsonFetch(endpoint + '/' + encodeURIComponent(state.selectedId), { method: 'DELETE' })
      .then(function () {
        resetForm();
        return loadProfiles();
      }).then(function () {
        setConnection('');
        setNotice('服务商已删除');
        emit('studio:provider-deleted');
      }).catch(function (error) {
        setConnection('');
        setNotice(errorMessage(error, '删除服务商失败'), true);
      });
  }

  function restoreAgentsHeader() {
    if (host.hidden) return;
    var modelToggle = $('studio-model-toggle');
    if (modelToggle) modelToggle.setAttribute('aria-expanded', 'false');
    setHeader(false);
  }

  function bind() {
    var toggle = $('studio-model-toggle');
    if (toggle) toggle.addEventListener('click', openModel);
    $('studio-provider-new').addEventListener('click', function () {
      resetForm();
      $('studio-provider-name').focus();
    });
    $('studio-provider-search').addEventListener('input', function (event) {
      state.filter = event.target.value;
      renderList();
    });
    $('studio-provider-form').addEventListener('submit', saveProfile);
    $('studio-provider-test').addEventListener('click', testConnection);
    $('studio-provider-refresh').addEventListener('click', refreshModels);
    $('studio-provider-delete-key').addEventListener('click', deleteKey);
    $('studio-provider-delete').addEventListener('click', deleteProfile);
    var workspace = api();
    if (workspace && workspace.events) {
      workspace.events.addEventListener('studio:drawer-opened', function (event) {
        var view = event.detail && event.detail.view;
        if (view === 'model') {
          renderModelView();
          if (!state.loaded) loadProfiles();
        } else {
          restoreAgentsHeader();
        }
      });
      workspace.events.addEventListener('studio:drawer-closed', function () {
        var modelToggle = $('studio-model-toggle');
        if (modelToggle) modelToggle.setAttribute('aria-expanded', 'false');
        setHeader(false);
      });
    }
  }

  bind();
  resetForm();
  global.AIRPStudioModelDrawer = Object.freeze({ open: openModel, state: state });
})(window, document);
