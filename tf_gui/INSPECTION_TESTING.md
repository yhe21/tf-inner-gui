# Raspberry Pi AI inspection test

Version 0.4.1 adds fixed-ROI NCNN classification for `INNER` and `GLUE`.
Inference calls NCNN directly and does not import PyTorch or Ultralytics.

## Required Raspberry Pi layout

Keep the exported directory names unchanged:

```text
~/tf-inner-gui/
├── models/
│   ├── inner_cls_ncnn_model/
│   │   ├── metadata.yaml
│   │   ├── model.ncnn.bin
│   │   └── model.ncnn.param
│   └── glue_cls_ncnn_model/
│       ├── metadata.yaml
│       ├── model.ncnn.bin
│       └── model.ncnn.param
└── tf_gui/
    ├── main.py
    └── inspection.py
```

Ultralytics uses the `_ncnn_model` suffix to recognize the model format.

## Update the application and models

The two NCNN model directories are committed with the application. Update
everything on Raspberry Pi with one pull:

```bash
cd ~/tf-inner-gui
git pull
```

No separate model copy is required.

## Verify the Raspberry Pi runtime

```bash
python3 -c "import ncnn, numpy, PIL; print('AI runtime OK')"
ls -l ~/tf-inner-gui/models/inner_cls_ncnn_model
ls -l ~/tf-inner-gui/models/glue_cls_ncnn_model
```

Start the application normally:

```bash
cd ~/tf-inner-gui/tf_gui
python3 main.py --fullscreen
```

The Camera Results page retains the latest `INNER` and `GLUE` results. Each
row shows the strict two-sided result, left/right class confidence, and total
AI processing time. A displayed overall result is `OK` only when both sides
are classified `OK`.

During this commissioning version, all VT6 production replies are forced to
`INNER,OK`, `GLUE,OK`, or `NP,OK`. A displayed NG result, a missing model, or a
camera failure will not send NG to the Epson controller.

The rotated full-resolution frame is cropped and classified in memory. Disk
writing happens only when the existing training-image saving option is on.
