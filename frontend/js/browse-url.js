export const FILTERS = ['tenant', 'level', 'iflow', 'grep', 'date_from', 'date_to'];
export const LEVELS = ['', 'ERROR', 'WARN', 'INFO', 'DEBUG', 'ALL'];
export const emptyFilters = () => Object.fromEntries(FILTERS.map(key => [key, '']));

// A position in the newest-first log list: the newest entries, a cursor of the API (after
// Newer/Older; also built here from an entry), a time (Jump to time), or a page number
// (links from earlier versions).
export const NEWEST = Object.freeze({ kind: 'newest' });
const POSITIONS = ['cursor', 'at', 'page'];
const CURSOR_RE = /^[ona]\d{8}T\d{6}(\.\d{1,6})?_\d{1,19}$/;
const END_OF_DAY = new Set(['date_to', 'at']); // a bare date means the end of that day

// The API's cursor for the entries older ('o') or newer ('n') than an entry, or from the entry
// on ('a'): kind, compact ISO timestamp, id (the Cursor format in app/repositories/logs.py).
export function entryCursor(kind, entry) {
  const stamp = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(\.\d{1,6})?$/.exec(entry?.timestamp || '');
  if (!stamp || !Number.isSafeInteger(entry.id)) return null;
  const [, year, month, day, hour, minute, second, fraction = ''] = stamp;
  return `${kind}${year}${month}${day}T${hour}${minute}${second}${fraction}_${entry.id}`;
}

function invalid(message) {
  throw new TypeError(`Invalid Browse URL: ${message}`);
}

function normalizeDate(value, key) {
  if (/^\d{4}-\d{2}-\d{2}$/.test(value)) {
    const parsed = new Date(`${value}T00:00:00Z`);
    if (Number(value.slice(0, 4)) === 0 || !Number.isFinite(parsed.getTime()) || parsed.toISOString().slice(0, 10) !== value)
      invalid(`${key} must be a valid ISO date or datetime`);
    return `${value}T${END_OF_DAY.has(key) ? '23:59:59' : '00:00:00'}`;
  }
  const match = value.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2})(\.\d{1,3})?)?(Z|[+-]\d{2}:\d{2})?$/);
  if (!match) invalid(`${key} must be an ISO date or datetime`);
  const [, day, hour, minute, second = '00', fraction = '', zone = ''] = match;
  const calendar = new Date(`${day}T00:00:00Z`);
  if (Number(day.slice(0, 4)) === 0 || !Number.isFinite(calendar.getTime()) || calendar.toISOString().slice(0, 10) !== day ||
      Number(hour) > 23 || Number(minute) > 59 || Number(second) > 59)
    invalid(`${key} must be a valid ISO date or datetime`);
  if (zone) {
    const parsed = new Date(value);
    if (!Number.isFinite(parsed.getTime())) invalid(`${key} must be a valid ISO date or datetime`);
    const utc = new Date(`${day}T${hour}:${minute}:${second}${zone}`);
    const normalized = `${utc.toISOString().slice(0, 19)}${fraction}`;
    if (!/^\d{4}-/.test(normalized)) invalid(`${key} is outside the supported date range`);
    return normalized;
  }
  return `${day}T${hour}:${minute}:${second}${fraction}`;
}

export function toUtcDateTime(value, key) {
  return value ? normalizeDate(value, key) : '';
}

function readHash(hash) {
  const fragment = hash.startsWith('#') ? hash.slice(1) : hash;
  const separator = fragment.indexOf('?');
  const page = separator < 0 ? fragment : fragment.slice(0, separator);
  return { page: page || 'browse', query: separator < 0 ? '' : fragment.slice(separator + 1) };
}

export function parseBrowseHash(hash) {
  const { page, query } = readHash(hash);
  if (page !== 'browse') return null;

  const params = new URLSearchParams(query);
  const allowed = new Set([...FILTERS, ...POSITIONS, 'entry']);
  for (const key of params.keys()) {
    if (!allowed.has(key)) invalid(`unknown parameter "${key}"`);
    if (params.getAll(key).length !== 1) invalid(`parameter "${key}" must appear once`);
  }

  const filters = emptyFilters();
  for (const key of FILTERS) {
    const value = params.get(key) || '';
    if (key === 'level' && !LEVELS.includes(value)) invalid('level must be ERROR, WARN, INFO, DEBUG, or ALL');
    if ((key === 'tenant' && value && value !== 'all' && !/^[a-z0-9][a-z0-9_-]{0,31}$/.test(value)))
      invalid('tenant is invalid');
    if ((key === 'iflow' || key === 'grep') && value.length > 500) invalid(`${key} is too long`);
    filters[key] = key === 'date_from' || key === 'date_to' ? (value ? normalizeDate(value, key) : '') : value;
  }
  if (filters.date_from && filters.date_to && filters.date_from > filters.date_to)
    invalid('date_from must not be after date_to');

  if (POSITIONS.filter(key => params.has(key)).length > 1) invalid('use only one of cursor, at, and page');
  let position = NEWEST;
  if (params.has('cursor')) {
    if (!CURSOR_RE.test(params.get('cursor'))) invalid('cursor is invalid');
    position = { kind: 'cursor', value: params.get('cursor') };
  } else if (params.has('at')) {
    position = { kind: 'at', value: normalizeDate(params.get('at'), 'at') };
  } else if (params.has('page')) {
    const pageValue = params.get('page');
    if (!/^[1-9]\d*$/.test(pageValue) || Number(pageValue) > 1_000_000) invalid('page must be between 1 and 1000000');
    if (Number(pageValue) > 1) position = { kind: 'page', value: Number(pageValue) };
  }
  const entryValue = params.get('entry');
  if (entryValue !== null && (!/^[1-9]\d*$/.test(entryValue) || !Number.isSafeInteger(Number(entryValue))))
    invalid('entry must be a positive integer');

  return { filters, position, entry: entryValue === null ? null : Number(entryValue) };
}

export function browseHash({ filters, position = NEWEST, entry = null }) {
  const params = new URLSearchParams();
  for (const key of FILTERS) {
    let value = filters[key] || '';
    if (key === 'date_from' || key === 'date_to') value = value ? normalizeDate(value, key) : '';
    if (value) params.set(key, value);
  }
  if (filters.date_from && filters.date_to && filters.date_from > filters.date_to)
    invalid('date_from must not be after date_to');
  if (position.kind === 'cursor') params.set('cursor', position.value);
  else if (position.kind === 'at') params.set('at', normalizeDate(position.value, 'at'));
  else if (position.kind === 'page' && position.value > 1) params.set('page', String(position.value));
  if (entry !== null) params.set('entry', String(entry));
  const query = params.toString();
  const hash = `#browse${query ? `?${query}` : ''}`;
  parseBrowseHash(hash);
  return hash;
}
