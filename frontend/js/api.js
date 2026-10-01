// API client: the only place that calls fetch(). Every function returns the JSON
// answer; a non-2xx answer throws an ApiError with the server's message.

export class ApiError extends Error {
  constructor(status, detail) {
    super(ApiError.message(status, detail));
    this.status = status;
    this.detail = detail;
  }

  // Errors are {"detail": message}, or for invalid input (422) a list of fields.
  static message(status, detail) {
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
  const res = await fetch(url, {
    method,
    headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, data?.detail);
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
