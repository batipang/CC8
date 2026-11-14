# CC8 安装 APK：已验证步骤

当前只验证下面这一台/固件；工程模式字样本身不算通道或权限已验证。先读 [兼容表/脱敏反馈](COMPATIBILITY.md) 和 [流程图/排障](INSTALL_TROUBLESHOOTING.md)。默认保护 staging 被该固件拒绝，显式共享装载路线仍有替换风险；未通过 probe / 签名 / 哈希 / 回执就停止，不自动卸载或改系统。

## 1. 适用范围与实际结果

本次实际设备：

| 项目 | 已验证值 |
| --- | --- |
| 底部标签型号 | TG_S6B01 |
| Android `ro.product.model` | AILABS_S6 |
| SDK | 27（Android 8.1 API 级别） |
| 固件版本 | 6.1.8-RS-20251125.1919 |
| 运行架构 | ARM 32 位 |
| 开发主机 | Apple Silicon Mac |

已经安装运行离线检测器 `com.happyhome.cc8diagnostics` 和语音助手 `com.happyhome.cc8assistant`。它们与原厂应用并存。本方法没有获得 Root，没有写入固件分区，没有修改 Bootloader，也没有停用原厂语音服务。

这里只记录当前设备上已成功的路线。工程模式是否能在其他固件开启、是否具有相同权限，必须重新验证；不是任意 CC8 的通用一键破解。

## 2. 设备与电脑准备

1. CC8 正常开机，保留原装电源。使用能传输数据的 USB 线连接 Mac。
2. 在 CC8 尝试说“天猫精灵，开启远程调试”。本机当时在屏幕角落出现“工程模式”字样，没有完整的 USB 调试开关页面。
3. 如果出现系统 USB 调试授权提示，在设备上确认。仅看到工程模式字样还不能证明 ADB 通道可用，后面必须执行探测。
4. 按[构建说明](DEVELOPMENT.md)准备 JDK、SDK、Gradle，构建安装助手和目标 APK。

不需要关机按组合键，不进入 BootROM、Preloader 或下载代理模式。此前底层读取尝试曾导致设备黑屏，需要复位恢复；它不是成功安装路线的前置步骤。

## 3. 构建并检查本地文件

从仓库根目录执行：

```sh
python3 device-access/installer-helper/build.py
python3 cc8-diagnostics/tools/build.py
python3 device-access/cc8_install.py --check
```

生成文件：

- `artifacts/cc8-installer-shell-1.0.jar`：临时 Java / DEX 安装助手。
- `artifacts/cc8-installer-shell-1.0.json`：助手 SHA-256 与 Java 源码 SHA-256。
- `artifacts/cc8-diagnostics-1.0.7.apk`：签名后的示例应用。
- 对应 APK 的 `.sha256` 文件。

`--check` 只检查电脑上的 ADB、AAPT、apksigner / JDK 和助手 / 源码哈希，不连接设备。修改 Java 安装助手后必须重新构建，不能复用旧哈希元数据。

## 4. 探测普通 Android 工程通道

```sh
python3 device-access/cc8_install.py --probe
```

成功应输出类似：

```json
{"shell_verified": true, "uid": 2000, "model": "AILABS_S6", "sdk": 27}
```

工具只接受一个在线的 USB `AILABS_S6`，使用 ADB transport ID 定位设备，排除网络 ADB 端点，避免误操作其他设备。设备端以工程诊断命令前缀进入普通 shell：

```sh
dumpsys activity com.ailabs.emode >/dev/null 2>&1; <普通 shell 命令>
```

前缀是这版原厂 adbd 限制下实测可用的命令路径，并不意味着获取 Root。UID 必须是 2000。型号、SDK、UID 或通道输出不符就停止。

## 5. 安装并启动 APK

代码检查发现：原成功路线将临时助手放在共享下载目录，存在被其他有存储权限的应用替换后以 shell UID 加载的风险。现在默认使用权限核验后的 `/data/local/tmp`；这台固件的安全策略会拒绝在该目录创建文件，工具明确停止，**不会自动回退**。

以下命令显式选择已验证的共享目录兼容路线 `--shared-staging`，只适用于你信任设备上现有应用的环境。共享目录不能提供受保护的装载保证，传输哈希也无法消除此类竞态；如果不接受此风险，保持默认模式并停止安装，不能靠关闭系统安全策略解决。

```sh
python3 device-access/cc8_install.py --install artifacts/cc8-diagnostics-1.0.7.apk --launch --shared-staging
```

安装语音助手：

```sh
python3 cc8-assistant/tools/build.py
python3 device-access/cc8_install.py --install artifacts/cc8-assistant-0.9.129.apk --launch --shared-staging
```

安装其他单 APK 时替换 `--install` 后的路径。不想启动就省略 `--launch`。没有桌面入口的 APK 不能使用 `--launch`。

安装报告保存到本机忽略目录 `device-access/private/apk-install-result-<随机值>.json`，每次独立新建，不覆盖历史报告。安装前就校验目录为当前用户的真实私有目录（0700），并预留 0600 原子报告；已有目录权限不私有或为链接时在探测设备之前停止，不自动 chmod。先核对本机目录的所有权/类型，再修正该专用目录权限。`--probe` / `--verify-apk` / `--check` 不建安装报告。

`--install` 把进度、兼容暂存提示和报告路径写入标准错误，标准输出保留结构化结果。进度输出关闭时，已开始的安装仍会核对系统回执和已装包摘要。若控制台未显示最终结果，先查看本机安装报告，再决定是否需要重试。

