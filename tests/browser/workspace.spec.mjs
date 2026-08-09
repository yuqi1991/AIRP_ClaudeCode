import { expect, test } from '@playwright/test';
import { spawn } from 'node:child_process';
import readline from 'node:readline';

let server;
let baseURL;

async function startFixtureServer() {
  const python = process.env.AIRP_PYTHON || 'python3';
  server = spawn(python, ['-u', 'tests/browser_fixture_server.py'], {
    cwd: process.cwd(),
    env: { ...process.env, PYTHONPATH: 'src' },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let stderr = '';
  server.stderr.on('data', (chunk) => { stderr += chunk.toString(); });
  const lines = readline.createInterface({ input: server.stdout });
  baseURL = await Promise.race([
    new Promise((resolve, reject) => {
      lines.once('line', resolve);
      server.once('exit', (code) => reject(new Error(`AIRP fixture server exited (${code}): ${stderr}`)));
    }),
    new Promise((_, reject) => setTimeout(() => reject(new Error(`AIRP fixture server timed out: ${stderr}`)), 10_000)),
  ]);
}

async function stopFixtureServer() {
  if (!server || server.exitCode !== null) return;
  const exited = new Promise((resolve) => server.once('exit', resolve));
  server.kill('SIGTERM');
  await Promise.race([exited, new Promise((resolve) => setTimeout(resolve, 5_000))]);
  if (server.exitCode === null) server.kill('SIGKILL');
}

test.beforeAll(startFixtureServer);
test.afterAll(stopFixtureServer);

async function openWorkspace(page) {
  await page.goto(`${baseURL}/`, { waitUntil: 'networkidle' });
  await expect(page.locator('#workspace-navigation')).toBeVisible();
  await page.waitForFunction(() => window.AIRPGameDrawer && window.AIRPWorkspace);
  await page.keyboard.press('Escape');
  await expect(page.locator('#workspace-navigation [aria-expanded="true"]')).toHaveCount(0);
}

async function onlyExpanded(page, selector) {
  await expect(page.locator(selector)).toHaveAttribute('aria-expanded', 'true');
  await expect(page.locator('#workspace-navigation [aria-expanded="true"]')).toHaveCount(1);
  await page.waitForTimeout(280);
}

async function studioPost(page, path, body) {
  return page.evaluate(async ({ endpoint, payload }) => {
    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || data.error || endpoint);
    return data;
  }, { endpoint: path, payload: body });
}

async function studioPut(page, path, body) {
  return page.evaluate(async ({ endpoint, payload }) => {
    const response = await fetch(endpoint, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.message || data.error || endpoint);
    return data;
  }, { endpoint: path, payload: body });
}

test.describe('desktop 1440x900 release baseline', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

  test('materialized default suite loads into every Studio editor on first open', async ({ page }) => {
    await openWorkspace(page);

    await page.locator('#studio-model-toggle').click();
    await expect(page.locator('#studio-provider-name')).toHaveValue('DeepSeek');
    await expect(page.locator('#studio-provider-key-status')).toContainText('未配置');

    await page.locator('#studio-agents-toggle').click();
    await expect(page.locator('#studio-agent-name')).not.toHaveValue('');
    await page.locator('#studio-agent-list').getByRole('button', { name: /正文创作者/ }).click();
    await expect(page.locator('#studio-agent-name')).toHaveValue('正文创作者');
    await expect(page.locator('#studio-agent-model')).toHaveValue('deepseek-v4-flash');

    await page.locator('#studio-model-toggle').click();
    const originalProviderId = await page.locator('#studio-provider-selection').textContent();
    const copiedProvider = page.waitForResponse((response) => response.request().method() === 'POST' && /\/v1\/studio\/providers\/[^/]+\/copy$/.test(response.url()));
    await page.locator('#studio-provider-copy').click();
    expect((await copiedProvider).status()).toBe(201);
    await expect(page.locator('#studio-provider-selection')).not.toHaveText(originalProviderId);
    await expect(page.locator('#studio-provider-key-status')).toContainText('未配置');

    await page.locator('#studio-agents-toggle').click();
    await page.locator('#studio-drawer-mode-toggle').click();
    await expect(page.locator('#studio-graph-name')).toHaveValue('默认双轮创作审查');
    await expect(page.locator('#studio-graph-topology .studio-topology-node')).toHaveCount(3);

    await page.locator('#studio-regex-toggle').click();
    await expect(page.locator('#regex-drawer-name')).toHaveValue('默认正文提取');
    await expect(page.locator('#regex-drawer-rules .regex-rule-card')).toHaveCount(1);
  });

  test('startup diagnostics render every safe project warning and Graph selection only lives in Monitor', async ({ page }) => {
    await page.route('**/v1/studio/startup', async (route) => {
      await route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({
          ok: true,
          status: 'degraded',
          diagnostics: [
            {
              code: 'first_warning', boundary: 'project_activation', project_id: 'project-one',
              message: '第一个安全诊断。', action: '执行第一个动作。',
            }, {
              code: 'second_warning', boundary: 'project_activation', project_id: 'project-two',
              message: '第二个安全诊断。', action: '执行第二个动作。',
            },
          ],
          secret: 'must-not-render',
        }),
      });
    });

    await openWorkspace(page);
    await expect(page.locator('#studio-startup-diagnostics')).toBeVisible();
    await expect(page.locator('.studio-startup-diagnostic')).toHaveCount(2);
    await expect(page.locator('#studio-startup-diagnostics')).toContainText('project-one');
    await expect(page.locator('#studio-startup-diagnostics')).toContainText('project-two');
    await expect(page.locator('body')).not.toContainText('must-not-render');
    await expect(page.locator('#runtime-graph-select')).toHaveCount(1);
    await expect(page.locator('#studio-graph-use')).toHaveCount(0);
    await expect(page.locator('#studio-graph-runtime-state')).toHaveCount(0);
    await page.locator('#game-drawer-toggle').click();
    await expect(page.getByRole('tab', { name: '编排选择' })).toHaveCount(0);
    await expect(page.locator('#game-graph-select')).toHaveCount(0);
  });

  test('ordinary Studio saves carry the loaded Library revision', async ({ page }) => {
    await openWorkspace(page);

    await page.locator('#studio-model-toggle').click();
    await expect(page.locator('#studio-provider-name')).toHaveValue('DeepSeek');
    const providerRequest = page.waitForRequest((request) => request.method() === 'PUT' && /\/v1\/studio\/providers\/[^/]+$/.test(request.url()));
    await page.locator('#studio-provider-form button[type="submit"]').click();
    expect((await providerRequest).postDataJSON().expected_revision).toEqual(expect.any(Number));

    await page.locator('#studio-agents-toggle').click();
    await page.locator('#studio-agent-list').getByRole('button', { name: /正文创作者/ }).click();
    const agentRequest = page.waitForRequest((request) => request.method() === 'PUT' && /\/v1\/studio\/agents\/[^/]+$/.test(request.url()));
    await page.locator('#studio-agent-form button[type="submit"]').click();
    expect((await agentRequest).postDataJSON().expected_revision).toEqual(expect.any(Number));

    await page.locator('#studio-drawer-mode-toggle').click();
    await expect(page.locator('#studio-graph-name')).toHaveValue('默认双轮创作审查');
    const graphRequest = page.waitForRequest((request) => request.method() === 'PUT' && /\/v1\/studio\/graphs\/[^/]+$/.test(request.url()));
    await page.locator('#studio-graph-form button[type="submit"]').click();
    expect((await graphRequest).postDataJSON().expected_revision).toEqual(expect.any(Number));

    await page.locator('#studio-regex-toggle').click();
    await expect(page.locator('#regex-drawer-name')).toHaveValue('默认正文提取');
    const regexRequest = page.waitForRequest((request) => request.method() === 'PUT' && /\/v1\/studio\/regex-collections\/[^/]+$/.test(request.url()));
    await page.locator('#regex-drawer-save').click();
    expect((await regexRequest).postDataJSON().expected_revision).toEqual(expect.any(Number));
  });

  test('startup receipt IDs select collision resources and missing hints fall back', async ({ page }) => {
    await openWorkspace(page);
    const suffix = Date.now().toString(36);
    const provider = await studioPost(page, '/v1/studio/providers', {
      id: 'hint-provider-' + suffix, name: 'Hint Provider ' + suffix,
      base_url: 'http://127.0.0.1:9', api_format: 'chat_completions',
    });
    const regex = await studioPost(page, '/v1/studio/regex-collections', {
      id: 'hint-regex-' + suffix, name: 'Hint Regex ' + suffix, rules: [],
    });
    const writer = await studioPost(page, '/v1/studio/agents', {
      agent_id: 'hint-writer-' + suffix, name: 'Hint Writer ' + suffix, instruction: 'hint writer',
      provider_profile_id: provider.profile.id, regex_collection_id: regex.collection.id,
    });
    const reviewer = await studioPost(page, '/v1/studio/agents', {
      agent_id: 'hint-reviewer-' + suffix, name: 'Hint Reviewer ' + suffix, instruction: 'hint reviewer',
    });
    const graph = await studioPost(page, '/v1/studio/graphs', {
      id: 'hint-graph-' + suffix, name: 'Hint Graph ' + suffix,
      nodes: [{ node_id: 'writer', agent_id: writer.agent.agent_id }], output_node_id: 'writer',
    });
    await page.route('**/v1/studio/startup', async (route) => route.fulfill({
      status: 200, contentType: 'application/json', body: JSON.stringify({
        ok: true, status: 'success', diagnostics: [], initial_resource_ids: {
          provider: provider.profile.id, regex: regex.collection.id,
          writer: writer.agent.agent_id, reviewer: reviewer.agent.agent_id, graph: graph.graph.id,
        },
      }),
    }));

    await openWorkspace(page);
    await page.locator('#studio-model-toggle').click();
    await expect.poll(() => page.evaluate(() => window.AIRPStudioModelDrawer.state.selectedId)).toBe(provider.profile.id);
    await page.locator('#studio-agents-toggle').click();
    await expect.poll(() => page.evaluate(() => window.AIRPStudioAgentsDrawer.state.selectedAgentId)).toBe(writer.agent.agent_id);
    await page.locator('#studio-drawer-mode-toggle').click();
    await expect.poll(() => page.evaluate(() => window.AIRPStudioAgentsDrawer.state.selectedGraphId)).toBe(graph.graph.id);
    await page.locator('#studio-regex-toggle').click();
    await expect.poll(() => page.evaluate(() => window.AIRPRegexDrawer.state.selectedId)).toBe(regex.collection.id);

    await page.evaluate(async (id) => fetch('/v1/studio/graphs/' + encodeURIComponent(id), { method: 'DELETE' }), graph.graph.id);
    await openWorkspace(page);
    await page.locator('#studio-agents-toggle').click();
    await page.locator('#studio-drawer-mode-toggle').click();
    await expect.poll(() => page.evaluate(() => window.AIRPStudioAgentsDrawer.state.selectedGraphId)).not.toBe(graph.graph.id);
    await expect.poll(() => page.evaluate(() => window.AIRPStudioAgentsDrawer.state.selectedGraphId)).not.toBeNull();
  });

  test('stale Agent save presents current revision and Reloads the latest object', async ({ page }) => {
    await openWorkspace(page);
    const suffix = Date.now().toString(36);
    const created = await studioPost(page, '/v1/studio/agents', {
      agent_id: 'conflict-agent-' + suffix,
      name: 'Conflict Agent ' + suffix,
      instruction: 'loaded instruction',
    });

    await page.locator('#studio-agents-toggle').click();
    await page.getByRole('button', { name: 'Conflict Agent ' + suffix }).click();
    const latest = await studioPut(page, '/v1/studio/agents/' + created.agent.agent_id, {
      expected_revision: created.agent.revision,
      instruction: 'latest instruction',
    });
    await page.locator('#studio-agent-instruction').fill('stale draft');
    await page.locator('#studio-agent-form button[type=submit]').click();

    await expect(page.locator('#studio-drawer-status')).toContainText('当前 revision ' + latest.agent.revision);
    await page.locator('#studio-drawer-status .studio-conflict-reload').click();
    await expect(page.locator('#studio-agent-instruction')).toHaveValue('latest instruction');
    await expect(page.locator('#studio-drawer-status')).toContainText('Reload');
  });

  test('default collaboration fields, handoffs, binding, and Regex previews are fully visible', async ({ page }) => {
    await openWorkspace(page);
    await page.locator('#studio-agents-toggle').click();

    await page.locator('#studio-agent-list').getByRole('button', { name: /正文创作者/ }).click();
    await expect(page.locator('#studio-agent-provider')).toHaveValue('default-deepseek');
    await expect(page.locator('#studio-agent-model')).toHaveValue('deepseek-v4-flash');
    await expect(page.locator('#studio-agent-temperature')).toHaveValue('0.8');
    await expect(page.locator('#studio-agent-max-tokens')).toHaveValue('8000');
    await expect(page.locator('#studio-agent-tools')).toHaveValue('load_worldbook_entry, get_recent_memory');
    await expect(page.locator('#studio-agent-instruction')).toHaveValue(/正文创作者/);
    await expect(page.locator('#studio-agent-regex')).toHaveValue('default-content');
    await expect(page.locator('#studio-agent-advanced')).toBeEditable();

    await page.locator('#studio-agent-list').getByRole('button', { name: /内容审查者/ }).click();
    await expect(page.locator('#studio-agent-temperature')).toHaveValue('0.2');
    await expect(page.locator('#studio-agent-max-tokens')).toHaveValue('8000');
    await expect(page.locator('#studio-agent-instruction')).toHaveValue(/内容审查者/);
    await expect(page.locator('#studio-agent-regex')).toHaveValue('');

    await page.locator('#studio-drawer-mode-toggle').click();
    await expect(page.locator('#studio-graph-topology .studio-topology-node')).toHaveCount(3);
    await expect(page.locator('#studio-loop-list')).toContainText('writer → reviewer · 2 次');
    await expect(page.locator('#studio-graph-output')).toHaveValue('final-writer');
    await page.locator('#studio-graph-topology .studio-topology-node').nth(0).click();
    await expect(page.locator('#studio-node-handoff')).toHaveValue(/第 \{\{node\.loop_iteration\}\} 轮候选正文/);
    await page.locator('#studio-graph-topology .studio-topology-node').nth(1).click();
    await expect(page.locator('#studio-node-handoff')).toHaveValue(/第一轮审查意见/);
    await page.locator('#studio-loop-list .studio-loop-item').click();
    await expect(page.locator('#studio-loop-exit-handoff')).toHaveValue(/第二轮也是最后一轮审查意见/);

    await page.locator('#studio-regex-toggle').click();
    await expect(page.locator('.regex-rule-pattern')).toHaveValue(/<content>/);
    await expect(page.locator('select[data-agent-id="default-writer"]')).toHaveValue('default-content');

    await page.locator('#regex-drawer-test-input').fill('<content>可见正文</content>');
    await page.locator('#regex-drawer-test').click();
    await expect(page.locator('#regex-drawer-test-output')).toHaveText('可见正文');

    await page.locator('#regex-drawer-test-input').fill('没有标签');
    await page.locator('#regex-drawer-test').click();
    await expect(page.locator('#regex-drawer-test-output')).toHaveText('');
  });

  test('active Graph selector reflects initial choice, change, clear, and deletion', async ({ page }) => {
    await openWorkspace(page);
    const suffix = Date.now().toString(36);
    const projectId = `selector-project-${suffix}`;
    await studioPost(page, '/v1/studio/projects', { id: projectId, name: 'Selector lifecycle project' });
    await studioPost(page, '/v1/session/project/switch', { project_id: projectId });
    await page.evaluate(() => window.loadActiveGraphPanel());
    await expect(page.locator('#runtime-graph-select')).toHaveValue('default-two-round-review');

    const graphId = `selector-graph-${suffix}`;
    await studioPost(page, '/v1/studio/graphs', {
      id: graphId,
      name: 'Selector lifecycle graph',
      mode: 'handoff',
      nodes: [{ node_id: 'writer', agent_id: 'default-writer', enabled: true }],
      loops: [],
      output_node_id: 'writer',
    });
    await page.evaluate(() => window.loadActiveGraphPanel());

    async function applySelection(selectedGraphId) {
      await page.locator('#runtime-graph-select').selectOption(selectedGraphId || '');
      const saved = page.waitForResponse((response) => response.request().method() === 'PUT' && response.url().endsWith('/v1/session/runtime/graph'));
      await page.getByRole('button', { name: '应用选择' }).click();
      expect((await saved).status()).toBe(200);
      await page.waitForFunction((expected) => ((window.AIRPWorkspace.state.activeGraph || {}).selected || {}).graph_id === expected, selectedGraphId || null);
      await expect(page.locator('#runtime-graph-select')).toHaveValue(selectedGraphId || '');
    }

    await applySelection(graphId);
    await applySelection(null);

    await applySelection(graphId);
    await page.evaluate(async (id) => {
      const response = await fetch('/v1/studio/graphs/' + encodeURIComponent(id), { method: 'DELETE' });
      if (!response.ok) throw new Error(await response.text());
      await window.loadActiveGraphPanel();
    }, graphId);
    await expect(page.locator('#runtime-graph-select')).toHaveValue('');
    await expect(page.locator(`#runtime-graph-select option[value="${graphId}"]`)).toHaveCount(0);
  });

  test('five mutually-exclusive top drawers preserve the Monitor boundary', async ({ page }) => {
    await openWorkspace(page);
    const navigation = page.locator('#workspace-navigation');
    await expect(navigation.getByRole('button')).toHaveCount(5);
    await expect(navigation.getByRole('button').allTextContents()).resolves.toEqual([
      '游戏', '世界书', 'Agents 与编排', '正则集合', '模型',
    ]);

    for (const selector of [
      '#game-drawer-toggle',
      '#studio-worldbooks-toggle',
      '#studio-agents-toggle',
      '#studio-regex-toggle',
      '#studio-model-toggle',
    ]) {
      await page.locator(selector).click();
      await onlyExpanded(page, selector);
    }

    const geometry = await page.evaluate(() => {
      const drawer = document.querySelector('[data-airp-region="studio-drawer"]').getBoundingClientRect();
      const monitor = document.querySelector('[data-airp-region="monitor"]').getBoundingClientRect();
      return {
        pageWidth: document.documentElement.scrollWidth,
        viewportWidth: innerWidth,
        drawer: { left: drawer.left, right: drawer.right, top: drawer.top, bottom: drawer.bottom },
        monitor: { left: monitor.left, right: monitor.right },
      };
    });
    expect(geometry.pageWidth).toBeLessThanOrEqual(geometry.viewportWidth);
    expect(geometry.drawer.right).toBeLessThanOrEqual(geometry.monitor.left + 1);
    expect(geometry.drawer.bottom).toBeGreaterThanOrEqual(899);
    expect(geometry.drawer.top).toBeGreaterThan(0);

    await expect(page).toHaveScreenshot('workspace-desktop.png', {
      animations: 'disabled',
      fullPage: true,
      maxDiffPixelRatio: 0.01,
    });

    await page.locator('#studio-model-toggle').click();
    await expect(page.locator('#workspace-navigation [aria-expanded="true"]')).toHaveCount(0);
  });

  test('Escape closes the drawer and Markdown tables scroll inside their wrapper', async ({ page }) => {
    await openWorkspace(page);
    await page.locator('#studio-agents-toggle').click();
    await onlyExpanded(page, '#studio-agents-toggle');
    await page.keyboard.press('Escape');
    await expect(page.locator('#workspace-navigation [aria-expanded="true"]')).toHaveCount(0);

    const markdown = await page.evaluate(() => {
      const turn = document.createElement('div');
      turn.className = 'turn-ai';
      const text = document.createElement('div');
      text.className = 'turn-text';
      text.textContent = '**粗体**\n\n| 名称 | 很长很长很长很长很长很长很长很长很长很长的说明 |\n| --- | --- |\n| AIRP | 内容 |';
      turn.appendChild(text);
      document.getElementById('doc').replaceChildren(turn);
      formatMessageBubbles(turn);
      const wrap = turn.querySelector('.airp-markdown-table-wrap');
      return {
        bold: Boolean(turn.querySelector('strong')),
        table: Boolean(turn.querySelector('table')),
        wrapperOverflow: getComputedStyle(wrap).overflowX,
        pageOverflow: document.documentElement.scrollWidth > innerWidth,
      };
    });
    expect(markdown).toEqual({ bold: true, table: true, wrapperOverflow: 'auto', pageOverflow: false });

    const emptyOutput = await page.evaluate(() => {
      const turn = document.createElement('div');
      turn.className = 'turn-ai';
      const text = document.createElement('div');
      text.className = 'turn-text';
      text.textContent = ' \n\t ';
      turn.appendChild(text);
      formatMessageBubbles(turn);
      return { text: text.textContent, marked: text.dataset.airpEmptyOutput };
    });
    expect(emptyOutput).toEqual({ text: '本回合提交了空正文', marked: 'true' });
  });
});

