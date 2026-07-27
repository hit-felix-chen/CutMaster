"""Music analysis operations."""

from cutmaster.music.analysis import (
    analyze_music,
    compact_music_profile,
    write_music_profile,
)
from cutmaster.music.beats import detect_beats

__all__ = [
    "analyze_music",
    "compact_music_profile",
    "detect_beats",
    "write_music_profile",
]
