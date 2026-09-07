# JDBox Athena Flash Tool

京东云雅典娜 AX6600（RE-CS-02）一站式备份与刷机工具，提供 Windows 图形界面和命令行入口。

它可以在局域网内自动完成 Telnet 探测与开启、失败后恢复原厂 r4211 并重试、root 登录、设备识别、
GPT 与 p1-p26 备份、U-Boot 刷写、免按 Reset 进入 U-Boot Web、Factory 固件刷写，以及受保护的
rootfs 扩容。Windows 发布包已包含运行环境、原厂恢复固件、配套 U-Boot、Factory 固件、Scapy
和 Npcap 安装程序；正常使用不需要另外准备 Telnet 客户端、TFTP/HTTP 服务器或 uBootEnter，
浏览器也不是必需组件。

当前版本：**v0.6.0**

[下载 Windows 版本](https://github.com/fishand73/JDBoxFlashTool/releases) ·
[查看可视化教程](docs/JDBox_Athena_Windows_可视化操作教程.md)

> [!WARNING]
> 本工具只应在你拥有或明确获准管理的京东云雅典娜 AX6600 上使用。刷写 U-Boot、系统固件或
> GPT 都可能导致设备无法启动。请使用稳定电源和有线连接，先完成 `split` 备份并复制到另一块
> 磁盘；日志出现“正在写入”后绝对不要断电、拔线、刷新页面或重复提交。

> [!CAUTION]
> “扩容 rootfs + 刷固件”会重建分区布局，p19-p27 中的原有数据将不可继续使用，其中包括
> 原厂系统槽 1、`rootfs_data`、`plugin`、`log`、`swap` 和 `storage`。执行前必须先复制
> `storage` 中需要保留的文件。

## 功能一览

| 任务 | 自动完成的操作 | 写入范围 |
| --- | --- | --- |
| 自动备份 / 开启 Telnet | 探测并开启 Telnet；失败时可确认恢复原厂 r4211 后重试；随后校验设备并完成备份 | 通常不写；确认恢复时写原厂全量固件 |
| 刷写 U-Boot | 备份 p13/p14、校验锁定镜像、先写备用 APPSBL、再写主 APPSBL、逐个回读 | p14、p13 |
| 进入 U-Boot Web | 通过网卡发送启动中断包，等待并验证 U-Boot Web | 不写设备分区 |
| 刷写 Factory 固件 | 校验备份与锁定固件、上传 U-Boot 内存复核、写系统槽 0 | `0:HLOS`、`rootfs` |
| 扩容 rootfs + 刷固件 | 从本机备份生成专属 GPT，扩容后连续刷入 Factory 固件 | 主/备 GPT、`0:HLOS`、`rootfs` |

所有危险写入都必须通过设备、分区、文件格式和哈希校验，并在独立确认窗口中完整输入现场生成的
确认短语。直接回车、输入错误、取消或关闭确认窗口都不会开始写入。

![rootfs 扩容界面](docs/images/07-resize-rootfs.png)

## Windows 快速开始

发布包面向 64 位 Windows，使用时不需要安装 Python。

1. 从 [Releases](https://github.com/fishand73/JDBoxFlashTool/releases) 下载并解压整个
   `JDBox_Athena_v0.6.0` 目录，不要只复制 EXE。
2. 用网线连接电脑和路由器 LAN 口，建议操作期间断开路由器的互联网连接。
3. 双击 `JDBox_Athena_v0.6.0.exe`。
4. 第一次使用先选择“自动备份 / 开启 Telnet”，使用推荐的 `split` 模式。
5. 确认备份报告为 `complete`，并把整个备份目录复制到其他磁盘。
6. 再根据需要刷写 U-Boot、刷写 Factory 固件，或扩容 rootfs 后刷入固件。

### 常用默认值

| 设置 | 默认值 | 说明 |
| --- | --- | --- |
| 京东云原厂管理地址 | `http://192.168.68.1/` | 修改过网段时填写实际地址 |
| 后台 / Telnet 用户名 | `root` | Shell/API 默认用户名 |
| 后台 / Telnet 密码 | 空 | 填写首次配置路由器时设置的管理员密码；官方没有统一出厂密码 |
| Telnet 端口 | `23` | 一般无需修改 |
| U-Boot Web 地址 | `http://192.168.1.1/` | 配套 U-Boot 默认地址 |
| 路由器临时挂载点 | 自动选择 | 优先可写 U 盘，其次 `/mnt/mmcblk0p27` |
| 电脑局域网 IP | 自动检测 | 根据管理地址和系统路由选择 |
| rootfs 目标大小 | `1024 MiB` | 可选择 512、1024、2048、8192 MiB |
| 输出父目录 | EXE 所在目录 | 每次运行新建带时间戳的目录，不覆盖旧结果 |
| Telnet 失败恢复 | 开启 | 使用内置、固定哈希的 JDCOS `4.3.0.r4211`；真正写入前仍需确认 |

新 Factory 固件首次启动后的默认值为：管理地址 `192.168.10.1`、管理密码为空、Wi-Fi 名称
`OWRT`、Wi-Fi 密码 `12345678`。首次登录后请立即设置管理密码并修改无线密码。

### Npcap、管理员权限与高 DPI

- 自动进入 U-Boot 需要 Npcap。发布包附带安装程序，界面检测到未安装时可直接点击“安装 Npcap”。
- 普通备份通常不需要管理员权限；若底层网卡通信失败，可右键 EXE 选择“以管理员身份运行”。
- 图形界面启用 Windows Per-Monitor V2，支持 125%、150%、200% 等缩放比例，并可在不同
  DPI 的显示器之间自动调整。
- 任务页支持滚动；耗时操作在后台线程执行，运行日志会持续更新。

### 什么时候可以取消

“取消当前任务”在等待 U-Boot 启动、等待 Web 就绪，以及原厂恢复固件上传到 `/tmp` 的安全阶段
启用。点击后会停止当前安全阶段和后续刷写流程。原厂恢复固件完成校验并提交写入后，以及其他
实际写入阶段，该按钮会自动禁用。

如果取消前已经收到 `UBOOT:ABORTED`，路由器可能仍停留在 U-Boot，可手动访问
`http://192.168.1.1/` 或重新启动路由器。

## 推荐操作顺序

1. **备份**：执行 `split`，确认主/备 GPT、p1-p26、manifest 和校验清单完整。
2. **另存备份**：将整个备份目录复制到另一块磁盘或可信存储。
3. **刷 U-Boot（按需）**：完成 p13/p14 回读校验后再继续。
4. **进入 U-Boot**：可以单独执行，也可以让 Factory/扩容流程自动等待并中断启动。
5. **选择系统操作**：
   - 不改变分区大小：选择“刷写 Factory 固件”；
   - 需要更大 overlay：选择“扩容 rootfs + 刷固件”。
6. **核对报告**：所有相关报告均为 `complete` 后，再启动新系统。

如果只需要备份，完成第 1、2 步即可，不需要执行任何写入操作。

## 备份模式

| 模式 | 内容 | 特点 |
| --- | --- | --- |
| `split` | 主 GPT、备用 GPT、p1-p26 独立文件 | 首次使用和刷写前必做，推荐 |
| `raw` | `/dev/mmcblk0` 前 2555 MiB | 直接通过 TCP 流向电脑，不占用路由器内部临时空间 |
| `both` | 同时执行 split 和 raw | 占用空间和时间更多 |

`split` 会先检查 p1-p26 是否完整以及 APPSBL/ART 标签是否匹配。每个文件先在路由器端计算
MD5/SHA256，下载后再由电脑复算；只有校验成功的文件才会写入 manifest。

`raw` 不会把 2555 MiB 镜像暂存在 p27。传输完成后，程序重新读取源区域，并比较远端与本机的
MD5/SHA256。

## 自动开启 Telnet 的边界

- 已经可以连接 TCP 23 时，不调用管理 API。
- Telnet 未开启时，使用后台密码调用 `/jdcapi` 的 `session/login` 获取临时会话。
- 先尝试 r4546 的 `jdcapi.static/set_port_forward` 策略，不兼容时再尝试旧版
  `set_iptv_info` 策略。
- 两种策略只能执行程序内置的 Telnet 开启命令；CLI 不提供任意命令执行入口。
- JSON-RPC 返回成功不代表 Telnet 已开启，只有实际连接 TCP 23 成功后才会继续。
- 两种策略均失败时，图形界面默认准备内置 JDCOS `4.3.0.r4211`。只有本地固定哈希、路由器端
  上传大小/MD5 和 `firmware_check` 全部通过后，才显示独立的降级确认窗口。
- 完整输入 `DOWNGRADE-R4211-...` 并确认后，工具调用原厂 `local_upgrade_action`，等待路由器
  重启和管理接口恢复，再重新登录并尝试开启 Telnet。
- Telnet 密码默认与路由器后台密码相同。服务可能在工具退出后继续存在，备份完成后请按当前
  固件的管理方式关闭 Telnet 或重启设备。

原厂固件必须仍保留已知 `/jdcapi` 行为；已经修复相关接口的版本可能无法自动开启 Telnet。

### Telnet 失败后的原厂 r4211 恢复

该备用流程仅用于“自动备份 / 开启 Telnet”和“刷写 U-Boot”，并且只在管理登录成功、两种
Telnet 开启策略均失败后触发。密码错误或管理接口无法登录时不会自动刷固件。

恢复包是完整签名 FIT。工具会先通过只读的 `web_get_router_info` 确认型号为 `RE-CS-02`，再使用
原厂页面的 `/cgi-bin/luci-upload`、`firmware_check` 和 `local_upgrade_action` 接口升级。它包含
官方 U-Boot、启动链和系统固件，可能覆盖已安装的第三方 U-Boot，也可能清除路由器设置。提交后
不能取消，写入和重启期间不能断电；如果不希望自动准备此流程，可在任务设置中取消勾选
“Telnet 开启失败时，使用内置原厂 r4211 恢复后重试”。

若提交请求后连接意外中断，报告会标记为 `write-result-unknown`。此时工具不会重复提交，请保持
供电至少 10 分钟，再检查当前版本。

## 受保护地刷写 U-Boot

刷写前会重新备份 p13/p14，并严格检查设备为 JDCloud IPQ6018、分区标签为 `0:APPSBL` 和
`0:APPSBL_1`、分区大小正确、镜像与锁定哈希一致。`--force-device` 对刷写无效。

确认后按以下顺序执行：

1. 将镜像传到路由器 `/tmp`，再次核对大小、MD5 和 SHA256；
2. 输入 `FLASH-UBOOT-...` 确认短语；
3. 先写 p14 并回读校验；
4. 再写 p13 并回读校验；
5. 生成 `uboot-flash-report.json`，保持路由器不自动重启。

命令行示例：

```powershell
python athena_backup.py --flash-uboot --verbose
```

## 免按 Reset 进入 U-Boot Web

工具集成 [chenxin527/uBootEnter](https://github.com/chenxin527/uBootEnter) 的网络启动中断协议：
通过物理网卡向 UDP 37541 广播 `UBOOT:ABORT`，监听 UDP 37540 的 `UBOOT:ABORTED` 回复，
随后验证 `/version` 是否就绪。

```powershell
# 查看物理网卡
python athena_backup.py --list-interfaces

# 使用全部物理网卡等待
python athena_backup.py --enter-uboot

# 使用指定网卡索引，不自动打开浏览器
python athena_backup.py --enter-uboot 7 --no-open-browser
```

开始显示“现在请给路由器通电或重启”后，再给路由器上电或手动重启。该功能不会主动重启设备，
也不需要后台密码或 Telnet。

## 刷写锁定的 Factory 固件

Factory 固件通过配套 U-Boot Web 的 `/upload` 和 `/result` 接口刷写，不在原厂 Linux 中直接
`dd` 系统分区。

写入前会完成以下检查：

1. 验证完整 split 备份中的恢复关键文件；
2. 验证 Factory 固件的固定哈希、ARM64 FIT、`jdcloud,re-cs-02` 标识和 SquashFS 结构；
3. 将固件上传到 U-Boot 内存，要求 U-Boot 返回 `FIT Image`、相同大小和相同 MD5；
4. 输入 `FLASH-FIRMWARE-...` 确认短语后，才写入系统槽 0 的 `0:HLOS` 和 `rootfs`。

单独刷 Factory 不会修改 GPT、APPSBL、ART 或系统槽 1。默认刷写完成后停留在 U-Boot Web。

```powershell
# U-Boot Web 已经就绪
python athena_backup.py --flash-firmware --uboot-web-url http://192.168.1.1/

# 先使用第 7 块网卡自动进入 U-Boot
python athena_backup.py --flash-firmware 7

# 明确指定备份并在成功后自动重启
python athena_backup.py --flash-firmware 7 `
  --firmware-backup D:\AthenaBackup\Athena_AX6600_backup_20260902_003551 `
  --firmware-reboot
```

如果 `/result` 提交后连接中断，报告会标记 `write-result-unknown`。此时不要断电或再次提交，
应保持供电至少 10 分钟，再根据 U-Boot Web、串口或启动状态人工判断。

## 扩容 rootfs + 刷固件

原厂 GPT 中的 `rootfs` 只有 60 MiB。图形界面固定提供 512、1024、2048、8192 MiB 四档，
默认选择 1024 MiB；命令行必须显式指定目标大小。

### 分区调整规则

工具不会套用其他设备的通用 GPT，而是从所选备份动态生成本机专属 GPT。
所有分区 GUID 均保持不变。

| 分区 | 处理方式 |
| --- | --- |
| p1-p17 | 分区项逐字节保留，位置和 GUID 不变 |
| p18 `rootfs` | 起点不变，终点扩展到所选容量 |
| p19-p26 | 大小和 GUID 不变，整体向后顺移 |
| p27 `storage` | 起点向后顺移、终点不变，容量相应缩小 |

### 写入前校验

- 复算主/备 GPT 和 p1-p26 共 28 个备份文件的 SHA256；
- 校验主、备用 GPT 的头部 CRC32、分区项 CRC32、磁盘 GUID 和分区项一致性；
- 校验 27 个分区标签、关键分区几何，以及 p1-p26 文件大小与 GPT 声明一致；
- 重新计算新 GPT 的 CRC32，并再次解析验证；
- 在第一次危险确认前，提前完成锁定 Factory 固件及恢复备份校验。

新 GPT 上传到 U-Boot 内存后，还必须被识别为 `GPT (Single Image for eMMC device)`，且大小
和 MD5 与本地一致。随后需要完成两次独立确认：

1. 输入 `WRITE-GPT-...`，写入主/备 GPT；该阶段固定不重启；
2. 输入 `FLASH-FIRMWARE-...`，继续刷入锁定 Factory 固件。

```powershell
python athena_backup.py --resize-rootfs 1024 --resize-interface 7 `
  --firmware-backup D:\AthenaBackup\Athena_AX6600_backup_20260902_003551
```

工具只允许扩容，不允许把较大的 rootfs 缩回较小档位。完成后，
`rootfs-resize-report.json` 和 `firmware-flash-report.json` 都必须为 `complete`。

> [!WARNING]
> 如果 GPT 已经写入成功，但 Factory 刷写失败，请不要让路由器启动旧系统。保持在 U-Boot Web，
> 使用“刷写 Factory 固件”重新提交锁定镜像。

## 锁定镜像

程序只接受下列内容完全一致的镜像副本，文件名可以不同，但大小和哈希必须匹配。

<details>
<summary>查看 U-Boot 与 Factory 镜像信息</summary>

### U-Boot

- 文件：`uboot-ipq60xx-jdcloud_re-cs-02-260816_142236_3011049.bin`
- 大小：655,360 bytes
- MD5：`6071da758bc06dc284ae94f7417a3617`
- SHA256：`fa4f13a4465a3271307ea7f6031f95b1e6420697539e4210721c2645541602cf`

### Factory 固件

- 文件：`ones20250-main-pure-ipq60xx-jdcloud_re-cs-02-squashfs-factory-26.08.30-10.38.06.bin`
- 大小：32,980,572 bytes
- MD5：`6608cce1adc444db393d00ceb3256515`
- SHA256：`a2d706dc02a68159f90502d1b253c48bfb316e9e166033c520ec846b9094c1ab`

### Telnet 失败备用原厂固件

- 文件：`JDCOS-JDC02-4.3.0.r4211-9e319914fce041a0519e4445c4b77372-single-signed.img`
- 大小：37,064,796 bytes
- MD5：`9e319914fce041a0519e4445c4b77372`
- SHA256：`1f568d59da273dbeee36cac4210e8f4d20c9468f2933bbfa068e4c16e6944e5d`

</details>

## 从源码运行

需要 Python 3.9 或更高版本。备份与 U-Boot 刷写仅使用标准库；自动进入 U-Boot 需要 Scapy，
Windows 还必须安装 Npcap 驱动。

```powershell
# 图形界面
python -m pip install -e ".[windows-gui]"
python athena_gui.py

# 命令行默认执行 split 备份
python athena_backup.py

# 查看全部参数
python athena_backup.py --help
```

常用备份参数：

```powershell
# 指定管理地址和输出目录
python athena_backup.py --url http://192.168.68.1/ --output D:\AthenaBackup

# raw 或 split+raw
python athena_backup.py --mode raw
python athena_backup.py --mode both

# 手动指定路由器临时挂载点或电脑局域网 IP
python athena_backup.py --remote-target /mnt/sda1
python athena_backup.py --mode raw --pc-host 192.168.68.10
```

密码省略时会在终端安全询问，不建议通过 `--password` 直接传入，以免被命令历史保存。

## 构建 Windows 程序

在 64 位 Windows PowerShell 中运行：

```powershell
.\build_windows.ps1
```

构建结果：

```text
dist\JDBox_Athena_v0.6.0\JDBox_Athena_v0.6.0.exe
```

这是 PyInstaller 目录版程序，发布或移动时必须保留整个 `JDBox_Athena_v0.6.0` 目录。
程序目录包含固定 U-Boot、Factory 固件、原厂 r4211 恢复固件、Npcap 安装器、README 和第三方
声明，但不包含可视化教程。

已经准备好构建环境时，可以跳过依赖安装：

```powershell
.\build_windows.ps1 -SkipInstall
```

## 输出文件

每次任务都会创建独立的时间戳目录，例如 `Athena_AX6600_backup_20260902_012345`。

| 文件 | 用途 |
| --- | --- |
| `backup-info.json` | 备份状态与模式，不记录密码和 session |
| `device-info.txt` | 型号、固件、块设备、挂载点等诊断信息 |
| `gpt-primary.bin` / `gpt-backup.bin` | 主、备用 GPT 备份 |
| `p01_*.bin` … `p26_*.bin` | split 分区备份 |
| `mmcblk0-prefix-2555MiB.img` | raw 前缀镜像 |
| `manifest.json` / `MD5SUMS` / `SHA256SUMS` | 文件清单和完整性校验 |
| `preflash_p13_*.bin` / `preflash_p14_*.bin` | U-Boot 刷写前备份 |
| `uboot-flash-report.json` | 双 APPSBL 写入与回读报告 |
| `firmware-flash-report.json` | Factory 内存复核与写入报告 |
| `gpt-rootfs-目标MiB.bin` | 从本机备份生成的设备专属 GPT |
| `rootfs-resize-report.json` | GPT 几何变化、远端校验与写入报告 |
| `official-firmware-upgrade-report.json` | Telnet 失败后原厂 r4211 的校验、提交和重启状态 |

中途失败时，已经完成并通过校验的文件会保留；未完整完成的备份会在 `backup-info.json` 中标记为
`incomplete`，不能作为后续刷写的合格恢复备份。

## 项目结构

```text
athena_gui.py                         Windows 图形界面入口
athena_backup.py                      命令行入口
build_windows.ps1                     PyInstaller 构建脚本
src/jdbox_athena/gui.py               图形界面、后台任务和风险确认
src/jdbox_athena/backup.py            备份工作流与清理
src/jdbox_athena/jdcapi.py            登录与两种 Telnet 开启策略
src/jdbox_athena/official_upgrade.py  原厂 r4211 校验、上传、升级和重启等待
src/jdbox_athena/device.py            设备、分区和磁盘几何校验
src/jdbox_athena/transfer.py          HTTP 下载与 raw 流式传输
src/jdbox_athena/flash.py             U-Boot 校验、双 APPSBL 写入与回读
src/jdbox_athena/firmware_flash.py    Factory 校验和 U-Boot Web 写入
src/jdbox_athena/partition_resize.py  GPT 解析、生成、全备份校验与受保护写入
src/jdbox_athena/uboot_enter.py       网卡选择、启动中断和 Web 检测
src/jdbox_athena/integrity.py         MD5/SHA256 与 manifest
tests/                                完全离线的自动测试
```

## 开发校验

以下检查不会连接路由器：

```powershell
python -m compileall -q athena_backup.py athena_gui.py src tests
python -m unittest discover -s tests -v
python -m ruff check .
python -m mypy
python athena_backup.py --help
```

## 已知限制

- 只支持京东云雅典娜 AX6600（RE-CS-02 / IPQ6018）和项目锁定的 U-Boot/Factory 镜像。
- 不提供自动恢复或一键回滚，备份文件需要用户自行妥善保存。
- 自动开启 Telnet 依赖原厂固件仍保留兼容的 `/jdcapi` 行为。
- 原厂 r4211 备用恢复会写入完整官方固件，无法保证保留第三方 U-Boot，也无法绕过原厂平台校验。
- 自动进入 U-Boot 依赖兼容的配套 U-Boot、Scapy 和 Npcap。
- rootfs 调整是破坏性重建布局，不会搬移 p19-p27 的旧文件系统，也不支持缩小。
- 工具不能消除刷机风险；真实写入前仍应核对设备、镜像哈希、分区变化和确认短语。

## 参考与致谢

- [r4546 / set_port_forward 方法与备份讨论](https://www.right.com.cn/forum/thread-8478775-1-1.html)
- [雅典娜免拆机刷机及 2555 MiB 备份讨论](https://www.right.com.cn/forum/forum.php?mod=viewthread&tid=8464665)
- [legacy set_iptv_info 方法](https://www.right.com.cn/forum/forum.php?mod=viewthread&tid=8461568)
- [chenxin527/uBootEnter](https://github.com/chenxin527/uBootEnter)
- [chenxin527/uboot-qsdk12.5-build](https://github.com/chenxin527/uboot-qsdk12.5-build)
- [ones20250/Openwrt-AX6600](https://github.com/ones20250/Openwrt-AX6600)
- [PURE 26.08.30-10.38.06 Release](https://github.com/ones20250/Openwrt-AX6600/releases/tag/IPQ60XX-WIFI-YES-PURE-ones20250-main-26.08.30-10.38.06)
- [上游雅典娜刷机与恢复说明](https://github.com/ones20250/Openwrt-AX6600/blob/main/Docs/%E5%88%B7%E6%9C%BA%E6%95%91%E7%A0%96%E6%95%99%E7%A8%8B.md)

第三方许可和修改说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。程序不会加载论坛提供的
远程 JavaScript，相关逻辑均保存在本项目中。
