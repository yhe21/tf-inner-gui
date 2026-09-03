# TF Inner GUI

当前版本：`v0.4.1`。触摸屏菜单和运行状态均使用英文。

本版本的主要行为：

- `INNER`、`GLUE`和`NP`默认保存原生分辨率训练图片；
- `Camera Results`页面可以随时开启或关闭训练图片保存；
- `Auto Calibrate & Lock`只运行一次自动曝光和自动白平衡，然后保存并锁定参数；
- `Capture and Save`手动按钮仍保存原生分辨率 JPG，并将像素逆时针旋转 90°；
- `INNER`和`GLUE`使用各自的YOLO26s分类NCNN模型检测左右固定ROI；
- 树莓派直接使用NCNN运行模型，不导入PyTorch或Ultralytics，避免系统BLAS兼容问题；
- `Camera Results`保留并显示最近一次左右分类、置信度和AI处理时间；
- 机器人故障代码仍保存统一日志和故障照片；
- TCP结果仅发送给原请求连接，不向重连后的新连接补发旧结果。

第一版触摸屏主页面，使用 PyQt5，并将界面、样式和程序逻辑分开：

- `ui/main_menu.ui`：用 Qt Designer 编辑的主页面
- `styles/app.qss`：颜色、字体和触摸按钮样式
- `main.py`：按钮事件、时钟和运行模式

三个调整页面共用同一套触摸界面。PickNP、PickNPS 和 DropNP 分别保存
X/Y/Z/U 调整数值，固定步长为 0.05，允许范围为 -0.50～+0.50。
点击“应用并保存”后，数据会写入：

```text
~/.config/tf_inner/adjustments.json
```

程序每次启动都会自动读取该文件；点击“取消”不会改变已保存的数据。

`Camera Results`页面的`Save INNER/GLUE/NP images for training`选项用于控制生产触发
图片是否保存。默认开启，修改后立即生效，并保存在：

```text
~/.config/tf_inner/capture_settings.json
```

因此程序重启后仍会使用上次选择。这个选项不影响手动拍摄和故障照片保存。

程序启动后会在后台初始化 Picamera2，并读取摄像头的原生传感器分辨率持续运行
（IMX477 通常为 4056×3040）。保存的 JPG 不缩小到 1080p，并在编码前将像素逆时针旋转
90°，所以 4056×3040 的采集结果会保存为 3040×4056。

摄像头固定参数保存在程序外部：

```text
~/.config/tf_inner/camera_settings.json
```

文件内容示例（数值由实际摄像头校准产生）：

```json
{
  "exposure_time_us": 12000,
  "analogue_gain": 1.25,
  "colour_gains": [1.7, 1.4]
}
```

第一次升级到本版本时，进入`Camera Results`页面，把正常产品和正常生产照明放好，然后点击
`Auto Calibrate & Lock`。程序让自动曝光和自动白平衡运行约 2 秒，读取`ExposureTime`、
`AnalogueGain`和`ColourGains`，立即关闭自动控制，并把这些值写入上述 JSON 文件。
在完成这一步之前，手动拍摄和`INNER/GLUE/NP`生产触发均不会拍照；生产指令会返回`NG`。

以后每次启动，程序都会在摄像头开始输出第一帧之前加载固定值，并明确设置
`AeEnable=false`和`AwbEnable=false`。所有手动、生产和故障触发都使用同一组参数；只有再次点击
`Auto Calibrate & Lock`才会重新运行自动控制并覆盖文件。

如需微调，可在树莓派终端运行：

```bash
nano ~/.config/tf_inner/camera_settings.json
```

减小`exposure_time_us`可以减少运动模糊但画面更暗；增大它会变亮但曝光时间更长。增大
`analogue_gain`可以变亮，但会增加噪声。修改后保存并重启程序即可应用。不要把任何数值改成 0
或负数。`Camera Results`页面中的`Capture and Save`按钮保存逆时针旋转 90° 的原生分辨率
JPG；VT6 TCP 触发使用同一个相机服务，并根据页面上的训练图片保存选项决定是否写入文件。

触发时使用 `capture_request(flush=True)`，确保被保存图片的曝光不会早于触发
时刻。采集完成后再编码 JPG、保存并显示。图片保存在程序目录下：

```text
captures/YYYYMMDD/YYYYMMDD_HHMMSS_mmm.jpg
```

