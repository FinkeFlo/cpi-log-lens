// Short messages in the corner: $store.toast.notify(msg, 'info' | 'success' | 'error').
export default {
  show: false,
  msg: '',
  type: 'info',
  _timer: null,

  notify(msg, type = 'info') {
    this.msg = msg;
    this.type = type;
    this.show = true;
    clearTimeout(this._timer);
    this._timer = setTimeout(() => { this.show = false; }, 3500);
  },
};
