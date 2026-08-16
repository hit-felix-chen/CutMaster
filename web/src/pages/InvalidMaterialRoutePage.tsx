import { Navigate, useLocation } from 'react-router-dom'

export function InvalidMaterialRoutePage() {
  const location = useLocation()
  return <Navigate to={`/materials/video${location.search}`} replace />
}
