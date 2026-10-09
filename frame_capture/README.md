# 按信号时间取历史帧

独立的 Picamera2 连续采集模块，不修改 TF GUI、机器人协议、自动启动入口或相机校准文件。

相机持续运行，内存最多保留 **200 帧**。信号带入目标时间 `t`，返回满足
`SensorTimestamp < t` 的最大时间戳所对应的图像。**相等的帧不选，晚于信号的帧不选。**
固定 200 帧与固定 1 秒不是同一要求：实际 15 fps 时约保留 13.3 秒，30 fps 时约保留 6.67 秒。

## 使用方式

在仓库根目录运行；Python 需要 NumPy，实机还需要树莓派系统提供的 Picamera2/libcamera。
模块导入和纯逻辑单元测试不要求安装 Picamera2。

```python
from pathlib import Path
import time
from frame_capture import (
    CameraSettings, CaptureConfig, Picamera2FrameCapture, now_sensor_clock_ns,
)

settings = CameraSettings.from_json(
    Path.home() / ".config/tf_inner/camera_settings.json"
)
# 省略宽高：从连接的相机读取原生分辨率，同时使用原生传感器输出尺寸。
# 不额外限制为 1 GiB，仍按系统实际可用内存检查全部 200 帧及运行余量。
config = CaptureConfig(capacity=200)

with Picamera2FrameCapture(config, settings) as camera:
    # start() 表示相机已启动；至少一帧入缓存后才产生示例信号。
    deadline = time.monotonic() + 3
    while camera.stats()["count"] == 0:
        if camera.stats()["error"] or time.monotonic() >= deadline:
            raise RuntimeError("未收到首帧")
        time.sleep(0.01)
    # 实际应用传入信号产生时刻。这个例子在本机产生一个信号。
    signal_timestamp_ns = now_sensor_clock_ns()
    frame = camera.get_before(signal_timestamp_ns, timeout=2.0)
    image = frame.image
    delta_ms = (signal_timestamp_ns - frame.sensor_timestamp_ns) / 1_000_000
    print(frame.sequence, delta_ms, image.shape)
```

`image` 是模块拥有的独立 NumPy 数组，设为只读。需要修改时使用 `image.copy()`。
调用方持有的图像在环形缓存覆盖后仍有效；长期持有大量结果会增加缓存以外的内存。
NumPy 中 Picamera2 的 `RGB888` 格式按 B、G、R 字节排列，不应直接当作 PIL 的 RGB。
`YUV420` 是包含亮度及色度平面的数组，需要按对应格式转换后显示或送入模型。

## 时间与选帧语义

- 直接保存同一次相机请求的图像和原始 `SensorTimestamp`，单位为整数纳秒。
- `now_sensor_clock_ns()` 在树莓派 Linux 上使用 `CLOCK_BOOTTIME`。传入的信号时间必须
  与传感器时间戳处于同一时钟域；不能直接传 Unix/UTC 时间或另一台设备开机后的计时。
  外部设备的时间必须由调用方完成同步和转换。建议实机验证事件时间与相机时间的偏移。
- 当前 Picamera2 手册把树莓派 `SensorTimestamp` 解释为首像素读出时刻；这里按该原始值
  比较，不自动减曝光时间，也不声称整幅滚动快门图像在该时刻同时曝光。
- 查询会等待缓存最新时间戳达到或超过目标时间，再找其严格前一帧，避免相机流水线里
  尚未送达的旧时间帧被漏选。`timeout` 是等待限额，不会改变传入的信号时间。
- 无可用前帧、目标早于保留历史、超过 `max_age_ns`、相机故障或等待超时会抛出
  `FrameUnavailable`；调用方应读取 `reason`，不要把未匹配当作正常图片。
- 缓存只包含成功交付给模块的帧。连续运行不能保证传感器每帧都交付；需要结合测试报告中
  的时间间隔、帧率及异常间隔数量判断。若在暂停/重启后改变了时间基准，应重新启动模块。

## 固定曝光与内存

读取现有 JSON 的三个字段：`exposure_time_us`、`analogue_gain`、`colour_gains`。
不自动校准或覆盖文件。启动前关闭自动曝光和自动白平衡并设置固定值。
可选 `fps` 会显式设置帧周期；不指定时不强制 30 fps，避免已有约 66 ms 曝光被 30 fps 限制。
实际曝光和增益仍以每帧元数据为准，硬件的可取值会有量化误差。

