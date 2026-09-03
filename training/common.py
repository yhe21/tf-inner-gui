"""Shared configuration for the TF INNER/GLUE classification workflow."""

from __future__ import annotations

import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Keep Ultralytics settings inside the project instead of the Windows profile.
ULTRALYTICS_CONFIG_ROOT = ROOT / ".ultralytics"
ULTRALYTICS_CONFIG_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("YOLO_CONFIG_DIR", str(ULTRALYTICS_CONFIG_ROOT))

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
CLASS_NAMES = ("OK", "NG")

TARGETS = {
    "inner": {
        "data": ROOT / "datasets" / "inner",
        "imgsz": 320,
        "batch": 32,
    },
    "glue": {
        "data": ROOT / "datasets" / "glue",
        "imgsz": 224,
        "batch": 64,
    },
}


def image_files(folder: Path) -> list[Path]:
    """Return supported image files below a folder in stable order."""
    if not folder.exists():
        return []
    return sorted(
        path
        for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    )


def trained_weights(target: str) -> Path:
    """Return the fixed best-weight location produced by this project."""
    return ROOT / "runs" / "classify" / target / "weights" / "best.pt"