test('handoff editor saves a fixed loop and text-only debug hides telemetry', async ({ page }) => {
  await openWorkspace(page);
  const suffix = String(Date.now());
  const providerId = 'handoff-browser-provider-' + suffix;
  const plannerId = 'handoff-browser-planner-' + suffix;
  const writerId = 'handoff-browser-writer-' + suffix;
  const reviewerId = 'handoff-browser-reviewer-' + suffix;
  const graphId = 'handoff-browser-graph-' + suffix;
  const graphName = '浏览器接力链-' + suffix;
  await studioPost(page, '/v1/studio/providers', {
    id: providerId, name: 'Handoff Browser Provider',
    base_url: 'http://127.0.0.1:9', api_format: 'chat_completions', api_key: 'browser-key',
  });
  for (const [id, name] of [[plannerId, '规划 Agent'], [writerId, '写作 Agent'], [reviewerId, '审阅 Agent']]) {
    await studioPost(page, '/v1/studio/agents', {
      agent_id: id, name, instruction: name,
      provider_profile_id: providerId, model_id: 'browser-model',
    });
  }
  await studioPost(page, '/v1/studio/graphs', {
    id: graphId, name: graphName, mode: 'handoff',
    nodes: [
      { node_id: 'plan', agent_id: plannerId, handoff_prompt: '规划下一步。' },
      { node_id: 'write', agent_id: writerId, handoff_prompt: '检查正文。' },
      { node_id: 'review', agent_id: reviewerId, handoff_prompt: '修订正文。' },
      { node_id: 'final', agent_id: writerId },
    ],
    loops: [{ id: 'revision', mode: 'fixed', start_node_id: 'write', end_node_id: 'review', iterations: 2 }],
    output_node_id: 'final',
  });

  await page.locator('#studio-agents-toggle').click();
  await page.locator('#studio-drawer-mode-toggle').click();
  await expect(page.locator('#studio-orchestration-view')).toBeVisible();
  await page.getByRole('button', { name: new RegExp(graphName) }).click();
  await expect(page.locator('#studio-graph-id')).toHaveValue(graphId);
  await expect(page.locator('#studio-graph-mode')).toHaveValue('handoff');
  await expect(page.locator('#studio-graph-topology .studio-handoff-edge')).toHaveCount(3);
  await expect(page.locator('#studio-loop-list .studio-loop-item')).toContainText('write → review · 2 次');

  await page.locator('#studio-node-handoff').fill('按冻结的角色卡继续规划。');
  await page.locator('#studio-graph-form').getByRole('button', { name: '保存编排图' }).click();
  await expect(page.locator('#studio-drawer-status')).toHaveText('编排图已保存');
  const saved = await page.evaluate(async (id) => (await fetch('/v1/studio/graphs/' + encodeURIComponent(id))).json(), graphId);
  expect(saved.graph.nodes[0].handoff_prompt).toBe('按冻结的角色卡继续规划。');
  expect(saved.graph.loops[0]).not.toHaveProperty('exit_handoff_prompt');

  await page.evaluate(() => {
    const modal = document.getElementById('node-detail-modal');
    modal.hidden = false;
    nodeDetailState = {
      nodeRunId: 'debug-node',
      debugReplay: null,
      data: {
        node_run_id: 'debug-node', node_id: 'plan', label: '规划', state: 'succeeded',
        input_artifact: { content: '玩家输入' },
        model_calls: [{ request: { messages: [{ role: 'user', content: '模型输入' }] }, final_output: '模型文本' }],
        effective_config: { regex_output_transform: { transformed: '正则结果' } },
        final_output: '节点产物', handoff_artifact: { content: '交接文本' },
      },
    };
    renderNodeDetail(nodeDetailState.data);
  });
  await page.locator('#node-debug-text-only').check();
  await expect(page.locator('.node-debug-panel')).toHaveClass(/is-text-only/);
  await expect(page.locator('#node-detail-body')).toContainText('交接文本');
  await expect(page.locator('#node-detail-body')).not.toContainText('node_run_id');
});

