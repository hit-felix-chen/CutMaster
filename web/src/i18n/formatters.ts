export function formatDuration(seconds: number, locale: string): string {
  const rounded = Math.max(0, Math.round(seconds))
  const hours = Math.floor(rounded / 3600)
  const minutes = Math.floor((rounded % 3600) / 60)
  const remainder = rounded % 60
  return new Intl.DurationFormat(locale, { style: 'narrow' }).format({
    hours,
    minutes,
    seconds: remainder,
  })
}

export function formatBytes(value: number, locale: string): string {
  if (!Number.isFinite(value) || value < 0) return '—'
  if (value === 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB'] as const
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1)
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(
    value / 1024 ** index,
  )} ${units[index]}`
}

export function formatFrameRate(value: number, locale: string): string {
  return `${new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(value)} FPS`
}

export function formatNumber(value: number, locale: string): string {
  return new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(value)
}

export function formatDateTime(value: string | Date, locale: string): string {
  const date = typeof value === 'string' ? new Date(value) : value
  return new Intl.DateTimeFormat(locale, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(date)
}

export function formatCost(value: number, locale: string): string {
  return new Intl.NumberFormat(locale, {
    style: 'currency',
    currency: 'CNY',
    minimumFractionDigits: 2,
    maximumFractionDigits: 6,
  }).format(value)
}
