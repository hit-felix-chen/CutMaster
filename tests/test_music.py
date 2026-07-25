import numpy as np

from cutmaster.music.analysis import _duration_range, _normalize, _section_boundaries


def test_music_energy_helpers() -> None:
    normalized = _normalize(np.array([0.0, 1.0, 2.0]))
    assert normalized[0] == 0.0
    assert normalized[-1] == 1.0
    assert _duration_range(0.9)[1] < _duration_range(0.1)[0]
    boundaries = _section_boundaries(
        np.array([0.1] * 10 + [0.9] * 10 + [0.2] * 10),
        0.5,
        15.0,
    )
    assert boundaries[0] == 0.0
    assert boundaries[-1] == 15.0
    assert boundaries == sorted(boundaries)
