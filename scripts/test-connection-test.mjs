import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

// Wording of connection test results in the tenant dialog and list.
const source = await readFile(new URL('../frontend/js/connection.js', import.meta.url));
const c = await import(`data:text/javascript;base64,${source.toString('base64')}`);

assert.deepEqual(c.connectionTestResult({ ok: true, trace_files: 3 }),
  { ok: true, title: 'Connection works.', hint: 'Got a token and found 3 trace log files.' });
assert.equal(c.connectionTestResult({ ok: true, trace_files: 1 }).hint, 'Got a token and found 1 trace log file.');
assert.equal(c.connectionTestResult({ ok: true, demo: true }).title, 'The demo tenant needs no connection.');

const apiError = (status, data) => Object.assign(new Error(typeof data?.detail === 'string' ? data.detail : `HTTP ${status}`), { status, data });
assert.deepEqual(
  c.connectionTestError(apiError(502, {
    detail: 'x', kind: 'missing_role', step: 'api',
    message: 'The credentials work, but they may not read log files (HTTP 403).',
    hint: 'Assign a role that allows reading log files.',
  })),
  { ok: false, title: 'The credentials work, but they may not read log files (HTTP 403).', hint: 'Assign a role that allows reading log files.' },
);
assert.equal(c.connectionTestError(apiError(0, null)).title, "Can't reach CPI Log Lens.");
assert.deepEqual(c.connectionTestError(apiError(422, { detail: 'Enter the client secret.' })),
  { ok: false, title: 'Enter the client secret.', hint: '' });
const invalid = c.connectionTestError(apiError(422, { detail: [
  { loc: ['body', 'api_url'], type: 'string_pattern_mismatch', msg: "String should match pattern '^https?://\\S+$'" },
  { loc: ['body', 'client_id'], type: 'string_too_short', msg: 'String should have at least 1 character' },
] }));
assert.equal(invalid.title, 'Check the connection details.');
assert.equal(invalid.hint, 'API URL must start with https:// and contain no spaces. Client ID is required.');
assert.equal(c.connectionTestError(apiError(500, { detail: 'Internal server error' })).title, 'Internal server error');

const form = { api_url: 'https://t.example', oauth_url: 'https://a.example/oauth/token', client_id: 'id', client_secret: '' };
assert.deepEqual(c.missingForTest(form, true), [], 'a saved tenant may keep its secret');
assert.deepEqual(c.missingForTest(form, false), ['Client secret']);
assert.deepEqual(c.missingForTest({ ...form, api_url: ' ', client_id: '' }, true), ['API URL', 'Client ID']);

// Pasting a service key fills the fields after a short delay; a test result (or a test still
// running) from before belongs to the old values and is dropped.
const moduleUrl = text => `data:text/javascript;base64,${Buffer.from(text).toString('base64')}`;
const settingsSource = (await readFile(new URL('../frontend/js/pages/settings.js', import.meta.url), 'utf8'))
  .replace("'../api.js'", `'${moduleUrl('export const api = {};')}'`)
  .replace("'../connection.js'", `'${moduleUrl(source.toString())}'`)
  .replace("'../events.js'", `'${moduleUrl('export const LOGS_CHANGED = 1, SCHEDULES_CHANGED = 2; export function emit() {}')}'`)
  .replace("'../states.js'", `'${moduleUrl('export function describeError() {}')}'`);
const { default: settingsPage } = await import(moduleUrl(settingsSource));
const page = settingsPage();
page.openTenantModal();
page.tenantModal.test = { busy: false, result: { ok: true, title: 'Connection works.', hint: '' } };
const runBefore = page._testRun;
page.tenantModal.serviceKey = JSON.stringify({ oauth: {
  url: 'https://tenant.example.test/', tokenurl: 'https://auth.example.test/oauth/token', clientid: 'id', clientsecret: 's',
} });
page.applyServiceKey();
assert.equal(page.tenantModal.form.api_url, 'https://tenant.example.test');
assert.equal(page.tenantModal.test.result, null, 'the result of the old values is gone');
assert.ok(page._testRun > runBefore, 'an answer to a test of the old values is dropped');

console.log('Connection test results, readable errors, missing fields and service key paste passed');
