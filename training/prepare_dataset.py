"""Create deterministic train/validation splits from the inbox folders."""

from __future__ import annotations

import argparse
import hashlib
import random
import re
import shutil
from collections import defaultdict
from pathlib import Path

from PIL import Image

from common import CLASS_NAMES, IMAGE_EXTENSIONS, TARGETS, image_files


SIDE_SUFFIX = re.compile(r"(?:[_-](?:LEFT|RIGHT))$", re.IGNORECASE)


def group_name(path: Path) -> str:
    """Keep LEFT and RIGHT crops from one source image in the same split."""
    return SIDE_SUFFIX.sub("", path.stem).casefold()


def verify_image(path: Path) -> str | None:
    try:
        with Image.open(path) as image:
            image.verify()
    except Exception as exc:  # Pillow raises several exception types for corrupt images.
        return str(exc)
    return None


def clear_generated_images(dataset_root: Path) -> None:
    """Remove only generated image copies; inbox originals are never touched."""
    for split in ("train", "val"):
        for class_name in CLASS_NAMES:
            folder = dataset_root / split / class_name
            folder.mkdir(parents=True, exist_ok=True)
            for path in folder.iterdir():
                if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS:
                    path.unlink()


def destination_for(source: Path, inbox: Path, output: Path) -> Path:
    destination = output / source.name
    if not destination.exists():
        return destination

    relative = str(source.relative_to(inbox)).encode("utf-8")
    suffix = hashlib.sha1(relative).hexdigest()[:8]
    return output / f"{source.stem}_{suffix}{source.suffix.lower()}"


def prepare_target(target: str, val_ratio: float, seed: int) -> bool:
    dataset_root = TARGETS[target]["data"]
    clear_generated_images(dataset_root)
    ready = True

    print(f"\n[{target.upper()}]")
    for class_name in CLASS_NAMES:
        inbox = dataset_root / "inbox" / class_name
        sources = image_files(inbox)
        valid_sources: list[Path] = []

        for source in sources:
            error = verify_image(source)
            if error:
                print(f"  SKIP corrupt image: {source} ({error})")
            else:
                valid_sources.append(source)

        groups: dict[str, list[Path]] = defaultdict(list)
        for source in valid_sources:
            groups[group_name(source)].append(source)

        group_ids = sorted(groups)
        random.Random(f"{seed}:{target}:{class_name}").shuffle(group_ids)

        if len(group_ids) >= 2:
            val_count = max(1, round(len(group_ids) * val_ratio))
            val_count = min(val_count, len(group_ids) - 1)
        else:
            val_count = 0

        val_groups = set(group_ids[:val_count])
        counts = {"train": 0, "val": 0}

        for current_group, group_sources in groups.items():
            split = "val" if current_group in val_groups else "train"
            output = dataset_root / split / class_name
            for source in group_sources:
                destination = destination_for(source, inbox, output)
                shutil.copy2(source, destination)
                counts[split] += 1

        print(
            f"  {class_name}: inbox={len(valid_sources)}, groups={len(group_ids)}, "
            f"train={counts['train']}, val={counts['val']}"
        )

        if counts["train"] == 0 or counts["val"] == 0:
            ready = False
            print(f"  ERROR: {class_name} needs at least two source-image groups.")

    return ready


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=("all", *TARGETS), default="all")
    parser.add_argument("--val-ratio", type=float, default=0.20)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    if not 0.05 <= args.val_ratio <= 0.50:
        parser.error("--val-ratio must be between 0.05 and 0.50")

    targets = TARGETS if args.target == "all" else (args.target,)
    results = [prepare_target(target, args.val_ratio, args.seed) for target in targets]
    ready = all(results)

    if ready:
        print("\nDataset preparation: PASS")
        return 0

    print("\nDataset preparation: NOT READY")
    print("Add OK and NG images to each inbox folder, then run this tool again.")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
