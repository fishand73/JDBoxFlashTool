# JDBox_Athena

京东云雅典娜 AX6600（RE-CS-02）本地全自动备份、受保护 U-Boot/Factory 固件刷写与网络启动中断工具。默认连接
`http://192.168.68.1/`，在局域网内完成 Telnet 检测/开启、root 登录、设备校验、
GPT 与 p1-p26 备份、下载和 MD5/SHA256 校验。

工具**不包含自动恢复功能**，所有刷写操作默认都不自动重启。默认备份流程不会写入任何分区；
只有显式传入 `--flash-uboot` 或 `--flash-firmware`、通过全部强制校验并输入哈希绑定的确认短语后，
才会执行对应写入。

## 功能与安全边界

- 先探测 TCP 23；已经开启 Telnet 时不会调用管理 API。
- Telnet 未开启时，用后台密码调用 `/jdcapi` 的 `session/login` 获取临时
  `ubus_rpc_session`。
- 先尝试 r4546 的 `jdcapi.static/set_port_forward` 注入；端口仍未开放时，再尝试旧版
  `set_iptv_info`。两种策略只能执行内置的 `factory_hm info telnet 1` 和对应固件所需的
  `telnetd` 启动命令，CLI 不提供任意命令入口。
- 不相信 JSON-RPC 的“成功”提示；只有实际连通 TCP 23 才继续。
- root Telnet 密码默认与路由器后台密码相同。
- 默认 `split` 模式先验证 p1-p26 全部存在，并检查 p13 `APPSBL`、p15 `ART` 标签；
  然后备份主 GPT、备用 GPT 和 p1-p26。
- 每个 split 文件都在路由器端计算 MD5/SHA256，下载后在电脑端重新计算并逐项比对；
  校验成功后立即清理该临时文件。
- `raw` 固定读取 `/dev/mmcblk0` 前 **2555 MiB**。数据通过 TCP 直接流到电脑，绝不把
  2555 MiB 文件暂存在内置 p27；接收完成后会重新读取源区域并比对远端与本机的
  MD5/SHA256。
- 清理范围仅限带随机令牌的临时目录、Web 链接和临时进程。
- U-Boot 模式只接受项目锁定的
  `uboot-ipq60xx-jdcloud_re-cs-02-260816_142236_3011049.bin`：655,360 bytes，
  MD5 `6071da758bc06dc284ae94f7417a3617`，SHA256
  `fa4f13a4465a3271307ea7f6031f95b1e6420697539e4210721c2645541602cf`。
- 刷写前必须严格匹配 JDCloud IPQ6018、p13 `0:APPSBL`、p14 `0:APPSBL_1` 和分区大小；
  `--force-device` 对刷写无效。
- 刷写前自动生成并校验 p13/p14 的新备份。随后把镜像传到路由器 `/tmp`，再次核对大小、
  MD5 和 SHA256；输入确认短语后先写 p14 并回读校验，再写 p13 并回读校验。
- 刷写完成后生成 `uboot-flash-report.json`，但绝不自动重启。
- 已集成 `chenxin527/uBootEnter` 的网络中断协议：通过物理网卡广播
  `UBOOT:ABORT` 到 UDP 37541，监听 UDP 37540 的 `UBOOT:ABORTED` 回复，随后验证
  `/version` 并打开 U-Boot Web。此操作与备份/刷写互斥，也不会自动重启路由器。
- Factory 固件模式只接受项目锁定的
  `ones20250-main-pure-ipq60xx-jdcloud_re-cs-02-squashfs-factory-26.08.30-10.38.06.bin`：
  32,980,572 bytes，MD5 `6608cce1adc444db393d00ceb3256515`，SHA256
  `a2d706dc02a68159f90502d1b253c48bfb316e9e166033c520ec846b9094c1ab`。SHA256 与上游 Release
  的 `sha256sums.txt` 一致。
- 本地先校验固定哈希、ARM64 FIT、`jdcloud,re-cs-02` 标识，以及 6 MiB 偏移处的 SquashFS；
  同时重新校验最近一次完整备份中的 GPT、BOOTCONFIG、APPSBL、ART、HLOS、rootfs、WIFIFW 和
  rootfs_data。
- 固件先通过 `/upload` 进入 U-Boot 内存；U-Boot 必须再次返回 `FIT Image`、相同大小及 MD5。
  只有随后输入确认短语，工具才调用 `/result`，由配套 U-Boot 写入 `0:HLOS`、`rootfs` 并选择
  firmware slot 0。不会改写 GPT、APPSBL、ART 或系统 1。
- `/result` 不会自动重试；如果提交后连接中断，报告会标记 `write-result-unknown`，此时必须保持供电，
  不得再次提交或断电。

