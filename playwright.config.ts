import { defineConfig, devices } from '@playwright/test';

const port = Number(process.env.E2E_PORT || 4173);
const python = process.env.PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
const publishedURL = process.env.E2E_BASE_URL;

export default defineConfig({
  testDir: './tests/e2e',
  timeout: 30_000,
  expect: { timeout: 5_000 },
  use: {
    baseURL: publishedURL || `http://127.0.0.1:${port}`,
    trace: 'on-first-retry'
  },
  webServer: publishedURL ? undefined : {
    command: `"${python}" scripts/serve_static.py --port ${port}`,
    url: `http://127.0.0.1:${port}`,
    reuseExistingServer: false
  },
  projects: [
    { name: 'desktop-chrome', use: { ...devices['Desktop Chrome'] } },
    { name: 'mobile-chrome', use: { ...devices['Pixel 5'] } },
    { name: 'mobile-safari', use: { ...devices['iPhone 12'] } }
  ]
});
