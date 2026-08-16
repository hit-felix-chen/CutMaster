import { isColorMode, type ColorMode } from '@/color-mode/color-mode'

const colorModeStorageKey = 'cutmaster.color-mode'

export function readColorModePreference(): ColorMode | null {
  try {
    const stored = window.localStorage.getItem(colorModeStorageKey)
    return isColorMode(stored) ? stored : null
  } catch {
    return null
  }
}

export function writeColorModePreference(mode: ColorMode) {
  try {
    window.localStorage.setItem(colorModeStorageKey, mode)
  } catch {
    // Browser storage can be unavailable; the in-memory preference still applies.
  }
}
