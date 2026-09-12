import { WriteForm, WriteInput, WriteButton } from '@/components/ui/WriteControls'
import { AlertTriangle, CheckCircle2, LoaderCircle, UploadCloud, X } from 'lucide-react'
import { type FormEvent, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'

import {
  ApiError,
  api,
  type MaterialPreflightResult,
  type MaterialSummary,
  type MaterialSubmission,
  type MaterialType,
} from '@/features/shared/api'
import { problemMessage } from '@/i18n/problem-messages'

interface MaterialImportDialogProps {
  initialType: MaterialType
  onClose: () => void
  onCreated: (submission: MaterialSubmission) => void
  onViewExisting: (material: MaterialSummary) => void
}

function basename(filename: string) {
  const index = filename.lastIndexOf('.')
  return index > 0 ? filename.slice(0, index) : filename
}

export function MaterialImportDialog({
  initialType,
  onClose,
  onCreated,
  onViewExisting,
}: MaterialImportDialogProps) {
  const { t } = useTranslation('common')
  const dialogRef = useRef<HTMLDivElement>(null)
  const nameRef = useRef<HTMLInputElement>(null)
  const [materialType, setMaterialType] = useState(initialType)
  const [name, setName] = useState('')
  const [source, setSource] = useState<File | null>(null)
  const [subtitle, setSubtitle] = useState<File | null>(null)
  const [nameCheck, setNameCheck] = useState<MaterialPreflightResult | null>(null)
  const [checking, setChecking] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const checkName = async () => {
    const candidate = name.trim()
    if (!candidate) {
      setNameCheck(null)
      return null
    }
    setChecking(true)
    setError(null)
    try {
      const result = await api.materials.preflight(materialType, candidate)
      setNameCheck(result)
      return result
    } catch (caught) {
      setNameCheck(null)
      setError(
        caught instanceof ApiError
          ? problemMessage(t, caught.problem)
          : t('materials.nameCheckFailed'),
      )
      return null
    } finally {
      setChecking(false)
    }
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    if (!name.trim() || !source) return
    if (subtitle && !subtitle.name.toLocaleLowerCase().endsWith('.srt')) {
      setError(t('materials.subtitleMustBeSrt'))
      return
    }

    const result = await checkName()
    if (!result || !result.available) return

    setUploading(true)
    try {
      const submission = await api.materials.create({
        materialType,
        name: result.normalized_name,
        source,
        subtitle,
      })
      onCreated(submission)
    } catch (caught) {
      if (
        caught instanceof ApiError &&
        caught.problem?.code === 'material_name_collision'
      ) {
        setNameCheck({
          available: false,
          normalized_name: name.trim(),
        })
      }
      setError(
        caught instanceof ApiError
          ? problemMessage(t, caught.problem)
          : t('materials.importFailed'),
      )
    } finally {
      setUploading(false)
    }
  }

  useEffect(() => {
    const previous = document.activeElement
    dialogRef.current?.focus()
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !uploading) onClose()
    }
    window.addEventListener('keydown', onKeyDown)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      if (previous instanceof HTMLElement) previous.focus()
    }
  }, [onClose, uploading])

  const nameUnavailable = nameCheck?.available === false
  const submitDisabled =
    uploading || checking || !name.trim() || source === null || nameUnavailable

  return (
    <div className="dialog-layer material-import-layer" role="presentation">
      <div
        ref={dialogRef}
        className="dialog material-import-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="material-import-title"
        tabIndex={-1}
      >
        <header>
          <div>
            <span className="eyebrow">M · Material Analyst</span>
            <h2 id="material-import-title">{t('materials.import')}</h2>
          </div>
          <button
            className="icon-button"
            type="button"
            onClick={onClose}
            disabled={uploading}
            aria-label={t('common.close')}
          >
            <X size={18} aria-hidden="true" />
          </button>
        </header>

        <WriteForm noValidate onSubmit={(event) => void submit(event)}>
          <fieldset className="material-import-types" disabled={uploading}>
            <legend>{t('materials.materialType')}</legend>
            {(['video', 'music'] as const).map((value) => (
              <label key={value}>
                <WriteInput
                  type="radio"
                  name="material-type"
                  value={value}
                  checked={materialType === value}
                  onChange={() => {
                    setMaterialType(value)
                    setNameCheck(null)
                    if (value === 'music') setSubtitle(null)
                  }}
                />
                <span>{t(value === 'video' ? 'nav.video' : 'nav.music')}</span>
              </label>
            ))}
          </fieldset>

          <label className="field" htmlFor="material-import-name">
            <span>{t('materials.materialName')}</span>
            <WriteInput
              ref={nameRef}
              id="material-import-name"
              aria-label={t('materials.materialName')}
              value={name}
              disabled={uploading}
              required
              autoComplete="off"
              onChange={(event) => {
                setName(event.target.value)
                setNameCheck(null)
              }}
              onBlur={() => void checkName()}
            />
            <small>{t('materials.nameUniqueWithinType')}</small>
          </label>

          <label className="field material-file-field" htmlFor="material-import-source">
            <span>{t('materials.sourceFile')}</span>
            <WriteInput
              id="material-import-source"
              aria-label={t('materials.sourceFile')}
              type="file"
              required
              disabled={uploading}
              accept={materialType === 'video' ? 'video/*' : 'audio/*'}
              onChange={(event) => {
                const file = event.target.files?.[0] ?? null
                setSource(file)
                if (file && !name.trim()) {
                  setName(basename(file.name))
                  setNameCheck(null)
                }
              }}
            />
          </label>

          {materialType === 'video' ? (
            <label
              className="field material-file-field"
              htmlFor="material-import-subtitle"
            >
              <span>{t('materials.subtitleFile')}</span>
              <WriteInput
                id="material-import-subtitle"
                aria-label={t('materials.subtitleFile')}
                type="file"
                disabled={uploading}
                accept=".srt,application/x-subrip,text/plain"
                onChange={(event) => setSubtitle(event.target.files?.[0] ?? null)}
              />
              <small>{t('materials.subtitleOptional')}</small>
            </label>
          ) : null}

          <div className="material-import-feedback" aria-live="polite">
            {checking ? (
              <p>
                <LoaderCircle className="spin" size={16} aria-hidden="true" />
                {t('materials.checkingName')}
              </p>
            ) : null}
            {nameCheck?.available ? (
              <p className="material-import-feedback--success">
                <CheckCircle2 size={16} aria-hidden="true" />
                {t('materials.nameAvailable')}
              </p>
            ) : null}
            {nameUnavailable ? (
              <div className="material-import-collision">
                <p className="material-import-feedback--danger">
                  <AlertTriangle size={16} aria-hidden="true" />
                  {t('materials.nameCollision', {
                    name: nameCheck.normalized_name,
                  })}
                </p>
                <div>
                  <button
                    className="button button--secondary"
                    type="button"
                    onClick={() => {
                      nameRef.current?.focus()
                      nameRef.current?.select()
                    }}
                  >
                    {t('materials.changeName')}
                  </button>
                  {nameCheck.existing_material ? (
                    <button
                      className="button button--secondary"
                      type="button"
                      onClick={() => onViewExisting(nameCheck.existing_material!)}
                    >
                      {t('materials.viewExisting')}
                    </button>
                  ) : null}
                </div>
              </div>
            ) : null}
            {error ? (
              <p className="material-import-feedback--danger" role="alert">
                <AlertTriangle size={16} aria-hidden="true" />
                {error}
              </p>
            ) : null}
            {uploading ? (
              <p>
                <LoaderCircle className="spin" size={16} aria-hidden="true" />
                {t('materials.uploading')}
              </p>
            ) : null}
          </div>

          <footer>
            <button
              className="button button--secondary"
              type="button"
              disabled={uploading}
              onClick={onClose}
            >
              {t('common.cancel')}
            </button>
            <WriteButton
              className="button button--primary"
              type="submit"
              disabled={submitDisabled}
            >
              {uploading ? (
                <LoaderCircle className="spin" size={16} aria-hidden="true" />
              ) : (
                <UploadCloud size={16} aria-hidden="true" />
              )}
              {uploading ? t('materials.uploading') : t('materials.importAndAnalyse')}
            </WriteButton>
          </footer>
        </WriteForm>
      </div>
    </div>
  )
}
