import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

// The fetch job store, fed with the server's events: progress per tenant and log type,
// a partial failure shown as such, and reconnecting after the event stream broke.
const moduleUrl = source => `data:text/javascript;base64,${Buffer.from(source).toString('base64')}`;
globalThis.__events = [];
globalThis.__status = [];
globalThis.__streams = [];
const apiStub = moduleUrl(`
  export const api = { fetch: {
    status: async () => { const next = globalThis.__status.shift(); if (next instanceof Error) throw next; return next; },
    stream: () => { const es = { closed: false, close() { this.closed = true; } }; globalThis.__streams.push(es); return es; },
  } };
`);
const eventsStub = moduleUrl(`
  export const LOGS_CHANGED = 'logs-changed';
  export function emit(name, detail) { globalThis.__events.push({ name, detail }); }
`);
const source = (await readFile(new URL('../frontend/js/stores/fetchJob.js', import.meta.url), 'utf8'))
  .replace("'../api.js'", `'${apiStub}'`)
  .replace("'../events.js'", `'${eventsStub}'`);
const { default: store } = await import(moduleUrl(source));
const part = (index, tenant, logType, changes = {}) => ({
  index, tenant, tenant_name: tenant.toUpperCase(), log_type: logType, status: 'pending', files_total: null,
  files_done: 0, imported: 0, warnings: 0, error: '', ...changes,
});
const s = Object.defineProperties({}, Object.getOwnPropertyDescriptors(store));
s.status = 'running';
s.handle({ type: 'parts', parts: [part(0, 'dev', 'trace'), part(1, 'qas', 'trace'), part(2, 'prd', 'trace')] });
assert.equal(s.percent, null, 'nothing known yet: indeterminate');
const seen = [];
s.handle({ type: 'status', msg: 'Requesting token for DEV…', part: part(0, 'dev', 'trace', { status: 'running' }) });
s.handle({ type: 'files_found', count: 2, tenant: 'DEV', log_type: 'trace', part: part(0, 'dev', 'trace', { status: 'running', files_total: 2 }) });
seen.push(s.percent);
s.handle({ type: 'progress', done: 1, total: 2, file: 'a.log', new_rows: 3, imported: 3,
  part: part(0, 'dev', 'trace', { status: 'running', files_total: 2, files_done: 1, imported: 3 }) });
seen.push(s.percent);
assert.equal(s.partText(s.parts[0]), '1 of 2 files · 3 new entries');
s.handle({ type: 'warn', msg: 'Download failed for b.log: 500', file: 'b.log',
  part: part(0, 'dev', 'trace', { status: 'running', files_total: 2, files_done: 1, imported: 3, warnings: 1 }) });
s.handle({ type: 'progress', done: 2, total: 2, file: 'b.log', new_rows: 0, imported: 3,
  part: part(0, 'dev', 'trace', { status: 'running', files_total: 2, files_done: 2, imported: 3, warnings: 1 }) });
s.handle({ type: 'part', part: part(0, 'dev', 'trace', { status: 'done', files_total: 2, files_done: 2, imported: 3, warnings: 1 }) });
seen.push(s.percent);
s.handle({ type: 'tenant_error', msg: "Couldn't get an OAuth token for QAS: 401",
  part: part(1, 'qas', 'trace', { status: 'failed', error: "Couldn't get an OAuth token for QAS: 401" }) });
seen.push(s.percent);
assert.equal(s.status, 'running', 'a failed tenant does not end the job');
s.handle({ type: 'part', part: part(2, 'prd', 'trace', { status: 'done', files_total: 0 }) });
seen.push(s.percent);
assert.deepEqual(seen, [0, 17, 33, 67, 100], 'overall progress runs over all parts and never goes back');
s.handle({ type: 'done', imported: 3, warnings: 1, errors: 1 });
assert.equal(s.status, 'done');
assert.equal(s.outcome, 'partial', 'a run with a failed tenant is a partial failure, not an error');
assert.equal(s.statusMsg, 'Completed with errors');
assert.equal(s.failedParts.length, 1);
assert.deepEqual(s.problems.map(p => [p.level, s.problemSource(p)]), [['warn', 'DEV · trace · b.log'], ['error', 'QAS · trace']]);
assert.deepEqual(s.fileWarnings.map(p => p.file), ['b.log'], 'the warning list holds file warnings only');
assert.equal(s.partText(s.parts[1]), "Couldn't get an OAuth token for QAS: 401");
assert.equal(s.partText(s.parts[2]), 'No log files in the time range');
assert.ok(__events.some(e => e.name === 'logs-changed' && e.detail.firstPage));

