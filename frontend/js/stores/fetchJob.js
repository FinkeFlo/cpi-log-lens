// State of the fetch job, updated live from the server-sent events of /api/fetch/stream.
// Shared by the navbar badge, the progress banner and the Fetch page.
import { api } from '../api.js';
import { LOGS_CHANGED, emit } from '../events.js';

export default {
  jobId: null,
  status: 'idle', // idle | running | done | error | cancelled
  statusMsg: '',
  statusIcon: '',
  done: 0,
  total: 0,
  currentFile: '',
  currentTenant: '',
  imported: 0,
  errorMsg: '',
  _source: null, // active EventSource

  /** Start a fetch; body: { tenants, log_types, hours }. */
  async start(body) {
    if (this.status === 'running') return;
    Object.assign(this, {
      status: 'running', statusMsg: 'Connecting…', statusIcon: '', done: 0, total: 0,
      currentFile: '', imported: 0, errorMsg: '',
    });
    try {
      const data = await api.fetch.start(body);
      this.follow(data.job_id);
    } catch (e) {
      this.status = 'error';
      this.errorMsg = e.message;
    }
  },

  /** Show a job started elsewhere (e.g. the demo import) and listen to its events. */
  follow(jobId) {
    this.status = 'running';
    this.jobId = jobId;
    this.openStream();
  },

  /** Ask the running job to stop at the next file. */
  async cancel() {
    if (this.status !== 'running') return;
    this.setStatus('Cancelling…');
    try {
      await api.fetch.cancel();
    } catch (e) {
      console.error('cancelFetch:', e);
    }
  },

  /** On page load: pick up a job that is still running. */
  async reconnect() {
    try {
      const data = await api.fetch.status();
      if (data.status === 'idle') return;
      this.applySnapshot(data);
      if (data.status === 'running') this.openStream();
    } catch {
      // Server not reachable: the pages show that themselves; there is no job to show.
    }
  },

  applySnapshot(s) {
    this.jobId = s.job_id;
    this.status = s.status;
    this.setStatus(s.status_msg);
    this.done = s.done;
    this.total = s.total;
    this.currentFile = s.current_file;
    this.currentTenant = s.current_tenant;
    this.imported = s.imported;
    this.errorMsg = s.error_msg;
  },

  setStatus(message) {
    const keyPrefix = `${String.fromCodePoint(0x1f511)} `;
    this.statusIcon = message.startsWith(keyPrefix) ? 'key-round' : '';
    this.statusMsg = this.statusIcon ? message.slice(keyPrefix.length) : message;
  },

  /** Open/reopen the SSE stream for the active job. */
  openStream() {
    this.closeStream();
    const es = api.fetch.stream();
    this._source = es;
    es.onmessage = e => this.handle(JSON.parse(e.data));
    // SSE closed (server done or network hiccup) — stop listening
    es.onerror = () => this.closeStream();
  },

  closeStream() {
    if (this._source) {
      this._source.close();
      this._source = null;
    }
  },

  handle(ev) {
    switch (ev.type) {
      case 'snapshot':
        this.applySnapshot(ev);
        break;
      case 'status':
        this.setStatus(ev.msg);
        break;
      case 'files_found':
        this.total = ev.count;
        this.currentTenant = ev.tenant;
        this.setStatus(`${ev.tenant} · ${ev.log_type}: ${ev.count} files found`);
        break;
      case 'progress':
        this.done = ev.done;
        this.total = ev.total;
        this.currentFile = ev.file;
        this.imported = ev.imported;
        // Refresh the Browse list when new rows were imported
        if (ev.new_rows > 0) emit(LOGS_CHANGED, { firstPage: false, visibleOnly: true });
        break;
      case 'warn':
        console.warn(ev.msg);
        break;
      case 'error':
        this.status = 'error';
        this.errorMsg = ev.msg;
        this.closeStream();
        break;
      case 'done':
        this.status = 'done';
        this.imported = ev.imported;
        this.setStatus('Completed');
        this.closeStream();
        emit(LOGS_CHANGED, { firstPage: true, visibleOnly: true });
        break;
      case 'cancelled':
        this.status = 'cancelled';
        this.imported = ev.imported;
        this.setStatus('Cancelled');
        this.closeStream();
        emit(LOGS_CHANGED, { firstPage: true, visibleOnly: true });
        break;
    }
  },
};
