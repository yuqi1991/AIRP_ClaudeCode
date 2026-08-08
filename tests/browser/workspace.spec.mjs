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
