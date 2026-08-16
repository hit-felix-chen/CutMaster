"""Release hook that packages the prebuilt CutMaster SPA without source copies."""

from __future__ import annotations

from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    """Map ``web/dist`` into wheel resources and preserve it in sdists."""

    def initialize(self, version: str, build_data: dict[str, object]) -> None:
        # Editable installs map authored Python and let Vite serve the client.
        # They remain installable before the first Web build; release artifacts
        # are the boundary that requires embedded, validated SPA assets.
        if version == "editable":
            return
        project_root = Path(self.root).resolve()
        distribution = project_root / "web" / "dist"
        _validate_distribution(distribution)
        force_include = build_data.setdefault("force_include", {})
        if not isinstance(force_include, dict):
            raise TypeError("Hatch force_include build data must be a mapping")
        target = (
            "cutmaster/adapters/web/static"
            if self.target_name == "wheel"
            else "web/dist"
        )
        force_include[str(distribution)] = target


def _validate_distribution(distribution: Path) -> None:
    required = (
        distribution / "index.html",
        distribution / ".vite" / "manifest.json",
    )
    missing = [str(path) for path in required if not path.is_file()]
    assets = distribution / "assets"
    if not assets.is_dir() or not any(
        path.is_file() and path.suffix in {".js", ".css"}
        for path in assets.iterdir()
    ):
        missing.append(str(assets / "<hashed .js/.css asset>"))
    if missing:
        raise RuntimeError(
            "CutMaster Web assets are missing. Run `npm --prefix web ci` then "
            "`npm --prefix web run build` before packaging. Missing: "
            + ", ".join(missing)
        )


__all__ = ["CustomBuildHook"]
