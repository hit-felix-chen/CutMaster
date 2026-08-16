export type ProblemDetails = {
  code?: string
  title?: string
  detail?: string
  parameters?: Record<string, string | number | boolean>
}

export function problemTranslationKey(problem: ProblemDetails): string | null {
  return problem.code ? `problems:${problem.code}` : null
}
