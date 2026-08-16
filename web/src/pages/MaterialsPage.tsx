import { useParams } from 'react-router-dom'

import { MaterialsWorkspace } from '@/features/materials/MaterialsWorkspace'
import { InvalidMaterialRoutePage } from '@/pages/InvalidMaterialRoutePage'

export function MaterialsPage() {
  const { type } = useParams()
  if (type !== 'video' && type !== 'music') {
    return <InvalidMaterialRoutePage />
  }
  return <MaterialsWorkspace />
}
