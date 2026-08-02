import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './tests/browser',
  outputDir: './test-results/playwright',
  fullyParallel: false,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: [['list']],
  snapshotPathTemplate: '{testDir}/__snapshots__/{arg}{ext}',
  use: {
    browserName: 'chromium',
    headless: true,
    launchOptions: {
      executablePath: process.env.AIRP_CHROME_PATH || '/usr/bin/google-chrome',
    },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
});
