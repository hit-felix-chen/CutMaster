import type { ComponentProps } from 'react'

import { useCanWrite } from '@/app/access-context'

export function WriteButton(props: ComponentProps<'button'>) {
  const canWrite = useCanWrite()
  return canWrite ? <button {...props} /> : null
}

export function WriteInput(props: ComponentProps<'input'>) {
  const canWrite = useCanWrite()
  return <input {...props} disabled={!canWrite || props.disabled} />
}

export function WriteSelect(props: ComponentProps<'select'>) {
  const canWrite = useCanWrite()
  return <select {...props} disabled={!canWrite || props.disabled} />
}

export function WriteTextarea(props: ComponentProps<'textarea'>) {
  const canWrite = useCanWrite()
  return <textarea {...props} disabled={!canWrite || props.disabled} />
}

export function WriteForm({ onSubmit, ...props }: ComponentProps<'form'>) {
  const canWrite = useCanWrite()
  return (
    <form
      {...props}
      onSubmit={(event) => {
        if (!canWrite) {
          event.preventDefault()
          return
        }
        onSubmit?.(event)
      }}
    />
  )
}
