// Wording for the three kinds of failed requests, shared by the pages' error states:
// the server can't be reached, the database is busy (HTTP 503), or the request failed.

/** describeError(error, 'log entries') → { kind, icon, title, text, detail } */
export function describeError(error, what = 'data') {
  const kind = error?.kind || 'failed';
  if (kind === 'unreachable') {
    return {
      kind,
      icon: 'server-off',
      title: "Can't reach CPI Log Lens",
      text: 'The server is not responding. Check that it is running, then try again.',
      detail: '',
    };
  }
  if (kind === 'busy') {
    const wait = error.retryAfter ? `${error.retryAfter} seconds` : 'a few seconds';
    return {
      kind,
      icon: 'hourglass',
      title: 'The database is busy',
      text: `It is still working on other requests, for example a large import. Try again in ${wait}; narrower filters make queries faster.`,
      detail: error.message,
    };
  }
  return {
    kind,
    icon: 'circle-x',
    title: `Couldn't load ${what}`,
    text: 'The server answered with an error.',
    detail: error?.message || String(error),
  };
}
