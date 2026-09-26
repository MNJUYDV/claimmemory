export const money = (n) => '$' + Math.round(n ?? 0).toLocaleString('en-US')
const D = (s) => new Date(s + (s && s.length === 10 ? 'T00:00:00Z' : ''))
export const days = (a, b) => Math.round((D(b) - D(a)) / 86400000)
export const fmtDate = (s) => D(s).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' })
export const parseScore = (s) => {
  if (s == null) return { a: 0, b: 0, pct: 0, text: '—' }
  const [a, b] = typeof s === 'object' ? [s.caught, s.expected] : String(s).split('/').map(Number)
  return { a, b, pct: b ? (a / b) * 100 : 0, text: `${a}/${b}` }
}
