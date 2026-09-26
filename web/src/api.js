import { USE_SAMPLE_DATA, API_BASE } from './config.js'
import sampleWorkspace from './sample/workspace.json'
import sampleReplay from './sample/replay.json'
import sampleRulebook from './sample/rulebook.json'

async function get(path) {
  const res = await fetch(API_BASE + path)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
  return res.json()
}

export const fetchWorkspace = (id) => (USE_SAMPLE_DATA ? Promise.resolve(sampleWorkspace) : get(`/api/claims/${id}/workspace`))
export const fetchRulebook = (id) => (USE_SAMPLE_DATA ? Promise.resolve(sampleRulebook) : get(`/api/claims/${id}/rulebook`))
export const fetchReplay = (decisionId) => (USE_SAMPLE_DATA ? Promise.resolve(sampleReplay) : get(`/api/decisions/${decisionId}/replay`))

export async function uploadDocument(id, file) {
  const body = new FormData()
  body.append('file', file)
  const res = await fetch(`${API_BASE}/api/claims/${id}/documents`, { method: 'POST', body })
  if (!res.ok) throw new Error(`Upload failed: ${res.status}`)
  return res.json().catch(() => ({}))
}
