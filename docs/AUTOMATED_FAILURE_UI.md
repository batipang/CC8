# CC8 隔离故障界面自动检查

`verify-failure-ui` 将当前源码的真实主机控制器产生的固定失败/恢复协议包，重放给实体 CC8 上的独立 QA 应用。它验证错误界面和请求代次隔离，**不是生产助手的真实云端、音频或整条语音验收**。

## 隔离方式

`tools/qa_variant.py` 只快照 Git 跟踪的 Android 应用输入及版本文件，在新私有目录构建，不复制配置、密钥、配对或运行数据。只改 applicationId、标签、USB 回环端口和三个权限声明；源码 namespace 与实际界面/协议处理逻辑保持不变。测试包为 `com.happyhome.cc8assistant.qa`，设备回环端口28765，生产包和18765端口不变。

移除 `RECORD_AUDIO`、`CAMERA`、`SYSTEM_ALERT_WINDOW`，拒绝 shared UID，并用实际产物的 aapt badging/xmltree 二次核对。以现有原签名引用构建；安装前再次与已安装生产助手的证书比较。脚本不生成新钥匙、不授权录音拍照、不改变生产包权限或数据。

## 运行

### Mac 本机故障和恢复检查

```sh
./cc8 verify-failure-ui --host-only
.toolchains/cc8/dev-venv/bin/python cc8 verify-failure-ui --host-only --execute
```

第一条仅列计划。第二条默认用 `loopback_business` 和 `local_stdio` 执行全部 48 项，可用 `--http-fixture loopback_tls` 检查 HTTP 状态错误和连接故障。命令向标准输出写一份 JSON，包含选择的模式、用例数、请求计数和摘要。检查失败时退出码为 2，仅输出异常类型。该入口无需运行目录、APK 或 USB 设备，不访问当前运行实例，不录音、播放或调用真实提供方。实体界面的验证另走下方流程。

较早的矩阵使用模拟结构化 504 验证 Dot 超时分类。候选后台 0.14.45／APK code65 对应源码 `1033887` 已分别运行两套本机矩阵，各 48 项通过。两套均收到 30 次 ASR/TTS 故障请求、33 次初始 Dot 请求、48 次后续 Dot 请求和 3 次拒绝审批。业务错误模式还核对了 30 次独立的 ASR/TTS 恢复请求。

### 实体 CC8 界面重放

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

显式 `--http-fixture loopback_tls` 在 127.0.0.1 临时端口运行有界 TLS 服务。ASR、TTS 各 15 项经过实际 requests、累计 I/O 预算、证书校验和生产控制器，分别注入超时、连接关闭、HTTP 429 / 402 / 401。每项核对恰好一次故障请求、正确阶段和类别、任务回执及无自动重试。随后在同一个识别器或合成器上单独发起一次恢复探针，核对固定文本或完整数字 PCM，以及请求槽清空。探针只访问回环接口，PCM 仅在内存中校验。`--limit` 同时限制故障和恢复探针。临时自签名证书、固定无效凭证和端点限制适用于两类请求。Dot 默认使用隔离模拟，可显式选择本地 stdio。

TLS 模式在安装 QA 前生成协议包，再由实体 QA 界面重放。报告用 `provider_calls_before_ui_replay=true` 标注时序，单列故障和恢复请求数。两种 TLS 模式的语料摘要均绑定模式和实际请求计数，执行工具额外绑定 `qa_http.py` 摘要。默认 fake 保持完全离线，计划模式只需标准库。下方实机记录对应当时的工具版本。

每项检查：

- 错误发布先于收尾，固定类别与指引无提供方异常正文泄漏；提供方调用次数无自动重试。
- 实机显示对应摘要，自动点开“查看详情”，读取完整固定指引并关闭；点击不会发起新任务、开麦或审批。
- Dot失败任务保留失败；TTS失败不改变控制器已收到的完成回执。
- 开始下一个固定请求后注入旧代次错误，界面仍处于新请求；新请求结束后为idle、任务completed。

