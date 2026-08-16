import json

import pytest

from cutmaster.workflow.analyser.material_analyst import (
    _require_compatible_incomplete_analysis,
)


def test_incomplete_analysis_can_only_resume_with_the_same_spec(tmp_path) -> None:
    analysis_dir = tmp_path / "analysis"
    first = {"schema_version": "2.0", "subtitle": {"path": "first.srt"}}
    changed = {"schema_version": "2.0", "subtitle": {"path": "changed.srt"}}

    _require_compatible_incomplete_analysis(analysis_dir, first)
    _require_compatible_incomplete_analysis(analysis_dir, first)

    assert json.loads(
        (analysis_dir / "analysis_spec.json").read_text(encoding="utf-8")
    ) == first
    with pytest.raises(ValueError, match="different subtitle or analysis config"):
        _require_compatible_incomplete_analysis(analysis_dir, changed)


def test_unidentified_partial_checkpoints_are_not_mixed_into_new_analysis(
    tmp_path,
) -> None:
    analysis_dir = tmp_path / "analysis"
    analysis_dir.mkdir()
    (analysis_dir / "dialogues.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="without a recorded analysis specification"):
        _require_compatible_incomplete_analysis(
            analysis_dir,
            {"schema_version": "2.0"},
        )
