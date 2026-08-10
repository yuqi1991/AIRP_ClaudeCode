import { expect, test } from '@playwright/test';
import { spawn } from 'node:child_process';
import readline from 'node:readline';

let server;
let baseURL;

async function startRuntimeFixture() {
  const python = process.env.AIRP_PYTHON || 'python3';
  server = spawn(python, ['-u', 'tests/browser_runtime_fixture_server.py'], {
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
      server.once('exit', (code) => reject(new Error(`runtime fixture exited (${code}): ${stderr}`)));
    }),
    new Promise((_, reject) => setTimeout(() => reject(new Error(`runtime fixture timed out: ${stderr}`)), 15_000)),
  ]);
}

async function stopRuntimeFixture() {
  if (!server || server.exitCode !== null) return;
  const exited = new Promise((resolve) => server.once('exit', resolve));
  server.kill('SIGTERM');
  await Promise.race([exited, new Promise((resolve) => setTimeout(resolve, 5_000))]);
  if (server.exitCode === null) server.kill('SIGKILL');
}

async function openWorkspace(page) {
  await page.goto(`${baseURL}/`, { waitUntil: 'networkidle' });
  await page.waitForFunction(() => window.AIRPWorkspace);
  await expect(page.locator('#runtime-graph-select')).toHaveValue('default-two-round-review');
}

async function submit(page, text) {
  await page.locator('#user-input').fill(text);
  await page.locator('[data-airp-action="submit"]').click();
}

function monitorNodeButtons(page) {
  return page.getByRole('button', { name: /· (?:待运行|运行中|已完成|失败)$/ });
}

async function currentNodeRunIds(page) {
  return page.evaluate(async () => {
    const response = await fetch('/v1/studio/graph-runs', { cache: 'no-store' });
    const payload = await response.json();
    const run = payload.current || payload.most_recent;
    return (run && run.nodes ? run.nodes : []).map((node) => node.node_run_id);
  });
}

async function graphRuns(page) {
  return page.evaluate(async () => (await (await fetch("/v1/studio/graph-runs", { cache: "no-store" })).json()));
}

async function nodeRunDetail(page, nodeRunId) {
  return page.evaluate(async (id) => {
    const response = await fetch("/v1/studio/node-runs/" + encodeURIComponent(id), { cache: "no-store" });
    return (await response.json()).node_run;
  }, nodeRunId);
}

function recentTurnsFromSystemPrompt(prompt) {
  const marker = "已提交的近期回合：\n";
  const start = prompt.indexOf(marker);
  expect(start).toBeGreaterThanOrEqual(0);
  const jsonStart = start + marker.length;
  const end = prompt.indexOf("\n\n当前状态：", jsonStart);
  expect(end).toBeGreaterThan(jsonStart);
  return JSON.parse(prompt.slice(jsonStart, end));
}

async function layoutGeometry(page) {
  return page.evaluate(() => {
    const rect = (selector) => {
      const value = document.querySelector(selector).getBoundingClientRect();
      return { left: value.left, top: value.top, right: value.right, bottom: value.bottom, width: value.width, height: value.height };
    };
    const widths = [...document.querySelectorAll("#agent-trace [role=button]")].map((node) => node.getBoundingClientRect().width);
    return {
      pageWidth: document.documentElement.scrollWidth,
      pageHeight: document.documentElement.scrollHeight,
      conversation: rect("[data-airp-region=conversation]"),
      monitor: rect("[data-airp-region=monitor]"),
      composer: rect("[data-airp-region=composer]"),
      stableNodes: widths.length ? Math.max(...widths) - Math.min(...widths) : 0,
    };
  });
}

function assertLayout(geometry, viewport) {
  expect(geometry.pageWidth).toBeLessThanOrEqual(viewport.width);
  for (const region of [geometry.conversation, geometry.monitor, geometry.composer]) {
    expect(region.left).toBeGreaterThanOrEqual(0);
    expect(region.top).toBeGreaterThanOrEqual(0);
    expect(region.right).toBeLessThanOrEqual(viewport.width);
    expect(region.bottom).toBeLessThanOrEqual(viewport.height);
  }
  expect(geometry.conversation.bottom).toBeLessThanOrEqual(geometry.composer.top + 1);
  expect(geometry.monitor.bottom).toBeLessThanOrEqual(geometry.composer.top + 1);
  if (viewport.width > 760) {
    expect(geometry.conversation.right).toBeLessThanOrEqual(geometry.monitor.left + 1);
  }
  expect(geometry.stableNodes).toBeLessThanOrEqual(1);
}

