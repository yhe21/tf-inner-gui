"""Export trained classifiers to Raspberry Pi-friendly NCNN folders."""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from common import ROOT, TARGETS, trained_weights


def export(target: str) -> bool:
    weights = trained_weights(target)
    if not weights.exists():
        print(f"SKIP {target.upper()}: weights not found: {weights}")
        return False

    from ultralytics import YOLO

    config = TARGETS[target]
    print(f"\nExporting {target.upper()} to NCNN")
    exported = Path(
        YOLO(str(weights)).export(
            format="ncnn",
            imgsz=config["imgsz"],
            batch=1,
            device="cpu",
        )
    )
    # Ultralytics detects the backend from this required directory suffix.
    destination = ROOT / "models" / f"{target}_cls_ncnn_model"
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        shutil.rmtree(destination)
    shutil.copytree(exported, destination)
    print(f"Raspberry Pi model: {destination}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("all", *TARGETS), default="all")
    args = parser.parse_args()
    targets = TARGETS if args.target == "all" else (args.target,)
    results = [export(target) for target in targets]
    return 0 if all(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