报告的 `report_state=prepared` 表示尚未开始安装，`result_unconfirmed` 表示已进入安装但尚无可保存的最终核验，不冒充未执行；`installation_verified` 才是正常安装函数的已验证结果，前台/音频仍按各自字段判断。若最后保存失败，命令明确提醒可能已完成安装、先查实际包再决定，不自动重复安装。旧固定文件名的报告保持不动，不自动迁移其权限。核对：

| 字段 / 提示 | 含义 |
| --- | --- |
| `CC8_APK_STREAM_VERIFIED` | 设备接收的 APK 字节数与 SHA-256 正确 |
| `CC8_PACKAGE_INSTALLED` | 原生安装 session 成功回调，包已可查询 |
| `installed: true` | 包管理器返回安装路径 |
| `installed_apk_hash_verified: true` | 已安装 APK 的哈希与本地签名 APK 相同 |
| `launched: true` | `--launch` 后通过前台 Activity 再次确认 |

受保护的临时目录若在系统安装调用之前拒绝写入，私有回执记录 `pre_dispatch_failed`、`not_started` 和固定原因 `protected_staging_denied`。若已发起系统安装但未得到完成确认，保留 `result_unconfirmed`，先读取实际已装包状态再决定后续步骤。

如果安装成功但启动未确认，应查看 `launch_error`、`launch_command_accepted` 与设备画面，不能将“命令已发送”当成“应用已在前台”。如果设备不允许读取已安装文件的哈希，工具会以非零状态退出并明确说明“已安装但验证不完整”，不会继续启动或报告完整成功；不会自动卸载应用。

麦克风、摄像头等运行时权限仍由用户在设备前台授权。仅安装不打开界面时不会启动录音或预览。助手自 0.5.3 起的普通 `--launch` 会遵循用户选择的自动语音行为；若只做维护检查，先不加 `--launch`，随后使用 `launch.py --no-auto-voice` 打开。摄像头只在前台明确的看图/拍照请求或检测页操作时启动，维护打开不触发。

## 6. 安装器内部流程

本机原厂 `adb install` / `pm install` 路径会拒绝安装，直接重试不能解决。实际使用的是系统已有的 `PackageInstaller` API：

1. 将本地 APK 读取为有界私有快照，AAPT 读取包名与启动 Activity；官方 apksigner 验证 SDK 27 签名，并只读核对已装同包名 APK 的完整证书集合。失败在任何设备写入前停止。可先用 `--verify-apk <APK>` 单独检查，详见 [SIGNING.md](SIGNING.md)。
2. 默认核验 `/data/local/tmp` 的 shell 所有权及目录权限，写入随机临时 JAR、设置 0400 并核验哈希；策略拒绝写入就停止。只有显式 `--shared-staging` 才沿用 `/sdcard/Download/.cc8-installer-<随机值>.jar`，该共享目录的替换风险仍存在。
3. 通过 `CLASSPATH=... app_process /system/bin Cc8Installer --install <包名> <字节数> <SHA256>` 启动助手，从标准输入流入 APK。
4. Java 助手再次检查 UID 2000、AILABS_S6、SDK 27，用 `com.android.shell` 上下文创建 full-install session。
5. 写入 `base.apk`，校验大小与哈希，`fsync` 后提交，最多等待 60 秒系统安装完成回调。失败或未提交的 session 会清理。
6. 查询安装包信息，并由电脑核对 `pm path` 和安装文件哈希；可选启动和前台校验。
7. 删除本次随机命名的临时助手文件。安装的应用继续保留。

`INSTALL_REPLACE_EXISTING` 仅允许正常更新，系统仍检查签名；它不绕过证书验证。

## 7. 常见问题

| 现象 | 处理 |
| --- | --- |
| 找不到设备、offline、unauthorized | 保持正常 Android 启动，检查数据线 / USB 接口及设备授权；不要进入下载模式 |
| `closed`、探测输出不完整 | 工程命令通道没有通过；确认工程模式与当前固件，停止后续安装 |
| 多台 CC8 在线 | 只保留目标设备的 USB 连接后重试 |
| `Protected staging is blocked` | 固件拒绝受保护目录写入，未尝试 APK 安装；参见上文兼容选项和残留风险，不修改系统策略 |
| 工具或 JAR 缺失 / 哈希不符 | 重新执行工具链准备与助手构建，勿手改哈希来跳过检查 |
| 安装 session 失败 | 检查存储空间、Android / ABI 兼容性、APK 是否完整以及更新证书是否匹配 |
| 更新提示签名不一致 | 使用原签名私钥重新构建；不要为更新擅自卸载已有应用 |
| APK 已装但没声音 / 录音失败 | 打开应用检查运行时权限、媒体音量及音频测试；安装成功不等于音频能力验证通过 |
| 黑屏、不枚举普通 Android | 停止安装流程，恢复设备正常启动；本安装工具不是救砖或固件恢复工具 |

目前只支持单个完整 APK，不处理 split APK、APKS、XAPK。检测器和助手采用 Java、无 Android 原生 ABI 依赖；第三方 APK 若只有 arm64 库或最低系统要求高于 SDK 27，仍可能无法运行。

## 8. 签名与备份

首次构建会生成本地开发签名，两个示例应用共用该签名文件。仓库不含现有设备应用的签名私钥，因此新克隆生成的 APK 通常不能直接覆盖原开发机签名的已安装版本。应在原开发机保留并安全管理原签名，或者使用不同 applicationId 做独立安装；不要为绕过冲突直接清除数据。

原签名备份、路径导入、安装前证书比对和失败处理见 [SIGNING.md](SIGNING.md)。安装器默认强制签名预检，不提供跳过选项。

安装器不会备份应用数据，也不会创建系统分区镜像。需要保留的应用数据应在更新前通过应用自身支持的方式导出。无需任何原厂分区镜像才能使用本方法。
