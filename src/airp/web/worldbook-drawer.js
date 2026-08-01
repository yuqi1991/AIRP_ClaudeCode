/*
 * Worldbook Definition editor for the integrated game workspace.
 *
 * This module owns only the drawer presentation. Worldbook persistence and
 * Project binding semantics stay behind the existing Studio HTTP endpoints.
 */
(function installWorldbookDrawer(global, document) {
  'use strict';

  var host = document.getElementById('studio-drawer-host');
  var mount = document.getElementById('worldbook-drawer-mount') || host;
  var toggle = document.getElementById('studio-worldbooks-toggle');
  if (!host || !toggle) return;

  var endpoint = '/v1/studio/worldbooks';
  var projectEndpoint = '/v1/studio/projects/';
  var state = {
    worldbooks: [],
    selectedId: null,
    book: null,
    libraryQuery: '',
    entryQuery: '',
    entrySort: 'order',
    projects: [],
    projectId: null,
    boundIds: [],
    ready: false,
    loading: false
  };

  var workspace = global.AIRPWorkspace || null;
  var drawerMarkup = [
    '<div class="worldbook-drawer-backdrop" data-worldbook-drawer-close="true"></div>',
    '<section class="worldbook-drawer-panel" role="dialog" aria-modal="true" aria-labelledby="worldbook-drawer-title" data-airp-drawer-view="worldbooks" data-studio-drawer-panel="worldbooks">',
      '<header class="worldbook-drawer-header">',
        '<div><div class="worldbook-drawer-kicker">AIRP Studio</div><div class="worldbook-drawer-title" id="worldbook-drawer-title">Worldbook Definitions</div></div>',
        '<button class="worldbook-drawer-close" id="worldbook-drawer-close" type="button" data-worldbook-drawer-close="true" aria-label="Close Studio drawer">Close</button>',
      '</header>',
      '<div class="worldbook-drawer-toolbar">',
        '<input class="worldbook-drawer-search" id="worldbook-drawer-library-search" type="search" placeholder="Search Worldbooks" aria-label="Search Worldbooks">',
        '<button class="worldbook-drawer-button primary" id="worldbook-drawer-new" type="button">New</button>',
        '<button class="worldbook-drawer-button" id="worldbook-drawer-import-button" type="button">Import JSON</button>',
        '<input id="worldbook-drawer-import" type="file" accept="application/json,.json" hidden>',
      '</div>',
      '<div class="worldbook-drawer-body">',
        '<aside class="worldbook-drawer-column worldbook-library-column" aria-label="Worldbook library">',
          '<div class="worldbook-drawer-column-heading"><h2>Library</h2><span class="worldbook-drawer-count" id="worldbook-drawer-count">0 saved</span></div>',
          '<div class="worldbook-library-list" id="worldbook-drawer-list" aria-live="polite"></div>',
        '</aside>',
        '<main class="worldbook-drawer-column worldbook-editor-column" aria-label="Worldbook editor">',
          '<div class="worldbook-editor-header">',
            '<input class="worldbook-drawer-input" id="worldbook-drawer-name" required autocomplete="off" placeholder="Worldbook name" aria-label="Worldbook name">',
            '<button class="worldbook-drawer-button" id="worldbook-drawer-rename" type="button" hidden>Rename</button>',
          '</div>',
          '<div class="worldbook-editor-actions">',
            '<button class="worldbook-drawer-button" id="worldbook-drawer-copy" type="button" hidden>Copy</button>',
            '<button class="worldbook-drawer-button" id="worldbook-drawer-export" type="button" hidden>Export JSON</button>',
          '</div>',
          '<div class="worldbook-entry-toolbar">',
            '<h2>Entries</h2>',
            '<div class="worldbook-entry-toolbar-controls">',
              '<input class="worldbook-drawer-search" id="worldbook-drawer-entry-search" type="search" placeholder="Search entries" aria-label="Search entries">',
              '<select class="worldbook-drawer-select" id="worldbook-drawer-entry-sort" aria-label="Sort entries"><option value="order">Order</option><option value="title">Title</option><option value="enabled">Enabled</option></select>',
              '<button class="worldbook-drawer-button" id="worldbook-drawer-add-entry" type="button">Add entry</button>',
            '</div>',
          '</div>',
          '<div class="worldbook-drawer-hint" id="worldbook-drawer-entry-count"></div>',
          '<div class="worldbook-entry-list" id="worldbook-drawer-entries"></div>',
        '</main>',
        '<aside class="worldbook-drawer-column worldbook-binding-column" aria-label="Project Worldbook bindings">',
          '<div class="worldbook-binding-heading"><h2>Project bindings</h2></div>',
          '<div class="worldbook-drawer-hint">Choose which Worldbooks the selected Project loads on the next Graph Run.</div>',
          '<div class="worldbook-binding-project"><label class="worldbook-binding-label" for="worldbook-drawer-project">Project</label><select class="worldbook-drawer-select" id="worldbook-drawer-project" aria-label="Project"></select></div>',
          '<div class="worldbook-binding-list" id="worldbook-drawer-bindings"></div>',
          '<button class="worldbook-drawer-button primary" id="worldbook-drawer-save-bindings" type="button">Save bindings</button>',
        '</aside>',
      '</div>',
      '<footer class="worldbook-drawer-footer">',
        '<div class="worldbook-drawer-notice" id="worldbook-drawer-notice" role="status" aria-live="polite"></div>',
        '<div class="worldbook-drawer-actions">',
          '<button class="worldbook-drawer-button primary" id="worldbook-drawer-save" type="button">Save Worldbook</button>',
          '<button class="worldbook-drawer-button danger" id="worldbook-drawer-delete" type="button" hidden>Delete</button>',
        '</div>',
      '</footer>',
    '</section>'
  ].join('');

  function $(id) { return document.getElementById(id); }

  function setWorkspaceState(open, view) {
    if (!workspace) return;
    if (typeof workspace.patchState === 'function') {
      workspace.patchState('studioDrawer', { open: open, view: view || null });
    }
    if (typeof workspace.emit === 'function') {
      workspace.emit(open ? 'studio-drawer:opened' : 'studio-drawer:closed', { view: view || null });
    }
  }

  function activateSharedPanel() {
    var panels = host.querySelectorAll('[data-studio-drawer-panel]');
    var worldbookPanel = host.querySelector('.worldbook-drawer-panel');
    Array.prototype.forEach.call(panels, function(panel) {
      var active = panel === worldbookPanel;
      panel.hidden = !active;
      panel.classList.toggle('is-active', active);
    });
    if (worldbookPanel) worldbookPanel.hidden = false;
  }

  function ensureMarkup() {
    if (!$('worldbook-drawer-title')) mount.insertAdjacentHTML('beforeend', drawerMarkup);
  }

  function clone(value) {
    return value === null || value === undefined ? value : JSON.parse(JSON.stringify(value));
  }

  function showNotice(message, kind) {
    var notice = $('worldbook-drawer-notice');
    if (!notice) return;
    notice.className = 'worldbook-drawer-notice' + (kind ? ' ' + kind : '');
    notice.textContent = message || '';
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

  function errorMessage(error, fallback) {
    if (error && error.payload && Array.isArray(error.payload.references) && error.payload.references.length) {
      return (error.message || fallback) + ' Referenced by ' + error.payload.references.map(function(item) { return item.name || item.id; }).join(', ') + '.';
    }
    return error && error.message ? error.message : fallback;
  }

  function makeEntry(index) {
    return {
      id: '',
      title: '',
      usage: '',
      content: '',
      enabled: true,
      order: index,
      tags: []
    };
  }

  function resetEditor() {
    state.selectedId = null;
    state.book = { id: '', name: '', entries: [makeEntry(0)] };
    state.entryQuery = '';
    state.entrySort = 'order';
    if ($('worldbook-drawer-name')) $('worldbook-drawer-name').value = '';
    if ($('worldbook-drawer-entry-search')) $('worldbook-drawer-entry-search').value = '';
    if ($('worldbook-drawer-entry-sort')) $('worldbook-drawer-entry-sort').value = 'order';
    renderAll();
    showNotice('New Worldbook Definition');
    if ($('worldbook-drawer-name')) $('worldbook-drawer-name').focus();
  }

  function fillEditor(book) {
    state.selectedId = book && book.id ? book.id : null;
    state.book = clone(book) || { id: '', name: '', entries: [makeEntry(0)] };
    if (!Array.isArray(state.book.entries)) state.book.entries = [];
    state.book.entries.forEach(function(entry, index) {
      if (!Array.isArray(entry.tags)) entry.tags = [];
      if (!Number.isInteger(entry.order)) entry.order = index;
    });
    $('worldbook-drawer-name').value = state.book.name || '';
    $('worldbook-drawer-entry-search').value = state.entryQuery;
    $('worldbook-drawer-entry-sort').value = state.entrySort;
    renderAll();
  }

  function visibleBooks() {
    var query = state.libraryQuery.trim().toLowerCase();
    return state.worldbooks.filter(function(book) {
      if (!query) return true;
      var haystack = [book.name, book.id].concat((book.entries || []).map(function(entry) {
        return [entry.title, entry.usage, entry.content, (entry.tags || []).join(' ')].join(' ');
      })).join(' ').toLowerCase();
      return haystack.indexOf(query) >= 0;
    });
  }

  function renderLibrary() {
    var target = $('worldbook-drawer-list');
    if (!target) return;
    var count = $('worldbook-drawer-count');
    if (count) count.textContent = state.worldbooks.length + ' saved';
    var rows = visibleBooks().map(function(book) {
      var row = document.createElement('button');
      row.type = 'button';
      row.className = 'worldbook-library-row' + (book.id === state.selectedId ? ' selected' : '');
      row.dataset.worldbookId = book.id;
      row.setAttribute('aria-current', book.id === state.selectedId ? 'true' : 'false');
      var title = document.createElement('span');
      title.className = 'worldbook-library-title';
      title.textContent = book.name;
      var meta = document.createElement('span');
      meta.className = 'worldbook-library-meta';
      meta.textContent = (book.entries || []).length + ' entr' + ((book.entries || []).length === 1 ? 'y' : 'ies');
      row.append(title, meta);
      row.addEventListener('click', function() { loadWorldbook(book.id); });
      return row;
    });
    if (!rows.length) {
      var empty = document.createElement('div');
      empty.className = 'worldbook-empty';
      empty.textContent = state.libraryQuery ? 'No matching Worldbooks.' : 'No Worldbooks yet.';
      rows.push(empty);
    }
    target.replaceChildren.apply(target, rows);
  }

  function entryMatches(entry) {
    var query = state.entryQuery.trim().toLowerCase();
    if (!query) return true;
    return [entry.title, entry.usage, entry.content, (entry.tags || []).join(' ')].join(' ').toLowerCase().indexOf(query) >= 0;
  }

  function sortedEntries() {
    var entries = (state.book && Array.isArray(state.book.entries) ? state.book.entries : []).map(function(entry, index) {
      return { entry: entry, index: index };
    }).filter(function(item) { return entryMatches(item.entry); });
    entries.sort(function(left, right) {
      var a = left.entry;
      var b = right.entry;
      if (state.entrySort === 'title') return String(a.title || '').localeCompare(String(b.title || ''), undefined, { sensitivity: 'base' }) || left.index - right.index;
      if (state.entrySort === 'enabled') return Number(b.enabled !== false) - Number(a.enabled !== false) || left.index - right.index;
      return (Number(a.order) || 0) - (Number(b.order) || 0) || left.index - right.index;
    });
    return entries;
  }

  function field(labelText, fieldName, value, type, wide) {
    var wrapper = document.createElement('div');
    wrapper.className = 'worldbook-entry-field' + (wide ? ' wide' : '');
    var label = document.createElement('label');
    label.textContent = labelText;
    var control = document.createElement(type || 'input');
    control.className = type === 'textarea' ? 'worldbook-drawer-textarea' : 'worldbook-drawer-input';
    control.dataset.field = fieldName;
    control.value = value === undefined || value === null ? '' : value;
    wrapper.append(label, control);
    return { wrapper: wrapper, control: control };
  }

  function renderEntryCard(item, position) {
    var entry = item.entry;
    var card = document.createElement('section');
    card.className = 'worldbook-entry-card' + (entry.enabled === false ? ' is-disabled' : '');
    card.dataset.entryIndex = String(item.index);
    var header = document.createElement('div');
    header.className = 'worldbook-entry-card-header';
    var number = document.createElement('span');
    number.className = 'worldbook-entry-number';
    number.textContent = String(position + 1).padStart(2, '0');
    var title = document.createElement('strong');
    title.textContent = entry.title || 'Untitled entry';
    var enabledLabel = document.createElement('label');
    enabledLabel.className = 'worldbook-entry-enabled';
    var enabled = document.createElement('input');
    enabled.type = 'checkbox';
    enabled.checked = entry.enabled !== false;
    enabled.dataset.field = 'enabled';
    enabled.addEventListener('change', function() {
      entry.enabled = enabled.checked;
      card.classList.toggle('is-disabled', !enabled.checked);
      renderEntryCount();
    });
    enabledLabel.append(enabled, ' Enabled');
    header.append(number, title, enabledLabel);

    var grid = document.createElement('div');
    grid.className = 'worldbook-entry-grid';
    var titleField = field('Entry title', 'title', entry.title);
    var orderField = field('Order', 'order', entry.order);
    var usageField = field('Usage', 'usage', entry.usage, 'input', true);
    var contentField = field('Content', 'content', entry.content, 'textarea', true);
    var tagsField = field('Tags (comma separated)', 'tags', (entry.tags || []).join(', '), 'input', true);
    [titleField, orderField, usageField, contentField, tagsField].forEach(function(part) { grid.appendChild(part.wrapper); });
    titleField.control.addEventListener('input', function() { entry.title = titleField.control.value; title.textContent = entry.title || 'Untitled entry'; });
    orderField.control.type = 'number';
    orderField.control.min = '0';
    orderField.control.step = '1';
    orderField.control.addEventListener('input', function() { entry.order = Number.parseInt(orderField.control.value, 10); });
    usageField.control.addEventListener('input', function() { entry.usage = usageField.control.value; });
    contentField.control.addEventListener('input', function() { entry.content = contentField.control.value; });
    tagsField.control.addEventListener('input', function() { entry.tags = tagsField.control.value.split(',').map(function(tag) { return tag.trim(); }).filter(Boolean); });

    var actions = document.createElement('div');
    actions.className = 'worldbook-entry-actions';
    var remove = document.createElement('button');
    remove.type = 'button';
    remove.className = 'worldbook-drawer-button danger';
    remove.textContent = 'Remove entry';
    remove.addEventListener('click', function() {
      state.book.entries.splice(item.index, 1);
      renderEntries();
      renderEntryCount();
    });
    actions.appendChild(remove);
    card.append(header, grid, actions);
    return card;
  }

  function renderEntryCount() {
    var target = $('worldbook-drawer-entry-count');
    if (!target) return;
    var all = state.book && Array.isArray(state.book.entries) ? state.book.entries : [];
    var visible = sortedEntries();
    var enabled = all.filter(function(entry) { return entry.enabled !== false; }).length;
    target.textContent = visible.length + ' shown · ' + all.length + ' total · ' + enabled + ' enabled';
  }

  function renderEntries() {
    var target = $('worldbook-drawer-entries');
    if (!target) return;
    var cards = sortedEntries().map(renderEntryCard);
    if (!cards.length) {
      var empty = document.createElement('div');
      empty.className = 'worldbook-empty';
      empty.textContent = state.entryQuery ? 'No matching entries.' : 'Add an entry to this Worldbook.';
      cards.push(empty);
    }
    target.replaceChildren.apply(target, cards);
    renderEntryCount();
  }

  function renderEditorActions() {
    var hasBook = Boolean(state.book);
    var saved = Boolean(state.selectedId);
    $('worldbook-drawer-name').value = hasBook ? state.book.name || '' : '';
    $('worldbook-drawer-copy').hidden = !saved;
    $('worldbook-drawer-export').hidden = !saved;
    $('worldbook-drawer-rename').hidden = !saved;
    $('worldbook-drawer-delete').hidden = !saved;
    $('worldbook-drawer-save').disabled = !hasBook;
    $('worldbook-drawer-save-bindings').disabled = !state.projectId;
  }

  function renderAll() {
    renderLibrary();
    renderEditorActions();
    renderEntries();
    renderProjectOptions();
    renderBindings();
  }

  function renderProjectOptions() {
    var select = $('worldbook-drawer-project');
    if (!select) return;
    select.replaceChildren();
    var projects = state.projects.slice();
    if (state.projectId && !projects.some(function(project) { return project.id === state.projectId; })) {
      projects.unshift({ id: state.projectId, name: state.projectId });
    }
    projects.forEach(function(project) {
      var option = document.createElement('option');
      option.value = project.id;
      option.textContent = project.name + ' (' + project.id + ')';
      option.selected = project.id === state.projectId;
      select.appendChild(option);
    });
    if (!projects.length) {
      var option = document.createElement('option');
      option.value = '';
      option.textContent = 'No Project selected';
      select.appendChild(option);
    }
  }

  function renderBindings() {
    var target = $('worldbook-drawer-bindings');
    if (!target) return;
    var rows = state.worldbooks.map(function(book) {
      var label = document.createElement('label');
      label.className = 'worldbook-binding-row';
      var input = document.createElement('input');
      input.type = 'checkbox';
      input.value = book.id;
      input.checked = state.boundIds.indexOf(book.id) >= 0;
      var text = document.createElement('span');
      text.textContent = book.name;
      label.append(input, text);
      return label;
    });
    if (!rows.length) {
      var empty = document.createElement('div');
      empty.className = 'worldbook-empty';
      empty.textContent = 'Save a Worldbook before binding it.';
      rows.push(empty);
    }
    target.replaceChildren.apply(target, rows);
  }

  async function loadWorldbooks(selectId) {
    var payload = await request(endpoint);
    state.worldbooks = Array.isArray(payload.worldbooks) ? payload.worldbooks : [];
    renderLibrary();
    renderBindings();
    var nextId = selectId || state.selectedId;
    if (nextId && state.worldbooks.some(function(book) { return book.id === nextId; })) {
      await loadWorldbook(nextId);
    } else if (!state.selectedId || !state.worldbooks.some(function(book) { return book.id === state.selectedId; })) {
      resetEditor();
    }
  }

  async function loadWorldbook(id) {
    if (!id) return;
    try {
      var payload = await request(endpoint + '/' + encodeURIComponent(id));
      fillEditor(payload.worldbook);
      showNotice('Loaded ' + payload.worldbook.name + '.');
    } catch (error) {
      showNotice(errorMessage(error, 'Could not load this Worldbook.'), 'error');
    }
  }

  async function loadProjectContext() {
    try {
      var context = await request('/v1/studio/project-context');
      if (context.project_id) state.projectId = context.project_id;
    } catch (_) {
      // A project selector remains usable when the runtime has no project context.
    }
  }

  async function loadProjects() {
    try {
      var payload = await request('/v1/studio/projects');
      state.projects = Array.isArray(payload.projects) ? payload.projects : [];
    } catch (_) {
      state.projects = [];
    }
    if (!state.projectId && state.projects.length) state.projectId = state.projects[0].id;
    renderProjectOptions();
    await loadBindings();
  }

  async function loadBindings() {
    if (!state.projectId) {
      state.boundIds = [];
      renderBindings();
      return;
    }
    try {
      var payload = await request(projectEndpoint + encodeURIComponent(state.projectId) + '/worldbooks');
      state.boundIds = payload.project && Array.isArray(payload.project.worldbook_ids) ? payload.project.worldbook_ids.slice() : [];
      renderBindings();
    } catch (error) {
      state.boundIds = [];
      renderBindings();
      showNotice(errorMessage(error, 'Could not load Project bindings.'), 'error');
    }
  }

  function editorPayload() {
    if (!state.book) return null;
    var entries = state.book.entries.map(function(entry, index) {
      var value = {
        title: String(entry.title || '').trim(),
        usage: String(entry.usage || '').trim(),
        content: String(entry.content || ''),
        enabled: entry.enabled !== false,
        order: Number.isInteger(entry.order) ? entry.order : index,
        tags: Array.isArray(entry.tags) ? entry.tags.filter(Boolean) : []
      };
      if (entry.id) value.id = entry.id;
      return value;
    });
    return { name: String($('worldbook-drawer-name').value || '').trim(), entries: entries };
  }

  async function saveWorldbook() {
    var payload = editorPayload();
    if (!payload || !payload.name) {
      showNotice('Worldbook name is required.', 'error');
      $('worldbook-drawer-name').focus();
      return;
    }
    var button = $('worldbook-drawer-save');
    button.disabled = true;
    try {
      var path = state.selectedId ? endpoint + '/' + encodeURIComponent(state.selectedId) : endpoint;
      var response = await request(path, {
        method: state.selectedId ? 'PUT' : 'POST',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      await loadWorldbooks(response.worldbook.id);
      showNotice('Worldbook saved.', 'success');
    } catch (error) {
      showNotice(errorMessage(error, 'Could not save this Worldbook.'), 'error');
    } finally {
      button.disabled = false;
    }
  }

  async function renameWorldbook() {
    if (!state.selectedId || !state.book) return;
    var nextName = global.prompt('New Worldbook name', state.book.name || '');
    if (nextName === null) return;
    nextName = nextName.trim();
    if (!nextName) {
      showNotice('Worldbook name is required.', 'error');
      return;
    }
    $('worldbook-drawer-name').value = nextName;
    await saveWorldbook();
  }

  async function copyWorldbook() {
    if (!state.selectedId) return;
    try {
      var payload = await request(endpoint + '/' + encodeURIComponent(state.selectedId) + '/copy', { method: 'POST', headers: { Accept: 'application/json', 'Content-Type': 'application/json' }, body: '{}' });
      await loadWorldbooks(payload.worldbook.id);
      showNotice('Worldbook copied.', 'success');
    } catch (error) {
      showNotice(errorMessage(error, 'Could not copy this Worldbook.'), 'error');
    }
  }

  async function deleteWorldbook() {
    if (!state.selectedId || !global.confirm('Delete this Worldbook?')) return;
    try {
      await request(endpoint + '/' + encodeURIComponent(state.selectedId), { method: 'DELETE' });
      await loadWorldbooks();
      showNotice('Worldbook deleted.', 'success');
    } catch (error) {
      showNotice(errorMessage(error, 'Could not delete this Worldbook.'), 'error');
    }
  }

  async function exportWorldbook() {
    if (!state.selectedId) return;
    try {
      var payload = await request(endpoint + '/' + encodeURIComponent(state.selectedId) + '/export');
      var blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
      var link = document.createElement('a');
      link.href = URL.createObjectURL(blob);
      link.download = (state.book && state.book.name ? state.book.name : 'worldbook') + '.airp.json';
      link.click();
      URL.revokeObjectURL(link.href);
      showNotice('Worldbook export ready.', 'success');
    } catch (error) {
      showNotice(errorMessage(error, 'Could not export this Worldbook.'), 'error');
    }
  }

  async function importWorldbook(event) {
    var file = event.target.files && event.target.files[0];
    event.target.value = '';
    if (!file) return;
    var documentValue;
    try {
      documentValue = JSON.parse(await file.text());
    } catch (_) {
      showNotice('The selected file is not valid JSON.', 'error');
      return;
    }
    var body = { name: file.name.replace(/\.json$/i, ''), document: documentValue };
    var cardData = documentValue && documentValue.data && typeof documentValue.data === 'object' ? documentValue.data : {};
    if ((cardData.character_book || (documentValue && documentValue.character_book)) && state.projectId) body.project_id = state.projectId;
    try {
      var payload = await request(endpoint + '/import', { method: 'POST', headers: { Accept: 'application/json', 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      await loadWorldbooks(payload.worldbook.id);
      showNotice('Worldbook imported.', 'success');
      if (payload.renamed_entries && payload.renamed_entries.length) showNotice('Worldbook imported; duplicate entry titles were renamed.', 'success');
      await loadBindings();
    } catch (error) {
      showNotice(errorMessage(error, 'Could not import this Worldbook.'), 'error');
    }
  }

  async function saveBindings() {
    if (!state.projectId) {
      showNotice('Select a Project before saving bindings.', 'error');
      return;
    }
    var checked = Array.prototype.slice.call(document.querySelectorAll('#worldbook-drawer-bindings input:checked')).map(function(input) { return input.value; });
    var button = $('worldbook-drawer-save-bindings');
    button.disabled = true;
    try {
      var payload = await request(projectEndpoint + encodeURIComponent(state.projectId) + '/worldbooks', {
        method: 'PUT',
        headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
        body: JSON.stringify({ worldbook_ids: checked })
      });
      state.boundIds = payload.project && Array.isArray(payload.project.worldbook_ids) ? payload.project.worldbook_ids.slice() : checked;
      renderBindings();
      showNotice('Project bindings saved.', 'success');
    } catch (error) {
      showNotice(errorMessage(error, 'Could not save Project bindings.'), 'error');
    } finally {
      button.disabled = false;
    }
  }

  async function openDrawer() {
    if (global.AIRPGameDrawer && typeof global.AIRPGameDrawer.close === 'function') global.AIRPGameDrawer.close();
    if (global.AIRPStudioAgentsDrawer && typeof global.AIRPStudioAgentsDrawer.close === 'function') global.AIRPStudioAgentsDrawer.close();
    ensureMarkup();
    host.hidden = false;
    host.setAttribute('aria-hidden', 'false');
    toggle.setAttribute('aria-expanded', 'true');
    setWorkspaceState(true, 'worldbooks');
    activateSharedPanel();
    if (!state.ready) {
      state.ready = true;
      try {
        await Promise.all([loadWorldbooks(), loadProjectContext()]);
        await loadProjects();
        renderAll();
      } catch (error) {
        showNotice(errorMessage(error, 'Could not load Worldbooks.'), 'error');
      }
    } else {
      renderAll();
      loadProjectContext().then(loadProjects);
    }
    if ($('worldbook-drawer-close')) $('worldbook-drawer-close').focus();
  }

  function closeDrawer() {
    host.hidden = true;
    host.setAttribute('aria-hidden', 'true');
    toggle.setAttribute('aria-expanded', 'false');
    setWorkspaceState(false, null);
  }

  function bind() {
    toggle.addEventListener('click', function() {
      var current = workspace && workspace.state && workspace.state.studioDrawer;
      if (current && current.open && current.view === 'worldbooks') closeDrawer();
      else openDrawer();
    });
    host.addEventListener('click', function(event) {
      var close = event.target.closest('[data-worldbook-drawer-close="true"]');
      if (close) closeDrawer();
    });
    document.addEventListener('keydown', function(event) { if (event.key === 'Escape' && !host.hidden) closeDrawer(); });
    $('worldbook-drawer-library-search').addEventListener('input', function(event) { state.libraryQuery = event.target.value; renderLibrary(); });
    $('worldbook-drawer-entry-search').addEventListener('input', function(event) { state.entryQuery = event.target.value; renderEntries(); });
    $('worldbook-drawer-entry-sort').addEventListener('change', function(event) { state.entrySort = event.target.value; renderEntries(); });
    $('worldbook-drawer-project').addEventListener('change', function(event) { state.projectId = event.target.value || null; loadBindings(); renderEditorActions(); });
    $('worldbook-drawer-new').addEventListener('click', resetEditor);
    $('worldbook-drawer-import-button').addEventListener('click', function() { $('worldbook-drawer-import').click(); });
    $('worldbook-drawer-import').addEventListener('change', importWorldbook);
    $('worldbook-drawer-rename').addEventListener('click', renameWorldbook);
    $('worldbook-drawer-copy').addEventListener('click', copyWorldbook);
    $('worldbook-drawer-delete').addEventListener('click', deleteWorldbook);
    $('worldbook-drawer-export').addEventListener('click', exportWorldbook);
    $('worldbook-drawer-add-entry').addEventListener('click', function() {
      if (!state.book) resetEditor();
      state.book.entries.push(makeEntry(state.book.entries.length));
      renderEntries();
    });
    $('worldbook-drawer-save').addEventListener('click', saveWorldbook);
    $('worldbook-drawer-save-bindings').addEventListener('click', saveBindings);
  }

  ensureMarkup();
  bind();
  host.hidden = true;
  host.setAttribute('aria-hidden', 'true');
  global.AIRPWorldbookDrawer = Object.freeze({ open: openDrawer, close: closeDrawer, state: state });
})(window, document);
