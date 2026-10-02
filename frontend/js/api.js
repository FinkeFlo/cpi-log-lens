// API client: the only place that calls fetch(). Every function returns the JSON
// answer; a non-2xx answer throws an ApiError with the server's message, and a request
// that never reached the server throws an ApiError with status 0.

export class ApiError extends Error {
  constructor(status, detail, { retryAfter = null } = {}) {
    super(ApiError.message(status, detail));
    this.status = status;
    this.detail = detail;
    this.retryAfter = retryAfter; // seconds, from the Retry-After header of a 503
  }

  // unreachable: no answer (server stopped, network down) · busy: 503, the database
  // could not serve the request in time · failed: any other error answer.
  get kind() {
    if (this.status === 0) return 'unreachable';
    if (this.status === 503) return 'busy';
    return 'failed';
  }

  // Errors are {"detail": message}, or for invalid input (422) a list of fields.
  static message(status, detail) {
    if (status === 0) return "Can't reach the server";
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail.map(e => `${(e.loc || []).filter(x => x !== 'body').join('.')}: ${e.msg}`).join('; ');
    }
    return `HTTP ${status}`;
  }
}

async function request(method, path, { query, body } = {}) {
  const url = new URL(`/api${path}`, location.origin);
  for (const [key, value] of Object.entries(query || {})) {
    if (value !== '' && value !== null && value !== undefined) url.searchParams.set(key, value);
  }
  let res;
  try {
    res = await fetch(url, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, null); // fetch() rejects only when no answer arrived
  }
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) {
    const retryAfter = Number.parseInt(res.headers.get('Retry-After'), 10);
    throw new ApiError(res.status, data?.detail, { retryAfter: Number.isFinite(retryAfter) ? retryAfter : null });
  }
  return data;
}

const id = value => encodeURIComponent(value);

export const api = {
  tenants: {
    list: () => request('GET', '/tenants'),
    save: (tenant, isNew) =>
      isNew ? request('POST', '/tenants', { body: tenant }) : request('PUT', `/tenants/${id(tenant.id)}`, { body: tenant }),
    remove: (tenantId, purge) => request('DELETE', `/tenants/${id(tenantId)}`, { query: { purge } }),
    test: tenantId => request('POST', `/tenants/${id(tenantId)}/test`),
  },
  logs: {
    search: query => request('GET', '/logs', { query }),
    iflows: tenant => request('GET', '/logs/iflows', { query: { tenant } }),
    get: entryId => request('GET', `/logs/${id(entryId)}`),
  },
  fetch: {
    start: body => request('POST', '/fetch', { body }),
    demo: () => request('POST', '/demo'),
    status: () => request('GET', '/fetch/status'),
    cancel: () => request('POST', '/fetch/cancel'),
    stream: () => new EventSource('/api/fetch/stream'),
    defaultConfig: () => request('GET', '/fetch/default-config'),
    saveDefaultConfig: body => request('PUT', '/fetch/default-config', { body }),
  },
  schedules: {
    list: () => request('GET', '/schedules'),
    // scheduleId null: create a new schedule
    save: (scheduleId, body) =>
      scheduleId ? request('PUT', `/schedules/${id(scheduleId)}`, { body }) : request('POST', '/schedules', { body }),
    remove: scheduleId => request('DELETE', `/schedules/${id(scheduleId)}`),
  },
  stats: {
    get: tenant => request('GET', '/stats', { query: { tenant } }),
  },
  db: {
    info: () => request('GET', '/db/info'),
    clear: () => request('POST', '/db/clear'),
    cleanup: body => request('POST', '/db/cleanup', { body }),
  },
};
