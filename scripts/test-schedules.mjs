import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

// Schedule helpers of the Fetch page: one duration format, quick choices, the gap
// warning, relative times and the summary of a schedule's last run.
const source = await readFile(new URL('../frontend/js/schedules.js', import.meta.url));
const raw = await import(`data:text/javascript;base64,${source.toString('base64')}`);
// Durations use a no-break space ("1 h" stays on one line); compare them with plain spaces.
const plain = value => (typeof value === 'string' ? value.replaceAll('\u00a0', ' ')
  : value && typeof value === 'object' && !Array.isArray(value)
    ? Object.fromEntries(Object.entries(value).map(([k, v]) => [k, plain(v)])) : value);
const s = Object.fromEntries(Object.entries(raw).map(([name, value]) =>
  [name, typeof value === 'function' ? (...args) => plain(value(...args)) : value]));
assert.equal(raw.duration(60), '1\u00a0h');

assert.deepEqual([5, 15, 60, 90, 120, 255, 1440, 2880, 4320, 10080].map(s.duration),
  ['5 min', '15 min', '1 h', '1 h 30 min', '2 h', '4 h 15 min', '24 h', '2 days', '3 days', '7 days']);
assert.equal(s.intervalLabel(15), 'Every 15 min');
assert.equal(s.rangeLabel(1), 'Last 1 h');
assert.equal(s.rangeLabel(48), 'Last 2 days');
assert.equal(s.rangeLabel(0), 'All available');
assert.deepEqual(s.PRESETS.map(s.presetLabel), [
  'Every 15 min · last 1 h', 'Every 1 h · last 2 h', 'Every 6 h · last 12 h', 'Every 24 h · last 2 days',
]);
for (const p of s.PRESETS) {
  assert.ok(s.INTERVALS.includes(p.interval_minutes) && s.RANGES.includes(p.hours), 'presets are select options');
  assert.equal(s.coverage(p.interval_minutes, p.hours).level, 'overlap', `${s.presetLabel(p)} leaves no gaps`);
}

// Values set through the API stay selectable; "All available" stays last.
assert.equal(s.withValue(s.INTERVALS, 15), s.INTERVALS);
assert.deepEqual(s.withValue([1, 2, 6, 0], 5), [1, 2, 5, 6, 0]);
assert.deepEqual(s.withValue([5, 15, 60], 45), [5, 15, 45, 60]);

// Gaps: a run fetches the files changed within its time range.
assert.equal(s.coverage(120, 1).level, 'gap');
assert.match(s.coverage(120, 1).text, /^Gaps: runs are 2 h apart, but each one fetches only the last 1 h/);
assert.equal(s.coverage(60, 1).level, 'edge');
assert.equal(s.coverage(15, 1).level, 'overlap');
assert.equal(s.coverage(15, 1).text, 'Each run overlaps the previous one by 45 min.');
assert.equal(s.coverage(1440, 0).level, 'all');

assert.equal(s.suggestName(['PRD'], 15), 'PRD · every 15 min');
assert.equal(s.suggestName(['DEV', 'QAS', 'PRD', 'SBX'], 60), 'DEV, QAS +2 · every 1 h');
assert.equal(s.suggestName([], 15), 'Fetch every 15 min');

const now = Date.parse('2026-01-15T12:00:00Z');
const at = minutes => new Date(now + minutes * 60000).toISOString();
assert.equal(s.relativeTime(at(14), now), 'in 14 min');
assert.equal(s.relativeTime(at(-3 * 60), now), '3 h ago');
assert.equal(s.relativeTime(at(3 * 1440), now), 'in 3 days');
assert.equal(s.relativeTime(at(0.2), now), 'in less than a minute');
assert.equal(s.relativeTime(at(-0.2), now), 'just now');
assert.equal(s.relativeTime(null, now), null);

assert.equal(s.nextRunText({ enabled: false, next_run_at: null }, now), 'Disabled');
assert.equal(s.nextRunText({ enabled: true, next_run_at: at(14) }, now), 'in 14 min');
assert.equal(s.nextRunText({ enabled: true, next_run_at: at(-1) }, now), 'Due now');

const run = changes => ({
  last_run_at: null,
  last_run: { id: 'r', status: 'done', started_at: at(-12), finished_at: at(-11), files_total: 4, rows_imported: 1204,
    warnings: 0, errors: 0, error: null, parts: 2, ...changes },
});
const pick = summary => [summary.tone, summary.label, summary.detail];
assert.deepEqual(pick(s.lastRunSummary(run({}))), ['success', '1,204 new entries', '']);
assert.equal(s.lastRunSummary(run({})).at, at(-11));
assert.deepEqual(pick(s.lastRunSummary(run({ rows_imported: 0 }))), ['success', 'Up to date', '']);
assert.deepEqual(pick(s.lastRunSummary(run({ warnings: 2 }))), ['warning', '1,204 new entries · 2 warnings', '']);
assert.deepEqual(pick(s.lastRunSummary(run({ errors: 1, error: 'Couldn’t get an OAuth token for QAS.' }))),
  ['warning', 'Partly failed · 1,204 new entries', 'Couldn’t get an OAuth token for QAS.']);
assert.equal(s.lastRunSummary(run({ errors: 1, rows_imported: 0 })).label, 'Partly failed');
// Failed only when every tenant and log type failed; a part without files in the time range worked.
assert.equal(s.lastRunSummary(run({ errors: 1, files_total: 0, rows_imported: 0 })).label, 'Partly failed');
assert.equal(s.lastRunSummary(run({ errors: 1, files_total: 0, parts: undefined })).label, 'Failed', 'without parts');
assert.deepEqual(pick(s.lastRunSummary(run({ errors: 2, files_total: 0, rows_imported: 0, error: 'Rejected.' }))),
  ['error', 'Failed', 'Rejected.']);
assert.deepEqual(pick(s.lastRunSummary(run({ status: 'error', error: 'No tenants configured.' }))),
  ['error', 'Failed', 'No tenants configured.']);
assert.deepEqual(pick(s.lastRunSummary(run({ status: 'running', finished_at: null }))), ['running', 'Running', '']);
assert.equal(s.lastRunSummary(run({ status: 'interrupted' })).tone, 'muted');
assert.deepEqual(pick(s.lastRunSummary({ last_run: null, last_run_at: '2026-01-15 11:00:00+00' })), ['success', 'Succeeded', '']);
assert.deepEqual(pick(s.lastRunSummary({ last_run: null, last_run_at: null })), ['muted', 'Not run yet', '']);

console.log('Schedule durations, quick choices, gap warning, next run and last result passed');
