// The calls to the service. The token is kept in this browser (localStorage, so that closing the tab does not lose it) and sent with
// every call; the service asks for it only where something is changed (or everywhere on a network).
const TOKEN_KEY = 'tts-suite-token'
// Not `Authorization`: behind a reverse proxy with its own login that header belongs to the proxy.
const TOKEN_HEADER = 'X-TTS-Suite-Token'

export const token = {
  get: () => localStorage.getItem(TOKEN_KEY) ?? '',
  set: (value) => (value ? localStorage.setItem(TOKEN_KEY, value) : localStorage.removeItem(TOKEN_KEY)),
}

export class ApiError extends Error {
  constructor(status, code, detail) {
    super(detail)
    this.status = status
    this.code = code
  }
}

function headers(json) {
  const result = {}
  if (json) result['Content-Type'] = 'application/json'
  if (token.get()) result[TOKEN_HEADER] = token.get()
  return result
}

export async function fetchChecked(path, { method = 'GET', body, signal } = {}) {
  const response = await fetch(path, {
    method,
    headers: headers(body !== undefined),
    body: body === undefined ? undefined : JSON.stringify(body),
    signal,
  })
  if (!response.ok) {
    let data = {}
    try { data = await response.json() } catch { /* an answer that is no JSON has no code */ }
    throw new ApiError(response.status, data.error ?? `http${response.status}`, data.detail ?? response.statusText)
  }
  return response
}

export async function json(path, options) {
  const response = await fetchChecked(path, options)
  return response.status === 204 ? null : response.json()
}

const query = (parameters) => {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(parameters)) if (value !== null && value !== undefined) search.set(key, value)
  const text = search.toString()
  return text ? `?${text}` : ''
}

export const service = {
  engines: () => json('../engines'),
  config: () => json('../config'),
  saveConfig: (config) => json('../config', { method: 'POST', body: config }),
  speakTest: () => json('../speak-test'),
  saveSpeakTest: (state) => json('../speak-test', { method: 'PUT', body: state }),
  start: (engine, host) => json(`../engines/${encodeURIComponent(engine)}/start${query({ host })}`, { method: 'POST' }),
  stop: (engine, host) => json(`../engines/${encodeURIComponent(engine)}/stop${query({ host })}`, { method: 'POST' }),
  release: (targets) => json('../release', { method: 'POST', body: { targets } }),
  voices: (engine) => json(`../voices${query({ engine })}`),
  pushVoices: () => json('../voices/push', { method: 'POST' }),
  syncVoices: (engine) => json(`../voices/sync${query({ engine })}`, { method: 'POST' }),
  speak: (head, signal) => fetchChecked('../speak', { method: 'POST', body: head, signal }),
}
