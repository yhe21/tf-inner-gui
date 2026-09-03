"""Verify the local Ultralytics and CUDA training environment."""

from __future__ import annotations

from common import ROOT  # Sets YOLO_CONFIG_DIR before importing Ultralytics.

import torch
import ultralytics


def main() -> int:
    print(f"Project:       {ROOT}")
    print(f"Ultralytics:   {ultralytics.__version__}")
    print(f"PyTorch:       {torch.__version__}")
    print(f"PyTorch CUDA:  {torch.version.cuda}")
    print(f"CUDA enabled:  {torch.cuda.is_available()}")

    if not torch.cuda.is_available():
        print("ERROR: NVIDIA GPU is not available. Training would run on the CPU.")
        return 1

    device = torch.device("cuda:0")
    value = (torch.ones((256, 256), device=device) @ torch.ones((256, 256), device=device))[0, 0]
    torch.cuda.synchronize()
    print(f"GPU:           {torch.cuda.get_device_name(0)}")
    print(f"CUDA test:     PASS ({value.item():.1f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
