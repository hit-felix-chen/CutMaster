import {
  Component,
  type ErrorInfo,
  type PropsWithChildren,
  type ReactNode,
} from 'react'

import i18n from '@/i18n'

type State = { error: Error | null }

export class AppErrorBoundary extends Component<PropsWithChildren, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error('CutMaster Web render failed.', error, info)
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <main className="fatal-error" role="alert">
          <h1>CutMaster</h1>
          <p>{i18n.t('unexpectedError')}</p>
        </main>
      )
    }
    return this.props.children
  }
}