test.describe('mobile 390x844 release baseline', () => {
  test.use({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });

  test('drawer fills the topbar remainder without horizontal overflow', async ({ page }) => {
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await openWorkspace(page);
    await page.locator('#studio-regex-toggle').click();
    await onlyExpanded(page, '#studio-regex-toggle');

    const geometry = await page.evaluate(() => {
      const drawer = document.querySelector('[data-airp-region="studio-drawer"]').getBoundingClientRect();
      const topbar = document.querySelector('[data-airp-region="topbar"]').getBoundingClientRect();
      const animated = document.querySelector('.monitor-node-running');
      const drawerStyle = getComputedStyle(document.querySelector('[data-airp-region="studio-drawer"]'));
      const editor = document.querySelector('.regex-editor-column').getBoundingClientRect();
      const binding = document.querySelector('.regex-binding-column').getBoundingClientRect();
      const navButtonsVisible = [...document.querySelectorAll('#workspace-navigation button')].every((button) => {
        const box = button.getBoundingClientRect();
        return box.left >= 0 && box.right <= innerWidth && box.top >= 0 && box.bottom <= topbar.bottom;
      });
      return {
        pageWidth: document.documentElement.scrollWidth,
        viewportWidth: innerWidth,
        drawer: { left: drawer.left, right: drawer.right, top: drawer.top, bottom: drawer.bottom },
        topbarBottom: topbar.bottom,
        runningAnimation: animated ? getComputedStyle(animated).animationName : 'none',
        drawerTransitionDuration: drawerStyle.transitionDuration,
        navButtonsVisible,
        regexColumnsDoNotOverlap: binding.top >= editor.bottom - 1,
      };
    });
    expect(geometry.pageWidth).toBeLessThanOrEqual(geometry.viewportWidth);
    expect(geometry.drawer.left).toBeCloseTo(0, 0);
    expect(geometry.drawer.right).toBeCloseTo(390, 0);
    expect(geometry.drawer.top).toBeCloseTo(geometry.topbarBottom, 0);
    expect(geometry.drawer.bottom).toBeGreaterThanOrEqual(843);
    expect(geometry.runningAnimation).toBe('none');
    expect(geometry.drawerTransitionDuration.split(',').every((duration) => duration.trim() === '0s')).toBe(true);
    expect(geometry.navButtonsVisible).toBe(true);
    expect(geometry.regexColumnsDoNotOverlap).toBe(true);

    await expect(page).toHaveScreenshot('workspace-mobile.png', {
      animations: 'disabled',
      fullPage: true,
      maxDiffPixelRatio: 0.01,
    });
  });
});

