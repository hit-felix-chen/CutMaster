import { appRoutes } from '@/app/routes'
import type { ActivityNavigation } from '@/features/shared/api'

export function activityNavigationPath(navigation: ActivityNavigation): string {
  switch (navigation.type) {
    case 'material':
      return appRoutes.material(navigation.material_type, navigation.material_id)
    case 'run':
      return appRoutes.runDetail(navigation.project_id, navigation.run_id)
    case 'render_variant': {
      const search = new URLSearchParams({
        variant: navigation.render_variant_id,
      })
      return `${appRoutes.review(
        navigation.project_id,
        navigation.run_id,
        navigation.edit_id,
      )}?${search.toString()}`
    }
  }
}
