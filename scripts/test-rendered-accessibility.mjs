import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const baseUrl = process.env.CPI_LOG_LENS_URL || 'http://127.0.0.1:8080';
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE);
const axeSource = await readFile(process.env.AXE_CORE_PATH, 'utf8');
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage();
let auditScheduleId;
let auditTenantId;

async function audit(state) {
  const result = await page.evaluate(async () => window.axe.run(document));
  const critical = result.violations.filter(violation => violation.impact === 'critical');
  assert.deepEqual(
    critical.map(({ id, help, nodes }) => ({ id, help, nodes: nodes.length })),
    [],
    `${state} has no critical axe-core violations`,
  );
  const lower = result.violations.filter(violation => violation.impact !== 'critical');
  console.log(
    `${state}: 0 critical axe-core findings${lower.length ? `; lower impact: ${lower.map(item => `${item.id} (${item.impact}, ${item.nodes.length})`).join(', ')}` : ''}`,
  );
}

async function openRoute(route, title) {
  await page.goto(`${baseUrl}/#${route}`);
  await page.waitForFunction(expected => document.title === expected, title);
  await page.locator('main h1:visible').waitFor();
  if (!(await page.evaluate(() => Boolean(window.axe)))) {
    await page.addScriptTag({ content: axeSource });
  }
}

async function openDialog(buttonName, dialogSelector, state, exact = false) {
  const trigger = typeof buttonName === 'string'
    ? page.getByRole('button', { name: buttonName, exact })
    : buttonName;
  await trigger.click();
  const dialog = page.locator(dialogSelector);
  await dialog.waitFor({ state: 'visible' });
  await audit(state);
  return { dialog, trigger };
}

async function dismissWithEscape(dialog, trigger) {
  await page.keyboard.press('Escape');
  await dialog.waitFor({ state: 'hidden' });
  await page.waitForFunction(element => document.activeElement === element, await trigger.elementHandle());
}

try {
  auditTenantId = `axe-${Date.now().toString(36)}`;
  const tenantResponse = await page.request.post(`${baseUrl}/api/tenants`, {
    data: {
      id: auditTenantId,
      name: `Axe audit ${auditTenantId}`,
      api_url: 'https://example.invalid',
      oauth_url: 'https://example.invalid/token',
      client_id: 'axe-audit-client',
      client_secret: 'axe-audit-secret',
    },
  });
  assert.equal(tenantResponse.status(), 201, await tenantResponse.text());

  const scheduleResponse = await page.request.post(`${baseUrl}/api/schedules`, {
    data: {
      name: `Accessibility audit ${Date.now()}`,
      tenants: [auditTenantId],
      log_types: ['trace'],
      hours: 0,
      interval_minutes: 60,
      enabled: false,
    },
  });
  assert.equal(scheduleResponse.status(), 201, await scheduleResponse.text());
  auditScheduleId = (await scheduleResponse.json()).id;

  await openRoute('browse', 'Browse logs | CPI Log Lens');
  await audit('Browse');

  await openRoute('fetch', 'Fetch logs | CPI Log Lens');
  await audit('Fetch');
  const { dialog: scheduleDialog, trigger: scheduleTrigger } = await openDialog(
    'New schedule',
    'dialog[aria-labelledby="schedule-dialog-title"]',
    'Fetch / new schedule dialog',
  );
  await page.getByRole('heading', { name: 'New schedule' }).click();
  await scheduleDialog.waitFor({ state: 'visible' });
  await page.mouse.click(8, Math.floor((await page.evaluate(() => innerHeight)) / 2));
  await scheduleDialog.waitFor({ state: 'hidden' });
  await page.waitForFunction(
    element => document.activeElement === element,
    await scheduleTrigger.elementHandle(),
  );
  console.log('Native dialog backdrop click closes without dismissing on content clicks and restores focus');

  const { dialog: deleteScheduleDialog, trigger: deleteScheduleTrigger } = await openDialog(
    'Delete schedule',
    'dialog[aria-labelledby="delete-schedule-dialog-title"]',
    'Fetch / delete schedule dialog',
  );
  await dismissWithEscape(deleteScheduleDialog, deleteScheduleTrigger);

  await openRoute('stats', 'Statistics | CPI Log Lens');
  await audit('Stats');

  await openRoute('settings', 'Settings | CPI Log Lens');
  await audit('Settings');
  const { dialog: tenantDialog, trigger: tenantTrigger } = await openDialog(
    'Add tenant',
    'dialog[aria-labelledby="tenant-dialog-title"]',
    'Settings / add tenant dialog',
  );
  await dismissWithEscape(tenantDialog, tenantTrigger);

  const tenantRow = page.getByText(auditTenantId, { exact: true }).locator('xpath=../../..');
  const { dialog: deleteTenantDialog, trigger: deleteTenantTrigger } = await openDialog(
    tenantRow.getByRole('button', { name: 'Delete', exact: true }),
    'dialog[aria-labelledby="delete-tenant-dialog-title"]',
    'Settings / delete tenant dialog',
  );
  await dismissWithEscape(deleteTenantDialog, deleteTenantTrigger);

  const { dialog: clearDialog, trigger: clearTrigger } = await openDialog(
    'Clear database',
    'dialog[aria-labelledby="clear-database-dialog-title"]',
    'Settings / clear database dialog',
  );
  await dismissWithEscape(clearDialog, clearTrigger);

  const { dialog: cleanupDialog, trigger: cleanupTrigger } = await openDialog(
    'Delete old entries',
    'dialog[aria-labelledby="cleanup-dialog-title"]',
    'Settings / cleanup dialog',
  );
  await dismissWithEscape(cleanupDialog, cleanupTrigger);

  await page.setViewportSize({ width: 390, height: 844 });
  await openRoute('browse', 'Browse logs | CPI Log Lens');
  const menu = page.getByRole('button', { name: 'Menu', exact: true });
  await menu.click();
  await page.getByRole('button', { name: 'Close', exact: true }).waitFor();
  await page.getByRole('button', { name: 'Close', exact: true }).click();
  await page.getByRole('button', { name: 'Menu', exact: true }).waitFor();
  console.log('Mobile navigation accessible names match visible Menu and Close labels');
} finally {
  if (auditScheduleId) {
    const response = await page.request.delete(`${baseUrl}/api/schedules/${auditScheduleId}`);
    assert.equal(response.status(), 200, await response.text());
  }
  if (auditTenantId) {
    const response = await page.request.delete(`${baseUrl}/api/tenants/${auditTenantId}`);
    assert.equal(response.status(), 200, await response.text());
  }
  await browser.close();
}
