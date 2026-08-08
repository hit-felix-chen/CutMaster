from cutmaster.runtime.artifact_layout import ArtifactLayout


def test_artifact_layout_creates_three_stage_directories(tmp_path) -> None:
    layout = ArtifactLayout.create(tmp_path / "cutmaster")

    assert layout.analyser_dir.is_dir()
    assert layout.planners_dir.is_dir()
    assert layout.planning_diagnostics_dir.is_dir()
    assert layout.renderer_dir.is_dir()
    assert layout.analysis_result == layout.analyser_dir / "analysis_result.json"
    assert layout.render_plan == layout.planners_dir / "render_plan.json"
    assert layout.output_video == layout.renderer_dir / "output.mp4"
