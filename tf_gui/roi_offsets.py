"""Pixel translation for inference ROIs in the rotated, full-resolution image."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


DEFAULT_ROI_OFFSETS_PATH = Path(__file__).resolve().parent / "config" / "roi_offsets.json"
OVERRIDE_ROI_OFFSETS_PATH = Path.home() / ".config" / "tf_inner" / "roi_offsets.json"


@dataclass(frozen=True)
class RoiOffsets:
    offset_x_px: int = 0
    offset_y_px: int = 0

    def __post_init__(self) -> None:
        for name in ("offset_x_px", "offset_y_px"):
            if type(getattr(self, name)) is not int:
                raise ValueError(f"{name} must be an integer number of pixels")

    def apply(self, roi: Tuple[int, int, int, int]) -> Tuple[int, int, int, int]:
        x, y, width, height = roi
        return x + self.offset_x_px, y + self.offset_y_px, width, height


def load_roi_offsets(
    default_path: Path = DEFAULT_ROI_OFFSETS_PATH,
    override_path: Optional[Path] = OVERRIDE_ROI_OFFSETS_PATH,
) -> RoiOffsets:
    """Read once on classifier initialization; never silently ignore a bad file.

    A machine-local file replaces the bundled configuration. A broken symlink
    also counts as an override, so an unreadable local setting cannot silently
    switch the inspection to a different crop position.
    """
    override = Path(override_path) if override_path is not None else None
    use_override = override is not None and (override.exists() or override.is_symlink())
    source = override if use_override else Path(default_path)
    try:
        data = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("configuration must be a JSON object")
        if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
            raise ValueError("schema_version must be 1")
        for name in ("offset_x_px", "offset_y_px"):
            if name not in data:
                raise ValueError(f"missing required field: {name}")
        offsets = RoiOffsets(data["offset_x_px"], data["offset_y_px"])
    except (ValueError, UnicodeError) as error:
        raise ValueError(f"Invalid ROI offset configuration {source}: {error}") from error
    print(
        f"Inspection ROI offsets: x={offsets.offset_x_px:+d}px "
        f"y={offsets.offset_y_px:+d}px (rotated image), config={source}",
        flush=True,
    )
    return offsets
