import { useCallback, useEffect, useRef, useState } from 'react'
import { USE_SAMPLE_DATA, API_BASE } from './config.js'
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
        es = new EventSource(`${API_BASE}/api/claims/${claimId}/events`)
        es.onmessage = (m) => {
          let ev = {}
          try { ev = JSON.parse(m.data) } catch { /* non-JSON keepalive */ }
          const t = String(ev.type || '')
          if (ev.tool && ev.summary) setLiveSteps((s) => [...s, { tool: ev.tool, summary: ev.summary, at: ev.at || new Date().toISOString() }])
          if (/finding|totals|run/.test(t)) refetch()
        }
        es.onerror = () => { es.close(); startPolling() }
      } catch { startPolling() }
    }
    return () => { alive.current = false; es?.close(); clearInterval(poll) }
  }, [claimId, refetch])

  return { workspace, rulebook, error, refetch, liveSteps }
}
