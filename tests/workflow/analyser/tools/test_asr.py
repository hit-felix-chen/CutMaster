from pathlib import Path

import pytest

from cutmaster.configuration.schema import ASRConfig
from cutmaster.workflow.analyser.tools import asr as asr_module
from cutmaster.workflow.analyser.tools.asr import UploadPolicy, _to_srt


def test_fun_asr_words_are_split_into_srt() -> None:
    response = {
        "transcripts": [
            {
                "sentences": [
                    {
                        "speaker_id": 0,
                        "words": [
                            {"text": "Hello", "begin_time": 0, "end_time": 500, "speaker_id": 0},
                            {"text": " world", "punctuation": ".", "begin_time": 500, "end_time": 1000, "speaker_id": 0},
                        ],
                    }
                ]
            }
        ]
    }
    subtitle = _to_srt(response, ASRConfig(backend="bailian", api_key="test"))
    assert "00:00:00,000 --> 00:00:01,000" in subtitle
    assert "Speaker 1: Hello world." in subtitle


def test_fun_asr_uses_one_overall_deadline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "source_audio.m4a"
    audio_path.write_bytes(b"audio")
    subtitle_path = tmp_path / "source.srt"
    observed_deadlines: list[tuple[str, float]] = []
    monotonic_values = iter([100.0, 108.0, 108.0])
    monkeypatch.setattr(asr_module.time, "monotonic", lambda: next(monotonic_values))

    policy = UploadPolicy(
        upload_host="https://upload.example.test",
        upload_dir="dir",
        policy="policy",
        signature="signature",
        oss_access_key_id="key",
        object_acl="private",
        forbid_overwrite="true",
        max_file_size_mb=None,
    )

    def request_policy(api_key: str, *, deadline: float):
        assert api_key == "test-key"
        observed_deadlines.append(("policy", deadline))
        return policy

    def upload_audio(path: Path, value: UploadPolicy, *, deadline: float):
        assert path == audio_path
        assert value == policy
        observed_deadlines.append(("upload", deadline))
        return "oss://audio"

    def submit(api_key: str, oss_url: str, *, deadline: float):
        assert api_key == "test-key"
        assert oss_url == "oss://audio"
        observed_deadlines.append(("submit", deadline))
        return "task-id"

    def poll(
        api_key: str,
        task_id: str,
        config: ASRConfig,
        *,
        deadline: float,
        cancellation_token=None,
    ):
        assert api_key == "test-key"
        assert task_id == "task-id"
        assert config.timeout_sec == 10.0
        assert cancellation_token is None
        observed_deadlines.append(("poll", deadline))
        return {"transcription_url": "https://result.example.test"}

    class Response:
        def json(self):
            return {
                "transcripts": [
                    {
                        "sentences": [
                            {
                                "speaker_id": 0,
                                "words": [
                                    {
                                        "text": "Hello",
                                        "begin_time": 0,
                                        "end_time": 1000,
                                        "speaker_id": 0,
                                    }
                                ],
                            }
                        ]
                    }
                ]
            }

        def raise_for_status(self):
            return None

    download_timeouts: list[float] = []

    def download(url: str, *, timeout: float):
        assert url == "https://result.example.test"
        download_timeouts.append(timeout)
        return Response()

    monkeypatch.setattr(asr_module, "_request_upload_policy", request_policy)
    monkeypatch.setattr(asr_module, "_upload_audio", upload_audio)
    monkeypatch.setattr(asr_module, "_submit", submit)
    monkeypatch.setattr(asr_module, "_poll", poll)
    monkeypatch.setattr(asr_module.requests, "get", download)

    result = asr_module.transcribe_bailian(
        audio_path,
        subtitle_path,
        ASRConfig(backend="bailian", api_key="test-key", timeout_sec=10.0),
    )

    assert result == subtitle_path
    assert observed_deadlines == [
        ("policy", 110.0),
        ("upload", 110.0),
        ("submit", 110.0),
        ("poll", 110.0),
    ]
    assert download_timeouts == [2.0]


def test_fun_asr_upload_uses_remaining_overall_timeout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    audio_path = tmp_path / "source_audio.m4a"
    audio_path.write_bytes(b"audio")
    monkeypatch.setattr(asr_module.time, "monotonic", lambda: 25.0)
    observed_timeouts: list[float] = []

    class Response:
        text = ""

        def raise_for_status(self):
            return None

    class Session:
        def post(self, _url, **kwargs):
            observed_timeouts.append(kwargs["timeout"])
            return Response()

    policy = UploadPolicy(
        upload_host="https://upload.example.test",
        upload_dir="dir",
        policy="policy",
        signature="signature",
        oss_access_key_id="key",
        object_acl="private",
        forbid_overwrite="true",
        max_file_size_mb=None,
    )

    assert asr_module._upload_audio(
        audio_path,
        policy,
        deadline=100.0,
        session=Session(),
    ) == "oss://dir/source_audio.m4a"
    assert observed_timeouts == [75.0]


def test_prepare_subtitles_shares_deadline_with_audio_extraction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    video_path = tmp_path / "source.mp4"
    video_path.write_bytes(b"video")
    output_dir = tmp_path / "analysis"
    output_dir.mkdir()
    observed_deadlines: list[tuple[str, float]] = []
    monkeypatch.setattr(asr_module.time, "monotonic", lambda: 100.0)

    def extract(
        source: Path,
        target: Path,
        cancellation_token=None,
        *,
        deadline: float,
    ):
        assert source == video_path
        assert cancellation_token is None
        observed_deadlines.append(("extract", deadline))
        target.write_bytes(b"audio")
        return target

    def transcribe(
        audio: Path,
        subtitle: Path,
        config: ASRConfig,
        cancellation_token=None,
        *,
        deadline: float,
    ):
        assert audio == output_dir / "source_audio.m4a"
        assert config.timeout_sec == 10.0
        assert cancellation_token is None
        observed_deadlines.append(("transcribe", deadline))
        subtitle.write_text("subtitle", encoding="utf-8")
        return subtitle

    monkeypatch.setattr(asr_module, "extract_asr_audio", extract)
    monkeypatch.setattr(asr_module, "transcribe_bailian", transcribe)

    result = asr_module.prepare_subtitles(
        video_path,
        output_dir,
        ASRConfig(backend="bailian", api_key="test-key", timeout_sec=10.0),
    )

    assert result == output_dir / "source.srt"
    assert observed_deadlines == [("extract", 110.0), ("transcribe", 110.0)]