async function closeNodeDetail(page) {
  await page.getByRole('button', { name: '关闭详情' }).click();
}

test.describe('real Pi browser runtime', () => {
  test.beforeEach(startRuntimeFixture);
  test.afterEach(stopRuntimeFixture);
  test.use({ viewport: { width: 1440, height: 900 } });

  test('streams and commits a real five-node default collaboration turn', async ({ page }) => {
    test.setTimeout(60_000);
    await openWorkspace(page);

    await submit(page, 'Ring the harbor bell.');
    const runningNode = page.getByRole('button', { name: /· 运行中$/ });
    await expect(runningNode).toHaveCount(1, { timeout: 15_000 });
    await expect(runningNode).not.toContainText('draft loop 1');
    await expect.poll(async () => (await graphRuns(page)).current?.status).toBe('running');

    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForFunction(() => window.AIRPWorkspace);
    await expect(page.locator('#runtime-task-status')).toContainText(/生成中|排队/);
    await expect(monitorNodeButtons(page)).toHaveCount(5);
    await expect(page.locator('#streaming-preview')).toContainText('## Harbor', { timeout: 15_000 });
    await expect(page.locator('#doc .turn-ai:not(#streaming-preview)')).toHaveCount(0);
    await expect(page.getByRole('button', { name: /· 运行中$/ })).not.toContainText('## Harbor');
    const nodes = monitorNodeButtons(page);
    await expect(nodes).toHaveCount(5);
    await expect(page.getByRole('button', { name: /· 已完成$/ })).toHaveCount(5);
    await expect(nodes.nth(0)).toContainText(/reviewer\.loop1.*1/);
    await expect(nodes.nth(1)).toContainText(/writer\.loop2.*1/);
    await expect(nodes.nth(2)).toContainText(/reviewer\.loop2.*2/);
    await expect(nodes.nth(3)).toContainText(/final-writer.*2/);
    await expect(nodes.filter({ hasText: '已交接给' })).toHaveCount(4);

    const firstRun = (await graphRuns(page)).most_recent;
    expect(firstRun.status).toBe('succeeded');
    expect(firstRun.nodes).toHaveLength(5);
    const rawOutputs = ['draft loop 1', 'review loop 1', 'draft loop 2', 'review loop 2', '<content>## Harbor answer\n\n**Bell heard.**\nSecond line.</content>'];
    for (let index = 0; index < 5; index += 1) {
      const detail = await nodeRunDetail(page, firstRun.nodes[index].node_run_id);
      expect(detail.input_artifact).toBeTruthy();
      expect(detail.model_calls.at(-1).final_output).toBe(rawOutputs[index]);
      expect(detail.tool_snapshot).toBeTruthy();
      expect(detail.error).toBeFalsy();
      if (index < 4) expect(detail.handoff_artifact).toBeTruthy();
      else expect(detail.effective_config.regex_output_transform).toMatchObject({ raw: rawOutputs[index], transformed: '## Harbor answer\n\n**Bell heard.**\nSecond line.' });

      await nodes.nth(index).click();
      const dialog = page.getByRole('dialog');
      await expect(dialog).toBeVisible();
      await expect(page.locator('#node-debug-input')).not.toBeEmpty();
      for (const field of ['streamed_output', 'final_output', 'tool_snapshot', 'error']) await expect(page.locator('#node-detail-body')).toContainText(field);
      await expect(page.locator('#node-detail-body')).toContainText('节点输入');
      await expect(page.locator('#node-detail-body')).toContainText('Agent 第 1 轮');
      await expect(page.locator('#node-detail-body')).toContainText('本轮输出');
      await expect(dialog.getByText(/调用 #/)).toHaveCount(index === 0 ? 2 : 1);
      await expect(page.locator('#node-debug-model-calls')).toContainText(index === 4 ? '<content>## Harbor answer' : rawOutputs[index]);
      if (index < 4) await expect(page.locator('#node-detail-body')).toContainText('handoff_artifact');
      if (index === 0) {
        await expect(dialog.getByText(/get_recent_memory ·/)).toHaveCount(1);
        await expect(page.locator('#node-debug-tool-calls')).toContainText('get_recent_memory');
        await page.getByLabel('仅文本').check();
        await expect(page.locator('#node-debug-summary')).toBeHidden();
        await expect(page.locator('#node-debug-model-calls')).toBeHidden();
        await expect(page.locator('#node-debug-tool-calls')).toBeHidden();
        await expect(page.locator('#node-detail-body')).toContainText('节点输入');
        await expect(page.locator('#node-detail-body')).toContainText('Agent 第 1 轮');
        await expect(page.locator('#node-detail-body')).toContainText('本轮输入提示');
        await expect(page.locator('#node-detail-body')).toContainText('本轮输出');
        await expect(page.locator('#node-detail-body')).toContainText('draft loop 1');
        await expect(page.locator('#node-detail-body')).not.toContainText(/node_run_id|tokens|deepseek-v4-flash|tool_snapshot/);
        await page.getByLabel('仅文本').uncheck();
      }
      await closeNodeDetail(page);
    }

    const assistantTurns = page.locator('#doc .turn-ai:not(#streaming-preview)');
    await expect(assistantTurns).toHaveCount(1);
    await expect(assistantTurns.locator('h2')).toHaveText('Harbor answer');
    await expect(assistantTurns.locator('strong')).toHaveText('Bell heard.');

    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForFunction(() => window.AIRPWorkspace);
    await expect(page.locator('#session-meta')).toContainText(/revision\s*1/i);
    await expect(page.locator('#runtime-graph-select')).toHaveValue('default-two-round-review');
    await expect(monitorNodeButtons(page)).toHaveCount(5);
    await expect(page.locator('#doc .turn-ai:not(#streaming-preview)')).toHaveCount(1);

    const firstRunIds = await currentNodeRunIds(page);
    await submit(page, 'Follow the lanterns home.');
    await expect(page.locator('#graph-retry-button')).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole('button', { name: /· 失败$/ })).toHaveCount(1);
    await expect.poll(async () => (await graphRuns(page)).most_recent?.status).toBe('failed');
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForFunction(() => window.AIRPWorkspace);
    await expect(page.locator('#runtime-task-status')).toContainText('失败');
    await expect(page.locator('#graph-retry-button')).toBeVisible();
    await expect(monitorNodeButtons(page)).toHaveCount(5);
    const failedRun = (await graphRuns(page)).most_recent;
    expect(failedRun.status).toBe('failed');
    const failedNode = failedRun.nodes.find((node) => node.status === 'failed');
    const failedDetail = await nodeRunDetail(page, failedNode.node_run_id);
    expect(failedDetail.error).toMatchObject({ code: 'provider_unavailable', retryable: true });
    expect(failedDetail.error.message).toContain('temporary fixture failure');
    await page.getByRole('button', { name: /· 失败$/ }).click();
    await expect(page.locator('#node-detail-body')).toContainText('error');
    await expect(page.locator('#node-detail-body')).toContainText('provider_unavailable');
    await closeNodeDetail(page);
    await page.locator('#graph-retry-button').click();
    await expect(page.locator('#doc .turn-ai:not(#streaming-preview)')).toHaveCount(2, { timeout: 20_000 });
    await expect(page.locator('#session-meta')).toContainText(/Revision 2/i, { timeout: 20_000 });
    await expect(page.getByRole('button', { name: /· 已完成$/ })).toHaveCount(5);
    await expect.poll(async () => {
      const runs = await graphRuns(page);
      return { current: runs.current, status: runs.most_recent?.status };
    }).toEqual({ current: null, status: 'succeeded' });
    const secondRunIds = await currentNodeRunIds(page);
    expect(new Set([...firstRunIds, ...secondRunIds]).size).toBe(10);
    await expect(page.locator('#doc .turn-ai:not(#streaming-preview) h2')).toHaveText(['Harbor answer', 'Lantern answer']);
    await expect(page.locator('#doc .turn-ai:not(#streaming-preview)').nth(1).locator('strong')).toHaveText('Home found.');
    await expect(page.locator('#doc .turn-ai:not(#streaming-preview)').nth(1).locator('p')).toHaveText(['Home found.', 'Second line.']);
    await page.reload({ waitUntil: 'networkidle' });
    await page.waitForFunction(() => window.AIRPWorkspace);
    await expect(page.locator('#session-meta')).toContainText(/Revision 2/i);
    await expect(page.locator('#runtime-graph-select')).toHaveValue('default-two-round-review');
    await expect(monitorNodeButtons(page)).toHaveCount(5);
    await expect(page.locator('#doc .turn-ai:not(#streaming-preview)')).toHaveCount(2);
  });

  for (const viewport of [{ width: 1440, height: 900 }, { width: 1280, height: 800 }, { width: 390, height: 844 }, { width: 360, height: 800 }]) {
    test(`keeps running, preview, and commit layout stable at ${viewport.width}px`, async ({ page }) => {
      test.setTimeout(60_000);
      await page.setViewportSize(viewport);
      await openWorkspace(page);
      await submit(page, 'Ring the harbor bell.');
      if (viewport.width <= 760) {
        await page.locator('#sidebar-toggle').click();
        await expect(page.locator('#sidebar-toggle')).toHaveAttribute('aria-expanded', 'true');
        await page.waitForTimeout(220);
      }
      await expect(page.getByRole('button', { name: /· 运行中$/ })).toHaveCount(1, { timeout: 15_000 });
      const running = await layoutGeometry(page);
      assertLayout(running, viewport);
      await expect(page.locator('#streaming-preview')).toContainText('## Harbor', { timeout: 20_000 });
      const preview = await layoutGeometry(page);
      assertLayout(preview, viewport);
      await expect(page.getByRole('button', { name: /· 已完成$/ })).toHaveCount(5, { timeout: 20_000 });
      const committed = await layoutGeometry(page);
      assertLayout(committed, viewport);
      for (const region of ['conversation', 'monitor', 'composer']) {
        for (const edge of ['left', 'top', 'right', 'bottom']) {
          expect(Math.abs(running[region][edge] - preview[region][edge])).toBeLessThanOrEqual(1);
          expect(Math.abs(preview[region][edge] - committed[region][edge])).toBeLessThanOrEqual(1);
        }
      }
      await monitorNodeButtons(page).first().click();
      const panel = page.getByRole('document', { name: '节点运行详情' });
      await expect(panel).toBeVisible();
      const modal = await panel.evaluate((node) => {
        const rect = node.getBoundingClientRect();
        return { left: rect.left, right: rect.right };
      });
      expect(modal.left).toBeGreaterThanOrEqual(0);
      expect(modal.right).toBeLessThanOrEqual(viewport.width);
      await closeNodeDetail(page);
    });
  }

  for (const scenario of [
    { label: 'empty', input: 'Commit empty output.', raw: '', followup: 'After empty output.' },
    { label: 'whitespace', input: 'Commit whitespace output.', raw: ' \n\t ', followup: 'After whitespace output.' },
  ]) {
    test('preserves ' + scenario.label + ' committed text behind the browser placeholder', async ({ page }) => {
      test.setTimeout(60_000);
      await openWorkspace(page);
      await submit(page, scenario.input);
      await expect(page.locator('#session-meta')).toContainText(/Revision 1/i, { timeout:  40_000 });
      await expect(page.locator('#doc .turn-text[data-airp-empty-output=true]')).toHaveCount(1);
      const finalOutput = await page.evaluate(async () => {
        const runs = await (await fetch('/v1/studio/graph-runs')).json();
        const node = runs.most_recent.nodes.at(-1);
        const detail = await (await fetch('/v1/studio/node-runs/' + encodeURIComponent(node.node_run_id))).json();
        return detail.node_run.final_output;
      });
      expect(finalOutput).toBe(scenario.raw);
      await submit(page, scenario.followup);
      await expect(page.locator('#session-meta')).toContainText(/Revision 2/i, { timeout:  40_000 });
      const followupPrompt = await page.evaluate(async () => {
        const runs = await (await fetch('/v1/studio/graph-runs')).json();
        const node = runs.most_recent.nodes[0];
        const detail = await (await fetch('/v1/studio/node-runs/' + encodeURIComponent(node.node_run_id))).json();
        return detail.node_run.model_calls[0].request.messages.find((message) => message.role === 'system').content;
      });
      const recentTurns = recentTurnsFromSystemPrompt(followupPrompt);
      expect(recentTurns).toHaveLength(1);
      expect(recentTurns[0]).toEqual(expect.objectContaining({ user: scenario.input, assistant: scenario.raw, revision: 1 }));
      await expect(page.locator('#doc .turn-ai:not(#streaming-preview)')).toHaveCount(2);
      await expect(page.locator('#doc .turn-text[data-airp-empty-output=true]')).toHaveCount(1);
    });
  }
});
