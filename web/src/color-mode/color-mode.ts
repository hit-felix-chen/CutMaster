export const colorModes = ['dark', 'light', 'system'] as const
export type ColorMode = (typeof colorModes)[number]
export type ResolvedColorMode = Exclude<ColorMode, 'system'>

export const colorModeMediaQuery = '(prefers-color-scheme: dark)'

export function isColorMode(value: string | null): value is ColorMode {
  return value === 'dark' || value === 'light' || value === 'system'
}

export function resolveColorMode(
  mode: ColorMode,
  systemIsDark: boolean,
): ResolvedColorMode {
  return mode === 'system' ? (systemIsDark ? 'dark' : 'light') : mode
}

export function applyColorMode(mode: ColorMode, resolved: ResolvedColorMode) {
  const root = document.documentElement
  root.dataset.colorModePreference = mode
  root.dataset.colorMode = resolved
  root.style.colorScheme = resolved
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', resolved === 'dark' ? '#101318' : '#f4f6f8')
}
