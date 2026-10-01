// Display helpers shared by the pages.

export function shortIflow(iflow) {
  if (!iflow) return '';
  // Strip "Camel (NAME) thread #N" wrapper from existing DB data
  const camel = iflow.match(/Camel \(([^)]+)\)/);
  if (camel) return camel[1];
  // Strip "scheduler-NAME_Worker-N" and "12345-NAME_Worker-N"
  const sched = iflow.match(/^(?:scheduler-|\d+-?)(.+?)(?:_Worker.*)?$/);
  if (sched) iflow = sched[1];
  // Show short form: prefer IF_XXXX segment
  const parts = iflow.split('-');
  for (const p of parts) {
    if (p.startsWith('IF_')) return iflow; // show full name once we found IF_
  }
  return iflow.length > 40 ? iflow.slice(-38) + '…' : iflow;
}
