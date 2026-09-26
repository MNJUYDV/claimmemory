import { useEffect, useState } from 'react'
import { USE_SAMPLE_DATA, CLAIM_ID } from './config.js'
import { useWorkspace } from './useWorkspace.js'
import { fetchReplay, uploadDocument } from './api.js'
import { money, days, fmtDate, parseScore } from './format.js'

function Login({ onOk }) {
  const [email, setEmail] = useState('')
  const [pw, setPw] = useState('')
  const [err, setErr] = useState('')
  const submit = (e) => {
    e.preventDefault()
    if (email === import.meta.env.VITE_DEMO_USER && pw === import.meta.env.VITE_DEMO_PASSWORD) onOk()
    else setErr('Email or password is incorrect.')
  }
  return (
    <div className="center">
      <form className="card login" onSubmit={submit}>
        <h1>ClaimMemory</h1>
        <label>Email<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoFocus /></label>
        <label>Password<input type="password" value={pw} onChange={(e) => setPw(e.target.value)} /></label>
        {err && <div className="alert small">{err}</div>}
        <button className="btn primary" type="submit">Sign in</button>
      </form>
    </div>
  )
}

function Overlay({ title, onClose, children }) {
  useEffect(() => {
    const k = (e) => e.key === 'Escape' && onClose()
    window.addEventListener('keydown', k)
    return () => window.removeEventListener('keydown', k)
  }, [onClose])
  return (
    <div className="scrim" onClick={onClose}>
      <div className="card overlay" role="dialog" onClick={(e) => e.stopPropagation()}>
        <button className="close" aria-label="Close" onClick={onClose}>×</button>
        <h2>{title}</h2>
        {children}
      </div>
    </div>
  )
}

function Replay({ decisionId, onClose }) {
  const [r, setR] = useState(null)
  const [err, setErr] = useState(null)
  useEffect(() => { fetchReplay(decisionId).then(setR).catch((e) => setErr(e.message)) }, [decisionId])
  const title = r ? `${fmtDate(r.madeAt)} — the day ${r.filename} was written` : 'Replay'
  return (
    <Overlay title={title} onClose={onClose}>
      {err && <div className="alert small">Could not load replay: {err}</div>}
      {!r && !err && <p className="muted">Loading…</p>}
      {r && (
        <>
          <p className="muted label">Documents on file that day</p>
          <ul className="docs">
            {r.cited.map((d) => <li key={d.filename}><span className="mono">{d.filename}</span><span className="muted">received {fmtDate(d.receivedAt)} · cited</span></li>)}
            {r.notCited.map((d) => <li key={d.filename} className="miss"><span className="mono">{d.filename}</span><span>received {fmtDate(d.receivedAt)} · not cited</span></li>)}
          </ul>
          {r.notCited.map((d) => (
            <p key={d.filename} className="sentence"><b className="mono">{d.filename}</b> had been on file for {days(d.receivedAt, r.madeAt)} days when {r.filename} was written, and was not cited.</p>
          ))}
        </>
      )}
    </Overlay>
  )
}

function Bar({ label, score, tone }) {
  const s = parseScore(score)
  return (
    <div className="bar-row">
      <span className="mono">{label}</span>
      <div className="bar"><div className={'fill ' + tone} style={{ width: s.pct + '%' }} /></div>
      <span className="mono">{s.text}</span>
    </div>
  )
}

function bars(rulebook) {
  const asc = [...rulebook].sort((a, b) => a.version - b.version)
  const latest = asc[asc.length - 1]
  const prev = asc[asc.length - 2]
  const old = new Set((prev?.rules || []).map((r) => r.type))
  const added = (latest?.rules || []).filter((r) => !old.has(r.type))
  return { latest, prev, added, before: latest?.provenance.scoreBefore, after: latest?.provenance.scoreAfter }
}

