# TF Inner GUI

当前版本：`v0.4.9`。主界面名称为 `TF Inspection`，保留版本号；摄像头就绪时仅显示 `Camera ready`，不再附加分辨率和曝光说明。尚未校准时仍提示 `Not calibrated`，摄像头页面保留详细信息。触摸屏菜单和运行状态均使用英文。

## 相机超时与恢复

相机及 AI 运行在单独的子进程中，界面工作线程监督该进程。即使旧版 Picamera2 的
取帧或关闭操作无限等待，也能结束旧进程并重新初始化相机，界面和 TCP 服务继续运行。

- 开机先实际获取并释放一帧，再报告相机可用；仅发现设备或调用 `start()` 成功不算 Ready。
- 启动时没有相机：显示不可用，每 10 秒重新尝试，不自动重启树莓派。
- 正常运行时取帧超过 5 秒：该次拍照失败，清理旧相机进程，再重新连接。
- 空闲时不轮询、不主动取帧、不执行定时健康检查；生产/手动拍摄、停止请求、子进程消息和退出事件直接唤醒监督器，没有原先 50 毫秒轮询等待。
- 只保留首次启动/重连时的一次首帧验证。运行中相机停止出图，在下一次真实拍摄请求超过 5 秒时启动恢复；空闲期间不会仅因没有新拍摄而判故障。
- 运行中故障后，每次恢复间隔 3 秒，重新初始化最长等待 30 秒；连续 10 次未能恢复取帧才请求重启树莓派。恢复成功后计数清零。
- 如果驱动卡在内核中，旧相机进程无法结束，则重试清理旧进程，并计入同一个 10 次上限；旧进程退出前不会创建第二个相机进程。启动阶段仍不会因此自动重启。
- AI/JPG 处理与取帧分开计时，处理阶段最长 30 秒；一般模型报错、图片保存失败不计为相机连接失败。
- 自动校准最多等待 10 秒。相机恢复继续使用最近一次成功保存的手动参数，不自动重新校准。
- 自动重启只允许 Raspberry Pi，使用现有 `sudo -n systemctl reboot` 权限；30 分钟内最多请求一次自动重启，避免循环重启。权限不足或触发限制时显示错误，不声称重启成功。
- Camera Results 的 Back、Esc 和关闭窗口始终可用；离开页面不会关闭全局相机服务。自动恢复用状态文字提示，不反复弹出阻塞对话框。

极端的内核 I/O 阻塞不一定能通过结束进程解决；这种情况下仍可返回主界面，
但关闭整个应用可能要等待内核恢复或树莓派重启。

时间与次数常量集中在 `camera_recovery.py` 的 `RecoveryLimits`。
故障与恢复记录保存在 `~/.local/state/tf_inner/camera_recovery.log`（轮转保存），
重启限制记录保存在 `~/.local/state/tf_inner/camera_reboot.json`。

相机不可用或恢复期间，按现场要求保留异常放行：`INNER/GLUE/NP` 返回 `OK`，但没有完成拍照或视觉检测；
`CALIB` 仍返回保存的偏移量。故障代码写入 `error.log` 并标记 `CAMERA_NOT_READY`，没有照片。
故障时已经排队的旧生产触发被结束，不在恢复后补拍过去的工件；之后的新触发正常采集。

本版本的主要行为：

- `INNER`、`GLUE`和`NP`默认保存原生分辨率训练图片；
- `Camera Results`页面可以选择保存全部生产图片，关闭后进入95%复查图片保存模式；
- `Auto Calibrate & Lock`只运行一次自动曝光和自动白平衡，然后保存并锁定参数；
- `Capture and Save`手动按钮仍保存原生分辨率 JPG，并将像素逆时针旋转 90°；
- `INNER`和`GLUE`使用各自的YOLO26s分类NCNN模型检测左右固定ROI；
- 只有左右两侧都预测为`OK`且各自置信度不低于90%，整体结果才是`OK`；预测为`NG`时不设置最低置信度；
- 未选择保存全部时，任何一侧为`NG`，或任何一侧为`OK`但置信度低于95%，仍会自动保存原图；
- `Bypass AI (force all OK)`开启后仍然拍照并按保存选项处理图片，但跳过INNER/GLUE模型推理并显示强制OK；
- 树莓派直接使用NCNN运行模型，不导入PyTorch或Ultralytics，避免系统BLAS兼容问题；
- `Camera Results`保留并显示最近一次左右分类、置信度和AI处理时间；
- 摄像头页面按1024×600使用左右两栏：左侧等比例显示完整图片，右侧显示状态、结果和操作按钮；窗口缩放只影响预览，不裁剪或修改保存的原图；
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