// Outcomes of other finished runs
const outcome = (changes, parts = []) => Object.assign(Object.defineProperties({}, Object.getOwnPropertyDescriptors(store)),
  { status: 'done', imported: 0, warnings: 0, errors: 0, parts, ...changes }).outcome;
assert.equal(outcome({ imported: 5 }), 'success');
assert.equal(outcome({}), 'up-to-date');
assert.equal(outcome({ imported: 5, warnings: 2 }), 'warnings');
assert.equal(outcome({ errors: 1 }, [part(0, 'dev', 'trace', { status: 'failed' })]), 'failed');
assert.equal(outcome({ status: 'error' }), 'error');

// Timers run only when the test says so; the delays they were set with are recorded.
const timers = [];
globalThis.setTimeout = (fn, ms) => timers.push({ fn, ms, done: false });
globalThis.clearTimeout = id => { if (timers[id - 1]) timers[id - 1].done = true; };
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise(resolve => setImmediate(resolve)); };
const runTimer = async () => {
  const timer = timers.find(t => !t.done);
  assert.ok(timer, 'a reconnect is scheduled');
  timer.done = true;
  timer.fn();
  await flush();
  return timer.ms;
};
const fresh = changes => Object.assign(Object.defineProperties({}, Object.getOwnPropertyDescriptors(store)),
  { status: 'running', jobId: 'j', parts: [], problems: [], _reconnectAttempt: 0, _source: null, _reconnectTimer: null }, changes);
const snapshot = changes => ({
  job_id: 'j', status: 'running', status_msg: '', done: 0, total: 0, current_file: '', current_tenant: '', imported: 0,
  error_msg: '', parts: [part(0, 'dev', 'trace', { status: 'running' })], problems: [], warnings: 0, errors: 0, ...changes,
});
const unreachable = Object.assign(new Error("Can't reach the server"), { kind: 'unreachable' });

// The job ended while the stream was down.
const ended = fresh();
ended.openStream();
__streams.at(-1).onerror();
__status.push(snapshot({
  status: 'done', status_msg: 'Completed with errors', imported: 7, errors: 1,
  parts: [part(0, 'dev', 'trace', { status: 'failed', error: 'boom' })],
}));
await runTimer();
assert.equal(ended.connectionLost, false);
assert.equal(ended.status, 'done');
assert.equal(ended.outcome, 'failed');
assert.equal(ended.imported, 7);

// The server restarted meanwhile: the job is gone (idle), so it was interrupted.
const restarted = fresh();
restarted.openStream();
__streams.at(-1).onerror();
__status.push({ status: 'idle' });
await runTimer();
assert.equal(restarted.status, 'error');
assert.match(restarted.errorMsg, /restarted while the fetch was running/);

// A part update that does not fit the known parts (an announcement was missed):
// read the parts again from the status.
const missed = fresh();
__status.push(snapshot({ parts: [part(0, 'dev', 'trace', { status: 'running' }), part(1, 'dev', 'http')] }));
missed.handle({ type: 'files_found', count: 2, tenant: 'DEV', log_type: 'trace',
  part: part(0, 'dev', 'trace', { status: 'running', files_total: 2 }) });
await flush();
assert.equal(missed.parts.length, 2, 'parts re-read from the status');
missed.handle({ type: 'done', imported: 0, warnings: 0, errors: 0,
  parts: [part(0, 'dev', 'trace', { status: 'done', files_total: 2, files_done: 2 }), part(1, 'dev', 'http', { status: 'done', files_total: 0 })] });
assert.deepEqual(missed.parts.map(p => p.status), ['done', 'done'], 'the final event brings the final parts');

// A job that ended never shows a part as waiting or running.
const failed = fresh({ status: 'error', parts: [part(0, 'dev', 'trace', { status: 'running' }), part(1, 'dev', 'http')] });
assert.deepEqual(failed.parts.map(p => failed.partStatus(p)), ['stopped', 'stopped']);
assert.equal(failed.partText(failed.parts[0]), 'Not finished');
assert.equal(failed.partIcon(failed.parts[1]), 'square');
assert.equal(fresh({ parts: [part(0, 'dev', 'trace', { status: 'running' })] }).partStatus(part(0, 'dev', 'trace', { status: 'running' })), 'running');

console.log('Fetch progress per tenant and log type, partial failures, part resync and reconnect passed');
