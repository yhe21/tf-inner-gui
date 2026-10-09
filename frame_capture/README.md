# 按信号时间取历史帧

独立的 Picamera2 连续采集模块，不修改 TF GUI、机器人协议、自动启动入口或相机校准文件。

相机持续运行，默认在内存最多保留 **30 帧**。信号带入目标时间 `t`，返回满足
`SensorTimestamp < t` 的最大时间戳所对应的图像。**相等的帧不选，晚于信号的帧不选。**
固定 30 帧与固定 1 秒不是同一要求：实际 15 fps 时约保留 2 秒，30 fps 时约保留 1 秒。

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
# 按系统实际可用内存检查全部 30 帧及运行余量。
config = CaptureConfig(capacity=30)

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

默认测试使用连接相机的**原生分辨率、RGB888、30 帧**，例如 IMX477 通常是 4056×3040，
但程序不会写死这个尺寸。省略宽高时读取 `camera.sensor_resolution`，同时请求对应的传感器
输出尺寸，并核对协商后的图像和传感器尺寸；不允许先降低传感器分辨率再放大输出。
只有同时显式指定 `--width` 和 `--height` 才测试其他输出尺寸。
实际尺寸会进入统计信息；不会自动缩小图像、减少帧数或切换像素格式。

| 30 帧图像，不计对齐和其他缓冲 | 约占用 |
| --- | ---: |
| 1280×720 RGB888 | 79 MiB |
| 4056×3040 RGB888 | 1.03 GiB |
| 4056×3040 YUV420 | 0.52 GiB |

启动时按协商后的每帧大小、相机缓冲和复制余量检查实际可用内存，并保留 128 MiB 系统余量。
默认 `memory_budget_mb=None` 表示不附加固定预算上限；可以显式设置额外的 MiB 上限。
无法检测可用内存且未设置上限时会拒绝启动。内存不足会明确报错，不会降分辨率继续。
4056×3040 RGB888 的 30 帧本身约需 1.11 GB（1.03 GiB），另有相机缓冲和临时复制，
因此能否在 yras8 上填满缓存要以当时实际可用内存检查为准。
相机只允许一个采集程序占用；执行独立测试前退出占用同一相机的 TF GUI。

## 本地逻辑测试

```text
python -m unittest discover -s frame_capture/tests -v
```

逻辑和模拟相机测试只验证软件行为，不能替代 yras8 的真实相机测试。

当前验证记录（2026-10-09）：38 项本地逻辑、模拟相机和测试脚本回归测试通过，
包括 30 帧循环覆盖、原生分辨率选择、拒绝缩小尺寸，以及内存不足时在采集前退出。

同日用户在 yras8（报告主机名 `yras`）手动运行测试提交 `49a387e`，提供的终端报告为
`status: passed`，所有检查通过，包括循环覆盖、严格前帧选择、固定曝光和正常关闭相机。
本次为短时实机自测，结果如下：

| 项目 | 实测结果 |
| --- | --- |
| 图像和传感器输出尺寸 | 均为 4056×3040，图像格式 RGB888 |
| 缓存 | 30 帧；累计接收 91 帧，淘汰 61 帧 |
| 帧率及间隔 | 11.719 fps；中位间隔 85.330 ms，最大间隔 85.335 ms |
| 缓存首末帧时间跨度 | 2.474582 秒（30 帧之间有 29 个间隔） |
| 曝光 | 请求 66657 µs，缓存内实测恒为 66654 µs |
| 模拟增益 | 实测恒为 4.876190662384033 |
| 图像缓存大小 | 1109721600 字节，约 1.03 GiB |
| 进程峰值 RSS | 1337408 KiB，约 1.28 GiB；不代表整机总内存占用 |
| 信号检查 | 配置为 20 个信号，选帧检查全部通过 |
| 时间间隔异常 | 被测缓存中大于中位间隔 1.5 倍的间隔为 0 |

报告中当前模式的 `FrameDurationLimits` 下限为 85335 µs，对应约 11.72 fps；
因此不能用 `1 / 曝光时间` 直接预测实际帧率，缩短曝光也不保证该模式提速。
用户粘贴的 `matches` 仅包含第一个匹配：帧时间早于信号 61.515 ms，查询等待 146.668 ms。
两者分别表示所选帧的时间差和调用等待时长；等待包含确认信号前帧已交付所需的时间，
不能据单个样本推断全部信号的平均或最大延迟。本次短测也不替代长时间稳定性测试。

