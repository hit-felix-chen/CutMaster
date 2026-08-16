import { createBrowserRouter, type RouteObject } from 'react-router-dom'

export function createAppRouter(routes: RouteObject[]) {
  return createBrowserRouter(routes)
}