例如：`captures/20260819/20260819_153045_123.jpg`。

## VT6生产触发协议

程序默认作为TCP服务器监听所有IPv4网卡的5000端口。连接不设置空闲超时，
也不发送心跳。VT6发送的ASCII指令必须以CRLF结尾：

```text
INNER\r\n
GLUE\r\n
NP\r\n
CALIB\r\n
```

当前版本收到`INNER`、`GLUE`或`NP`后，都会以原生分辨率完成一次新帧采集。训练图片保存
默认开启，保存路径分别为：

```text
captures/YYYYMMDD/INNER/YYYYMMDD_HHMMSS_mmm.jpg
captures/YYYYMMDD/GLUE/YYYYMMDD_HHMMSS_mmm.jpg
captures/YYYYMMDD/NP/YYYYMMDD_HHMMSS_mmm.jpg
```

保存的图片同样逆时针旋转 90°。关闭页面上的保存选项后仍会正常采集，但不编码或
保存 JPG。无论是否保存，采集成功后都固定返回OK：

```text
INNER,OK\r\n
GLUE,OK\r\n
NP,OK\r\n
```

当前为调试运行阶段：即使页面显示模型判断为NG、模型加载失败、摄像头未就绪、队列已满
或拍摄失败，TCP也仍然只返回`INNER,OK`、`GLUE,OK`或`NP,OK`，不会输出NG。
手动拍摄仍保存在`captures/YYYYMMDD/`。机器人故障字符串仍写入统一日志并在
`error_records/`中保存逆时针旋转 90° 的故障照片。

`CALIB`不拍照，直接返回12个逗号分隔的数值，固定顺序为：

```text
NP_X,NP_Y,NP_Z,NP_U,NPS_X,NPS_Y,NPS_Z,NPS_U,DROP_X,DROP_Y,DROP_Z,DROP_U
```

这些返回值都是异步通知，VT6不需要等待。每个结果只会尝试返回给发出该请求
的TCP连接；如果该连接已经中断，结果会直接丢弃，不会在VT6重新连接后补发，
因此不会把上一生产周期的旧结果带入新连接。已经开始的新帧采集不受断线影响，
仍会继续完成，并按照该任务进入队列时的保存选项决定是否保存图片。

`CALIB`请求收到后会立即读取当前已经保存的调整值并返回。尚未设置的参数默认
为`0.00`；VT6主程序不需要等待这些参数，可以继续使用当前值或默认值。

修改端口或关闭TCP服务：

```bash
python3 main.py --tcp-port 5001
python3 main.py --no-tcp
```

## 在 Windows PC 上编辑和测试

在 PowerShell 中进入本目录，然后执行：

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-pc.txt
pyqt5-tools designer .\ui\main_menu.ui
```

在 Qt Designer 中保存后，直接运行测试：

```powershell
python .\main.py
```

开发阶段直接加载 `.ui`，不需要运行 `pyuic5`，所以每次保存后重新启动程序即可看到修改。

## 更新到 Raspberry Pi

程序和两个 NCNN 模型都保存在同一个 GitHub 仓库中。树莓派只需执行：

```bash
cd ~/tf-inner-gui
git pull
```

`git pull` 会同时更新程序、界面以及以下两个运行模型，不再需要使用
`scp` 单独复制模型：

```text
models/inner_cls_ncnn_model/
models/glue_cls_ncnn_model/
```

训练数据、训练记录和 `.pt` 文件仍然只保留在训练电脑上，不会下载到树莓派。

## 在 Raspberry Pi 上测试

窗口模式：

```bash
cd ~/tf-inner-gui/tf_gui
python3 main.py
```

触摸屏全屏模式：

```bash
cd ~/tf-inner-gui/tf_gui
python3 main.py --fullscreen
```

Qt 和 OpenGL 使用树莓派系统已经安装的 `python3-pyqt5`、`python3-opengl`，不需要再次用 pip 安装。

## 查看触摸屏分辨率

在树莓派桌面的终端中执行：

```bash
python3 -c "from PyQt5.QtWidgets import QApplication; a=QApplication([]); s=a.primaryScreen().size(); print(s.width(), s.height())"
```

界面使用布局管理器，会自动适应不同分辨率；当前设计预览尺寸为 1024×600。