function Score({ rulebook, onClose }) {
  const { latest, added, before, after } = bars(rulebook)
  if (!latest) return <Overlay title="Score & rulebook" onClose={onClose}><p className="muted">No rulebook yet.</p></Overlay>
  const p = latest.provenance
  return (
    <Overlay title="Score & rulebook" onClose={onClose}>
      <Bar label={`v${latest.version - 1}`} score={before} tone="grey" />
      <Bar label={`v${latest.version}`} score={after} tone="teal" />
      <dl className="facts">
        {added.length > 0 && <><dt>Missed</dt><dd>{added.map((r) => r.type.replace(/_/g, ' ')).join(', ')}</dd></>}
        <dt>Rule proposed</dt><dd>{(added.length ? added : latest.rules).map((r) => r.instruction).join(' ')}</dd>
        <dt>Why it was kept</dt><dd>{p.reason} <span className="muted">({p.decision})</span></dd>
      </dl>
      <p className="footer">Score = findings matched against labeled outcomes, checked before and after every rule change.</p>
    </Overlay>
  )
}

const DAY_PX = 6
const DAY_MS = 86400000
const MIN_BAR = 140
const LANE_H = 30
const LABEL_W = 116
const rowName = (r) => r.replace(/_/g, ' ')
const shortDate = (s) => (s ? new Date(s).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }) : 'ongoing')

function layoutTimeline(items) {
  const dated = items.filter((i) => i.validFrom && !Number.isNaN(Date.parse(i.validFrom)))
  if (!dated.length) return null
  const first = new Date(Math.min(...dated.map((i) => Date.parse(i.validFrom))))
  const start = Date.UTC(first.getUTCFullYear(), first.getUTCMonth(), 1)
  const today = Date.now()
  const end = today + 14 * DAY_MS
  const px = (t) => ((t - start) / DAY_MS) * DAY_PX
  const width = Math.ceil(px(end)) + MIN_BAR
  const rows = [...new Set(dated.map((i) => i.row))].map((row) => {
    const bars = dated.filter((i) => i.row === row).sort((a, b) => Date.parse(a.validFrom) - Date.parse(b.validFrom)).map((i) => {
      const left = px(Date.parse(i.validFrom))
      const right = i.validTo ? px(Date.parse(i.validTo)) : px(today)
      return { item: i, left, w: Math.max(right - left, MIN_BAR) }
    })
    const laneEnds = []  // each bar goes in the first lane where it does not overlap
    for (const b of bars) {
      let lane = laneEnds.findIndex((e) => e <= b.left)
      if (lane < 0) { lane = laneEnds.length; laneEnds.push(0) }
      laneEnds[lane] = b.left + b.w
      b.lane = lane
    }
    return { row, bars, lanes: Math.max(laneEnds.length, 1) }
  })
  const ticks = []
  for (let d = new Date(start); d.getTime() <= end; d = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1))) {
    ticks.push({ x: px(d.getTime()), label: d.toLocaleDateString('en-US', { month: 'short', timeZone: 'UTC' }) + (d.getUTCMonth() === 0 ? ' ' + d.getUTCFullYear() : '') })
  }
  return { rows, ticks, width }
}

