import { useCallback, useEffect, useRef, useState } from 'react'
import { USE_SAMPLE_DATA, API_BASE_URL, CLAIMS } from './config.js'
import { fetchWorkspace, fetchRulebook } from './api.js'

const RULE_EVENTS = ['improvement_started', 'miss_detected', 'rule_proposed', 'candidate_scored', 'rule_promoted', 'rule_rejected', 'no_change']
const REFETCH_EVENTS = ['run_started', 'run_finished', 'finding_created', 'finding_updated', 'finding_resolved', 'totals_changed']
const nice = (t) => String(t).replace(/_/g, ' ')
const score = (s) => (s ? `${s.caughtCount}/${s.expectedCount}` : '?')

function describeRuleEvent(type, ev) {
  switch (type) {
    case 'improvement_started': return `settlement received; scoring the review on rulebook v${ev.rulebookVersion}`
    case 'miss_detected': return `missed ${(ev.scoreBefore?.missed || []).map(nice).join(', ') || 'a finding'} · score ${score(ev.scoreBefore)}`
    case 'rule_proposed': return `proposed a rule for ${nice(ev.proposal?.type)}`
    case 'candidate_scored': return `rulebook v${ev.version} scored ${score(ev.scoreAfter)}`
    case 'rule_promoted': return `rulebook v${ev.version} promoted (was v${ev.previousVersion})`
    case 'rule_rejected': return `candidate rejected: ${ev.reason || ''}`.slice(0, 140)
    default: return 'nothing was missed'
  }
}

export function useWorkspace(claimId) {
  const [workspace, setWorkspace] = useState(null)
  const [rulebook, setRulebook] = useState([])
  const [error, setError] = useState(null)
  const [liveSteps, setLiveSteps] = useState([])
  const [ruleEvents, setRuleEvents] = useState([])  // rulebook learning, from any claim; kept across refetches and claim switches
  const [polling, setPolling] = useState(false)
  const alive = useRef(true)
  const current = useRef(claimId)
  current.current = claimId

  const refetch = useCallback(async () => {
    const id = current.current
    try {
      const [w, r] = await Promise.all([fetchWorkspace(id), fetchRulebook(id)])
      if (!alive.current || id !== current.current) return
      setWorkspace(w); setRulebook(r); setError(null); setLiveSteps([])
    } catch (e) {
      if (alive.current && id === current.current) setError(e.message || 'Request failed')
    }
  }, [])
  const refetchRef = useRef(refetch)
  refetchRef.current = refetch

  // load on claim change
  useEffect(() => {
    alive.current = true
    setWorkspace(null); setLiveSteps([]); setError(null)
    refetch()
    const poll = polling && !USE_SAMPLE_DATA ? setInterval(refetch, 3000) : null
    return () => { alive.current = false; clearInterval(poll) }
  }, [claimId, refetch, polling])

  // live events for every claim, so Park's rulebook learning shows while Maria is open
  useEffect(() => {
    if (USE_SAMPLE_DATA) return undefined
    const sources = []
    for (const c of CLAIMS) {
      let es
      try { es = new EventSource(`${API_BASE_URL}/api/claims/${c.id}/events`) } catch { setPolling(true); continue }
      const on = (type, fn) => es.addEventListener(type, (m) => { let ev = {}; try { ev = JSON.parse(m.data) } catch { /* ignore */ } fn(ev) })
      const mine = () => c.id === current.current
      on('tool_call', (ev) => mine() && setLiveSteps((s) => [...s, { tool: ev.name, summary: ev.summary, at: ev.ts || new Date().toISOString() }]))
      for (const t of REFETCH_EVENTS) on(t, () => mine() && refetchRef.current())
      for (const t of RULE_EVENTS) {
        on(t, (ev) => {
          setRuleEvents((s) => [...s, { tool: t, summary: describeRuleEvent(t, ev), at: ev.ts || new Date().toISOString(), claimId: c.id }])
          refetchRef.current()  // the rulebook is shared by both claims
        })
      }
      es.onerror = () => { es.close(); setPolling(true) }
      sources.push(es)
    }
    return () => sources.forEach((s) => s.close())
  }, [])

  return { workspace, rulebook, error, refetch, liveSteps, ruleEvents }
}
