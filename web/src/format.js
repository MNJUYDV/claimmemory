export const money = (n) => '$' + Math.round(n ?? 0).toLocaleString('en-US')
const D = (s) => new Date(s + (s.length === 10 ? 'T00:00:00Z' : ''))
export const days = (a, b) => Math.round((D(b) - D(a)) / 86400000)
export const fmtDate = (s) => D(s).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' })
export const parseScore = (s) => { const [a, b] = String(s).split('/').map(Number); return { a, b, pct: b ? (a / b) * 100 : 0 } }
