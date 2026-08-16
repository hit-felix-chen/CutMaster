from cutmaster.infrastructure.models.json_codec import parse_json_object


def test_parse_fenced_json() -> None:
    result = parse_json_object('prefix\n```json\n{"items": [{"_id": 1}]}\n```')
    assert result["items"][0]["_id"] == 1


def test_remove_trailing_comma() -> None:
    result = parse_json_object('{"items": [1, 2,],}')
    assert result == {"items": [1, 2]}