`Camera Results`页面的`Save all production images`选项用于选择保存模式。开启时保存
所有生产触发图片；关闭时不保存高置信度正常图片，但任何一侧预测为`NG`，或者任何一侧
预测为`OK`而置信度低于95%，仍然会保存用于复查和继续训练。修改后立即生效，并保存在：

```text
~/.config/tf_inner/capture_settings.json
```

同一文件也保存`Bypass AI (force all OK)`开关。跳过检测默认关闭，修改后立即生效，
并在程序重启后保持上次选择。保存模式不影响手动拍摄和故障照片保存。开启跳过检测时
没有真实AI置信度，因此只有勾选保存全部时才保存生产图片。

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
在完成这一步之前，手动拍摄和`INNER/GLUE/NP`生产触发均不会拍照；当前测试版本的生产指令仍返回`OK`。

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
JPG；VT6 TCP 触发使用同一个相机服务，并根据页面上的保存模式以及AI结果决定是否写入文件。

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

当前版本收到`INNER`、`GLUE`或`NP`后，都会以原生分辨率完成一次新帧采集。保存路径分别为：

```text
captures/YYYYMMDD/INNER/YYYYMMDD_HHMMSS_mmm.jpg
captures/YYYYMMDD/GLUE/YYYYMMDD_HHMMSS_mmm.jpg
captures/YYYYMMDD/NP/YYYYMMDD_HHMMSS_mmm.jpg
```

树莓派上的完整位置是`~/tf-inner-gui/tf_gui/captures/`。保存的图片同样逆时针旋转90°。
关闭`Save all production images`后，INNER/GLUE只保存任一NG或任一OK置信度低于95%的图片；
NP没有AI结果，因此只在保存全部开启时保存。正式检测回复规则：

- `INNER`：整体检测为 NG 时立即回复 `INNER,NG`，OK 时回复 `INNER,OK`。
- `GLUE`：与 INNER 相同，单次整体检测为 NG 就立即回复 `GLUE,NG`，OK 时回复 `GLUE,OK`，不再累计连续次数。
- 旁路开启时始终回复 OK；请求入队时已开启旁路的任务也保留强制 OK。
- 有效结果计算完成就回复，不等待 JPEG 保存；保存失败不会覆盖已经发出的结果，也不会再补发 OK。

回复示例：

```text
INNER,OK\r\n
INNER,NG\r\n
GLUE,OK\r\n
GLUE,NG\r\n
NP,OK\r\n
```

模型加载/计算失败、摄像头未就绪、队列已满或未取得有效检测结果的拍摄失败，
按现场确认的异常策略仍回复 OK；这些 OK 不代表视觉检测合格。
检测阈值仍为左右均 OK 且置信度均不低于 90%；95% 仅为复查照片保存阈值。
在关闭“保存全部”和旁路的正常检测模式下，任一侧为 NG 或 OK 置信度低于 90% 时，立即回复 NG 并保存照片；
左右均为 OK 且最低置信度在 90%（含）至 95%（不含）时，回复 OK 并保存；左右均为 OK 且均达到 95% 时，回复 OK 且不自动保存。
这里的置信度对应预测标签：界面显示 `NG 99%` 仍是 NG，不能把它当作 OK 分数。
`startup.log` 中的 `Production reply` 记录检测值和回复；`queued=True`
表示数据交给 TCP 发送缓冲区，不代表机器人已确认接收。

