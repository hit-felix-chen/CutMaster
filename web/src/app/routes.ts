export const appPaths = {
  root: '/',
  setup: '/setup',
  projects: '/projects',
  materials: '/materials/video',
  activity: '/activity',
  settings: '/settings',
} as const

export const appRoutes = {
  projectOverview: (projectId: string) => `/projects/${projectId}/overview`,
  projectSetup: (projectId: string) => `/projects/${projectId}/overview`,
  projectMaterials: (projectId: string) => `/projects/${projectId}/materials`,
  creativeBrief: (projectId: string) => `/projects/${projectId}/brief`,
  projectRuns: (projectId: string) => `/projects/${projectId}/runs`,
  runDetail: (projectId: string, runId: string) =>
    `/projects/${projectId}/runs/${runId}`,
  review: (projectId: string, runId: string, editId: string) =>
    `/projects/${projectId}/runs/${runId}/review/${editId}`,
  projectOutputs: (projectId: string) => `/projects/${projectId}/outputs`,
  material: (type: 'video' | 'music', materialId: string) =>
    `/materials/${type}/${materialId}`,
  materialMemory: (type: 'video' | 'music', materialId: string, tab: string) =>
    `/materials/${type}/${materialId}/memory/${tab}`,
} as const
