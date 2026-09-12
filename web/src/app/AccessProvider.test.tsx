import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'

import { AccessProvider } from '@/app/AccessProvider'
import { AccessContext } from '@/app/access-context'
import { ApplicationGate } from '@/app/ApplicationGate'
import { EventStreamContext } from '@/app/providers/event-stream-context'
import {
  WriteButton,
  WriteInput,
  WriteSelect,
  WriteTextarea,
  WriteForm,
} from '@/components/ui/WriteControls'
import { ProjectsLanding } from '@/features/projects/ProjectsLanding'
import i18n from '@/i18n'

afterEach(() => vi.unstubAllGlobals())

function client() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } })
}

it.each([
  [false, true],
  [true, true],
  [false, false],
])(
  'projects and sidebar reflect can_write=%s, configured=%s',
  async (canWrite, configured) => {
    await i18n.changeLanguage('zh-CN')
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string | URL | Request) => {
        const url = String(input)
        const value =
          url === '/api/access'
            ? { can_write: canWrite }
            : url === '/api/health'
              ? { configured: { llm: configured, vlm: configured, asr: configured } }
              : url.includes('migrations/current')
                ? { migration: null }
                : { items: [] }
        return new Response(JSON.stringify(value), { status: 200 })
      }),
    )
    const router = createMemoryRouter([
      {
        path: '/',
        element: <ApplicationGate />,
        children: [{ index: true, element: <ProjectsLanding /> }],
      },
    ])
    render(
      <QueryClientProvider client={client()}>
        <AccessProvider>
          <EventStreamContext
            value={{
              connectionState: 'connected',
              isConnected: true,
              pollingFallback: false,
            }}
          >
            <RouterProvider router={router} />
          </EventStreamContext>
        </AccessProvider>
      </QueryClientProvider>,
    )
    const sync = await screen.findByText('实时同步')
    const newProject = screen.queryByRole('button', { name: i18n.t('projects.new') })
    if (canWrite) {
      expect(newProject).toBeVisible()
      expect(screen.queryByText('只读模式')).not.toBeInTheDocument()
    } else {
      expect(newProject).not.toBeInTheDocument()
      const readOnly = screen.getByText('只读模式')
      expect(readOnly.parentElement).toBe(sync.parentElement)
      expect(readOnly.parentElement).toHaveClass('rail-status-row')
    }
    expect(screen.getByRole('textbox')).toBeEnabled()
  },
)

it('does not mount pages while permissions fail to load', async () => {
  await i18n.changeLanguage('en-US')
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => new Response('{}', { status: 503 })),
  )
  render(
    <QueryClientProvider client={client()}>
      <AccessProvider>
        <button>Write sentinel</button>
      </AccessProvider>
    </QueryClientProvider>,
  )
  expect(await screen.findByRole('button', { name: 'Try again' })).toBeVisible()
  expect(screen.queryByText('Write sentinel')).not.toBeInTheDocument()
})

it('disables editing and form submission without disabling read controls', () => {
  const submit = vi.fn()
  render(
    <AccessContext value={false}>
      <WriteForm aria-label="editor" onSubmit={submit}>
        <WriteInput aria-label="name" />
        <WriteSelect aria-label="material">
          <option>A</option>
        </WriteSelect>
        <WriteTextarea aria-label="brief" />
        <WriteButton>Save</WriteButton>
      </WriteForm>
      <button>Play</button>
    </AccessContext>,
  )
  expect(screen.getByLabelText('name')).toBeDisabled()
  expect(screen.getByLabelText('material')).toBeDisabled()
  expect(screen.getByLabelText('brief')).toBeDisabled()
  expect(screen.queryByText('Save')).not.toBeInTheDocument()
  expect(screen.getByText('Play')).toBeEnabled()
  fireEvent.submit(screen.getByRole('form'))
  expect(submit).not.toHaveBeenCalled()
})