`NP`当前只拍照并按保存选项处理图片，不运行分类模型，固定回复`NP,OK`。
Epson 的 NG 停机逻辑已改为：收到 NG 后锁存 Memory I/O `RpiNgStopReq`，
在下一次执行到 `Pick_Part` 指定检查点时才执行等待、退回和退出。
正式检测的 NG 回复会触发该停机逻辑；详见 [VT6 机器人设置](../robot/VT6/README.md)。
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

## 开机检查更新后启动 GUI

`tools/start_rpi.py` 使用树莓派自带的 Python 标准库，不需要安装新依赖。
它取代原来 labwc 中的直接启动命令，不要保留两条启动 GUI 的入口。

已提供可以直接复制的启动文件：[tools/rpi/labwc/autostart](../tools/rpi/labwc/autostart)。
文件名必须是 `autostart`，没有 `.sh` 或 `.txt` 后缀。复制到当前用户的
`~/.config/labwc/` 文件夹；当前机器的完整目标路径是 `/home/y/.config/labwc/autostart`。
这是 [labwc 官方支持的桌面启动入口](https://labwc.github.io/labwc-config.5.html)。

你之前截图中的原文件只有一条 TF GUI 启动命令，可以备份后直接替换。
如果后来添加了其他启动项目，请保留那些行，只替换 TF GUI 的启动行，不要整份覆盖。
在停机空闲时，先退出 GUI，再在树莓派上执行一次：

```bash
cd ~/tf-inner-gui
git pull --ff-only
mkdir -p ~/.config/labwc
cp -p ~/.config/labwc/autostart ~/.config/labwc/autostart.backup-$(date +%Y%m%d_%H%M%S)
cp tools/rpi/labwc/autostart ~/.config/labwc/autostart
```

也可以用文件管理器完成备份和复制，按 `Ctrl+H` 显示隐藏的 `.config` 文件夹。
不要把 `start_rpi.py` 移到启动目录，它应保留在仓库的 `tools/` 中。
复制的 `autostart` 内唯一的启动命令是：

```bash
/usr/bin/python3 "$HOME/tf-inner-gui/tools/start_rpi.py" &
```

`$HOME` 自动使用当前登录用户的主目录，仓库仍需放在 `~/tf-inner-gui`。
文件已固定使用 Linux 换行符，不需要编辑或设置可执行权限；labwc 会通过 shell 读取它。
首次安装且没有旧 `autostart` 时，跳过备份那一行即可。复制完毕后在停机空闲时重启树莓派。
不要在原 GUI 仍运行时手动运行新的启动入口；只保留一个 TF GUI 启动入口。

启动行为：

- 桌面启动后等待3秒，检查 `origin/main`，然后全屏启动 GUI；生产运行期间不会自动检查、更新或重启。
- 只有当前分支为 `main` 且受 Git 管理的文件没有本地修改时才检查更新；不会执行强制覆盖、自动暂存、重置或变基。
- 下载最多等待45秒，不弹出账号密码窗口；断网、下载失败、超时或本地提交无法直接更新时，继续启动现有本地版本。
- 使用 `fetch` 下载，再用 `merge --ff-only` 更新程序和模型。超时只限制下载，不在文件更新途中强行终止 Git。
- 同一个启动脚本重复运行时，第二个实例会退出；这不替代“删除旧启动入口”，也不会关闭手动启动的 GUI。
- 不自动安装 Python 依赖，也不自动回滚一个成功下载但自身运行出错的新版本；推送生产更新前仍需完成测试。

更新情况和 GUI 日志仍在原位置，每次有效启动覆盖上次日志：

```bash
tail -n 100 ~/.local/state/tf_inner/startup.log
```

需要延长下载等待时间时，可以把自启动命令改为：

```bash
/usr/bin/python3 /home/y/tf-inner-gui/tools/start_rpi.py --update-timeout 90 &
```

临时禁止自动更新但保留开机启动时，把参数改为 `--skip-update`。

## 查看触摸屏分辨率

在树莓派桌面的终端中执行：

```bash
python3 -c "from PyQt5.QtWidgets import QApplication; a=QApplication([]); s=a.primaryScreen().size(); print(s.width(), s.height())"
```

界面使用布局管理器，会自动适应不同分辨率；当前设计预览尺寸为 1024×600。
