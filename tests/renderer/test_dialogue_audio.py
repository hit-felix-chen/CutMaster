from pathlib import Path

from cutmaster.configuration.schema import DialogueAudioConfig
from cutmaster.renderer.dialogue_audio import _anchor_specs, prepare_dialogue_audio


def test_anchor_specs_add_padding_and_preserve_exact_speech_offset() -> None:
    script = [
        {
            "dialogue_anchor": {
                "anchor_id": "line_1-line_2",
                "source_audio_start_sec": 10.0,
                "source_audio_end_sec": 12.0,
            }
        },
        {
            "dialogue_anchor": {
                "anchor_id": "line_3-line_4",
                "source_audio_start_sec": 20.0,
                "source_audio_end_sec": 23.0,
            }
        },
    ]

    specs = _anchor_specs(script, source_duration_sec=30.0, padding_sec=1.0)

    assert specs[0]["padded_start_sec"] == 9.0
    assert specs[0]["padded_duration_sec"] == 4.0
    assert specs[0]["speech_reel_start_sec"] == 1.0
    assert specs[1]["speech_reel_start_sec"] == 5.0
    assert specs[1]["speech_duration_sec"] == 3.0


def test_disabled_vocal_separation_does_not_read_media(tmp_path: Path) -> None:
    script = [{"dialogue_anchor": {"anchor_id": "line_1"}}]

    result, reused = prepare_dialogue_audio(
        tmp_path / "missing.mp4",
        script,
        tmp_path,
        DialogueAudioConfig(enable_vocal_separation=False),
    )

    assert result == script
    assert result is not script
    assert reused is False
