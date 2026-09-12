import { Navigate, RouterProvider, type RouteObject } from 'react-router-dom'

import { ApplicationGate } from '@/app/ApplicationGate'
import { AccessProvider } from '@/app/AccessProvider'
import { createAppRouter } from '@/app/router'
import { ActivityPage } from '@/pages/ActivityPage'
import { InvalidMaterialRoutePage } from '@/pages/InvalidMaterialRoutePage'
import { MaterialsPage } from '@/pages/MaterialsPage'
import { NotFoundPage } from '@/pages/NotFoundPage'
import { ProjectsPage } from '@/pages/ProjectsPage'
import { SettingsPage } from '@/pages/SettingsPage'
import { SetupPage } from '@/pages/SetupPage'
import {
  ProjectLayout,
  ProjectOutputsPage,
  ProjectOverviewPage,
  ProjectRunsPage,
  ReviewPage,
  RunDetailPage,
} from '@/pages/project/ProjectPages'

const materialRoutes: RouteObject[] = [
  { path: 'materials/:type', element: <MaterialsPage /> },
  { path: 'materials/:type/:materialId', element: <MaterialsPage /> },
  {
    path: 'materials/:type/:materialId/memory/:tab',
    element: <MaterialsPage />,
  },
  { path: 'materials/:type/*', element: <InvalidMaterialRoutePage /> },
]

const router = createAppRouter([
  { path: '/setup', element: <SetupPage /> },
  {
    path: '/',
    element: <ApplicationGate />,
    children: [
      { index: true, element: <Navigate to="/projects" replace /> },
      { path: 'projects', element: <ProjectsPage /> },
      {
        path: 'projects/:projectId',
        element: <ProjectLayout />,
        children: [
          { index: true, element: <Navigate to="overview" replace /> },
          { path: 'overview', element: <ProjectOverviewPage /> },
          { path: 'materials', element: <Navigate to="../overview" replace /> },
          { path: 'brief', element: <Navigate to="../overview" replace /> },
          { path: 'runs', element: <ProjectRunsPage /> },
          { path: 'runs/:runId', element: <RunDetailPage /> },
          { path: 'runs/:runId/review/:editId', element: <ReviewPage /> },
          { path: 'outputs', element: <ProjectOutputsPage /> },
        ],
      },
      ...materialRoutes,
      { path: 'activity', element: <ActivityPage /> },
      { path: 'settings', element: <SettingsPage /> },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
])

export function App() {
  return (
    <AccessProvider>
      <RouterProvider router={router} />
    </AccessProvider>
  )
}
