import { useCallback, useEffect, useRef, useState } from 'react'
import { USE_SAMPLE_DATA, API_BASE_URL } from './config.js'
import { fetchWorkspace, fetchRulebook } from './api.js'

export function useWorkspace(claimId) {
  const [workspace, setWorkspace] = useState(null)
  const [rulebook, setRulebook] = useState([])
  const [error, setError] = useState(null)
  const [liveSteps, setLiveSteps] = useState([])
  const alive = useRef(true)

  const refetch = useCallback(async () => {
    try {
      const [w, r] = await Promise.all([fetchWorkspace(claimId), fetchRulebook(claimId)])
      if (!alive.current) return
      setWorkspace(w); setRulebook(r); setError(null); setLiveSteps([])
    } catch (e) {
      if (alive.current) setError(e.message || 'Request failed')
    }
  }, [claimId])

  useEffect(() => {
    alive.current = true
    refetch()
    let es, poll
    if (!USE_SAMPLE_DATA) {
      const startPolling = () => { if (!poll) poll = setInterval(refetch, 3000) }
      try {
        es = new EventSource(`${API_BASE_URL}/api/claims/${claimId}/events`)
        // the server sends named events (event: <type>), which onmessage never sees
        const on = (type, fn) => es.addEventListener(type, (m) => { let ev = {}; try { ev = JSON.parse(m.data) } catch { /* ignore */ } fn(ev) })
        on('tool_call', (ev) => setLiveSteps((s) => [...s, { tool: ev.name, summary: ev.summary, at: ev.ts || new Date().toISOString() }]))
        for (const t of ['run_started', 'run_finished', 'finding_created', 'finding_updated', 'finding_resolved', 'totals_changed']) on(t, refetch)
        es.onerror = () => { es.close(); startPolling() }
      } catch { startPolling() }
    }
    return () => { alive.current = false; es?.close(); clearInterval(poll) }
  }, [claimId, refetch])

  return { workspace, rulebook, error, refetch, liveSteps }
}
