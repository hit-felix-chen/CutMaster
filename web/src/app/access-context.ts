import { createContext, useContext } from 'react'

// Standalone components default to local mode. The application always mounts
// AccessProvider, which resolves server permissions before mounting any pages.
export const AccessContext = createContext(true)

export function useCanWrite() {
  return useContext(AccessContext)
}
