# TF Inner GUI

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

程序启动后会在后台初始化 Picamera2，并让摄像头持续以 1920×1080 运行。
第一次启动时等待约 1 秒让自动曝光和白平衡稳定，后续每次触发不再重复启动
摄像头。“摄像头结果监测”中的“手动拍摄并保存”按钮与以后 VT6 TCP 触发可
共用同一个拍摄入口。

触发时使用 `capture_request(flush=True)`，确保被保存图片的曝光不会早于触发
时刻。采集完成后再编码 JPG、保存并显示。图片保存在程序目录下：

```text
captures/YYYYMMDD/YYYYMMDD_HHMMSS_mmm.jpg
```

例如：`captures/20260819/20260819_153045_123.jpg`。

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
scp -6 -r .\tf_gui "y@[fe80::树莓派地址%网卡编号]:~/tf_inner/"
```

也可以通过 VS Code Remote SSH 把整个 `tf_gui` 文件夹上传到 `~/tf_inner/`。

## 在 Raspberry Pi 上测试

窗口模式：

```bash
cd ~/tf_inner/tf_gui
python3 main.py
```

触摸屏全屏模式：

```bash
cd ~/tf_inner/tf_gui
python3 main.py --fullscreen
```

Qt 和 OpenGL 使用树莓派系统已经安装的 `python3-pyqt5`、`python3-opengl`，不需要再次用 pip 安装。

## 查看触摸屏分辨率

在树莓派桌面的终端中执行：

```bash
python3 -c "from PyQt5.QtWidgets import QApplication; a=QApplication([]); s=a.primaryScreen().size(); print(s.width(), s.height())"
```

界面使用布局管理器，会自动适应不同分辨率；当前设计预览尺寸为 1024×600。
