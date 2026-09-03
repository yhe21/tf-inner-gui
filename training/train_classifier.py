"""Train one fixed-ROI classification model."""

from __future__ import annotations

import argparse
import os

from common import CLASS_NAMES, ROOT, TARGETS, image_files


def require_dataset(target: str) -> None:
    dataset_root = TARGETS[target]["data"]
    missing: list[str] = []
    for split in ("train", "val"):
        for class_name in CLASS_NAMES:
            count = len(image_files(dataset_root / split / class_name))
            print(f"{target.upper()} {split}/{class_name}: {count} images")
            if count == 0:
                missing.append(f"{split}/{class_name}")
    if missing:
        raise SystemExit(
            "Dataset is not ready. Missing images in: "
            + ", ".join(missing)
            + ". Run training\\01_prepare_data.bat first."
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--epochs", type=int, default=100)
    parser.add_argument("--batch", type=int)
    parser.add_argument("--device", default="0", help="CUDA GPU index, or 'cpu'")
    parser.add_argument("--model", default="yolo26s-cls.pt")
    args = parser.parse_args()

    require_dataset(args.target)
    config = TARGETS[args.target]
    batch = args.batch or config["batch"]

    # Keep automatic pretrained-weight downloads in the project directory.
    os.chdir(ROOT)
    from ultralytics import YOLO

    print(f"\nTraining {args.target.upper()} on device {args.device}")
    print(f"Image size: {config['imgsz']}, batch: {batch}, epochs: {args.epochs}")

    model = YOLO(args.model)
    model.train(
        data=str(config["data"]),
        epochs=args.epochs,
        imgsz=config["imgsz"],
        batch=batch,
        device=args.device,
        workers=4,
        project=str(ROOT / "runs" / "classify"),
        name=args.target,
        exist_ok=True,
        seed=42,
        deterministic=True,
        patience=20,
        plots=True,
        # These fixed ROIs are already square and correctly oriented. Avoid
        # augmentations that can erase/crop the inspected component while
        # retaining its OK label.
        scale=0.0,
        fliplr=0.0,
        flipud=0.0,
        auto_augment=None,
        erasing=0.0,
        hsv_h=0.0,
        hsv_s=0.0,
        hsv_v=0.0,
    )
    print(f"\nBest weights: {ROOT / 'runs' / 'classify' / args.target / 'weights' / 'best.pt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
