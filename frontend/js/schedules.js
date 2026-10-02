// Schedule helpers for the Fetch page: durations in one format, the quick choices, the
// check for gaps between runs, relative times and the summary of a schedule's last run.
// No Alpine or DOM access, so scripts/test-schedules.mjs tests them in Node.

export const INTERVALS = [5, 10, 15, 30, 60, 120, 180, 360, 720, 1440]; // minutes
export const RANGES = [1, 2, 3, 6, 12, 24, 48, 72, 168, 0]; // hours; 0 = all available files
export const PRESETS = [
  { interval_minutes: 15, hours: 1 },
  { interval_minutes: 60, hours: 2 },
  { interval_minutes: 360, hours: 12 },
  { interval_minutes: 1440, hours: 48 },
];

const count = n => n.toLocaleString('en-US');
const NBSP = '\u00a0'; // keeps "1 h" on one line

/** 15 → "15 min", 60 → "1 h", 90 → "1 h 30 min", 1440 → "24 h", 2880 → "2 days" (no-break spaces). */
export function duration(minutes) {
  if (minutes < 60) return `${count(minutes)}${NBSP}min`;
  if (minutes % 60) return `${duration(minutes - (minutes % 60))} ${minutes % 60}${NBSP}min`;
  const hours = minutes / 60;
  if (hours < 48 || hours % 24) return `${count(hours)}${NBSP}h`;
  return `${count(hours / 24)}${NBSP}days`;
}

export const intervalLabel = minutes => `Every ${duration(minutes)}`;
export const rangeLabel = hours => (hours === 0 ? 'All available' : `Last ${duration(hours * 60)}`);
export const presetLabel = p => `${intervalLabel(p.interval_minutes)} · ${rangeLabel(p.hours).toLowerCase()}`;

/** The choices of a select, with a value set elsewhere (e.g. through the API) added. */
export function withValue(options, value) {
  if (options.includes(value)) return options;
  // "All available" (0) stays last
  return [...options.filter(o => o !== 0), value].sort((a, b) => a - b).concat(options.includes(0) ? [0] : []);
}

/**
 * Whether the runs leave gaps: a run fetches the log files changed within its time range,
 * so the range has to reach back to the previous run.
 * → { level: 'gap' | 'edge' | 'overlap' | 'all', text }
 */
export function coverage(intervalMinutes, hours) {
  if (hours === 0) {
    return { level: 'all', text: 'Each run checks all available log files; files that did not change are skipped.' };
  }
  const overlap = hours * 60 - intervalMinutes;
  if (overlap < 0) {
    return {
      level: 'gap',
      text: `Gaps: runs are ${duration(intervalMinutes)} apart, but each one fetches only the last ` +
        `${duration(hours * 60)}, so log files changed in between are missed. Choose a longer time range or a shorter interval.`,
    };
  }
  if (overlap === 0) {
    return {
      level: 'edge',
      text: 'No overlap: a run that starts late, for example while another fetch is running, misses changes. ' +
        'Choose a longer time range.',
    };
  }
  return { level: 'overlap', text: `Each run overlaps the previous one by ${duration(overlap)}.` };
}

/** A name for a schedule from its tenants and interval, e.g. "PRD · every 15 min". */
export function suggestName(tenantNames, intervalMinutes) {
  const every = intervalLabel(intervalMinutes).toLowerCase();
  if (!tenantNames.length) return `Fetch ${every}`;
  const shown = tenantNames.length > 3 ? `${tenantNames.slice(0, 2).join(', ')} +${tenantNames.length - 2}` : tenantNames.join(', ');
  return `${shown} · ${every}`.slice(0, 100);
}

/** "in 14 min", "3 h ago", "in 2 days", "just now"; null for a missing time. */
export function relativeTime(iso, now = Date.now()) {
  if (!iso) return null;
  const diff = new Date(iso).getTime() - now;
  const minutes = Math.round(Math.abs(diff) / 60000);
  if (minutes < 1) return diff > 0 ? 'in less than a minute' : 'just now';
  let amount;
  if (minutes < 60) amount = `${minutes}${NBSP}min`;
  else if (minutes < 48 * 60) amount = `${Math.round(minutes / 60)}${NBSP}h`;
  else amount = `${Math.round(minutes / 1440)}${NBSP}days`;
  return diff > 0 ? `in ${amount}` : `${amount} ago`;
}

/** When a schedule runs next, for the list. */
export function nextRunText(schedule, now = Date.now()) {
  if (!schedule.enabled) return 'Disabled';
  if (!schedule.next_run_at) return '—';
  if (new Date(schedule.next_run_at).getTime() <= now) return 'Due now';
  return relativeTime(schedule.next_run_at, now);
}

// Every tenant and log type of a run failed (`parts` comes with the run; without it, a run
// that found no file at all counts as failed).
const allPartsFailed = run => (run.parts ? run.errors >= run.parts : !run.files_total);

const entries = n => `${count(n)} new ${n === 1 ? 'entry' : 'entries'}`;
const warnings = n => `${count(n)} ${n === 1 ? 'warning' : 'warnings'}`;

/**
 * How a schedule's last run went, for the list.
 * → { tone: 'running' | 'success' | 'warning' | 'error' | 'muted', icon, label, detail, at }
 */
export function lastRunSummary(schedule) {
  const run = schedule.last_run;
  if (!run) {
    // Runs before the run history was kept only left the time of the last success.
    if (schedule.last_run_at) return { tone: 'success', icon: 'circle-check', label: 'Succeeded', detail: '', at: schedule.last_run_at };
    return { tone: 'muted', icon: 'circle-dashed', label: 'Not run yet', detail: '', at: null };
  }
  const at = run.finished_at || run.started_at;
  const imported = run.rows_imported ? entries(run.rows_imported) : 'Up to date';
  const partly = run.rows_imported ? `Partly failed · ${entries(run.rows_imported)}` : 'Partly failed';
  switch (run.status) {
    case 'running':
      return { tone: 'running', icon: '', label: 'Running', detail: '', at: run.started_at };
    case 'done':
      if (run.errors && allPartsFailed(run)) return { tone: 'error', icon: 'circle-x', label: 'Failed', detail: run.error || '', at };
      if (run.errors) return { tone: 'warning', icon: 'triangle-alert', label: partly, detail: run.error || '', at };
      if (run.warnings) return { tone: 'warning', icon: 'triangle-alert', label: `${imported} · ${warnings(run.warnings)}`, detail: '', at };
      return { tone: 'success', icon: 'circle-check', label: imported, detail: '', at };
    case 'error':
      return { tone: 'error', icon: 'circle-x', label: 'Failed', detail: run.error || '', at };
    case 'cancelled':
      return { tone: 'muted', icon: 'square', label: 'Cancelled', detail: '', at };
    case 'interrupted':
      return { tone: 'muted', icon: 'square', label: 'Interrupted: the app stopped', detail: '', at };
    default:
      return { tone: 'muted', icon: 'circle-dashed', label: run.status, detail: run.error || '', at };
  }
}
