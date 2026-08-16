const DEFAULT_SETUP_RETURN = '/projects'
const LOCAL_ORIGIN = 'http://cutmaster.local'

export function setupReturnTarget(state: unknown): string {
  if (!isRecord(state)) return DEFAULT_SETUP_RETURN
  const candidate = state.returnTo
  if (
    typeof candidate !== 'string' ||
    !candidate.startsWith('/') ||
    candidate.startsWith('//') ||
    candidate.includes('\\')
  ) {
    return DEFAULT_SETUP_RETURN
  }
  try {
    const url = new URL(candidate, LOCAL_ORIGIN)
    if (url.origin !== LOCAL_ORIGIN || url.pathname === '/setup') {
      return DEFAULT_SETUP_RETURN
    }
    return `${url.pathname}${url.search}${url.hash}`
  } catch {
    return DEFAULT_SETUP_RETURN
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}
