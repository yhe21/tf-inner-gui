"""Validate a trained fixed-ROI classification model."""

from __future__ import annotations

import argparse

from common import ROOT, TARGETS, trained_weights


def validate(target: str, device: str) -> bool:
    weights = trained_weights(target)
    if not weights.exists():
        print(f"SKIP {target.upper()}: weights not found: {weights}")
        return False

    from ultralytics import YOLO

    config = TARGETS[target]
    print(f"\nValidating {target.upper()}: {weights}")
    model = YOLO(str(weights))
    model.val(
        data=str(config["data"]),
        split="val",
        imgsz=config["imgsz"],
        batch=config["batch"],
        device=device,
        project=str(ROOT / "runs" / "classify-validation"),
        name=target,
        exist_ok=True,
        plots=True,
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("all", *TARGETS), default="all")
    parser.add_argument("--device", default="0")
    args = parser.parse_args()

    targets = TARGETS if args.target == "all" else (args.target,)
    results = [validate(target, args.device) for target in targets]
    return 0 if all(results) else 2


if __name__ == "__main__":
    raise SystemExit(main())