界面树通过 UiAutomator stdout 有界读取，仅在内存中解析，不在共享目录或Mac保存原始XML。输入坐标来自唯一、可点击、包名正确且边界合法的固定QA标签；每次操作前核对QA前台且采集计数为零。后台只接收事件包，出现音频包或非预期交互立即失败。

显式 `--dot-fixture local_stdio` 让 Dot 的 18 个故障用例经正式 JSON-RPC 客户端、进程守护和响应控制器处理。它启动无用户环境变量的固定 Python 子进程，无需 Codex 登录。其中三次保持管道打开但不给 `turn/start` 回执，使用隔离实例的 1 秒请求预算触发生产客户端的真实截止时间。主机核对错误指引和任务阶段均提示结果待核对、原进程在显式恢复前已退出、新请求使用新进程。另有三次关闭 stdio 管道、三次发送审批请求并核对客户端返回拒绝，其余九次返回结构化的限流、额度或权限错误。子进程独立记录固定用例编号和请求计数，校验每次只提交一次、仅在发起明确的新请求后恢复。报告将未应答启动计入 `withheld_start_acks`，同时保存测试模式、子进程源码摘要、请求和拒绝计数。默认 `fake` 仍可在无进程、无网络的环境中运行。

可与证书验证的本地 ASR/TTS 故障模式同时启用。

```sh
./cc8 verify-failure-ui --runtime-root /absolute/live/workspace \
  --qa-receipt /absolute/private/qa-build.json \
  --http-fixture loopback_tls --dot-fixture local_stdio --execute
```

两种 I/O 模式均在界面重放前生成故障包。合跑不需要真实凭证、麦克风或摄像头。48 项合跑已在最小 PATH 下通过。本地 TLS 实际收到 30 次请求，Dot 子进程收到 33 次初始请求（含 TTS 故障前的 15 次正常 Dot 回答）、48 次明确发起的恢复请求和 3 次审批拒绝。隔离语料通过只证明本地协议与控制器处理，实体屏幕重放需单独执行。