> 只应在你拥有或明确获准管理的雅典娜设备上使用。建议电脑用网线直连 LAN，操作期间
> 不让路由器接入互联网，避免固件自动更新。开启后的 Telnet 服务可能在工具退出后仍然
> 存在；备份完成后请按你的固件管理方式关闭 Telnet 或重启设备。

> 刷写 U-Boot 或系统固件有变砖风险。必须使用稳定电源和有线连接，确认已有完整 GPT+p1-p26 备份；
> 从第一次出现“正在写入”到两个分区回读校验完成之间绝对不要断电。

## 环境

- Python 3.9 或更高版本
- 备份与 U-Boot 刷写无第三方运行时依赖
- `--enter-uboot` 以及 `--flash-firmware` 自动进入 U-Boot 时需要可选依赖 Scapy；Windows 还需要安装 Npcap。
  如果已经手动进入 U-Boot Web，固件上传/刷写本身只使用 Python 标准库
- 原厂固件必须仍保留已知 `/jdcapi` 行为；修复漏洞的新固件可能无法自动开启 Telnet
- `split` 模式需要可写临时挂载点：优先 U 盘，其次自动使用 `/mnt/mmcblk0p27`
- `raw` 模式要求电脑允许路由器连接一个本机 TCP 端口；Windows 防火墙弹窗时需允许当前
  局域网访问

## 快速开始

### Windows 图形版（推荐）

已提供原生 Windows 图形界面，双击源码入口或从 PowerShell 启动：

```powershell
python athena_gui.py
```

界面提供四个互相独立的入口：

1. **自动备份 / 开启 Telnet**：支持 `split`、`raw`、`both`，Telnet 未开启时自动尝试
   内置的两种管理接口策略；
2. **刷写 U-Boot**：先备份 p13/p14，再执行设备、镜像、远端与回读校验；
3. **刷写 Factory 固件**：自动查找或手动选择完整 split 备份，连接不到 U-Boot Web 时可自动
   启动 uBootEnter；
4. **进入 U-Boot Web**：选择物理网卡后等待路由器上电或重启。

耗时操作都在后台线程执行，窗口会持续显示日志。密码只保存在当前进程内，不会显示在日志、
报告或命令行参数中。U-Boot 和 Factory 固件真正写入前，仍必须在独立红色警告窗口中完整输入
与镜像 SHA256 绑定的确认短语；取消或关闭确认窗口不会写入闪存。
图形版等待 U-Boot 启动或等待 Web 就绪时会启用“取消当前任务”，用于停止发包、监听和后续固件流程；
进入固件上传、校验或实际写入阶段后该按钮会自动禁用。

图形版启用 Windows Per-Monitor V2 高 DPI 模式，自动适配 125%、150%、200% 等系统缩放，
并在窗口移到不同缩放比例的显示器后更新 Tk 字体缩放。任务页支持滚动，小尺寸高 DPI 屏幕也能
访问底部操作按钮；右上角会显示当前检测到的缩放比例。

图形版默认在可执行文件所在目录（源码运行时为项目根目录）为每次运行新建带时间戳的目录，
避免覆盖旧备份或报告；也可以通过“输出父目录”手动选择其他位置。
“路由器临时挂载点”默认自动选择可用 U 盘，找不到时使用 `/mnt/mmcblk0p27`；“电脑局域网 IP”
默认根据路由器管理地址和系统路由自动检测。两个字段都可以手动填写以覆盖自动选择结果。

### 构建可双击运行的 Windows 程序

在 64 位 Windows PowerShell 中运行：

```powershell
.\build_windows.ps1
```

脚本会安装锁定范围内的 Scapy/PyInstaller，并生成：

```text
dist\JDBox_Athena_v0.5.6\JDBox_Athena_v0.5.6.exe
```

这是目录版程序，发布或移动时应保留整个 `dist\JDBox_Athena_v0.5.6` 目录。固定 U-Boot、Factory 固件、
Npcap 安装器、README 和第三方声明会一并打包。Scapy 会被包含在程序中，但 Npcap 是 Windows
网络驱动，仍须安装到系统；界面会检测状态，并可由用户点击“安装 Npcap”后确认 UAC。

已经自行准备好构建环境时，可跳过依赖安装：

```powershell
.\build_windows.ps1 -SkipInstall
```

### 命令行版

在项目目录运行：

```powershell
python athena_backup.py
```

程序只会交互询问一次：

```text
路由器后台/Telnet 密码:
```

原厂系统默认管理地址是 `http://192.168.68.1/`，Shell/API 用户名是 `root`，默认模式是推荐的
`split`。京东云官方没有统一的出厂管理密码：首次配置时会要求设置管理员密码，工具中应填写该密码
（它不一定与 Wi-Fi 密码相同）。

指定其他地址或输出目录：

```powershell
python athena_backup.py --url http://192.168.68.1/ --output D:\AthenaBackup
```

