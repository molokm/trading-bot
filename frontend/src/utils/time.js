// Trade-time helpers. All trade timestamps are shown in Moscow time (UTC+3).
//
// The backend emits timezone-aware UTC ISO strings ("...+00:00"), which
// `new Date()` parses as an instant. Legacy rows (saved with naive UTC) have
// no offset — JS would otherwise treat them as local time and shift them by
// the UTC offset, so we normalize naive strings to UTC before parsing.

const MSK_TZ = 'Europe/Moscow'

export function parseTradeTs(ts) {
  if (ts === null || ts === undefined || ts === '') return null
  if (typeof ts === 'number') {
    const d = new Date(ts)
    return isNaN(d.getTime()) ? null : d
  }
  let s = String(ts).trim()
  if (/^\d{10,13}$/.test(s)) {
    const d = new Date(Number(s))
    return isNaN(d.getTime()) ? null : d
  }
  if (!/([zZ]|[+-]\d{2}:?\d{2})$/.test(s)) {
    s = s.replace(' ', 'T') + 'Z'
  }
  const d = new Date(s)
  return isNaN(d.getTime()) ? null : d
}

export function fmtTs(ts, locale = 'ru-RU') {
  const d = parseTradeTs(ts)
  if (!d) return '---'
  return d.toLocaleString(locale, {
    day: '2-digit',
    month: '2-digit',
    year: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    timeZone: MSK_TZ,
  })
}
