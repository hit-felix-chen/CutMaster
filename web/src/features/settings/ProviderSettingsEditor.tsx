import { useMutation, useQueryClient } from '@tanstack/react-query'
import {
  CheckCircle2,
  CircleAlert,
  LoaderCircle,
  RotateCcw,
  Save,
  ServerCog,
} from 'lucide-react'
import { useState } from 'react'
import { useTranslation } from 'react-i18next'

import { OperationProblem } from '@/components/ui/OperationProblem'
import {
  api,
  type AsrProviderConfiguration,
  type CredentialStatus,
  type CredentialUpdate,
  type ModelProviderConfiguration,
  type ProviderBundle,
  type ProviderCapability,
  type ProviderProfile,
  type ProviderSettings,
  type SaveProviderSettingsPayload,
  type SavedProviderSettings,
  type SettingsView,
} from '@/features/shared/api'

type CredentialDraft = {
  action: CredentialUpdate['action']
  value: string
}

type CredentialDrafts = Record<ProviderCapability, CredentialDraft>

const CAPABILITIES: ProviderCapability[] = ['llm', 'vlm', 'asr']
const MODEL_CAPABILITIES: Array<'llm' | 'vlm'> = ['llm', 'vlm']

const KEEP_CREDENTIALS: CredentialDrafts = {
  llm: { action: 'keep', value: '' },
  vlm: { action: 'keep', value: '' },
  asr: { action: 'keep', value: '' },
}

const MODEL_NUMBER_FIELDS: Array<{
  key:
    | 'temperature'
    | 'max_tokens'
    | 'timeout_sec'
    | 'max_retries'
    | 'max_concurrency'
    | 'input_price_yuan_per_million_tokens'
    | 'cached_input_price_yuan_per_million_tokens'
    | 'output_price_yuan_per_million_tokens'
  min: number
  step: number
}> = [
  { key: 'temperature', min: 0, step: 0.1 },
  { key: 'max_tokens', min: 1, step: 1 },
  { key: 'timeout_sec', min: 0.1, step: 0.1 },
  { key: 'max_retries', min: 0, step: 1 },
  { key: 'max_concurrency', min: 1, step: 1 },
  { key: 'input_price_yuan_per_million_tokens', min: 0, step: 0.01 },
  { key: 'cached_input_price_yuan_per_million_tokens', min: 0, step: 0.01 },
  { key: 'output_price_yuan_per_million_tokens', min: 0, step: 0.01 },
]

function cloneBundle(value: ProviderBundle): ProviderBundle {
  return JSON.parse(JSON.stringify(value)) as ProviderBundle
}

function cloneCredentialDrafts(value: CredentialDrafts): CredentialDrafts {
  return {
    llm: { ...value.llm },
    vlm: { ...value.vlm },
    asr: { ...value.asr },
  }
}

function statusForProvider(
  initial: ProviderSettings,
  providers: ProviderBundle,
  capability: ProviderCapability,
): CredentialStatus {
  const reference = providers[capability].api_key_env
  const existing = CAPABILITIES.find(
    (candidate) => initial.providers[candidate].api_key_env === reference,
  )
  return existing
    ? initial.credentials[existing]
    : { configured: false, suffix: null, source: 'none', writable: true }
}

function freshCredentials(
  initial: ProviderSettings,
  providers: ProviderBundle,
  setupMode: boolean,
): CredentialDrafts {
  const drafts = cloneCredentialDrafts(KEEP_CREDENTIALS)
  if (!setupMode) return drafts
  CAPABILITIES.forEach((capability) => {
    const status = statusForProvider(initial, providers, capability)
    if (!status.configured && status.writable) {
      drafts[capability] = { action: 'set', value: '' }
    }
  })
  return drafts
}

function credentialPayload(value: CredentialDraft): CredentialUpdate {
  if (value.action === 'set') return { action: 'set', value: value.value }
  return { action: value.action }
}

