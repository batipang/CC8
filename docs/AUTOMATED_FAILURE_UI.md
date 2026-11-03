# CC8 隔离故障界面自动检查

`verify-failure-ui` 将当前源码的真实主机控制器产生的固定失败/恢复协议包，重放给实体 CC8 上的独立 QA 应用。它验证错误界面和请求代次隔离，**不是生产助手的真实云端、音频或整条语音验收**。

## 隔离方式

`tools/qa_variant.py` 只快照 Git 跟踪的 Android 应用输入及版本文件，在新私有目录构建，不复制配置、密钥、配对或运行数据。只改 applicationId、标签、USB 回环端口和三个权限声明；源码 namespace 与实际界面/协议处理逻辑保持不变。测试包为 `com.happyhome.cc8assistant.qa`，设备回环端口28765，生产包和18765端口不变。

移除 `RECORD_AUDIO`、`CAMERA`、`SYSTEM_ALERT_WINDOW`，拒绝 shared UID，并用实际产物的 aapt badging/xmltree 二次核对。以现有原签名引用构建；安装前再次与已安装生产助手的证书比较。脚本不生成新钥匙、不授权录音拍照、不改变生产包权限或数据。

## 运行

默认计划只需要标准库，不访问设备、模型或配置，不创建报告：

```sh
./cc8 verify-failure-ui --runtime-root /absolute/live/workspace
python tools/qa_variant.py --output /absolute/private/new-qa-build
```

先配置已有 SDK/JDK/Gradle，并用开发 Python 构建；输出必须是尚不存在的新目录，父目录为当前用户私有目录：

```sh
.toolchains/cc8/dev-venv/bin/python tools/qa_variant.py \
  --output /absolute/private/new-qa-build \
  --keystore /absolute/existing/original-signing.jks --execute
.toolchains/cc8/dev-venv/bin/python cc8 verify-failure-ui \
  --runtime-root /absolute/live/workspace \
  --qa-receipt /absolute/private/new-qa-build/qa-build.json --execute
# ASR/TTS固定故障改用真实本地HTTPS，仍不访问真实提供方
.toolchains/cc8/dev-venv/bin/python cc8 verify-failure-ui \
  --runtime-root /absolute/live/workspace \
  --qa-receipt /absolute/private/new-qa-build/qa-build.json \
  --http-fixture loopback_tls --execute
```

首次调试可显式加 `--limit 1`，不会把部分轮次计为完整48项。当前固件拒绝默认受保护目录写入；需要既有 [APK 安装兼容路线](APK_INSTALL.md) 时显式添加 `--shared-staging`。它仍存在共享暂存文件被其他有存储权限应用替换的风险，哈希不能消除此竞态；脚本不会自动选择或回退到该模式。

## 自动判定

`tools/qa_faults.py` 用实际 Bridge、ASR/Response worker、MiniMax Transport 和任务回执路径生成48项：ASR五类、Dot六类、TTS五类，各重复三次。提供方I/O为固定假对象，Dot不启动子进程，Transport显式使用无效测试凭证，不读取用户服务密钥、不访问网络。ASR输入仅固定数字正弦；这是错误路径，不是语音识别准确率测试。恢复步骤明确跳过合成/播放，只验证界面恢复。

显式`--http-fixture loopback_tls`改为在127.0.0.1临时端口运行有界TLS故障服务器：ASR15项、TTS15项通过实际requests、累计I/O预算、证书校验和生产控制器，分别注入超时、连接关闭、HTTP429/402/401。每项须同时核对一次实际服务端请求、正确阶段/类别、任务回执及无自动重试；仅生成选定的`--limit`用例。使用临时自签名证书和固定无效凭证，关闭代理环境、拒绝非本地ASR/TTS POST与重定向，不修改生产端点白名单或用户配置。正常恢复仍为UI专用路径，Dot18项仍为隔离模拟，不把这项选择说成真实云端故障或完整语音验收。

TLS模式仍在安装QA/操作屏幕之前生成协议包，再由相同实体QA界面重放。报告明确`provider_calls_before_ui_replay=true`、固定I/O模式和实际请求数，协议语料摘要绑定该模式及计数；执行工具额外绑定qa_http.py摘要。默认fake模式保持完全离线，计划模式即使选择TLS也只需标准库且不启动进程/网络。七项新回归覆盖完整矩阵、选定用例、非法输入、证书不受信任及实际fixture客户端遇到不同证书时拒绝、端点/重定向隔离及准备失败清理；尚未做新模式实机QA，不沿用此前48项重放的通过状态。

