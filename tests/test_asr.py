from cutmaster.analyser.asr import _to_srt
from cutmaster.configuration.schema import ASRConfig


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