也兼容只传 IP 的旧写法：

```powershell
python athena_backup.py --host 192.168.68.1
```

完整 2555 MiB raw 前缀备份：

```powershell
python athena_backup.py --mode raw
```

如果电脑有多个网卡，明确告诉路由器连接哪个局域网 IP：

```powershell
python athena_backup.py --mode raw --pc-host 192.168.68.10
```

同时执行 split 与 raw：

```powershell
python athena_backup.py --mode both
```

## 刷写项目锁定的 U-Boot

确认项目根目录存在指定文件后运行：

```powershell
python athena_backup.py --flash-uboot --verbose
```

流程会先连接设备并重新备份 p13/p14；所有非破坏性检查完成后才显示如下哈希绑定确认：

```text
请输入 FLASH-UBOOT-FA4F13A4465A 以确认刷写:
```

只有完全一致地输入该短语才会开始写入。直接回车、关闭窗口或输入其他内容都不会写分区。
镜像可位于其他目录，但内容仍必须与锁定哈希完全相同：

```powershell
python athena_backup.py --flash-uboot --uboot-image D:\Images\uboot-ipq60xx-jdcloud_re-cs-02-260816_142236_3011049.bin
```

成功后检查新输出目录中的 `uboot-flash-report.json`，确认 `status` 为 `complete` 且 p13、
p14 的回读 SHA256 都与镜像一致。工具不会重启路由器。

## 刷写项目锁定的 Factory 固件

此功能通过配套 U-Boot Web 的原生 `/upload`、`/result` 接口完成，不在原厂 Linux 中直接 `dd`
系统分区。默认镜像就是项目根目录中的 ones20250 PURE Factory 文件，并强制验证上游发布哈希。

若 U-Boot Web 已经打开，可直接运行：

```powershell
python athena_backup.py --flash-firmware --uboot-web-url http://192.168.1.1/
```

若路由器仍在正常系统中，可指定连接 LAN 的有线网卡，由工具先运行 uBootEnter：

```powershell
python athena_backup.py --flash-firmware 7
```

看到“现在请给路由器通电或重启”后再重启路由器。工具会依次：

1. 自动选择并完整校验最近的 `Athena_AX6600_backup_*`；
2. 校验本地 Factory 文件格式及锁定 SHA256；
3. 确认目标是 U-Boot Web，再把文件上传到内存；
4. 比较 U-Boot 返回的类型、大小和 MD5；
5. 要求输入 `FLASH-FIRMWARE-A2D706DC02A6`；
6. 写 `0:HLOS`、`rootfs` 并生成 `firmware-flash-report.json`。

指定其他备份位置或同内容的镜像副本：

```powershell
python athena_backup.py --flash-firmware 7 `
  --firmware-backup D:\Code\JDBox_Athena\Athena_AX6600_backup_20260902_003551 `
  --firmware-image D:\Images\ones20250-main-pure-ipq60xx-jdcloud_re-cs-02-squashfs-factory-26.08.30-10.38.06.bin
```

默认刷完后停留在 U-Boot Web；确认需要直接启动新系统时才显式加入：

```powershell
python athena_backup.py --flash-firmware 7 --firmware-reboot
```

新系统的发布默认值为：管理地址 `192.168.10.1`、管理密码为空、Wi-Fi `OWRT`、密码
`12345678`。首次进入后应立即设置管理密码并修改无线密码。使用原厂 GPT 可以启动该固件，但原厂
`rootfs` 只有 60 MiB，overlay 可用空间会较小；本功能不会擅自重写 GPT。

## 免按 Reset 进入 U-Boot Web

