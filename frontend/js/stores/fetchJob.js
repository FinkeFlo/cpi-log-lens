// State of the fetch job, updated live from the server-sent events of /api/fetch/stream.
// Shared by the navbar badge, the progress banner and the Fetch page.
//
// A job fetches one or more parts (one per tenant and log type). A part that fails
// (token or file list) does not end the job; it ends "done" with errors, which the UI
// shows as a partial failure, not as a failed job.
import { api } from '../api.js';
import { LOGS_CHANGED, emit } from '../events.js';

// Warnings and errors kept for display, like the server's status.
const MAX_PROBLEMS = 50;
const FINISHED = ['done', 'failed', 'cancelled'];
// Waits before reconnecting a lost event stream, in seconds.
const RECONNECT_DELAYS = [1, 2, 5, 10];

const count = n => n.toLocaleString('en-US');
const files = n => `${count(n)} ${n === 1 ? 'file' : 'files'}`;
const newEntries = n => `${count(n)} new ${n === 1 ? 'entry' : 'entries'}`;

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
  parts: [], // per tenant and log type: status, files_total, files_done, imported, warnings, error
  problems: [], // latest warnings and per-tenant errors: level, tenant_name, log_type, file, msg
  warnings: 0,
  errors: 0,
  connectionLost: false, // the event stream broke while the job was running; reconnecting
  _source: null, // active EventSource
  _reconnectTimer: null,
  _reconnectAttempt: 0,
  _resyncing: false,

  /** Start a fetch; body: { tenants, log_types, hours }. */
  async start(body) {
    if (this.status === 'running') return;
    this.reset('running');
    this.setStatus('Connecting…');
    try {
      const data = await api.fetch.start(body);
      this.follow(data.job_id);
    } catch (e) {
      this.status = 'error';
      this.errorMsg = e.kind === 'unreachable' ? "Couldn't start the fetch: the server can't be reached." : e.message;
    }
  },

  reset(status = 'idle') {
    this.closeStream();
    Object.assign(this, {
      status, statusMsg: '', statusIcon: '', done: 0, total: 0, currentFile: '', currentTenant: '',
      imported: 0, errorMsg: '', parts: [], problems: [], warnings: 0, errors: 0, connectionLost: false,
    });
  },

  /** Show a job started elsewhere (e.g. the demo import) and listen to its events. */
  follow(jobId) {
    if (jobId !== this.jobId) this.reset('running');
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
      this.setStatus(`Couldn't cancel: ${e.message}`);
    }
  },

  /** On page load, or after the event stream broke: pick up the job from its status. */
  async reconnect() {
    try {
      const data = await api.fetch.status();
      this._reconnectAttempt = 0;
      this.connectionLost = false;
      if (data.status === 'idle') {
        if (this.status === 'running') {
          // The server restarted while the job ran (the job is recorded as interrupted).
          this.status = 'error';
          this.errorMsg = 'The server restarted while the fetch was running; the fetch was interrupted.';
        }
        return;
      }
      const wasRunning = this.status === 'running';
      this.applySnapshot(data);
      if (data.status === 'running') this.openStream();
      else if (wasRunning) emit(LOGS_CHANGED, { firstPage: true, visibleOnly: true });
    } catch {
      // Unreachable: on page load the pages show that themselves; a lost stream retries.
      if (this.status === 'running') this.scheduleReconnect();
    }
  },

  scheduleReconnect() {
    // A single dropped connection is retried quietly; say so once a retry failed.
    this.connectionLost = this._reconnectAttempt > 0;
    clearTimeout(this._reconnectTimer);
    const delay = RECONNECT_DELAYS[Math.min(this._reconnectAttempt, RECONNECT_DELAYS.length - 1)];
    this._reconnectAttempt++;
    this._reconnectTimer = setTimeout(() => this.reconnect(), delay * 1000);
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
    this.parts = s.parts || [];
    this.problems = s.problems || [];
    this.warnings = s.warnings || 0;
    this.errors = s.errors || 0;
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
    // Closed by the server after the last event, or a network problem: if the job was
    // still running, read its status again (with growing waits while unreachable).
    es.onerror = () => {
      this.closeStream();
      if (this.status === 'running') this.scheduleReconnect();
    };
  },

  closeStream() {
    clearTimeout(this._reconnectTimer);
    if (this._source) {
      this._source.close();
      this._source = null;
    }
  },

  addProblem(level, ev) {
    const part = ev.part || {};
    this.problems.push({
      level, tenant: part.tenant || '', tenant_name: part.tenant_name || '', log_type: part.log_type || '',
      file: ev.file || '', msg: ev.msg,
    });
    if (this.problems.length > MAX_PROBLEMS) this.problems.splice(0, this.problems.length - MAX_PROBLEMS);
  },

  /** Part updates that don't fit the known parts mean an announcement was missed. */
  async resyncParts() {
    if (this._resyncing) return;
    this._resyncing = true;
    try {
      const data = await api.fetch.status();
      if (data.job_id === this.jobId) this.parts = data.parts || [];
    } catch {
      // The stream itself reports a lost connection.
    } finally {
      this._resyncing = false;
    }
  },

  handle(ev) {
    if (ev.part) {
      if (ev.part.index < this.parts.length) this.parts[ev.part.index] = ev.part;
      else this.resyncParts();
    }
    if (ev.parts && ev.type !== 'snapshot') this.parts = ev.parts; // the announcement and the final events
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
        this.warnings++;
        this.addProblem('warn', ev);
        break;
      case 'tenant_error':
        this.errors++;
        this.addProblem('error', ev);
        break;
      case 'error': // the whole job failed
        this.status = 'error';
        this.errorMsg = ev.msg;
        this.closeStream();
        break;
      case 'done':
      case 'cancelled':
        this.status = ev.type;
        this.imported = ev.imported;
        if (ev.warnings !== undefined) this.warnings = ev.warnings;
        if (ev.errors !== undefined) this.errors = ev.errors;
        // Same wording as the server's status message
        if (ev.type === 'cancelled') this.setStatus('Cancelled');
        else this.setStatus(this.errors ? 'Completed with errors' : this.warnings ? 'Completed with warnings' : 'Completed');
        this.closeStream();
        emit(LOGS_CHANGED, { firstPage: true, visibleOnly: true });
        break;
    }
  },

  // ── Derived state for the progress displays ──

  get partsFinished() {
    return this.parts.filter(p => FINISHED.includes(p.status)).length;
  },

  get failedParts() {
    return this.parts.filter(p => p.status === 'failed');
  },

  /** The latest file warnings, newest first. */
  get fileWarnings() {
    return this.problems.filter(p => p.level === 'warn').reverse();
  },

  /** Overall progress in percent over all parts, or null while nothing is known yet. */
  get percent() {
    if (!this.parts.length) return null;
    let sum = 0;
    let known = false;
    for (const p of this.parts) {
      if (FINISHED.includes(p.status)) {
        sum += 1;
        known = true;
      } else if (p.status === 'running' && p.files_total) {
        sum += p.files_done / p.files_total;
        known = true;
      }
    }
    return known ? Math.round((sum / this.parts.length) * 100) : null;
  },

  /** How a finished job went: success | up-to-date | warnings | partial | failed | cancelled | error. */
  get outcome() {
    if (this.status === 'error' || this.status === 'cancelled') return this.status;
    if (this.status !== 'done') return '';
    if (this.parts.length && this.failedParts.length === this.parts.length) return 'failed';
    if (this.errors) return 'partial';
    if (this.warnings) return 'warnings';
    return this.imported ? 'success' : 'up-to-date';
  },

  /** A part's status for display: a part of a job that ended is never waiting or running. */
  partStatus(p) {
    return this.status !== 'running' && (p.status === 'pending' || p.status === 'running') ? 'stopped' : p.status;
  },

  /** One line about a part, e.g. "12 of 40 files" or "Failed: …". */
  partText(p) {
    switch (this.partStatus(p)) {
      case 'pending':
        return 'Waiting';
      case 'running':
        if (p.files_total === null) return 'Requesting token and file list…';
        return `${count(p.files_done)} of ${files(p.files_total)}${p.imported ? ` · ${newEntries(p.imported)}` : ''}`;
      case 'done':
        if (!p.files_total) return 'No log files in the time range';
        return p.imported ? `${files(p.files_total)} · ${newEntries(p.imported)}` : `Up to date (${files(p.files_total)})`;
      case 'failed':
        return p.error;
      case 'cancelled':
        return p.files_total ? `Cancelled after ${count(p.files_done)} of ${files(p.files_total)}` : 'Cancelled';
      case 'stopped':
        return 'Not finished';
      default:
        return p.status;
    }
  },

  partIcon(p) {
    const icons = { pending: 'circle-dashed', done: 'circle-check', failed: 'circle-x', cancelled: 'square', stopped: 'square' };
    return icons[this.partStatus(p)] || '';
  },

  problemSource(problem) {
    return [problem.tenant_name, problem.log_type, problem.file].filter(Boolean).join(' · ');
  },
};
