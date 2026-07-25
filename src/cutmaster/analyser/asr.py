from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests

from cutmaster.configuration.schema import ASRConfig
from cutmaster.runtime.observability import log_event


DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com"
UPLOAD_POLICY_URL = f"{DASHSCOPE_BASE_URL}/api/v1/uploads"
TRANSCRIPTION_URL = f"{DASHSCOPE_BASE_URL}/api/v1/services/audio/asr/transcription"
TASK_URL_TEMPLATE = f"{DASHSCOPE_BASE_URL}/api/v1/tasks/{{task_id}}"
TERMINAL_FAILURES = {"FAILED", "CANCELED", "UNKNOWN"}
PUNCTUATION_BREAKS = set("，。！？；,.!?;")


class ASRError(RuntimeError):
    pass


@dataclass(frozen=True)
class UploadPolicy:
    upload_host: str
    upload_dir: str
    policy: str
    signature: str
    oss_access_key_id: str
    object_acl: str = "private"
    forbid_overwrite: str = "true"
    max_file_size_mb: float | None = None


def _headers(api_key: str, **extra: str) -> dict[str, str]:
    if not api_key.strip():
        raise ASRError("DashScope ASR API key is empty")
    return {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", **extra}


def _response_json(response: requests.Response, action: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        detail = response.text[:500] if response is not None else ""
        raise ASRError(f"{action} failed: {detail or exc}") from exc
    try:
        data = response.json()
    except ValueError as exc:
        raise ASRError(f"{action} returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise ASRError(f"{action} returned a non-object response")
    return data


def extract_asr_audio(video_path: Path, audio_path: Path) -> Path:
    audio_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = audio_path.with_name(f"{audio_path.stem}.tmp{audio_path.suffix}")
    temporary.unlink(missing_ok=True)
    command = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(video_path), "-vn", "-ac", "1", "-ar", "16000",
        "-b:a", "64k", str(temporary),
    ]
    subprocess.run(command, check=True)
    temporary.replace(audio_path)
    return audio_path


def _request_upload_policy(api_key: str, session=requests) -> UploadPolicy:
    response = session.get(
        UPLOAD_POLICY_URL,
        params={"action": "getPolicy", "model": "fun-asr"},
        headers=_headers(api_key),
        timeout=30,
    )
    data = _response_json(response, "Requesting DashScope upload policy").get("data") or {}
    required = ("upload_host", "upload_dir", "policy", "signature", "oss_access_key_id")
    missing = [name for name in required if not data.get(name)]
    if missing:
        raise ASRError(f"DashScope upload policy is missing: {', '.join(missing)}")
    return UploadPolicy(
        upload_host=str(data["upload_host"]),
        upload_dir=str(data["upload_dir"]).rstrip("/"),
        policy=str(data["policy"]),
        signature=str(data["signature"]),
        oss_access_key_id=str(data["oss_access_key_id"]),
        object_acl=str(data.get("x_oss_object_acl") or "private"),
        forbid_overwrite=str(data.get("x_oss_forbid_overwrite") or "true"),
        max_file_size_mb=float(data["max_file_size_mb"]) if data.get("max_file_size_mb") else None,
    )


def _upload_audio(audio_path: Path, policy: UploadPolicy, session=requests) -> str:
    if policy.max_file_size_mb is not None:
        max_bytes = policy.max_file_size_mb * 1024 * 1024
        if audio_path.stat().st_size > max_bytes:
            raise ASRError(f"ASR audio exceeds DashScope upload limit ({policy.max_file_size_mb} MB)")
    safe_name = audio_path.name.replace("/", "_").replace("\\", "_")
    key = f"{policy.upload_dir}/{safe_name}"
    form = {
        "OSSAccessKeyId": policy.oss_access_key_id,
        "policy": policy.policy,
        "Signature": policy.signature,
        "key": key,
        "x-oss-object-acl": policy.object_acl,
        "x-oss-forbid-overwrite": policy.forbid_overwrite,
        "success_action_status": "200",
    }
    with audio_path.open("rb") as handle:
        response = session.post(
            policy.upload_host,
            data=form,
            files={"file": (safe_name, handle)},
            timeout=120,
        )
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise ASRError(f"Uploading audio to DashScope temporary storage failed: {response.text[:500]}") from exc
    return f"oss://{key}"


def _submit(api_key: str, oss_url: str, session=requests) -> str:
    response = session.post(
        TRANSCRIPTION_URL,
        headers=_headers(
            api_key,
            **{"X-DashScope-Async": "enable", "X-DashScope-OssResourceResolve": "enable"},
        ),
        json={
            "model": "fun-asr",
            "input": {"file_urls": [oss_url]},
            "parameters": {"diarization_enabled": True},
        },
        timeout=30,
    )
    data = _response_json(response, "Submitting Fun-ASR task")
    task_id = str((data.get("output") or {}).get("task_id") or "").strip()
    if not task_id:
        raise ASRError("Fun-ASR did not return a task_id")
    return task_id


def _poll(api_key: str, task_id: str, config: ASRConfig, session=requests) -> dict[str, Any]:
    deadline = time.monotonic() + config.timeout_sec
    last_status = "PENDING"
    while time.monotonic() < deadline:
        response = session.post(
            TASK_URL_TEMPLATE.format(task_id=task_id),
            headers=_headers(api_key),
            timeout=30,
        )
        output = _response_json(response, "Polling Fun-ASR task").get("output") or {}
        last_status = str(output.get("task_status") or "").upper()
        if last_status == "SUCCEEDED":
            results = output.get("results") or []
            if not results:
                raise ASRError("Fun-ASR succeeded without a transcription result")
            first = results[0]
            if str(first.get("subtask_status") or "SUCCEEDED").upper() != "SUCCEEDED":
                raise ASRError(f"Fun-ASR subtask failed: {first.get('subtask_status')}")
            return first
        if last_status in TERMINAL_FAILURES:
            raise ASRError(f"Fun-ASR task failed: {last_status}")
        time.sleep(config.poll_interval_sec)
    raise ASRError(f"Fun-ASR timed out; last status: {last_status}")


def _iter_sentences(data: dict[str, Any]) -> Iterable[dict[str, Any]]:
    transcripts = data.get("transcripts")
    if transcripts is None and "sentences" in data:
        transcripts = [{"sentences": data.get("sentences") or []}]
    for transcript in transcripts or []:
        yield from transcript.get("sentences") or []


def _srt_time(milliseconds: float) -> str:
    value = max(0, int(round(float(milliseconds))))
    hours, value = divmod(value, 3_600_000)
    minutes, value = divmod(value, 60_000)
    seconds, ms = divmod(value, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def _split_text(text: str, max_chars: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for character in text:
        current += character
        if character in PUNCTUATION_BREAKS or len(current) >= max_chars:
            chunks.append(current.strip())
            current = ""
    if current.strip():
        chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk]


def _sentence_blocks(sentence: dict[str, Any], config: ASRConfig) -> list[tuple[float, float, str, Any]]:
    words = sentence.get("words") or []
    if words:
        blocks: list[tuple[float, float, str, Any]] = []
        current: list[Any] | None = None
        for word in words:
            text = str(word.get("text") or word.get("word") or "")
            punctuation = str(word.get("punctuation") or "")
            if punctuation and not text.endswith(punctuation):
                text += punctuation
            if not text or word.get("end_time") is None:
                continue
            start = float(word.get("begin_time", word.get("start_time", 0)))
            end = float(word["end_time"])
            speaker = word.get("speaker_id", sentence.get("speaker_id"))
            should_split = current is not None and (
                speaker != current[3]
                or len(str(current[2]) + text) > config.max_chars
                or end - float(current[0]) > config.max_subtitle_duration_sec * 1000
            )
            if should_split:
                blocks.append((float(current[0]), float(current[1]), str(current[2]), current[3]))
                current = None
            if current is None:
                current = [start, end, text, speaker]
            else:
                current[1] = end
                current[2] = str(current[2]) + text
            if text[-1:] in PUNCTUATION_BREAKS:
                blocks.append((float(current[0]), float(current[1]), str(current[2]), current[3]))
                current = None
        if current is not None:
            blocks.append((float(current[0]), float(current[1]), str(current[2]), current[3]))
        if blocks:
            return blocks

    text = str(sentence.get("text") or "").strip()
    if not text:
        return []
    start = float(sentence.get("begin_time", 0))
    end = float(sentence.get("end_time", start + 500))
    chunks = _split_text(text, config.max_chars)
    total_chars = max(1, sum(len(chunk) for chunk in chunks))
    cursor = start
    blocks = []
    for index, chunk in enumerate(chunks):
        chunk_end = end if index == len(chunks) - 1 else cursor + (end - start) * len(chunk) / total_chars
        blocks.append((cursor, max(cursor + 200, chunk_end), chunk, sentence.get("speaker_id")))
        cursor = chunk_end
    return blocks


def _to_srt(data: dict[str, Any], config: ASRConfig) -> str:
    blocks = [block for sentence in _iter_sentences(data) for block in _sentence_blocks(sentence, config)]
    if not blocks:
        raise ASRError("Fun-ASR response contains no usable subtitle blocks")
    output: list[str] = []
    for index, (start, end, text, speaker) in enumerate(blocks, start=1):
        prefix = f"Speaker {int(speaker) + 1}: " if isinstance(speaker, (int, float)) else ""
        output.append(f"{index}\n{_srt_time(start)} --> {_srt_time(end)}\n{prefix}{text.strip()}\n")
    return "\n".join(output).rstrip() + "\n"


def transcribe_bailian(audio_path: Path, subtitle_path: Path, config: ASRConfig) -> Path:
    policy = _request_upload_policy(config.api_key)
    oss_url = _upload_audio(audio_path, policy)
    task_id = _submit(config.api_key, oss_url)
    result = _poll(config.api_key, task_id, config)
    transcription_url = str(result.get("transcription_url") or "").strip()
    if not transcription_url:
        raise ASRError("Fun-ASR result is missing transcription_url")
    response = requests.get(transcription_url, timeout=60)
    data = _response_json(response, "Downloading Fun-ASR result")
    subtitle_path.parent.mkdir(parents=True, exist_ok=True)
    subtitle_path.write_text(_to_srt(data, config), encoding="utf-8")
    return subtitle_path


def prepare_subtitles(
    video_path: Path,
    output_dir: Path,
    config: ASRConfig,
    provided_subtitle: Path | None = None,
) -> Path:
    subtitle_path = output_dir / "source.srt"
    if provided_subtitle is not None:
        if not provided_subtitle.is_file():
            raise FileNotFoundError(f"Subtitle file not found: {provided_subtitle}")
        if provided_subtitle.resolve() != subtitle_path.resolve():
            shutil.copy2(provided_subtitle, subtitle_path)
        return subtitle_path
    if config.reuse and subtitle_path.is_file() and subtitle_path.stat().st_size > 0:
        log_event(
            "INFO",
            "asr",
            "cache.hit",
            "Reusable ASR subtitle found",
            artifact="source_subtitle",
            path=subtitle_path,
        )
        return subtitle_path
    if config.backend != "bailian":
        raise ValueError(f"Unsupported ASR backend in CutMaster core: {config.backend}")
    audio_path = output_dir / "source_audio.m4a"
    if not (config.reuse and audio_path.is_file() and audio_path.stat().st_size > 0):
        stage_started = time.monotonic()
        log_event(
            "INFO",
            "asr",
            "stage.start",
            "ASR audio extraction started",
            stage="audio_extraction",
            sample_rate_hz=16000,
            channels=1,
        )
        extract_asr_audio(video_path, audio_path)
        log_event(
            "INFO",
            "asr",
            "stage.complete",
            "ASR audio extraction completed",
            stage="audio_extraction",
            path=audio_path,
            elapsed_sec=time.monotonic() - stage_started,
        )
    stage_started = time.monotonic()
    log_event(
        "INFO",
        "asr",
        "stage.start",
        "ASR transcription started",
        stage="transcription",
        backend=config.backend,
    )
    result = transcribe_bailian(audio_path, subtitle_path, config)
    log_event(
        "INFO",
        "asr",
        "stage.complete",
        "ASR transcription completed",
        stage="transcription",
        backend=config.backend,
        elapsed_sec=time.monotonic() - stage_started,
        path=result,
    )
    return result