function Timeline({ items }) {
  const lay = layoutTimeline(items)
  if (!lay) return <div className="card"><h3>Timeline</h3><p className="muted">No dated facts yet</p></div>
  const est = items.filter((i) => i.row.startsWith('estimate') && !i.superseded && /estimate|v\d/i.test(i.label))
  const newest = est.sort((a, b) => Date.parse(b.validFrom) - Date.parse(a.validFrom))[0]
  return (
    <div className="card tl">
      <h3>Timeline</h3>
      <div className="tl-scroll">
        <div style={{ width: LABEL_W + lay.width }}>
          <div className="tl-row">
            <div className="tl-label" />
            <div className="tl-track axis" style={{ width: lay.width, height: 22 }}>
              {lay.ticks.map((t) => <span key={t.x} className="tick" style={{ left: t.x }}>{t.label}</span>)}
            </div>
          </div>
          {lay.rows.map((r) => (
            <div className="tl-row" key={r.row}>
              <div className="tl-label muted">{rowName(r.row)}</div>
              <div className="tl-track" style={{ width: lay.width, height: r.lanes * LANE_H + 4 }}>
                {lay.ticks.map((t) => <i key={t.x} className="gridline" style={{ left: t.x }} />)}
                {r.bars.map((b, k) => {
                  const i = b.item
                  const cls = 'seg' + (i.superseded ? ' sup' : '') + (i === newest ? ' new' : '')
                  const tip = `${i.label}\n${shortDate(i.validFrom)} – ${i.validTo ? shortDate(i.validTo) : 'ongoing'}${i.sourceFilename ? '\nSource: ' + i.sourceFilename : ''}`
                  return <div key={k} className={cls} title={tip} style={{ left: b.left, width: b.w - 2, top: b.lane * LANE_H + 2 }}>{i.label}</div>
                })}
              </div>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

function Activity({ steps }) {
  const [open, setOpen] = useState(false)
  const last = steps[steps.length - 1]
  return (
    <div className="strip">
      <div className="strip-line">
        <span className="dot" />
        {last ? <><span className="mono tool">{last.tool}</span><span className="grow">{last.summary}</span></> : <span className="grow muted">Idle</span>}
        <button className="link" onClick={() => setOpen(!open)}>{open ? 'Hide activity' : 'View activity'}</button>
      </div>
      {open && (
        <ul className="panel">
          {steps.slice(-20).reverse().map((s, i) => (
            <li key={i}><span className="mono tool">{s.tool}</span><span className="grow">{s.summary}</span><span className="muted mono">{new Date(s.at).toLocaleTimeString()}</span></li>
          ))}
        </ul>
      )}
    </div>
  )
}

function Workspace() {
  const { workspace: w, rulebook, error, refetch, liveSteps } = useWorkspace(CLAIM_ID)
  const [replayId, setReplayId] = useState(null)
  const [scoreOpen, setScoreOpen] = useState(false)
  const [uploadMsg, setUploadMsg] = useState('')
  const [showResolved, setShowResolved] = useState(false)

  const onFile = async (e) => {
    const f = e.target.files[0]
    e.target.value = ''
    if (!f) return
    try { setUploadMsg('Uploading…'); await uploadDocument(CLAIM_ID, f); setUploadMsg('Uploaded'); refetch() } catch (err) { setUploadMsg(err.message) }
  }

  const banner = error && (
    <div className="alert banner">Couldn’t reach the server ({error}). <button className="link" onClick={refetch}>Retry</button></div>
  )
  if (!w) return <div className="center">{banner || <p className="muted">Loading…</p>}</div>

  const { header, totals, timeline, findings, latestRun } = w
  const resolved = w.resolvedFindings || []
  const steps = [...(latestRun?.steps || []), ...liveSteps]
  const codeFinding = findings.find((f) => f.decisionId)
  const { latest, added, before, after } = bars(rulebook)

  return (
    <>
      <header className="top">
        <b className="brand">ClaimMemory</b>
        <div className="top-right">
          <span className="mono">{CLAIM_ID}</span><span>{header.insurer}</span><span>Day {header.day}</span>
          <label className={'btn' + (USE_SAMPLE_DATA ? ' disabled' : '')} title={USE_SAMPLE_DATA ? 'Upload is disabled while using sample data' : 'Upload a document'}>
            Upload document
            <input type="file" hidden disabled={USE_SAMPLE_DATA} onChange={onFile} />
          </label>
        </div>
      </header>
      <Activity steps={steps} />
      {banner}
      {uploadMsg && <div className="note">{uploadMsg}</div>}
      <main className="grid">
        <section className="left">
          <div>
            <h1>{header.family} · {header.lossType}</h1>
            <p className="muted paid">Paid <span className="mono">{money(totals.paid)}</span> of <span className="mono">{money(totals.owed)}</span> owed</p>
            <div className="recov-label muted">Recoverable</div>
            <div className="recov mono">{money(totals.recoverable)}</div>
          </div>
          <Timeline items={timeline} />
          <div>
            <h3>Findings</h3>
            {findings.length === 0 && <p className="muted">No findings yet</p>}
            {findings.map((f, i) => <FindingCard key={i} f={f} onOpen={setReplayId} />)}
            {resolved.length > 0 && (
              <div className="resolved">
                <button className="link" onClick={() => setShowResolved(!showResolved)}>
                  {showResolved ? 'Hide' : 'Show'} resolved ({resolved.length})
                </button>
                {showResolved && resolved.map((f, i) => <FindingCard key={i} f={f} done />)}
              </div>
            )}
          </div>
        </section>
        <aside className="right">
          {codeFinding && (
            <div className="card">
              <h3>Replay</h3>
              <p className="muted">{codeFinding.title}</p>
              <button className="link" onClick={() => setReplayId(codeFinding.decisionId)}>Open →</button>
            </div>
          )}
          {latest && (
            <div className="card click" onClick={() => setScoreOpen(true)}>
              <h3>Score & rulebook</h3>
              <Bar label={`v${latest.version - 1}`} score={before} tone="grey" />
              <Bar label={`v${latest.version}`} score={after} tone="teal" />
              {added.length > 0 && <p className="muted small">Missed: {added.map((r) => r.type.replace(/_/g, ' ')).join(', ')}.</p>}
              <p className="muted small">Kept: {latest.provenance.decision}, {latest.provenance.reason.split('.')[0].toLowerCase()}.</p>
              <button className="link" onClick={(e) => { e.stopPropagation(); setScoreOpen(true) }}>Open →</button>
            </div>
          )}
        </aside>
      </main>
      {replayId && <Replay decisionId={replayId} onClose={() => setReplayId(null)} />}
      {scoreOpen && <Score rulebook={rulebook} onClose={() => setScoreOpen(false)} />}
    </>
  )
}

function FindingCard({ f, done, onOpen }) {
  const [open, setOpen] = useState(false)
  const files = {}  // filename -> quotes, so one chip per document; hover shows the quotes
  ;(f.evidence || []).forEach((e) => { (files[e.filename] = files[e.filename] || []).push(e.quote) })
  const clickable = !done && f.decisionId
  return (
    <div className={'card finding' + (done ? ' done' : '') + (clickable ? ' click' : '')} onClick={() => clickable && onOpen(f.decisionId)}>
      <div className="fhead">
        <div className="ftitle">
          <b>{f.title}</b>
          {!done && f.ruleAddedInVersion > 1 && <span className="tag">Caught by a new rule</span>}
        </div>
        <span className="mono amt">{money(f.amount)}</span>
      </div>
      <p className="summary muted">{f.summary}</p>
      <button className="link" onClick={(e) => { e.stopPropagation(); setOpen(!open) }}>{open ? 'Hide evidence' : 'Show evidence'}</button>
      {open && (
        <div className="evidence" onClick={(e) => e.stopPropagation()}>
          <ul className="points">{(f.points || []).map((p, i) => <li key={i}>{p}</li>)}</ul>
          <div className="chips">
            {Object.entries(files).map(([name, quotes]) => (
              <span key={name} className="chip mono" title={quotes.map((q) => `“${q}”`).join('\n\n')}>{name}</span>
            ))}
          </div>
        </div>
      )}
      {done && <div className="muted small">fixed in <span className="mono">{f.resolvedByFilename}</span></div>}
    </div>
  )
}

export default function App() {
  const [authed, setAuthed] = useState(false)
  return authed ? <Workspace /> : <Login onOk={() => setAuthed(true)} />
}
