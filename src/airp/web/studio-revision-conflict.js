/* Shared Studio revision-conflict presentation. */
(function installStudioRevisionConflict(global, document) {
  'use strict';

  function isConflict(error) {
    var payload = error && (error.payload || error.data);
    return Boolean(
      error && error.status === 409 &&
      payload && payload.error === 'revision_conflict' &&
      Number.isInteger(payload.current_revision)
    );
  }

  function render(target, error, reload) {
    if (!target || !isConflict(error)) return false;
    var payload = error.payload || error.data;
    target.replaceChildren();
    target.classList.add('is-error');

    var message = document.createElement('span');
    message.textContent = '配置已被其他保存更新；当前 revision ' + payload.current_revision + '。';
    var button = document.createElement('button');
    button.type = 'button';
    button.className = 'studio-conflict-reload';
    button.textContent = 'Reload';
    button.addEventListener('click', function () {
      button.disabled = true;
      Promise.resolve(reload(payload)).catch(function (reloadError) {
        target.replaceChildren();
        target.textContent = reloadError && reloadError.message || 'Reload 失败';
        target.classList.add('is-error');
      });
    });
    target.append(message, button);
    return true;
  }

  global.AIRPStudioRevisionConflict = Object.freeze({
    isConflict: isConflict,
    render: render
  });
})(window, document);
