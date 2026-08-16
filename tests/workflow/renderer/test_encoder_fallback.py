from cutmaster.configuration.schema import RendererConfig
from cutmaster.workflow.renderer.ffmpeg import RenderError
from cutmaster.workflow.renderer.renderer import render_montage


def test_auto_encoder_falls_back_to_libx264(tmp_path, monkeypatch) -> None:
    encoders: list[str] = []

    def fake_render_clip(
        _source,
        output,
        _start,
        _frame_count,
        _config,
        encoder,
    ) -> None:
        encoders.append(encoder)
        if encoder == "h264_videotoolbox":
            raise RenderError("hardware unavailable")
        output.write_bytes(b"clip")

    monkeypatch.setattr(
        "cutmaster.workflow.renderer.renderer.check_media_tools",
        lambda: None,
    )
    monkeypatch.setattr(
        "cutmaster.workflow.renderer.renderer.probe_media",
        lambda _path: {"duration": 10.0},
    )
    monkeypatch.setattr(
        "cutmaster.workflow.renderer.renderer.select_encoder",
        lambda _requested: "h264_videotoolbox",
    )
    monkeypatch.setattr(
        "cutmaster.workflow.renderer.renderer.render_clip",
        fake_render_clip,
    )
    monkeypatch.setattr(
        "cutmaster.workflow.renderer.renderer.concatenate_clips",
        lambda _clips, output: output.write_bytes(b"montage"),
    )
    monkeypatch.setattr(
        "cutmaster.workflow.renderer.renderer.mix_bgm",
        lambda _montage, _bgm, output, *_args, **_kwargs: output.write_bytes(
            b"output"
        ),
    )

    render_montage(
        tmp_path / "source.mp4",
        tmp_path / "bgm.wav",
        [
            {
                "timestamp": "00:00:00,000-00:00:01,000",
                "output_frame_range": [0, 30],
            },
            {
                "timestamp": "00:00:01,000-00:00:02,000",
                "output_frame_range": [30, 60],
            },
        ],
        tmp_path,
        RendererConfig(fps=30, encoder="auto"),
        include_dialogue_audio=False,
    )

    assert encoders == ["h264_videotoolbox", "libx264", "libx264"]
