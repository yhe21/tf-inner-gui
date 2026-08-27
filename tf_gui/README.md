# TF Inner GUI

当前版本：`v0.2.3`。触摸屏菜单和运行状态均使用英文。

本版本的主要行为：

- `INNER`、`GLUE`和`NP`默认保存原生分辨率训练图片；
- `Camera Results`页面可以随时开启或关闭训练图片保存；
- `Capture and Save`手动按钮仍保存原生分辨率 JPG，并将像素顺时针旋转 90°；
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
（IMX477 通常为 4056×3040）。保存的 JPG 不缩小到 1080p，并在编码前将像素顺时针旋转
90°，所以 4056×3040 的采集结果会保存为 3040×4056。
第一次启动时等待约 1 秒让自动曝光和白平衡稳定，后续每次触发不再重复启动
摄像头。`Camera Results`页面中的`Capture and Save`按钮保存顺时针旋转 90° 的原生分辨率 JPG；
VT6 TCP 触发使用同一个相机服务，并根据页面上的训练图片保存选项决定是否写入文件。

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

保存的图片同样顺时针旋转 90°。关闭页面上的保存选项后仍会正常采集，但不编码或
保存 JPG。无论是否保存，采集成功后都固定返回OK：

```text
INNER,OK\r\n
GLUE,OK\r\n
NP,OK\r\n
```

只有摄像头未就绪、队列已满或采集失败时才会返回`INNER,NG`、`GLUE,NG`或`NP,NG`。
手动拍摄仍保存在`captures/YYYYMMDD/`。机器人故障字符串仍写入统一日志并在
`error_records/`中保存顺时针旋转 90° 的故障照片。正式检测阶段将运行对应YOLO模型，NG保存
图片，OK不保存图片。

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

## 复制到 Raspberry Pi

从该目录的上一级执行。将下面的 IPv6 地址替换为树莓派实际地址：

```powershell
scp -6 -r .\tf_gui "y@[fe80::树莓派地址%网卡编号]:~/tf-inner-gui/"
```

也可以通过 VS Code Remote SSH 把整个`tf_gui`文件夹上传到
`~/tf-inner-gui/`。

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
