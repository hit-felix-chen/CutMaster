import json
import threading
import time

from cutmaster.analyser.dialogue import candidate_passages, parse_srt, postprocess_dialogues
from cutmaster.configuration.schema import LLMConfig

SRT = """58
00:02:29,270 --> 00:02:32,550
Speaker 1: 导致对方的门将老伦威廉斯在出球的时候

59
00:02:32,550 --> 00:02:33,350
Speaker 1: 出现了失误。

60
00:02:35,000 --> 00:02:36,000
Speaker 2: 这是另一句话。

62
00:02:42,190 --> 00:02:42,830
Speaker 1: 在此前呢，

63
00:02:42,910 --> 00:02:46,390
Speaker 1: 他曾经带领着墨西哥国家队参加过20

64
00:02:46,390 --> 00:02:49,430
Speaker 1: 02年世界杯以及2010年世界杯，

65
00:02:49,950 --> 00:02:52,910
Speaker 1: 两次都是止步于1/8决赛当中。
"""


def test_candidate_passages_preserve_fragment_sequences() -> None:
    passages = candidate_passages(parse_srt(SRT))
    assert [[cue.cue_id for cue in passage] for passage in passages] == [[58, 59], [62, 63, 64, 65]]


def test_llm_merge_groups_create_sentences_with_anchors(tmp_path) -> None:
    source = tmp_path / "source.srt"
    source.write_text(SRT, encoding="utf-8")
    responses = iter(
        [
            json.dumps(
                {
                    "merge_candidate_ids": [
                        "candidate_001",
                        "candidate_002",
                    ]
                },
                ensure_ascii=False,
            )
        ]
    )

    def fake_generator(_prompt, _config, _system_prompt):
        return next(responses)

    merged_srt, dialogue_json = postprocess_dialogues(
        source,
        tmp_path,
        LLMConfig(model="test", base_url="", api_key="test"),
        generator=fake_generator,
    )
    document = json.loads(dialogue_json.read_text(encoding="utf-8"))

    first = document["sentences"][0]
    assert first["start"] == "00:02:29,270"
    assert first["end"] == "00:02:33,350"
    assert first["text"] == "导致对方的门将老伦威廉斯在出球的时候出现了失误。"
    assert [anchor["cue_id"] for anchor in first["anchors"]] == [58, 59]

    third = document["sentences"][2]
    assert third["start"] == "00:02:42,190"
    assert third["end"] == "00:02:52,910"
    assert third["text"] == (
        "在此前呢，他曾经带领着墨西哥国家队参加过2002年世界杯以及2010年世界杯，"
        "两次都是止步于1/8决赛当中。"
    )
    assert [anchor["cue_id"] for anchor in third["anchors"]] == [62, 63, 64, 65]
    assert document["statistics"] == {
        "source_cue_count": 7,
        "sentence_count": 3,
        "merged_sentence_count": 2,
    }
    assert "00:02:29,270 --> 00:02:33,350" in merged_srt.read_text(encoding="utf-8")


def test_dialogue_batches_run_in_parallel_and_preserve_cue_order(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source.srt"
    source.write_text(SRT, encoding="utf-8")
    lock = threading.Lock()
    active = 0
    max_active = 0

    def two_chunks(passages):
        return [[passages[0]], [passages[1]]]

    def delayed_generator(_prompt, _config, _system_prompt):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        return json.dumps({"merge_candidate_ids": ["candidate_001"]})

    monkeypatch.setattr("cutmaster.analyser.dialogue._chunk_passages", two_chunks)
    _, dialogue_json = postprocess_dialogues(
        source,
        tmp_path,
        LLMConfig(model="test", base_url="", api_key="test", max_concurrency=2),
        generator=delayed_generator,
    )
    document = json.loads(dialogue_json.read_text(encoding="utf-8"))

    assert max_active == 2
    assert document["sentences"][0]["anchors"][0]["cue_id"] == 58
    assert document["sentences"][2]["anchors"][0]["cue_id"] == 62


def test_invalid_candidate_ids_trigger_contract_retry(tmp_path) -> None:
    source = tmp_path / "source.srt"
    source.write_text(SRT, encoding="utf-8")
    responses = iter(
        [
            {"merge_candidate_ids": ["candidate_001", 82, "not_a_candidate"]},
            {"merge_candidate_ids": ["candidate_001"]},
        ]
    )
    calls = 0

    def fake_generator(prompt, _config, _system_prompt):
        nonlocal calls
        calls += 1
        assert '"candidate_label": "candidate_001"' in prompt
        return json.dumps(next(responses))

    _, dialogue_json = postprocess_dialogues(
        source,
        tmp_path,
        LLMConfig(
            model="test",
            base_url="",
            api_key="test",
            max_retries=1,
        ),
        generator=fake_generator,
    )
    document = json.loads(dialogue_json.read_text(encoding="utf-8"))

    assert document["statistics"]["merged_sentence_count"] == 1
    assert document["merge_operations"][0]["cue_ids"] == [58, 59]
    assert calls == 2


def test_dialogue_postprocessing_retries_invalid_json(tmp_path, monkeypatch) -> None:
    source = tmp_path / "source.srt"
    source.write_text(SRT, encoding="utf-8")
    responses = iter(["not json", '{"merge_candidate_ids": ["candidate_001"]}'])
    call_count = 0

    def fake_generator(_prompt, _config, _system_prompt):
        nonlocal call_count
        call_count += 1
        return next(responses)

    monkeypatch.setattr("cutmaster.runtime.model_gateway.time.sleep", lambda _delay: None)
    _, dialogue_json = postprocess_dialogues(
        source,
        tmp_path,
        LLMConfig(model="test", base_url="", api_key="test", max_retries=1),
        generator=fake_generator,
    )
    document = json.loads(dialogue_json.read_text(encoding="utf-8"))

    assert call_count == 2
    assert document["statistics"]["merged_sentence_count"] == 1
