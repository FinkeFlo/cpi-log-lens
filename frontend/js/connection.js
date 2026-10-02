// Wording of connection test results, for the tenant dialog and the tenant list.

const FIELDS = { id: 'ID', api_url: 'API URL', oauth_url: 'OAuth URL', client_id: 'Client ID', client_secret: 'Client secret' };

/** A successful test answer → { ok, title, hint } */
export function connectionTestResult(answer) {
  if (answer?.demo) return { ok: true, title: 'The demo tenant needs no connection.', hint: '' };
  const n = answer?.trace_files ?? 0;
  return {
    ok: true,
    title: 'Connection works.',
    hint: `Got a token and found ${n.toLocaleString('en-US')} trace log ${n === 1 ? 'file' : 'files'}.`,
  };
}

/** The ApiError of a failed test → { ok: false, title, hint } */
export function connectionTestError(error) {
  if (error?.status === 0) {
    return { ok: false, title: "Can't reach CPI Log Lens.", hint: 'Check that the server is running, then try again.' };
  }
  const data = error?.data;
  // 502: the CPI side failed; the server says what and what to check.
  if (data?.message) return { ok: false, title: data.message, hint: data.hint || '' };
  if (error?.status === 422 && Array.isArray(data?.detail)) {
    const problems = data.detail.map(e => {
      const field = FIELDS[(e.loc || []).at(-1)] || (e.loc || []).at(-1);
      if (e.type === 'string_pattern_mismatch' && /url/i.test(field)) return `${field} must start with https:// and contain no spaces.`;
      if (e.type === 'string_too_short' || e.type === 'missing') return `${field} is required.`;
      return `${field}: ${e.msg}.`;
    });
    return { ok: false, title: 'Check the connection details.', hint: problems.join(' ') };
  }
  if (typeof data?.detail === 'string') return { ok: false, title: data.detail, hint: '' };
  return { ok: false, title: "The connection test didn't work.", hint: error?.message || String(error) };
}

/** Fields a test needs, by their labels; [] when it can run. A saved tenant may keep its secret. */
export function missingForTest(form, editing) {
  const missing = ['api_url', 'oauth_url', 'client_id'].filter(f => !String(form[f] || '').trim());
  if (!editing && !form.client_secret) missing.push('client_secret');
  return missing.map(f => FIELDS[f]);
}
