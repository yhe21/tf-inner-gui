# TF fixed-ROI classification training

The local Windows environment is configured for the NVIDIA RTX 3080 Ti. Both
classifiers use `yolo26s-cls.pt` as their pretrained base model. The Raspberry
Pi will only run inference after the trained models are exported.

## 1. Add cropped images

Copy the Fiji crop results into these four inbox folders according to the real
inspection result:

```text
datasets/inner/inbox/OK/
datasets/inner/inbox/NG/
datasets/glue/inbox/OK/
datasets/glue/inbox/NG/
```

Put both `_LEFT` and `_RIGHT` images in the same class folder. Do not rename the
side suffix: the preparation tool uses it to keep both crops from one original
photo in the same train/validation split.

At minimum, every inbox needs two different source-image groups. In practice,
use many more images and include different real NG conditions.

## 2. Prepare the dataset

Double-click `01_prepare_data.bat`. It verifies the image files and makes a
repeatable 80/20 train/validation split. Files in `inbox` are never modified.

## 3. Train

Double-click, in order:

1. `02_train_inner.bat`
2. `03_train_glue.bat`

Best weights are written to:

```text
runs/classify/inner/weights/best.pt
runs/classify/glue/weights/best.pt
```

The scripts disable random crop, flip, automatic augmentation, and random
erasing because those operations can remove the inspected INNER/GLUE feature
while incorrectly retaining an OK label.

## 4. Validate and export

Double-click `04_validate_models.bat`, then `05_export_ncnn.bat`.

Raspberry Pi-ready folders are written to:

```text
models/inner_cls_ncnn_model/
models/glue_cls_ncnn_model/
```

Keep the `_ncnn_model` directory suffix when copying the models to the
Raspberry Pi. Ultralytics uses that suffix to recognize the NCNN format.

The images, training runs, pretrained weights, and exported models are ignored
by Git because they may be large or contain production data.
