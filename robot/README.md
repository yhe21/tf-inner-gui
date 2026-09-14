# Epson 机器人程序

两个控制器的主程序按机器分别保存，不要混用：

- [VT6/Main.prg](VT6/Main.prg)：与 Raspberry Pi 通讯、取放 part/NP、正常摆盘的 VT6 程序。
  保留当前版本的盘号防抖和 `RpiNgStopReq` 延后停机逻辑。
  TCP 设置、Memory I/O 标签和使用注意事项见 [VT6 说明](VT6/README.md)。
- [T6/Main.prg](T6/Main.prg)：用户上传的另一台 Epson T6 程序，包含
  nameplate tray、inner 和 glue/stamp 的协调流程。
  本次只复制归档，没有修改其程序逻辑。来源见 [T6 说明](T6/README.md)。

原来的 `robot/Main.prg` 已移动至 `robot/VT6/Main.prg`，不再保留重复入口。
树莓派 GUI 的入口仍为 `tf_gui/main.py`，此次整理不改变树莓派启动方式。

这里只保存已提供的主程序，不是完整 Epson RC+ 项目。点位、工具、I/O 标签和
控制器配置仍属于各自机器，导入前必须确认控制器型号及对应项目，不能把一个机器的
`Main.prg` 加载到另一台机器上。

从仓库根目录运行机器人离线检查：

```text
python -m unittest discover -s robot/tests -v
```

这些检查不会驱动机器人，也不替代 RC+ 编译和现场验证。