## yras8 实机测试

使用独立测试目录和 `codex/timestamp-frame-buffer` 分支。保持原 TF GUI 的生产检出和启动入口不变。
退出占用相机的程序后，在测试仓库根目录运行：

```bash
python3 tools/test_frame_capture.py --self-test \
  --settings "$HOME/.config/tf_inner/camera_settings.json" \
  --capacity 30 \
  --report /tmp/frame-capture-yras8.json \
  --save-dir /tmp/frame-capture-sample
```

上面的命令直接使用原生分辨率，不需要另填宽高。实机脚本等到至少收到 50 帧，
检查 30 帧容量、旧帧覆盖、严格小于边界、过期拒绝，
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

## 超时与故障处理

| 阶段 | 默认限时 | 超时后的行为 |
| --- | --- | --- |
| 启动相机 | 10 秒 | 抛出 `FrameUnavailable("timeout", ...)`，请求停止并清理 |
| 等待相机新请求 | 连续 3 秒没有进展 | 采集状态变为 `failed`，记录错误，尝试释放请求并关闭相机 |
| 按信号查询 | 模块默认 1 秒；测试脚本默认 2 秒 | 抛出 `FrameUnavailable("timeout", ...)`，不返回未经确认的旧帧 |
| 停止和清理 | 3 秒 | 报告清理超时，不声称相机已释放 |

帧时间戳倒退、长时间重复同一帧和图像读取错误也会使采集失败；查询方会收到
`capture_failed`。查询超时本身不停止后台采集，调用方可以处理错误后继续发新的信号。
当前模块**不自动重连或重新启动相机**。采集对象只能启动一次；如果底层驱动的原生调用
卡死，线程内超时不能强制中断该调用，需要由外部进程管理来完成强制恢复。

## 连续 10 分钟温度测试

该入口新增 20 项模拟回归测试，与原模块合计 58 项本地测试通过（2026-10-09）。
模拟时钟可验证 600 秒调度和超时处理；真实温度与长时间稳定性仍须运行下面的实机测试。

先退出占用相机的 TF GUI，然后在独立测试目录更新分支并运行：

```bash
cd "$HOME/tf-frame-buffer-test" &&
git pull --ff-only &&
/usr/bin/python3 tools/soak_frame_capture.py \
  --duration 600 \
  --capacity 30 \
  --settings "$HOME/.config/tf_inner/camera_settings.json" \
  --sample-interval 5 \
  --query-interval 1 \
  --report "$HOME/frame-capture-10min.json" \
  --log "$HOME/frame-capture-10min.jsonl"
```

保持原生分辨率、RGB888 和固定曝光，填满缓存后持续运行 600 秒，启动和关闭另计。
每秒发送一个本机时间信号测试选帧，每 5 秒记录处理器温度、进程 RSS、帧率和降频状态。
这里只读取树莓派处理器温度，不代表摄像头传感器温度；不保存连续图像到磁盘。
JSONL 日志逐条写入并刷新，JSON 报告汇总温度和查询耗时；普通异常及 Ctrl+C 都会尝试
写出已有结果。强制断电或强制终止进程时不能保证最终报告完成，可检查已有 JSONL 日志。

单次查询超时记入计数并继续观察，但最终测试不能标记为通过；采集错误或持续无帧则终止
测试并保留部分记录。开始前记录 `vcgencmd get_throttled` 基线，将当前告警、开机以来的
历史告警和测试中新出现的历史位分开报告；已有历史位无法用于判断该故障在本次是否重现。
温度读取失败会明确记录，全部温度样本缺失时不能宣称温度测试通过。
`passed` 表示采集、查询和测试完成检查通过；供电或降频告警会单独写入 `warnings`，
因此判断散热表现还需要查看温度曲线及告警，不能仅看 `status`。

降频位参考：[Raspberry Pi 官方 `get_throttled` 定义](https://www.raspberrypi.com/documentation/computers/os.html#get_throttled)。