每项检查：

- 错误发布先于收尾，固定类别与指引无提供方异常正文泄漏；提供方调用次数无自动重试。
- 实机显示对应摘要，自动点开“查看详情”，读取完整固定指引并关闭；点击不会发起新任务、开麦或审批。
- Dot失败任务保留失败；TTS失败不改变控制器已收到的完成回执。
- 开始下一个固定请求后注入旧代次错误，界面仍处于新请求；新请求结束后为idle、任务completed。

界面树通过 UiAutomator stdout 有界读取，仅在内存中解析，不在共享目录或Mac保存原始XML。输入坐标来自唯一、可点击、包名正确且边界合法的固定QA标签；每次操作前核对QA前台且采集计数为零。后台只接收事件包，出现音频包或非预期交互立即失败。

## 设备门控与恢复

要求唯一正常Android AILABS_S6、SDK27、shell UID2000；所有命令重核同一USB target。QA包事先必须不存在，包列表独立确认；已存在时拒绝替换/删除。生产助手必须版本匹配、前台空闲，双端采集关闭、无播放/校准或未完请求，既有守护及backend归属匹配。与其他设备检查共享实际runtime的互斥锁。

安装成功后才将原助手置后台、以 `auto_start_voice=false` 打开QA。不重启生产后台或72小时观察器；报告记录这段计划内生产连接中断，不计为物理USB切换。

固定语料生成、证书预检和安装都可能耗时数秒，因此在证书预检前、安装前、切换界面前重新核对同一生产后台PID/实例及完整空闲条件。任一次发现用户开麦、取景、离开前台或实例变化，立即退出，不重新选择目标。尚未安装时不碰设备；已安装但尚未接管界面时只清理本轮同摘要QA，不Home或重开原助手。报告`owner_rechecks`只列已成功核对的边界；这降低准备期间的占用竞态，不保证跨多个ADB命令的原子交接。

结束或异常后检查QA仍是本轮新装的相同APK摘要，再force-stop并经 `cmd package uninstall` 移除，仅操作测试包；原助手以不开麦方式恢复前台，核对原backend PID/实例、就绪和采集关闭。即使安装返回异常，也检查是否已经实际提交，避免漏清理。清理失败不能报告通过。进程遭SIGKILL/整机断电时不能保证finally执行；先确认残留包身份和实际状态，不能盲目重装。

报告在 `device-access/private/ui-failure-checks/`，目录0700、文件0600、独立原子检查点，记录源码/产物/实际协议语料摘要、阶段、每项结果、清理与范围限制。不保存设备标识、PID/实例、音视频、原始界面树、密钥或聊天正文。任何未完成项、界面读取错误或恢复未确认都不能通过；`full_p0_acceptance` 固定为false。

尚需分别验证生产版本上线、真实网络/权限/播放故障、画面路径、真人语音与物理切换。本工具不会通过吊销生产权限、耗尽真实额度或重复执行工具任务制造故障。

## 本机执行记录（2026-11-03）

独立QA0.9.3/code38完成48/48：ASR15、Dot18、TTS15，均点开完整错误指引，下一请求完成、旧错误被忽略，结束清理核验通过；耗时614.238秒。报告 `qa-ui-20261102T222214Z-5dcb3d19.json` 只保存在私有目录，协议语料摘要 `93bf06fd90d2c0fd3db2d16f71581c81fd824475adf1b24a30b007931f77eefb`。

QA APK摘要 `516eebd0868a5eb9a1fcb82955bf640ab483c7de3d2c16bb209fb03246d7848a`；构建发生于提交前，回执如实标记dirty，应用输入及四个自动化模块逐文件摘要随后与提交 `85456d9` 一致核验。生产仍0.13.7/APK0.8.8，无部署替换；原backend实例恢复确认。受保护目录拒绝和初期兼容适配失败的报告保留；一次旧 `pm uninstall` 失败留下的QA包已核验同一摘要后经 `cmd package uninstall` 移除，未抹掉失败记录。