这部分集成自 [chenxin527/uBootEnter](https://github.com/chenxin527/uBootEnter)。锁定的
U-Boot 镜像已经过本地扫描，确认包含 `UBOOT:ABORT` 和 `UBOOT:ABORTED` 两个协议标记。

先安装可选依赖：

```powershell
python -m pip install -e ".[uboot-enter]"
```

Windows 还必须从 [Npcap 官网](https://npcap.com/#download) 安装 Npcap。若普通终端无法发送
二层数据包，请使用管理员权限打开 PowerShell。

查看可用物理网卡及其索引：

```powershell
python athena_backup.py --list-interfaces
```

使用全部物理网卡等待 U-Boot 启动：

```powershell
python athena_backup.py --enter-uboot
```

程序开始显示“现在请给路由器通电或重启”后，再给路由器上电或手动重启。收到
`UBOOT:ABORTED` 后会等待 `/version` 就绪并打开默认浏览器。

推荐明确指定连接路由器 LAN 口的有线网卡：

```powershell
python athena_backup.py --enter-uboot 7
python athena_backup.py --enter-uboot "Realtek PCIe"
```

不打开浏览器或延长等待时间：

```powershell
python athena_backup.py --enter-uboot 7 --no-open-browser --enter-timeout 180
```

`uBootEnter` 不使用后台密码或 Telnet，也不会主动重启路由器。配套 U-Boot 的 `bootdelay`
建议至少为 3 秒；如果超时，请确认网线直连 LAN、选对网卡、Npcap 正常工作，并在工具运行后
再重启路由器。

指定 U 盘作为 split 临时区：

```powershell
python athena_backup.py --remote-target /mnt/sda1
```

查看全部公开参数：

```powershell
python athena_backup.py --help
```

首次在真实设备上运行时可加 `--verbose`。若 APPSBL/ART 标签因固件差异无法读取，先人工
核对 `device-info.txt`；只有确认设备与分区布局正确后才使用 `--force-device`。该选项也不能
跳过 p1-p26 完整性检查。

## 输出内容

默认目录名形如 `Athena_AX6600_backup_20260902_012345`：

```text
backup-info.json       本次执行状态与模式（不记录密码/session）
device-info.txt        型号、固件、块设备、挂载点等诊断信息
gpt-primary.bin        主 GPT 头与分区表（split）
gpt-backup.bin         备用 GPT 头与分区表（split）
p01_*.bin ... p26_*.bin
mmcblk0-prefix-2555MiB.img   raw 模式
preflash_p13_0_APPSBL.bin    U-Boot 刷写前备份
preflash_p14_0_APPSBL_1.bin  U-Boot 刷写前备份
uboot-flash-report.json      分步写入与回读校验报告
firmware-flash-report.json   U-Boot Web 双重校验与 Factory 写入结果
manifest.json
MD5SUMS
SHA256SUMS
```

每个文件校验完成后才会进入 manifest。中途失败时，之前已完成并校验的文件会保留，
`backup-info.json` 状态为 `incomplete`。

## 项目结构

```text
athena_backup.py                 源码目录直接运行入口
src/jdbox_athena/cli.py         参数、密码提示和日志
src/jdbox_athena/jdcapi.py      session/login 与两种 Telnet 策略
src/jdbox_athena/telnet_client.py  Python 3.13+ 可用的最小 Telnet 客户端
src/jdbox_athena/device.py      设备信息、p1-p26 与 GPT 几何校验
src/jdbox_athena/transfer.py    临时区、HTTP 下载、raw 直接流传输
src/jdbox_athena/flash.py       固定镜像校验、上传、双 APPSBL 刷写和回读
src/jdbox_athena/firmware_flash.py Factory 校验、备份复核、U-Boot Web 上传与写入
src/jdbox_athena/uboot_enter.py uBootEnter 数据包、网卡选择、回复与 Web 检测
src/jdbox_athena/backup.py      工作流编排与清理
src/jdbox_athena/integrity.py   MD5/SHA256 与 manifest
src/jdbox_athena/gui.py         Windows 图形界面、后台日志与写入确认对话框
athena_gui.py                    图形版源码入口
build_windows.ps1               PyInstaller 目录版构建脚本
tests/                           完全离线的基础测试
third_party/uBootEnter/LICENSE   上游 MIT 许可证
THIRD_PARTY_NOTICES.md           上游版本、许可与修改说明
```

也可以安装为命令：

```powershell
python -m pip install -e .
athena-backup --help
```

## 开发校验

这些检查不会连接路由器：

```powershell
python -m compileall -q athena_backup.py src tests
python -m unittest discover -s tests -v
python -m ruff check .
python -m mypy
python athena_backup.py --help
```

## 参考资料

- [r4546 / set_port_forward 方法与备份讨论](https://www.right.com.cn/forum/thread-8478775-1-1.html)
- [雅典娜免拆机刷机及 2555 MiB 备份讨论](https://www.right.com.cn/forum/forum.php?mod=viewthread&tid=8464665)
- [legacy set_iptv_info 方法](https://www.right.com.cn/forum/forum.php?mod=viewthread&tid=8461568)
- [chenxin527/uBootEnter](https://github.com/chenxin527/uBootEnter)
- [配套 uBootKit 对雅典娜的支持说明](https://github.com/chenxin527/uboot-qsdk12.5-build)
- [ones20250/Openwrt-AX6600](https://github.com/ones20250/Openwrt-AX6600)
- [PURE 26.08.30-10.38.06 Release](https://github.com/ones20250/Openwrt-AX6600/releases/tag/IPQ60XX-WIFI-YES-PURE-ones20250-main-26.08.30-10.38.06)
- [上游雅典娜刷机与恢复说明](https://github.com/ones20250/Openwrt-AX6600/blob/main/Docs/%E5%88%B7%E6%9C%BA%E6%95%91%E7%A0%96%E6%95%99%E7%A8%8B.md)

实现不加载论坛提供的远程 JavaScript，相关逻辑全部保存在本项目中。
