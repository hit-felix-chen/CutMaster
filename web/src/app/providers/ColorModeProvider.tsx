import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type PropsWithChildren,
} from 'react'

import {
  applyColorMode,
  colorModeMediaQuery,
  resolveColorMode,
  type ColorMode,
  type ResolvedColorMode,
} from '@/color-mode/color-mode'
import { readColorModePreference, writeColorModePreference } from '@/color-mode/storage'

type ColorModeContextValue = {
  colorMode: ColorMode
  resolvedColorMode: ResolvedColorMode
  setColorMode: (mode: ColorMode) => void
}

const ColorModeContext = createContext<ColorModeContextValue | null>(null)

function initialColorMode(): ColorMode {
  const bootstrapped = document.documentElement.dataset.colorModePreference
  if (
    bootstrapped === 'dark' ||
    bootstrapped === 'light' ||
    bootstrapped === 'system'
  ) {
    return bootstrapped
  }
  return readColorModePreference() ?? 'dark'
}

export function ColorModeProvider({ children }: PropsWithChildren) {
  const [colorMode, setColorModeState] = useState<ColorMode>(initialColorMode)
  const [systemIsDark, setSystemIsDark] = useState(
    () => window.matchMedia(colorModeMediaQuery).matches,
  )
  const resolvedColorMode = resolveColorMode(colorMode, systemIsDark)

  useEffect(() => {
    const media = window.matchMedia(colorModeMediaQuery)
    const update = (event: MediaQueryListEvent) => setSystemIsDark(event.matches)
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [])

  useEffect(() => {
    applyColorMode(colorMode, resolvedColorMode)
  }, [colorMode, resolvedColorMode])

  const setColorMode = useCallback((mode: ColorMode) => {
    writeColorModePreference(mode)
    setColorModeState(mode)
  }, [])

  const value = useMemo(
    () => ({ colorMode, resolvedColorMode, setColorMode }),
    [colorMode, resolvedColorMode, setColorMode],
  )

  return <ColorModeContext.Provider value={value}>{children}</ColorModeContext.Provider>
}

export function useColorMode(): ColorModeContextValue {
  const context = useContext(ColorModeContext)
  if (!context) {
    throw new Error('useColorMode must be used within ColorModeProvider.')
  }
  return context
}