切换到真实启动回执超时后，从仓库根目录及最小 PATH 重跑 `loopback_tls` 和 `loopback_business`，均通过 48 项。本地子进程每轮记录 3 次未应答启动、33 次初始请求、48 次明确恢复请求和 3 次拒绝审批。实体屏幕回放见[业务错误与本地 Dot 的实机记录](#业务错误与本地-dot-的实体回放2026-11-05)。

## 设备播放与输出连接专项

```sh
.toolchains/cc8/dev-venv/bin/python cc8 verify-failure-ui --host-only --case-set playback --execute
.toolchains/cc8/dev-venv/bin/python cc8 verify-failure-ui --case-set playback --runtime-root /absolute/live/workspace \
  --qa-receipt /absolute/private/new-qa-build/qa-build.json --execute
```

设备播放错误、播完回执超时、输出连接断管各三次。主机先取得固定任务完成回执，再经真实播放控制器注入故障。检查原任务仍为已完成、录音关闭、没有重试或遗留播报，并在下一条独立请求时核对恢复。两端只用隔离替身，发送至 QA 包的回放只有协议控制事件。实体模式核对错误详情及新请求状态，音频设备性能另行验收。

## MiniMax 业务错误与下一次请求恢复

### 联网检索专项

```sh
./cc8 verify-failure-ui --host-only --case-set search
.toolchains/cc8/dev-venv/bin/python cc8 verify-failure-ui --host-only --case-set search --execute
.toolchains/cc8/dev-venv/bin/python cc8 verify-failure-ui --case-set search \
  --runtime-root /absolute/live/workspace --qa-receipt /absolute/private/new-qa-build/qa-build.json --execute
```

专项单列五类检索故障各三次。失败请求只执行查询规划和检索；下一条明确查询执行规划、检索及有来源的流式回答。默认使用证书校验的本地 TLS 业务故障，可显式选 `loopback_tls`。本专项拒绝假 HTTP 和 Dot 子进程，固定端点之外不发请求。报告记录 `case_set: search`、30 次失败路径和 45 次恢复路径的实际请求。实体模式用同样的协议包核对失败详情、原任务失败、新请求恢复及旧错误隔离。默认 48 项矩阵继续独立统计。

2026-11-06，实体 CC8 的 USB 隔离 QA 包通过 **15/15** 项，用时 224.536 秒。测试包使用 `76ac7c6` 的 Android 源码和原签名，禁止录音、拍照及 Wi-Fi 客户端。逐轮核对失败详情、原任务失败、新请求完成和旧错误隔离。独立复核报告与 APK 摘要，并确认 QA 包已移除，生产助手 0.15.19 仍在前台，原签名 code79、USB 优先、Wi-Fi 就绪及双端关麦。检索请求只访问本地 TLS；实体环节重放故障协议包。

### ASR 与 TTS 业务错误

`--http-fixture loopback_business` 在原有固定用例中，用 HTTP 200 加非零业务状态模拟请求超时、限流、额度不足和凭证权限错误。ASR 使用 JSON，TTS 使用 SSE，断线仍在完整接收本地请求后关闭连接。每条 ASR/TTS 故障结束后，同一个识别器或合成器主动发起一次独立的恢复探针，校验固定识别文本或完整数字 PCM，并确认请求槽清空。恢复探针只在本地回环中运行，PCM 不保存或播放。随后继续用原有界面恢复包验证新请求和旧错误隔离。

```sh
./cc8 verify-failure-ui --runtime-root /absolute/live/workspace \
  --qa-receipt /absolute/private/qa-build.json \
  --http-fixture loopback_business --dot-fixture local_stdio --execute
```

完整隔离矩阵验证 48 项，其中 30 次 ASR/TTS 故障请求、30 次独立 HTTP 恢复请求。报告分别记录两类请求计数，语料摘要绑定选择的模式与恢复计数。此模式的实体屏幕重放已在 0.14.70／code71 上执行，见[实机记录](#业务错误与本地-dot-的实体回放2026-11-05)。

## 设备门控与恢复

要求唯一正常Android AILABS_S6、SDK27、shell UID2000；所有命令重核同一USB target。QA包事先必须不存在，包列表独立确认；已存在时拒绝替换/删除。生产助手必须版本匹配、前台空闲，双端采集关闭、无播放/校准或未完请求，既有守护及backend归属匹配。与其他设备检查共享实际runtime的互斥锁。

新worktree需要已有安装辅助JAR及其本地构建清单；在生成语料前核对产物和当前Java源码摘要，缺失或不匹配即退出，不自动下载、编译或执行安装。可按[安装文档](APK_INSTALL.md)准备该忽略产物；不能仅复制APK就认为执行环境齐全。

安装成功后才将原助手置后台、以 `auto_start_voice=false` 打开QA。不重启生产后台或72小时观察器；报告记录这段计划内生产连接中断，不计为物理USB切换。

固定语料生成、证书预检和安装都可能耗时数秒，因此在证书预检前、安装前、切换界面前重新核对同一生产后台PID/实例及完整空闲条件。任一次发现用户开麦、取景、离开前台或实例变化，立即退出，不重新选择目标。尚未安装时不碰设备；已安装但尚未接管界面时只清理本轮同摘要QA，不Home或重开原助手。报告`owner_rechecks`只列已成功核对的边界；这降低准备期间的占用竞态，不保证跨多个ADB命令的原子交接。

结束或异常后检查QA仍是本轮新装的相同APK摘要，再force-stop并经 `cmd package uninstall` 移除，仅操作测试包；原助手以不开麦方式恢复前台，核对原backend PID/实例、就绪和采集关闭。即使安装返回异常，也检查是否已经实际提交，避免漏清理。清理失败不能报告通过。进程遭SIGKILL/整机断电时不能保证finally执行；先确认残留包身份和实际状态，不能盲目重装。

报告在 `device-access/private/ui-failure-checks/`，目录0700、文件0600、独立原子检查点，记录源码/产物/实际协议语料摘要、阶段、每项结果、清理与范围限制。不保存设备标识、PID/实例、音视频、原始界面树、密钥或聊天正文。任何未完成项、界面读取错误或恢复未确认都不能通过；`full_p0_acceptance` 固定为false。

尚需分别验证生产版本上线、真实网络/权限/播放故障、画面路径、真人语音与物理切换。本工具不会通过吊销生产权限、耗尽真实额度或重复执行工具任务制造故障。

## 业务错误与本地 Dot 的实体回放（2026-11-05）

在 0.14.70／生产 APK 0.9.34（code71）上，用同源码 `7400f7b`、同签名的独立 QA APK 重放 `loopback_business` 与 `local_stdio` 的全部 48 个用例。实际屏幕核对错误摘要、完整恢复指引、新请求恢复与旧代次错误隔离，各 48 项通过。报告 `qa-ui-20261105T010425Z-a9dcfc63.json` 耗时 617.672 秒。

故障包由本地 TLS 和隔离 Dot 子进程生成，记录 30 次 ASR/TTS 故障、30 次独立恢复、33 次初始 Dot 请求、48 次明确发起的恢复、3 次拒绝审批和 3 次未收到启动回执。设备回放结束后核对 QA 包已移除、生产助手恢复前台且停采。生产后台实例和生产 APK 均保留。真实云服务故障、拍摄错误和语音声学仍各自验收。

### 0.14.94／code75 实体回归

2026-11-06（北京时间）从已部署的 `5d3474d` 冻结源码构建原签名的独立 QA，使用相同 `loopback_business` 和 `local_stdio` 矩阵。48/48 项逐项确认错误摘要、完整恢复指引、独立新请求完成和旧错误隔离，累计 625.921 秒。独立复核按固定顺序核对 48 个用例及六个执行模块的摘要，线协议计数与上一轮一致。结束移除测试包、核对原后台和 APK 保持、USB 前台和停采。该批覆盖本地故障注入和实体界面。

### 0.15.10／code78 实体回归

2026-11-06，按已部署的 `1c0cbab` 源码构建独立 QA。首个构建在安装前因签名与已验证的 QA 不匹配而排除，随后用原项目签名重建。`loopback_business` 与 `local_stdio` 的 48 个场景均通过，耗时 625.678 秒。独立复核确认测试包已移除，原助手恢复前台，USB 就绪且双端关麦。生产 APK、后台实例和权限保持原样。真实云服务、实际拍摄与真人声学仍分别验收。

### 0.15.28／code79 实体回归

2026-11-07，生产助手为 0.15.28／原签名 APK 0.9.42（code79）。用 `b830280` 构建无录音和拍照权限的隔离 QA 包。常规矩阵由本地 TLS 和隔离 Dot 子进程生成，实体界面回放 **48/48** 项通过，用时 634.479 秒。逐轮核对错误详情、任务结果、旧错误隔离和下一条请求恢复。主机核对三次 Dot 启动回执超时及三次审批拒绝。

随后复用同一签名与摘要的 QA 包，以 `5a0e7fd` 生成设备播放错误、播完回执超时和输出连接断管三组控制事件。实体界面 **9/9** 项通过，用时 147.549 秒。原任务保持已完成，下一请求独立完成，迟到错误未覆盖新状态。 [播放故障专项](https://github.com/batipang/CC8/pull/354)

两轮结束分别复核测试包移除、正式助手前台、原 APK 摘要、USB 主用、配对 Wi-Fi 就绪及麦克风和摄像头关闭。该轮使用故障注入和协议重放，实际远端服务故障与声学行为分别验收。

## P0-06 验收对照

在后台 0.15.28／原签名 APK 0.9.42（code79）部署期间，使用隔离 QA 包在实体 CC8 上重放本机控制器的故障回执。ASR、Dot 与 TTS 的超时、限流、额度、凭证或权限故障各通过三次。ASR、TTS 分别三次在本地 TLS 连接上遇到服务端断开，Dot 三次遇到本地协议管道断开。故障回执在实体屏幕显示完整指引，原请求按实际结果收尾。下一次显式请求通过，迟到错误未覆盖新状态。主机逐轮核对故障调用次数和恢复请求，确认没有额外执行。

三次 Dot 审批拒绝均在实体屏幕确认本次需要审批的操作未执行，并提示到电脑端核对。界面明确说明当前没有待批准请求。另有三次 Dot 启动未返回回执，界面保留结果待核对。设备播放错误、播完回执超时和输出断管各三次，已完成的原任务保留其结果。

同一正式助手随后通过三轮空闲双通道隔离。每轮固定隔离应用通道 10 秒，核对「未连接」「正在自动重连」及「麦克风关闭」。恢复后继续观察至少 5 秒，并核对同一后台实例、USB 优先、配对 Wi-Fi 就绪、双端停采和零新增请求。三轮含收尾分别用时 21.397、21.575、21.439 秒，结束独立复核原安装包与正式界面。该检查未启动收音、播报或摄像头，也未拔动线缆。

P0-06 按上述受控注入与恢复验收关闭。远端真实服务行为、物理拔插、语音声学与 72 小时运行仍按对应 P0 单独验收。

## TLS模式实机记录（2026-11-04）

后台0.14.17／生产APK0.9.13、code48保持不变；无采集权限的独立QA同为0.9.13/code48，原证书兼容。构建收据保留clean源码`482aff706f468614a465208509041ece6e66e340`，执行驱动冻结`d918734c4e899b9ed0b599a163466a6b17691498`；应用/Gradle/release输入逐文件摘要一致，不把构建收据改标成驱动提交。QA APK摘要`fa47a38a139e293764070076b9dec3a7e5b3730af29c2d8bfa14aabd2cd46188`。

首次执行`qa-ui-20261103T191314Z-49cb738a.json`在安装准备遇到本地辅助JAR缺失，13.934秒失败，未切换原助手界面，清理确认通过；保留原失败。随后只补齐按产物/Java源码摘要核对的忽略构建文件，并将该前置检查提前；没有编译新助手或改变运行依赖。修复后的独立执行`qa-ui-20261103T191535Z-ed757f2d.json`完成48/48：ASR15、Dot18、TTS15，错误摘要/完整指引、下一请求UI恢复和旧错误隔离全部通过，重放及收尾621.053秒（不含预生成语料时间）。

ASR/TTS30项各经一次真实本地TLS服务端请求，Dot18项仍模拟，恢复仍跳过音频。三次生产实例/空闲复核均通过；结束仅卸载同摘要QA，原后台未重启、原助手不开麦恢复。独立只读终审再核对48个固定case及全部布尔回执、30次请求、模块摘要、QA确实不存在、原实例空闲就绪；报告SHA256为`bd155bcfdce3a9d596f463098fb63a516f3123f932b30aac97c542607f6df8a3`，终审保存在`terminal-audit.json`。没有音视频、真实提供方调用或生产权限变更，不关闭完整P0-06。

## 本机执行记录（2026-11-03）

独立QA0.9.3/code38完成48/48：ASR15、Dot18、TTS15，均点开完整错误指引，下一请求完成、旧错误被忽略，结束清理核验通过；耗时614.238秒。报告 `qa-ui-20261102T222214Z-5dcb3d19.json` 只保存在私有目录，协议语料摘要 `93bf06fd90d2c0fd3db2d16f71581c81fd824475adf1b24a30b007931f77eefb`。

QA APK摘要 `516eebd0868a5eb9a1fcb82955bf640ab483c7de3d2c16bb209fb03246d7848a`；构建发生于提交前，回执如实标记dirty，应用输入及四个自动化模块逐文件摘要随后与提交 `85456d9` 一致核验。生产仍0.13.7/APK0.8.8，无部署替换；原backend实例恢复确认。受保护目录拒绝和初期兼容适配失败的报告保留；一次旧 `pm uninstall` 失败留下的QA包已核验同一摘要后经 `cmd package uninstall` 移除，未抹掉失败记录。
