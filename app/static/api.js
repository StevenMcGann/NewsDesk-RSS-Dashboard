// Calls to the local server. Every request carries the per-launch token from index.html.
export const TOKEN = document.querySelector('meta[name="api-token"]').content;
const WINDOW_ID = crypto.randomUUID();

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

async function request(method, path, body, { raw = false, contentType = null } = {}) {
  const opts = { method, headers: { 'X-Token': TOKEN } };
  if (body !== undefined) {
    if (contentType) {
      opts.body = body;
      opts.headers['Content-Type'] = contentType;
    } else {
      opts.body = JSON.stringify(body);
      opts.headers['Content-Type'] = 'application/json';
    }
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch {
    throw new ApiError('NewsDesk has stopped. Double-click "Start NewsDesk" to open it again.', 0);
  }
  if (raw && res.ok) return res;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new ApiError(data.error || `Request failed (${res.status})`, res.status);
  return data;
}

export const api = {
  get: (path, opts) => request('GET', path, undefined, opts),
  post: (path, body = {}, opts) => request('POST', path, body, opts),
  patch: (path, body) => request('PATCH', path, body),
  put: (path, body) => request('PUT', path, body),
  del: (path) => request('DELETE', path),
  heartbeat: () => request('POST', '/api/heartbeat', { window_id: WINDOW_ID }),
  bye: () => navigator.sendBeacon(`/api/bye?t=${encodeURIComponent(TOKEN)}&w=${encodeURIComponent(WINDOW_ID)}`),
};