export function ProviderSettingsEditor({
  settings,
  setupMode = false,
  onSaved,
}: {
  settings: SettingsView
  setupMode?: boolean
  onSaved?: (result: SavedProviderSettings) => void | Promise<void>
}) {
  const { t } = useTranslation('common')
  const queryClient = useQueryClient()
  const initial = settings.connections
  const [profile, setProfile] = useState<ProviderProfile>(initial.profile)
  const [providers, setProviders] = useState<ProviderBundle>(() =>
    cloneBundle(initial.providers),
  )
  const [credentials, setCredentials] = useState<CredentialDrafts>(() =>
    freshCredentials(initial, initial.providers, setupMode),
  )
  const [dirty, setDirty] = useState(false)
  const [validationError, setValidationError] = useState<string | null>(null)
  const [restartRequired, setRestartRequired] = useState(false)
  const [connectionResults, setConnectionResults] = useState<
    Partial<Record<ProviderCapability, number>>
  >({})
  const [credentialResults, setCredentialResults] = useState<
    Partial<Record<ProviderCapability, string>>
  >({})

  const save = useMutation({
    mutationFn: (payload: SaveProviderSettingsPayload) =>
      api.settings.saveProviders(payload),
    onSuccess: async (result) => {
      void queryClient.setQueryData(['settings'], result.settings)
      setProfile(result.settings.connections.profile)
      setProviders(cloneBundle(result.settings.connections.providers))
      setCredentials(
        freshCredentials(
          result.settings.connections,
          result.settings.connections.providers,
          setupMode,
        ),
      )
      setConnectionResults({})
      setCredentialResults(result.credential_results)
      setDirty(false)
      setRestartRequired(result.restart_required)
      await onSaved?.(result)
    },
  })
  const test = useMutation({
    mutationFn: ({
      capability,
      configuration,
      apiKey,
    }: {
      capability: ProviderCapability
      configuration: ModelProviderConfiguration | AsrProviderConfiguration
      apiKey?: string
    }) => api.settings.testProvider(capability, configuration, apiKey),
    onSuccess: (result) => {
      setConnectionResults((current) => ({
        ...current,
        [result.capability]: result.latency_ms,
      }))
    },
  })

  const markChanged = (next: ProviderBundle) => {
    setProviders(next)
    setProfile('custom')
    setConnectionResults({})
    setCredentialResults({})
    setRestartRequired(false)
    setValidationError(null)
    setDirty(true)
  }

  const chooseProfile = (nextProfile: ProviderProfile) => {
    setProfile(nextProfile)
    if (nextProfile !== 'custom') {
      const nextProviders = cloneBundle(initial.presets[nextProfile])
      setProviders(nextProviders)
      setCredentials(freshCredentials(initial, nextProviders, setupMode))
    } else {
      setCredentials(freshCredentials(initial, providers, setupMode))
    }
    setConnectionResults({})
    setCredentialResults({})
    setRestartRequired(false)
    setValidationError(null)
    setDirty(true)
  }

  const updateCredential = (
    capability: ProviderCapability,
    action: CredentialDraft['action'],
    value = '',
  ) => {
    const reference = providers[capability].api_key_env
    setCredentials((current) => {
      const next = cloneCredentialDrafts(current)
      CAPABILITIES.forEach((candidate) => {
        if (providers[candidate].api_key_env === reference) {
          next[candidate] = { action, value }
        }
      })
      return next
    })
    setConnectionResults({})
    setCredentialResults({})
    setRestartRequired(false)
    setDirty(true)
  }

  const statusFor = (capability: ProviderCapability): CredentialStatus => {
    return statusForProvider(initial, providers, capability)
  }

  const validate = () => {
    const fallback = t('settings.validation.invalid')
    return (
      validateProviders(providers, fallback) ??
      validateCredentialDrafts(credentials, fallback)
    )
  }

  const submit = () => {
    const error = validate()
    if (error) {
      setValidationError(error)
      return
    }
    const payload: SaveProviderSettingsPayload = {
      profile,
      ...(profile === 'custom' ? { providers } : {}),
      credentials: {
        llm: credentialPayload(credentials.llm),
        vlm: credentialPayload(credentials.vlm),
        asr: credentialPayload(credentials.asr),
      },
    }
    save.mutate(payload)
  }

  const testCapability = (capability: ProviderCapability) => {
    const error = validate()
    if (error) {
      setValidationError(error)
      return
    }
    const draft = credentials[capability]
    test.mutate({
      capability,
      configuration: providers[capability],
      ...(draft.action === 'set' ? { apiKey: draft.value } : {}),
    })
  }

  const undoChanges = () => {
    setProfile(initial.profile)
    setProviders(cloneBundle(initial.providers))
    setCredentials(freshCredentials(initial, initial.providers, setupMode))
    setConnectionResults({})
    setCredentialResults({})
    setRestartRequired(false)
    setValidationError(null)
    setDirty(false)
    save.reset()
    test.reset()
  }

  return (
    <section
      className={`settings-section settings-section--connections${setupMode ? ' settings-section--setup' : ''}`}
    >
      <header className="settings-connections-header">
        <ServerCog size={18} />
        <div>
          <h2>{t(setupMode ? 'setup.connectionsTitle' : 'settings.connections')}</h2>
          <p>
            {t(
              setupMode
                ? 'setup.connectionsDescription'
                : 'settings.connectionsDescription',
            )}
          </p>
        </div>
        <div className="settings-connections-header__actions">
          <button
            className="button button--secondary"
            type="button"
            disabled={!dirty || save.isPending}
            onClick={undoChanges}
          >
            <RotateCcw size={16} aria-hidden="true" />
            {t('common.undoChanges')}
          </button>
          <button
            className="button button--primary"
            type="button"
            disabled={!dirty || save.isPending}
            onClick={submit}
          >
            {save.isPending ? (
              <LoaderCircle className="spin" size={16} aria-hidden="true" />
            ) : (
              <Save size={16} aria-hidden="true" />
            )}
            {t(setupMode ? 'setup.saveAndCheck' : 'common.save')}
          </button>
        </div>
      </header>

      <fieldset className="provider-profile-picker">
        <legend>{t('settings.profile')}</legend>
        {(['cost_saving', 'simple', 'custom'] as ProviderProfile[]).map((item) => (
          <label key={item} className={profile === item ? 'active' : ''}>
            <input
              type="radio"
              name="provider-profile"
              value={item}
              checked={profile === item}
              onChange={() => chooseProfile(item)}
            />
            <strong>{t(`settings.profiles.${item}.title`)}</strong>
            <small>{t(`settings.profiles.${item}.description`)}</small>
          </label>
        ))}
      </fieldset>

      <div className="provider-editor-grid">
        {MODEL_CAPABILITIES.map((capability) => (
          <ProviderCard
            key={capability}
            capability={capability}
            value={providers[capability]}
            status={statusFor(capability)}
            credential={credentials[capability]}
            connectionLatency={connectionResults[capability]}
            testing={test.isPending && test.variables?.capability === capability}
            testDisabled={credentials[capability].action === 'clear'}
            onChange={(value) => markChanged({ ...providers, [capability]: value })}
            onCredential={(action, value) =>
              updateCredential(capability, action, value)
            }
            onTest={() => testCapability(capability)}
          />
        ))}
        <AsrProviderCard
          value={providers.asr}
          status={statusFor('asr')}
          credential={credentials.asr}
          connectionLatency={connectionResults.asr}
          testing={test.isPending && test.variables?.capability === 'asr'}
          testDisabled={credentials.asr.action === 'clear'}
          onChange={(value) => markChanged({ ...providers, asr: value })}
          onCredential={(action, value) => updateCredential('asr', action, value)}
          onTest={() => testCapability('asr')}
        />
      </div>

      {validationError ? (
        <p className="settings-validation-error" role="alert">
          {validationError}
        </p>
      ) : null}
      {save.error ? <OperationProblem error={save.error} /> : null}
      {test.error ? <OperationProblem error={test.error} /> : null}
      {restartRequired ? (
        <div className="settings-restart-notice" role="status">
          <p>{t('settings.restart')}</p>
          <ul>
            {CAPABILITIES.map((capability) => (
              <li key={capability}>
                {t(`settings.${capability}`)} ·{' '}
                {t(
                  `settings.credentialResults.${credentialResults[capability] ?? 'kept'}`,
                )}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  )
}

function ProviderCard({
  capability,
  value,
  status,
  credential,
  connectionLatency,
  testing,
  testDisabled,
  onChange,
  onCredential,
  onTest,
}: {
  capability: 'llm' | 'vlm'
  value: ModelProviderConfiguration
  status: CredentialStatus
  credential: CredentialDraft
  connectionLatency?: number
  testing: boolean
  testDisabled: boolean
  onChange: (value: ModelProviderConfiguration) => void
  onCredential: (action: CredentialDraft['action'], value?: string) => void
  onTest: () => void
}) {
  const { t } = useTranslation('common')
  return (
    <article className="provider-card">
      <ProviderCardHeader capability={capability} status={status} />
      <div className="settings-field-grid">
        <label>
          <span>{t('settings.fields.model')}</span>
          <input
            required
            value={value.model}
            onChange={(event) => onChange({ ...value, model: event.target.value })}
          />
        </label>
        <label>
          <span>{t('settings.fields.baseUrl')}</span>
          <input
            required
            type="url"
            value={value.base_url}
            onChange={(event) => onChange({ ...value, base_url: event.target.value })}
          />
        </label>
        <label>
          <span>{t('settings.fields.apiKeyEnv')}</span>
          <input
            required
            value={value.api_key_env}
            onChange={(event) =>
              onChange({ ...value, api_key_env: event.target.value })
            }
          />
        </label>
        <label>
          <span>{t('settings.fields.timeout')}</span>
          <input
            required
            min="0.1"
            step="0.1"
            type="number"
            value={value.timeout_sec}
            onChange={(event) =>
              onChange({ ...value, timeout_sec: Number(event.target.value) })
            }
          />
        </label>
      </div>
      <details className="provider-advanced">
        <summary>{t('settings.advanced')}</summary>
        <label className="settings-checkbox">
          <input
            type="checkbox"
            checked={value.enable_thinking}
            onChange={(event) =>
              onChange({ ...value, enable_thinking: event.target.checked })
            }
          />
          {t('settings.fields.enableThinking')}
        </label>
        <div className="settings-field-grid settings-field-grid--advanced">
          {MODEL_NUMBER_FIELDS.map((field) => (
            <label key={field.key}>
              <span>{t(`settings.fields.${field.key}`)}</span>
              <input
                required
                min={field.min}
                step={field.step}
                type="number"
                value={value[field.key]}
                onChange={(event) =>
                  onChange({ ...value, [field.key]: Number(event.target.value) })
                }
              />
            </label>
          ))}
        </div>
      </details>
      <CredentialEditor
        capability={capability}
        status={status}
        value={credential}
        onChange={onCredential}
      />
      <ConnectionTestRow
        latency={connectionLatency}
        pending={testing}
        disabled={testDisabled}
        onTest={onTest}
      />
    </article>
  )
}

function AsrProviderCard({
  value,
  status,
  credential,
  connectionLatency,
  testing,
  testDisabled,
  onChange,
  onCredential,
  onTest,
}: {
  value: AsrProviderConfiguration
  status: CredentialStatus
  credential: CredentialDraft
  connectionLatency?: number
  testing: boolean
  testDisabled: boolean
  onChange: (value: AsrProviderConfiguration) => void
  onCredential: (action: CredentialDraft['action'], value?: string) => void
  onTest: () => void
}) {
  const { t } = useTranslation('common')
  const numberFields: Array<{
    key: 'timeout_sec' | 'poll_interval_sec' | 'max_chars' | 'max_subtitle_duration_sec'
    min: number
    step: number
  }> = [
    { key: 'timeout_sec', min: 0.1, step: 0.1 },
    { key: 'poll_interval_sec', min: 0.1, step: 0.1 },
    { key: 'max_chars', min: 1, step: 1 },
    { key: 'max_subtitle_duration_sec', min: 0.1, step: 0.1 },
  ]
  return (
    <article className="provider-card">
      <ProviderCardHeader capability="asr" status={status} />
      <div className="settings-field-grid">
        <label>
          <span>{t('settings.fields.backend')}</span>
          <select
            value={value.backend}
            onChange={(event) =>
              onChange({ ...value, backend: event.target.value as 'bailian' })
            }
          >
            <option value="bailian">Bailian</option>
          </select>
        </label>
        <label>
          <span>{t('settings.fields.apiKeyEnv')}</span>
          <input
            required
            value={value.api_key_env}
            onChange={(event) =>
              onChange({ ...value, api_key_env: event.target.value })
            }
          />
        </label>
        {numberFields.map((field) => (
          <label key={field.key}>
            <span>{t(`settings.fields.${field.key}`)}</span>
            <input
              required
              min={field.min}
              step={field.step}
              type="number"
              value={value[field.key]}
              onChange={(event) =>
                onChange({ ...value, [field.key]: Number(event.target.value) })
              }
            />
          </label>
        ))}
      </div>
      <label className="settings-checkbox">
        <input
          type="checkbox"
          checked={value.reuse}
          onChange={(event) => onChange({ ...value, reuse: event.target.checked })}
        />
        {t('settings.fields.reuse')}
      </label>
      <CredentialEditor
        capability="asr"
        status={status}
        value={credential}
        onChange={onCredential}
      />
      <ConnectionTestRow
        latency={connectionLatency}
        pending={testing}
        disabled={testDisabled}
        onTest={onTest}
      />
    </article>
  )
}

function ProviderCardHeader({
  capability,
  status,
}: {
  capability: ProviderCapability
  status: CredentialStatus
}) {
  const { t } = useTranslation('common')
  return (
    <header className="provider-card__header">
      <div>
        <span className="eyebrow">{t('settings.provider')}</span>
        <h3>{t(`settings.${capability}`)}</h3>
      </div>
      <span
        className={
          status.configured ? 'provider-status success' : 'provider-status warning'
        }
      >
        {status.configured ? <CheckCircle2 size={15} /> : <CircleAlert size={15} />}
        {t(status.configured ? 'settings.configured' : 'settings.missing')}
        {status.suffix ? ` · ••••${status.suffix}` : ''}
      </span>
    </header>
  )
}

function CredentialEditor({
  capability,
  status,
  value,
  onChange,
}: {
  capability: ProviderCapability
  status: CredentialStatus
  value: CredentialDraft
  onChange: (action: CredentialDraft['action'], value?: string) => void
}) {
  const { t } = useTranslation('common')
  return (
    <div className="credential-editor">
      <div>
        <strong>{t('settings.credential')}</strong>
        <small>
          {t(`settings.credentialSource.${status.source}`)}
          {!status.writable ? ` · ${t('settings.processLocked')}` : ''}
        </small>
      </div>
      {status.writable ? (
        <>
          <label>
            <span className="sr-only">
              {t('settings.credentialAction', { provider: capability.toUpperCase() })}
            </span>
            <select
              aria-label={t('settings.credentialAction', {
                provider: capability.toUpperCase(),
              })}
              value={value.action}
              onChange={(event) =>
                onChange(event.target.value as CredentialDraft['action'])
              }
            >
              <option value="keep">{t('settings.credentialActions.keep')}</option>
              <option value="set">{t('settings.credentialActions.set')}</option>
              <option value="clear">{t('settings.credentialActions.clear')}</option>
            </select>
          </label>
          {value.action === 'set' ? (
            <label className="credential-editor__secret">
              <span>{t('settings.apiKey')}</span>
              <input
                required
                autoComplete="new-password"
                type="password"
                value={value.value}
                onChange={(event) => onChange('set', event.target.value)}
              />
            </label>
          ) : null}
        </>
      ) : null}
    </div>
  )
}

function ConnectionTestRow({
  latency,
  pending,
  disabled,
  onTest,
}: {
  latency?: number
  pending: boolean
  disabled: boolean
  onTest: () => void
}) {
  const { t } = useTranslation('common')
  return (
    <footer className="provider-card__actions">
      <span role="status">
        {latency === undefined
          ? t('settings.notTested')
          : t('settings.connectedLatency', { latency })}
      </span>
      <button
        className="button button--secondary"
        type="button"
        disabled={pending || disabled}
        onClick={onTest}
      >
        {pending ? <LoaderCircle className="spin" size={15} /> : null}
        {t('settings.testConnection')}
      </button>
    </footer>
  )
}

function validateProviders(providers: ProviderBundle, fallback: string): string | null {
  const environmentPattern = /^[A-Za-z_][A-Za-z0-9_]*$/
  for (const capability of MODEL_CAPABILITIES) {
    const provider = providers[capability]
    if (!provider.model.trim() || !validHttpUrl(provider.base_url)) return fallback
    if (!environmentPattern.test(provider.api_key_env)) return fallback
    if (
      provider.timeout_sec <= 0 ||
      provider.max_tokens < 1 ||
      provider.max_retries < 0 ||
      provider.max_concurrency < 1 ||
      provider.temperature < 0 ||
      provider.input_price_yuan_per_million_tokens < 0 ||
      provider.cached_input_price_yuan_per_million_tokens < 0 ||
      provider.output_price_yuan_per_million_tokens < 0
    ) {
      return fallback
    }
  }
  if (
    !environmentPattern.test(providers.asr.api_key_env) ||
    providers.asr.timeout_sec <= 0 ||
    providers.asr.poll_interval_sec <= 0 ||
    providers.asr.max_chars < 1 ||
    providers.asr.max_subtitle_duration_sec <= 0
  ) {
    return fallback
  }
  for (const capability of CAPABILITIES) {
    const values = Object.values(providers[capability])
    if (values.some((value) => typeof value === 'number' && !Number.isFinite(value))) {
      return fallback
    }
  }
  return null
}

function validHttpUrl(value: string): boolean {
  try {
    const url = new URL(value)
    return (
      (url.protocol === 'https:' || url.protocol === 'http:') &&
      Boolean(url.hostname) &&
      !url.username &&
      !url.password &&
      !url.search &&
      !url.hash
    )
  } catch {
    return false
  }
}

function validateCredentialDrafts(
  credentials: CredentialDrafts,
  fallback: string,
): string | null {
  for (const capability of CAPABILITIES) {
    const credential = credentials[capability]
    if (
      credential.action === 'set' &&
      (!credential.value.trim() ||
        credential.value.length > 4096 ||
        /[\0\r\n]/.test(credential.value))
    ) {
      return fallback
    }
  }
  return null
}
