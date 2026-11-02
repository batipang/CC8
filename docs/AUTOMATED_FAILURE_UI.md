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
```

首次调试可显式加 `--limit 1`，不会把部分轮次计为完整48项。当前固件拒绝默认受保护目录写入；需要既有 [APK 安装兼容路线](APK_INSTALL.md) 时显式添加 `--shared-staging`。它仍存在共享暂存文件被其他有存储权限应用替换的风险，哈希不能消除此竞态；脚本不会自动选择或回退到该模式。

## 自动判定

`tools/qa_faults.py` 用实际 Bridge、ASR/Response worker、MiniMax Transport 和任务回执路径生成48项：ASR五类、Dot六类、TTS五类，各重复三次。提供方I/O为固定假对象，Dot不启动子进程，Transport显式使用无效测试凭证，不读取用户服务密钥、不访问网络。ASR输入仅固定数字正弦；这是错误路径，不是语音识别准确率测试。恢复步骤明确跳过合成/播放，只验证界面恢复。

每项检查：

- 错误发布先于收尾，固定类别与指引无提供方异常正文泄漏；提供方调用次数无自动重试。
- 实机显示对应摘要，自动点开“查看详情”，读取完整固定指引并关闭；点击不会发起新任务、开麦或审批。
- Dot失败任务保留失败；TTS失败不改变已经完成的真实任务回执。
- 开始下一个固定请求后注入旧代次错误，界面仍处于新请求；新请求结束后为idle、任务completed。

界面树通过 UiAutomator stdout 有界读取，仅在内存中解析，不在共享目录或Mac保存原始XML。输入坐标来自唯一、可点击、包名正确且边界合法的固定QA标签；每次操作前核对QA前台且采集计数为零。后台只接收事件包，出现音频包或非预期交互立即失败。

## 设备门控与恢复

要求唯一正常Android AILABS_S6、SDK27、shell UID2000；所有命令重核同一USB target。QA包事先必须不存在，包列表独立确认；已存在时拒绝替换/删除。生产助手必须版本匹配、前台空闲，双端采集关闭、无播放/校准或未完请求，既有守护及backend归属匹配。与其他设备检查共享实际runtime的互斥锁。

安装成功后才将原助手置后台、以 `auto_start_voice=false` 打开QA。不重启生产后台或72小时观察器；报告记录这段计划内生产连接中断，不计为物理USB切换。

结束或异常后检查QA仍是本轮新装的相同APK摘要，再force-stop并经 `cmd package uninstall` 移除，仅操作测试包；原助手以不开麦方式恢复前台，核对原backend PID/实例、就绪和采集关闭。即使安装返回异常，也检查是否已经实际提交，避免漏清理。清理失败不能报告通过。进程遭SIGKILL/整机断电时不能保证finally执行；先确认残留包身份和实际状态，不能盲目重装。

报告在 `device-access/private/ui-failure-checks/`，目录0700、文件0600、独立原子检查点，记录源码/产物/实际协议语料摘要、阶段、每项结果、清理与范围限制。不保存设备标识、PID/实例、音视频、原始界面树、密钥或聊天正文。任何未完成项、界面读取错误或恢复未确认都不能通过；`full_p0_acceptance` 固定为false。

尚需分别验证生产版本上线、真实网络/权限/播放故障、画面路径、真人语音与物理切换。本工具不会通过吊销生产权限、耗尽真实额度或重复执行工具任务制造故障。