test('supplemental 1280x800 and 360x800 viewports preserve geometry', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 800 });
  await openWorkspace(page);
  await page.locator('#studio-agents-toggle').click();
  await onlyExpanded(page, '#studio-agents-toggle');
  let geometry = await page.evaluate(() => {
    const drawer = document.querySelector('[data-airp-region="studio-drawer"]').getBoundingClientRect();
    const monitor = document.querySelector('[data-airp-region="monitor"]').getBoundingClientRect();
    return {
      pageWidth: document.documentElement.scrollWidth,
      viewportWidth: innerWidth,
      separated: drawer.right <= monitor.left + 1,
    };
  });
  expect(geometry).toEqual({ pageWidth: 1280, viewportWidth: 1280, separated: true });

  await page.setViewportSize({ width: 360, height: 800 });
  await page.reload({ waitUntil: 'networkidle' });
  await page.waitForFunction(() => window.AIRPGameDrawer && window.AIRPWorkspace);
  await page.keyboard.press('Escape');
  await page.locator('#studio-model-toggle').click();
  await onlyExpanded(page, '#studio-model-toggle');
  geometry = await page.evaluate(() => {
    const drawer = document.querySelector('[data-airp-region="studio-drawer"]').getBoundingClientRect();
    const topbar = document.querySelector('[data-airp-region="topbar"]').getBoundingClientRect();
    return {
      pageWidth: document.documentElement.scrollWidth,
      viewportWidth: innerWidth,
      drawerWidth: drawer.width,
      drawerTop: drawer.top,
      topbarBottom: topbar.bottom,
    };
  });
  expect(geometry.pageWidth).toBeLessThanOrEqual(geometry.viewportWidth);
  expect(geometry.drawerWidth).toBeCloseTo(360, 0);
  expect(geometry.drawerTop).toBeCloseTo(geometry.topbarBottom, 0);
});
