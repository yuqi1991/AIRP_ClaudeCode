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

test.describe('desktop 1440x900 release baseline', () => {
  test.use({ viewport: { width: 1440, height: 900 } });

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
