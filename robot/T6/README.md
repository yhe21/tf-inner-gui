# Epson T6

- 主程序：[Main.prg](Main.prg)。
- 来源：用户上传的 `C:\Users\y\Downloads\Main.prg`，原文件修改时间为
  2026-09-11；2026-09-14 收入当前 Git 仓库。
- 本次按原文件复制，复制后已进行 SHA-256 校验，内容未修改；Downloads 中的原文件保留。
- 该程序包含 `T6_Cycle`、`Search_Nameplate`、`Dump_Tray`、`Feeder_Control`
  以及与 VT6、glue/stamp 工位的协调流程。

这不是与 Raspberry Pi 建立 TCP 连接的 VT6 程序；后者位于
[../VT6/Main.prg](../VT6/Main.prg)。VT6 的 `RpiNgStopReq` 和盘号防抖修改没有移植到此文件。

此处没有完整的 RC+ 项目、点位或 I/O 定义。使用时必须配合这台 T6 自己的项目配置；
加入 Git 不代表已经编译、下载或运行到控制器。