默认测试使用连接相机的**原生分辨率、RGB888、200 帧**，例如 IMX477 通常是 4056×3040，
但程序不会写死这个尺寸。省略宽高时读取 `camera.sensor_resolution`，同时请求对应的传感器
输出尺寸，并核对协商后的图像和传感器尺寸；不允许先降低传感器分辨率再放大输出。
只有同时显式指定 `--width` 和 `--height` 才测试其他输出尺寸。
实际尺寸会进入统计信息；不会自动缩小图像、减少帧数或切换像素格式。

| 200 帧图像，不计对齐和其他缓冲 | 约占用 |
| --- | ---: |
| 1280×720 RGB888 | 527 MiB |
| 4056×3040 RGB888 | 6.89 GiB |
| 4056×3040 YUV420 | 3.45 GiB |

启动时按协商后的每帧大小、相机缓冲和复制余量检查实际可用内存，并保留 128 MiB 系统余量。
默认 `memory_budget_mb=None` 表示不附加固定预算上限；可以显式设置额外的 MiB 上限。
无法检测可用内存且未设置上限时会拒绝启动。内存不足会明确报错，不会降分辨率继续。
4056×3040 RGB888 的 200 帧本身约需 7.4 GB（6.89 GiB），另有相机缓冲和临时复制，
因此能否在 yras8 上填满缓存要以当时实际可用内存检查为准。
相机只允许一个采集程序占用；执行独立测试前退出占用同一相机的 TF GUI。

## 本地逻辑测试

```text
python -m unittest discover -s frame_capture/tests -v
```

逻辑和模拟相机测试只验证软件行为，不能替代 yras8 的真实相机测试。

当前验证记录（2026-10-09）：38 项本地逻辑、模拟相机和测试脚本回归测试通过，
包括原生分辨率选择、拒绝缩小尺寸，以及原生 200 帧内存不足时在采集前退出。
yras8 实机测试尚未执行：历史 SSH 地址连接超时，内置浏览器控制进程在启动时失败。
因此尚无该设备上的实际帧率、内存峰值或真实相机选帧结果。

## yras8 实机测试

使用独立测试目录和 `codex/timestamp-frame-buffer` 分支。保持原 TF GUI 的生产检出和启动入口不变。
退出占用相机的程序后，在测试仓库根目录运行：

```bash
python3 tools/test_frame_capture.py --self-test \
  --settings "$HOME/.config/tf_inner/camera_settings.json" \
  --capacity 200 \
  --report /tmp/frame-capture-yras8.json \
  --save-dir /tmp/frame-capture-sample
```

上面的命令直接使用原生分辨率，不需要另填宽高。实机脚本等到至少收到 220 帧，
检查 200 帧容量、旧帧覆盖、严格小于边界、过期拒绝，
并发送 20 个本机时间信号验证选出的确是最新前帧。报告包含实际帧率、历史覆盖时长、
曝光/增益范围、缓存字节数、进程峰值内存、匹配时间差及查询等待时间。
曝光与设定值允许最多 1% 或 100 微秒的量化差；模拟增益允许 5%，白平衡增益允许
1% 或 0.02，同时检查曝光和模拟增益在缓存期间保持稳定。指定帧率时，测得帧率需在 5% 内。
所有检查和相机正常关闭都成功后才输出 `status: passed`。相机不存在或无法启动会退出失败。
`intervals_above_1_5_median` 是异常间隔计数，不等于已确认的传感器丢帧数。
样本保存为 `.npy` 和配套元数据 `.json`，保持像素值；采集过程不逐帧写磁盘。

不带 `--self-test` 时，脚本通过标准输入接收每行一个 JSON 信号：

```json
{"timestamp_ns": 123456789000, "clock": "linux_clock_boottime"}
```

这个数字只是协议示例，实际必须用对应树莓派的时钟。脚本逐行返回匹配结果；指定
`--save-dir` 可保存每次选中的图像，正常 EOF 或 Ctrl+C 关闭相机。
标准输入模式用于独立测试；其他程序可直接调用 Python 模块获取数组，不必保存文件。

参考：[Picamera2 手册](https://datasheets.raspberrypi.com/camera/picamera2-manual.pdf)，
[libcamera SensorTimestamp 定义](https://libcamera.org/api-html/namespacelibcamera_1_1controls.html)。
