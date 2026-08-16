import '@testing-library/jest-dom/vitest'

import { afterEach } from 'vitest'

afterEach(() => {
  if (typeof window.localStorage.clear === 'function') {
    window.localStorage.clear()
  }
})
