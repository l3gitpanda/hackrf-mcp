// The backend: JSON over fetch, and the live websocket.

export class ApiError extends Error {}

export async function api(path, options = {}) {
  const init = { credentials: 'same-origin', headers: { Accept: 'application/json' }, ...options };
  if (options.body !== undefined) init.headers['Content-Type'] = 'application/json';
  const res = await fetch(path, init);
  if (res.status === 401) {
    location.assign('/login');
    throw new ApiError('signed out');
  }
  let data = {};
  try {
    data = await res.json();
  } catch (e) {
    // not JSON
  }
  if (!res.ok) throw new ApiError(data.error || `${res.status} ${res.statusText}`);
  return data;
}

export const post = (path, body) => api(path, { method: 'POST', body: JSON.stringify(body || {}) });

// The /ws/live stream: calls onMessage(msg) for every message and
// onState('open' | 'closed') when the connection changes; reconnects with a
// growing delay.
export class LiveSocket {
  constructor(onMessage, onState) {
    this.onMessage = onMessage;
    this.onState = onState;
    this.delay = 1000;
    this.closedAt = null;
    this.connect();
  }

  connect() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(`${proto}://${location.host}/ws/live`);
    this.ws = ws;
    ws.onopen = () => {
      this.delay = 1000;
      this.closedAt = null;
      this.onState('open');
    };
    ws.onmessage = (e) => {
      let msg;
      try {
        msg = JSON.parse(e.data);
      } catch (err) {
        return;
      }
      this.onMessage(msg);
    };
    ws.onclose = () => {
      if (this.closedAt == null) this.closedAt = Date.now() / 1000;
      this.onState('closed');
      this.retryAt = Date.now() / 1000 + this.delay / 1000;
      setTimeout(() => this.connect(), this.delay);
      this.delay = Math.min(this.delay * 2, 15000);
    };
  }
}
