# 2026-11-11 全面代码检查（只读）

检查基线：`338511a31f11659970bb96b6c93bc673dd5736ba`（release `guest-isolation-166-20261111.1`，后台 0.16.36 / 助手 APK 0.9.128 code 166 / 检测器 1.0.6 / 协议 2）。

本轮是**只读检查**：没有修改任何源码、没有修复、没有部署；没有连接设备，没有打开麦克风或相机，没有访问私人录音，没有发起云端调用。此前两轮记录见 [CODE_REVIEW.md](CODE_REVIEW.md) 和 [CODE_REVIEW_20261102.md](CODE_REVIEW_20261102.md)，其中已修复的问题不重复列出。

本文件是检查记录，不是第二份待办清单。采纳的条目应按 [TODO.md](TODO.md) 的编号和批次规则登记后再实施。

## 结论

- **没有确认到 P1**（必然导致错误行为、数据丢失或隐私泄露的缺陷）。
- **P2 共 22 条**：特定条件下出错、或安全/隐私边界不够严的问题。汇总时离线复现了 F-1，另有 F-2、F-3、T-1 等由分模块审查用离线片段复现；其余为读代码确认或带明确前提的推断，逐条标注。
- **P3 共 35 条**：健壮性、可维护性、测试质量和文档一致性。
- **注释**：模块级说明做得好（Python 模块 docstring 覆盖 100%，Java 小类几乎都有一句职责说明），已有的行内注释多数在解释"为什么"。但**函数/方法级说明几乎空白**（host 生产代码 1974 个函数只有 93 个有 docstring，约 5%），最大的两个文件注释最少，锁保护关系、状态机和魔法数字普遍缺说明，并有少量过期或误导的注释。详见[注释质量专项](#注释质量专项)。
- 自动检查基线全部通过（见下节）。AGENTS.md 列出的核心不变量在本轮范围内未发现可绕过的路径，例外已作为发现列出（A-1、A-2、A-3、H-3）。

## 范围与方法

| 范围 | 规模 | 方式 |
| --- | --- | --- |
| `cc8-assistant/host/` 生产代码 | 213 个文件，约 4.7 万非空行 | 语音主链路、传输与安全、功能与存储三部分分别通读；诊断/证据类工具抽查 |
| `cc8-assistant/host/test_*.py` | 231 个文件，约 6.3 万非空行 | 运行全套；抽样约 10 个文件评估测试质量 |
| `cc8-assistant/app/` Java | 74 个文件，约 1.7 万非空行 | 通读（`CaptureClockStats`、`RenderClockStats`、滤波系数表为略读） |
| `cc8-diagnostics/` | 9 个 Java 文件加构建脚本 | 通读 |
| `device-access/`、`tools/`、`scripts/`、CI 配置、两个 Swift 包 | 约 1 万行 | 通读（`qa_faults.py`、`qa_dot.py`、`qa_startup.py` 仅检索） |

未覆盖或覆盖较浅的部分见文末[局限](#局限)。

每条发现的"核实"列含义：

- **已复现**：本轮用离线片段实际跑出了该行为。
- **已复核**：汇总时再次对照源码确认代码路径与描述一致；并发类条目的结论仍是静态推断，没有实测到竞态。
- **未复核**：来自分模块审查的读代码结论，汇总时没有再逐行核对。
- **推测**：依赖未验证的前提，条目里写明前提是什么。

## 自动检查基线

在独立开发环境（Python 3.13、JDK 21、`requirements-dev.txt`）中运行：

| 检查 | 结果 |
| --- | --- |
| `python tools/quality.py check --python-only`（Ruff 0.17.3 lint + format、mypy strict 27 个模块） | 通过 |
| `python tools/test_all.py` installer 套件 | 24 项通过 |
| host 套件 | 2710 项通过，5 项按设计跳过（4 项需显式 `--with-private-audio`，1 项仅 macOS） |
| tooling 套件 | 180 项通过 |
| Java 音频检查 | 76 项通过 |

没有运行的检查：Java 格式检查（需要先 `quality.py setup` 下载格式工具，本轮未下载）、Android `assembleRelease` / `lintRelease`（环境无 Android SDK）、Swift 构建、GitHub Actions。上表是本机离线结果，不代表受保护 CI 的状态，也不是实机验收。

环境说明：首次运行 host 套件有 23 项失败，全部在 `test_paired_wire.py` 和 `test_paired_capture_pcm.py`。原因是本检查环境设置了 `JAVA_TOOL_OPTIONS`，JVM 启动时向 stderr 打印一行横幅，而这两个测试把 stderr 合并进 stdout 后做精确比对。去掉该变量后全部通过。这不是产品缺陷，但测试对环境不够健壮，记为 T-6。

## P2 发现

### 主机语音主链路

| 编号 | 位置 | 问题与触发 | 核实 | 建议 |
| --- | --- | --- | --- | --- |
| H-1 | `capture_pipeline.py:347-358`、`session_controller.py:489-509`、`identity_controller.py:169,232-239`，入口 `announce.py:114` | 主循环处理 MIC 包时不持会话 guard。桌面"恢复主人"在 IPC 线程执行 `stop()` → `capture.close()`，会重置端点状态并清空 `pending_mic`。若主线程此刻正在 `endpoint.consume`，可能抛 `AttributeError` / `IndexError`；主循环（`app_runtime.py:298`）不捕获这两类，后台会退出重启。窗口为毫秒级。 | 线程归属未复核；崩溃为推测，未复现 | 把 stop/close 投递到主循环执行，或由采集入口消费关闭标志 |
| H-2 | `dsp.py:40-61`、`echo_pipeline.py:121-131` | `RenderTimeline` 无锁。`cancel_pause()` 可经 `session.cancel` 在任意线程执行，而 `add` / `_append` / `resume_at` 在主线程。插话暂停期间取消与迟到的 RENDER 包交错时，`self.paused_at - stamp` 会对 `None` 运算并抛 `TypeError`，同样导致后台退出；或丢失暂存片段。概率低。 | 已复核（无锁、读写分属不同线程）；异常类型由分模块审查用模拟交错确认 | 纳入 `dsp_lock`，或先把 `paused_at` 读入局部变量 |
| H-3 | `announce.py:223-225` | 播报等待 120 秒超时后，先比较 `generation` 再调用 `cancel('announcement_timeout')`，两步不原子。间隙里进入的新请求会被误取消。与 11-02 修复的"旧错误取消新 generation"是同一类问题，此处漏网。 | 已复核 | 放进会话 guard，或改用带期望 generation/epoch 的取消 |
| H-4 | `calibration_controller.py:140,198`、`bridge.py:497-501`、`status_reporter.py:262` | 回声自检的 `diagnostic_text_pairs`（麦克风听到内容的识别文本，最多 8 条、每条 200 字）随 `**metrics` 写入 `status.json`，并经 `echo_self_check_finished` 写入服务日志。`health()` 有白名单，这两处没有。仅在显式触发自检时发生，但识别文本属于用户内容，落盘和入日志超出了"诊断不含内容"的约定。 | 已复核 | 落盘和日志只保留计数，文本只留在内存 |

### 传输、协议与外部服务

| 编号 | 位置 | 问题与触发 | 核实 | 建议 |
| --- | --- | --- | --- | --- |
| S-1 | `transport.py:119-135`、`protocol.py:485-494` | 局域网认证可被慢速连接长期占住。accept 是单线程串行；TLS 握手有 3 秒总期限，但其后的 `receive(tls)` 只有"每次 recv 3 秒"的超时，并且先按声明长度读满整包（上限为 `MAX_PACKET`）才判断是否超过 1024 字节。同网段任一主机完成握手后慢速送字节，可长时间占住该线程，期间已配对设备建不了 Wi-Fi 备用通道。USB 通道不受影响。第 127 行注释称"已限制未认证握手"，与实际不符。 | 已复核 | 握手和认证共用一个总截止时间；读到长度前缀即拒绝超过 1024 的包 |
| S-2 | `usb_transport.py:123,156` | `usb_target()` 和 `connect_device()` 每次调用都 `sys.path.insert(0, …)`。USB 监视每秒调用一到两次，`sys.path` 无界增长，并把 `device-access` 置于导入搜索最前。分模块审查按 72 小时规模模拟（约 26 万项），一次未缓存的模块查找耗时约 0.76 秒。长时间运行验收前应修。 | 已复核 | 模块级只插入一次 |
| S-3 | `minimax_client.py:275-282` | 请求会话使用 `requests` 默认的 `trust_env=True`。主人 `~/.netrc` 若有 `default` 条目或对应主机条目，`requests` 会用其中的 Basic 凭证覆盖显式设置的 Bearer 头：无关账号的密码被发往 MiniMax，且认证失败。 | 已复核会话构造；覆盖行为由分模块审查用锁定版本 requests 2.34.2 离线构造请求确认。前提：存在这样的 `.netrc` | 用 `auth` 钩子固定写 Bearer（保留代理支持），并补回归 |

### 功能与持久化

| 编号 | 位置 | 问题与触发 | 核实 | 建议 |
| --- | --- | --- | --- | --- |
| F-1 | `preference_memory.py:238`，由 `brain_router.py:212` 先于模型拦截 | "记住，明天叫我起床"被解析成偏好 `{明天: 我起床}`，直接落盘并注入后续上下文，提醒没有创建，用户也不会被告知。分隔词"是/为/叫"过宽：`记住，这件事很重要，因为老板是这样说的` 同样被存成偏好。 | 已复现 | 收紧分隔规则或落盘前口头确认；补反例测试 |
| F-2 | `meeting_export.py:190-204,232-241,349-351` | 导出目录里出现任何未知文件就拒绝再导出；包目录内多一个文件，则列表、保存、删除全部抛 `OSError`。用户按返回的路径在 Finder 打开目录会生成 `.DS_Store`，之后只看到"会议状态未确认"。 | 未复核（分模块审查已用离线片段复现） | 忽略 `.DS_Store`，或允许删除含未知项的包并给专用提示 |
| F-3 | `saved_tasks.py:28-39`、`workspace.py:387` | 任务结果含 `\r`、ANSI 转义等控制符时永远无法保存，提示却是"状态未确认…异常文件保留"，与实际原因不符。工具输出带回车很常见。 | 已复核校验函数；保存失败由分模块审查用片段复现 | 在 `task_finish` 归一化控制符；校验失败给独立文案 |
| F-4 | `report_archive.py:194`、`report_checkpoint.py:287`、`workflow_store.py:88`、`preference_memory.py:156`、`saved_tasks.py:257,274`、`workspace.py:136-152` | 存储文件损坏或版本不兼容时没有自愈路径：清空、重置、删除都是先读后校验，坏文件连"清空"也失败。`workspace` 在构造时直接抛错，是否导致启动失败未验证。 | 未复核；启动失败为推测 | 显式清空时把坏文件改名隔离并提示路径 |
| F-5 | `meeting_session.py:169-172` 对比 `194-198` | 末包越过时长上限时走 `interrupt`，已封片待识别的行全部变成 unconfirmed；恰好命中上限则保留。同样的录音因分包边界不同得到不同结果。 | 未复核（主机行为由分模块审查用片段确认）；设备侧是否会产生非整除分包为推测 | 越界包截断到剩余样本后走正常封片 |

### Android 核心运行时

| 编号 | 位置 | 问题与触发 | 核实 | 建议 |
| --- | --- | --- | --- | --- |
| A-1 | `VoiceSession.java:555-583,983-1004,1010-1024` | USB 通道是 `127.0.0.1:18765` 明文监听，没有认证：`transport_activate` 的 token 可省略，旧式 `hello` 可取得 owner。设备上任一有 INTERNET 权限的本机应用若先连上，即可冒充主机、收到麦克风 PCM（配合"打开即自动语音"），并能在未配对设备上用 `lan_config` 写入自己的 Wi-Fi 配对。[ARCHITECTURE.md](ARCHITECTURE.md) 记录了该端口，但没有把"设备上其他应用"写成信任边界。 | 已复核（无认证）。前提：该固件上存在这样的应用，未验证 | 配对后对 USB 也做 token HMAC 挑战，已配对设备拒绝旧式 hello；至少在文档中写明该信任假设 |
| A-2 | `VoiceSession.java:665-684` 对比 `2295-2318` | 通道切换只把 `visual` 置为 false，不清 `pendingVisionNonce`，也不走 `cancelVision`。点"看一看"之后、`camera_capture` 到达之前发生 USB↔Wi-Fi 切换时，旧 nonce 在新通道 15 秒内仍可被接受，与"传输变化即停止"不符；随后 15 秒定时器调用 `cancelVision()`，会打断新通道上无关的播报并显示"拍摄请求超时"。 | 已复核设备侧。主机是否会在新通道复用旧 nonce 未验证 | 通道切换的 changed 分支复用 `cancelVision` 的清理 |
| A-3 | `VoiceSession.java:2443-2457` | `interrupt()` 不抬高 `minimumGeneration`，而 `cancelVision` 会。设置里点"立即打断播报"后，主机处理前已在途的同代 `play_begin` 仍被接受，可能再播一小段旧回答。 | 已复核缺本地围栏；可闻程度取决于主机时延，未验证 | 参照 `cancelVision`，设为被停播放器的 generation+1 |
| A-4 | `VoiceSession.java:870,910-915,2357-2368` | 发送队列 128 包、非阻塞入队，满了就关闭整条连接；图片分片不限速连续入队（768 KB 档约 63 片）。录音中经慢速 Wi-Fi 传图时可能把连接挤断，麦克风随之关闭。 | 推测，未验证吞吐 | 相机分片按队列剩余容量节流 |

### Android 界面与检测器

| 编号 | 位置 | 问题与触发 | 核实 | 建议 |
| --- | --- | --- | --- | --- |
| U-1 | `cc8-diagnostics/…/MainActivity.java:707-741,916-931` | "导出最近录音 WAV"先把录音复制到 `getExternalFilesDir(Documents)/reports`，再弹文件选择器；取消后副本保留。"删除本地录音缓存"只清 `cacheDir`，外部目录里的录音副本没有应用内删除入口。 | 已复核。该目录在 SDK 27 上能否被其他持存储权限的应用读取是平台行为推断，未验证 | 暂存改到内部目录，写入目标后删除；清理按钮同时清 `reports` |
| U-2 | `MeetingDialog.java:452-453,478` | `rows.optJSONObject(i)` 未判空即调用 `row.optLong`，后台返回的 `rows` 含非对象元素时在 UI 线程空指针崩溃；行数也没有上限。 | 已复核。触发需要畸形的主机数据 | 参照 `ReportSourcesDialog` 做全量校验并加上限 |
| U-3 | `DotChatsDialog.java:126,137,202,236,240`、`SavedTasksDialog.java`、`ResultDetailsDialog.java` | 结果和确认对话框直接 `.show()`，`close()` 只关进度框。`LargeDialogBuilder.dismissAll` 只在身份切换时调用（`MainActivity.java:142`），`onPause` 和通道切换都不调用。从 Home 返回或 USB↔Wi-Fi 切换后，旧列表、结果摘录仍在屏上；"摘要接续"没有通道校验，会把旧 id 发到新连接。 | 已复核 `dismissAll` 的调用点；未实机复现 | 按 `WorkflowDialog` 的 epoch 模式跟踪并在暂停/换线时关闭 |

### 工具、安装器与供应链

| 编号 | 位置 | 问题与触发 | 核实 | 建议 |
| --- | --- | --- | --- | --- |
| T-1 | `device-access/cc8_install.py:428-433` | `result_unconfirmed` 回执写在 `install()` 之前，而 `install()` 先做本地预检。APK 路径写错或签名不符时并没有尝试安装，私有回执却仍是 `unconfirmed`，与 [APK_INSTALL.md](APK_INSTALL.md) 的定义矛盾，会误导后续"是否需要核对已装版本"的判断。 | 已复核（分模块审查用零设备命令的片段确认） | 把写回执移到实际流式安装之前；预检失败记 `not_started` |
| T-2 | `requirements-*.txt`、`.github/workflows/ci.yml:41,73,202`、`tools/cc8.py:116`、`scripts/setup-voice.py:152-162` | 依赖只用 `==` 固定版本，没有哈希，安装时也没有 `--require-hashes`；`requirements-runtime.lock` 只是 `-r` 别名，不是真正的锁文件。同版本文件被替换或镜像被污染时无法发现，而语音运行环境持有凭证。 | 已复核 | 用 `--generate-hashes` 全量锁定，安装时加 `--require-hashes` |
| T-3 | `cc8-assistant/host/test_runtime.py:604-612,627-635,966-970`、`tools/check_failure_ui.py:526-530` | 固定 `sleep`（0.05–0.15 秒）之后断言"某事件没有发生"。线程或设备慢于这个窗口时，本应失败的回归会被判通过。host 测试里共有 71 处 `time.sleep`，分布在 29 个文件。 | 已复核其中一处 | 等待确定性的完成信号后再断言 |

## P3 发现

### 主机

- **H-5** `app_runtime.py:242-246`、`bridge.py:314-323`：心跳线程只捕获 `OSError` / `EOFError` / `ValueError` / `RuntimeError`，其他异常会让线程静默退出，提醒、待机检查和 ping 随之永久停止，而 `health` 不报告心跳存活。已复核；实际触发路径为推测。建议捕获 `Exception` 并记录类型，`health` 增加最近心跳时间。
- **H-6** `bridge.py:345-351`：`reminders.tick()` 抛错会跳过同一轮的汇报订阅、流程调度和 ping；回执落盘持续失败时每次心跳都如此。未复核。
- **H-7** `calibration_controller.py:84,92,109-120`：校准线程在解锁后才发布 listening 状态，可覆盖刚准入的文字请求的 thinking；failure 不带 trace_key，会记到当前 generation。窗口为微秒级，只影响界面和诊断。未复核。
- **H-8** `composition.py:78-94,311-327`、`composition_audio.py:168-182`：忙闲判断重复三份，且"出队到置 busy"之间有缺口，提醒或唤醒超时理论上可取消刚说完的一句。概率极低。未复核。
- **S-4** `codex_rpc.py:108-113`、`runtime_env.py:31`：Codex 子进程继承完整环境。手动启动且以 `CC8_MINIMAX_API_KEY` 环境变量提供密钥时，密钥会进入 app-server 及其 shell/MCP 工具；唤醒校验子进程用的是净化环境。登录服务方式不受影响。未复核。
- **S-5** `transport_pairing.py:92-99`：`pairing.json` 首次写入不是原子写，写完前崩溃会留下空文件，之后每次启动都报错，需手工删除；读取已有文件前不校验属主和权限，只事后 `chmod`。已复核。建议复用现有的原子私有写入。
- **S-6** `camera.py:57,73,110`：`VisionError` 是 `RuntimeError` 子类，`main()` 只捕获 `OSError` / `ValueError`，校验失败时输出 traceback 而不是约定的 JSON。未复核。
- **S-7** `codex_rpc.py:321-349`：读线程的 except 缺 `AttributeError` / `TypeError`，收到非对象 JSON 行时线程经未处理异常退出（finally 仍会断开，不会悬挂）；`failure_reason` 未初始化且无人读取。未复核。
- **S-8** `voice_tools.py:163-171`：`private/signing` 路径过滤区分大小写，macOS 默认卷不区分大小写，`Private/…` 可能绕过。影响限于 Finder 定位和存在性探测。推测，未在 macOS 验证。
- **F-6** `workspace.py:571-584`：提醒认领后、回执前系统时钟回拨，重复提醒不前进，同一次会再播。未复核（分模块审查用假时钟复现）。
- **F-7** `workflow_schedule.py:32-45`：每日时段的 15 分钟宽限跨零点失效。23:55 的流程在 00:02 检查时，当天的时段在未来，前一天的时段没有评估，结果是漏跑。已复核。
- **F-8** `saved_task_controller.py:79-80`：`busy=True` 与 `self.worker` 赋值不在同一临界区，前一线程的 finally 可清掉后继占位，与第 142-143 行注释不符。窗口极窄。未复核。
- **F-9** `meeting_controller.py:299-303`：先让进行中的总结失效，再校验编辑；失败或无变化的编辑也会取消总结。未复核。建议调换顺序。
- **F-10** 存储层重复：`ReminderStore` 被 6 个模块当通用存储使用，报错文案仍写 Reminder；锁文件 `writer()` 复制了 5 份且语义不一致（`workflow_store.py:60` 不等待，其余等 1–2 秒）；崩溃遗留的 `.tmp` 文件没有清理。未复核。
- **G-1** `upgrade_preparation.py:350-355`：循环对 `credential_file` 和 `signing_keystore` 共用同一个 `None` 回退值（检测器签名库路径）。当前 `credential_file` 不可为空，所以不会触发，但这是潜在的错误默认值。已复核。
- **G-2** `upgrade_preparation.py:360`、`workflow_schedule.py`、多个 Java 对话框：时区写死，且 `Asia/Shanghai` 与 `GMT+08:00` 混用。个人版可接受，公开发布前需要统一出处。已复核前两处。

### Android

- **A-5** `VoiceSession.java:1346-1350`：`vision_unavailable` 不校验 generation，迟到事件会清掉当前看图（结果是关相机，偏安全）。未复核。
- **A-6** `MainActivity.java:297,2021-2041`：`onCreate` 不看 `savedInstanceState`，进程被回收后系统重建 Activity 时会用旧 intent 再次武装自动语音。字面上符合"每次创建一次"，但不是用户显式打开。未复核。建议仅在 `savedInstanceState == null` 时武装。
- **A-7** `VoiceSession.java:1014-1017` → `LanConnector.java:110`：读线程持 `routeLock` 做 `commit()` 写盘，UI 线程也要拿这把锁。卡顿程度为推测。未复核。
- **A-8** `LanConnector.java:246-247`：发现阶段收到一个非 JSON 的 UDP 回包就抛异常中止本轮，应跳过继续。未复核。
- **A-9** 死代码与重复：`VoiceSession.java:105-106` 两个只写不读的字段；`MainActivity.java:616-617` 未加入布局的按钮；能力位清零的赋值链重复三处（`VoiceSession.java:529-542,685-698,827-840`）；`AndroidManifest.xml` 的 `usesCleartextTraffic` 对裸 Socket 无作用，`MODIFY_AUDIO_SETTINGS` 未见使用点。第三项已复核其中一处，其余未复核。
- **U-4** `PreferenceMemoryDialog.java:105-168`：唯一没有 epoch 守卫、直接持有网络 JSON、不校验 key/value 长度和类型的对话框。未复核。
- **U-5** `KaraokeTextView.java:37,46,51`、`PhotoCropView.java:51,56`：`onDraw` 每帧分配对象，字幕越长 `getSelectionPath` 越慢。未复核。
- **U-6** `MeetingAnalysisDialog.java:158,232-233`："复制完整结果"上限 524288 码点，接近 Binder 1 MB 限制时 `setPrimaryClip` 可能抛异常。推测，未验证。
- **U-7** `TaskStepsView.java:33-40` 靠中文标题子串做分类；`WorkflowEditor.java:403-406` 星期全不选时提示后对话框关闭，草稿丢失。未复核。
- **U-8** 对话框重复代码："请求中 + 超时 + 正在核对"的骨架在 9 个对话框里各写一遍；`RecognitionLanguageDialog` 与 `RecognitionEnvironmentDialog` 几乎逐行相同；剪贴板复制多处重复且判空不一致。未复核。

### 工具与测试

- **T-4** `installer-helper/Cc8Installer.java:113-124`：安装状态广播没有接收者，失败原因（降级、空间不足、签名不符）丢失；主机侧 90 秒超时与 Java 侧 60 秒没有协调。未复核。
- **T-5** `tools/cc8.py:449`：直接运行 `python3 tools/cc8.py verify-failure-ui` 报 `ModuleNotFoundError: No module named 'tools'`，通过 `./cc8` 入口正常。已复现。另外第 447-523 行的 17 个手写分支可以改成分发表。
- **T-6** `test_paired_wire.py:91`、`test_paired_capture_pcm.py:131`：把 JVM 的 stderr 合并进 stdout 后精确比对，环境里只要设置了 `JAVA_TOOL_OPTIONS` 就全部失败。已复现。建议分开捕获 stderr。
- **T-7** `test_runtime.py:186-189,229-231,268-270`：显式传了 `--with-private-audio` 后素材缺失仍然 skip 并报告通过，与"缺少核心依赖必须失败"的约定不一致。未复核。
- **T-8** `tools/release.py:226-230`、`tools/test_apk_signing.py:96-99`：吞掉全部异常只给通用提示，CI 失败时无法定位。未复核。
- **T-9** `installer-helper/build.py:12-35`：不清理 `classes/` 目录，旧的内部类会进 JAR。未复核。
- **T-10** 工具重复：流式哈希三份、下载回退两份、私有文件读取四份、`build-tools/35.0.0` 硬编码 8 处、清单解析两套且容错不同。未复核。
- **T-11** 文档与代码不一致：`README.md:84` 的示例仍是 `cc8-diagnostics-1.0.1.apk`（当前 1.0.6），`cc8-assistant/README.md:22`、`docs/APK_INSTALL.md:77,84` 同类；`codex_rpc.py:153` 向 Codex 上报的客户端版本硬编码为 `0.4.0`（后台实为 0.16.36）。第一项和最后一项已复核。
- **T-12** `.gitignore` 没有忽略 `mac-notifier/.build/`；私有目录以外的 `*.wav` / `*.pcm` 没有兜底忽略规则。未复核。

## 注释质量专项

### 量化统计

统计方式：Python 用 `tokenize` 数 `#` 注释行、用 `ast` 数 docstring；Java 按行识别 `//` 和块注释，属近似值。

| 范围 | 文件 | 非空行 | 注释 + 文档行占比 | 模块 docstring | 函数 docstring | 类 docstring |
| --- | --- | --- | --- | --- | --- | --- |
| host 生产代码 | 213 | 46878 | 3.8% | 213/213 | 93/1974（5%） | 45/338（13%） |
| host 测试 | 231 | 62921 | 0.7% | 215/231 | 0/4282 | 4/531 |
| tools（不含测试） | 24 | 4878 | 2.8% | 24/24 | 5/186（3%） | 1/25 |
| tools/tests | 16 | 3459 | 0.9% | 16/16 | 0/231 | 0/28 |
| device-access | 5 | 1023 | 3.3% | 5/5 | 0/53 | 0/3 |

| Java 范围 | 文件 | 非空行 | 注释行占比 | Javadoc 块 |
| --- | --- | --- | --- | --- |
| 助手 app/main | 74 | 16754 | 1.6% | 77 |
| 助手 app/test | 2 | 707 | 0.7% | 2 |
| 检测器 | 9 | 3133 | 0.7% | 7 |

体量最大而注释最少的文件：

| 文件 | 非空行 | 注释/文档行 | 占比 | 说明 |
| --- | --- | --- | --- | --- |
| `MainActivity.java`（助手） | 2562 | 13 | 0.5% | 0 个 Javadoc |
| `VoiceSession.java` | 3394 | 69 | 2.0% | 1 个 Javadoc，约 60 个字段 |
| `ReportSourcesDialog.java` | 857 | 2 | 0.2% | |
| `MeetingDialog.java` | 730 | 2 | 0.3% | |
| `composition.py` | 655 | 5 | 0.8% | 10 个函数全无 docstring |
| `report_voice.py` | 631 | 4 | 0.6% | |
| `device_smoke.py` | 4838 | 66 | 1.4% | 110 个函数 4 个有 docstring |
| `chat_controller.py` | 614 | 14 | 2.3% | `run` 264 行仅 3 处注释 |
| `protocol.py` | 457 | 9 | 2.0% | 290 行的校验函数只有一行 docstring |
| `transport.py` | 346 | 9 | 2.6% | 15 个函数全无 docstring |

注释占比本身不是目标，短小自明的代码不需要注释。问题在于占比最低的恰好是并发最复杂、最需要解释的文件。

### 做得好的地方

- **模块级说明完整且有内容**。每个 Python 模块都有一行 docstring，多数写明"归属"和"不做什么"，例如 `upgrade_preparation.py:1`（"never deploy"）、`diagnostics.py:1`、`tls_stream.py:1-6`。Java 小类几乎都有一句职责说明。
- **竞态点的行内注释在解释原因**，而不是复述代码：`session_controller.py:448-454`、`playback_controller.py:298-300`、`bridge.py:66-67`（epoch 与 generation 的区别）、`photo_followup.py:49-54`（锁反转说明）、`transport_channel.py:117-118`（为何只由读线程释放描述符）、`capture_pipeline.py:154-155`、`wake_controller.py:301-303`。
- **带实测依据的数字**：`tools/test_all.py:62-64`（480 秒超时附 CI 实测 296.9 秒）、`PairedCapturePcm.java:5-13`（滤波器参数与因果延迟）。
- **安全决策有理由**：`cc8_install.py:60-62,210-212`、`source_page.py:126-128`、`.gitleaksignore:1-9`、`ci.yml:112-113`、`diagnostics.py:85`（不复制异常消息、URL、凭证）。
- 其他值得保留的范例：`CaptionViewport.java:13,61`、`SpeechMotion.java:26-27,50,55`、`StatusFeedback.java:20-21,74-75`、`SpeakerSession.java:8,108,217`、`PlaybackClock.java:23-25`、`OpenVoiceGate.java:49`、`workspace.py:125-126,176-177,331-332`、`meeting_worker.py:126-127`、`result_files.py:21-27`。

### 不足

**1. 锁、线程归属和状态机没有说明**（最影响后续修改的安全性）

- `VoiceSession.java:82-145`：约 60 个字段，`volatile` 与普通字段混用，没写各由哪把锁保护；第 141-145 行的字段实际靠 `routeLock`，只在第 1854 行提到。第 87-88 行多把锁没有获取顺序说明。
- `VoiceSession.java:22-80`：`Listener` 的 30 个回调没写调用线程，实际常在持锁状态下从读线程发出。
- `session_controller.py:34-70`：无类 docstring；`rebind_seen`、`capture_route_pending`、`capture_start_required`、`capture_start_ack` 四个标志的关系没有说明。
- `transport_channel.py:33-34,39-43`：两把锁各保护什么、次序如何，`ready` / `application_ready` / `route_*` 的语义都没写。
- `meeting_session.py:10-14,219-236`：五态状态机和 `interrupt` 的终态判定没有转换说明。
- `reminder_store.py`：全文件零注释，`expected` 所代表的乐观并发身份（第 19、33 行）没有解释，而这个类被 6 个模块复用。
- `identity_runtime.py:46-50`：`pending` / `last_ready` / `hello_token` 无说明。

**2. 协议和数据格式缺逐项说明**

- `Wire.java:8-16`：没有包格式说明（29 字节包头的组成、6400 上限的来由）。
- `protocol.py:10-13,415-431`：包头字段与包类型无逐项说明；ROUTE_BOUND 的布局（16 字节令牌 + 1 字节类型）和 6400 / 3216 / 18 没有解释。两端各自定义同一格式，却都没有文字描述。
- `recognition_worker.py:91-106`：识别任务是 2 到 8 元的位置元组，各位含义没写，读取处是 `job[7] if len(job) > 7`。
- `saved_tasks.py:18-20,186-192`：存储 schema 1 和 2 的差异没有解释。
- `VoiceSession.java:960`：591 行的 `handle` 没有协议说明；第 368-391 行的访客白名单没有解释。

**3. 魔法数字没有单位和理由**

- 超时：`transport.py:130,148,198,205`（3 / 10 / 2 / 9 秒）、`playback_pause.py:174,189,201`（0.6 / 20 秒）、`playback_controller.py:293`（4 秒）、`dot_client.py:434`（600 秒）、`workflow_controller.py:188`（90 秒）、`meeting_controller.py:135`（30 秒）、`report_watch_scheduler.py:241`（600 / 90 秒）、`VoiceSession.java` 中 15000 重复四处（1293、2049、2238、2276）、`MeetingDialog.java:328`（12 秒超时会停止录音，未说明）。
- 阈值：`endpoint_pipeline.py:182-213`（-65 dBFS、3 秒、1.2 秒）、`recognition_worker.py:166-210`（幻听黑名单、8 秒、`RATE // 3`、0.001）、`dsp.py:81-82`（`* 0.35`、12 秒）、`echo_pipeline.py:14,159`（750 帧；不足 100 帧直接返回 True）。
- 容量：`transport.py:57`（队列 512）、`VoiceSession.java:870,2358`（128、12288）、`workspace.py:144-518`（64 / 32 / 120 / 12 / 5 / 1600 / 16）、`minimax_client.py:314,765-771`（字节上限无单位）。
- 跨文件须保持一致却无关联说明：`VoiceSession.java:778` 的 `.35` 须与 `NativeSpeaker.java:31` 一致。
- 自定义 View 的几何常量：`BumblebeeAvatar.java:26-62`（眼睑/嘴部坐标、6200 / 5970 / 230 毫秒眨眼）、`WaveformView.java:15-95`（粒子数、`% 112`、攻击/释放系数）、`CompanionView.java:117-196`、`PlaybackProgressView.java:27-91`。
- 权限请求码 `MainActivity.java:2096-2110`（21 / 34 / 35）；`session_controller.py:64` 的 `generation = 100`。

**4. 长函数内部几乎没有分段说明**

- `dot_client.py:250-606`：约 350 行的生成器只有两行注释。
- `chat_controller.py:376-639`：264 行，3 处注释。
- `device_session.py:155-243`：`start_capture` 的分支没有总览。
- 大对话框和 `AudioProbe.java` 的 235 行方法同样如此。

**5. 过期或误导的注释**（应优先修，因为会误导维护者）

| 位置 | 问题 |
| --- | --- |
| `transport.py:127` | 称已限制未认证握手，实际只限制了握手阶段，认证包读取没有总期限（见 S-1） |
| `session_controller.py:3-4` | 说 ACK 不取会话锁，但 `device_event_router.py:62` 已让所有设备事件持 guard |
| `playback_controller.py:206-208` | 说渲染线程不取 guard，但 `bridge.py:281-291` 在身份运行时下每次发事件都取 |
| `saved_task_controller.py:142-143` | 描述的互斥关系与实际临界区不符（见 F-8） |
| `cc8_install.py:45` | 称防"被替换产物"，但 JSON 与 JAR 同源，只能查出陈旧 |
| `LargeDialogBuilder.java:8` | 只提排版，实际主要职责是身份门控和对话框注册表 |
| `CaptionTimeline.java:3` | 称 Immutable，但直接暴露外部数组 |
| `VoiceSession.java:2515-2516` | 称"打开可先于 hello"，但第 2470 行已要求 `isConnected()`（疑似过期） |
| `VoiceSession.java:2459-2461,2469` | SuppressLint 的理由挂在无参重载上；关于 `mine` 的注释比赋值处早 20 多行 |
| 检测器 `MainActivity.java:753-754` | 语句残缺，且所述"无副作用"的命令实际有副作用 |
| `dsp.py:166` | 注释重复常量值，容易随修改漂移 |

**6. 测试不说明所防的回归**。4282 个 host 测试函数没有一个 docstring，只靠长方法名；`test_device_smoke.py` 5422 行只有 6 行注释。测试失败时很难判断它在保护哪条约定。

**7. 注释语言混用**。同一文件里中英文注释并存（如 `diagnostics.py:114` 为中文，其余为英文），偶有中英重复表述（`minimax_client.py:97-98`、`web_search.py:124-125`）。

### 注释改进建议（按收益排序）

1. **先修第 5 类的过期/误导注释**。数量少（约 11 处），改动小，风险最低。
2. **给并发核心补"字段—锁—线程"说明**：`VoiceSession` 字段区、`SessionController` 类 docstring、`TransportChannel` 两把锁、`RenderTimeline`。写明每个字段由哪把锁保护、哪些方法在哪个线程调用、锁的获取顺序。H-1、H-2 这类问题正是缺这份说明时容易引入的。
3. **写一份包格式说明**，同时放在 `Wire.java` 和 `protocol.py` 顶部并互相指向，改一端时能看到另一端。
4. **把魔法数字提成具名常量**，常量名带单位，注释写理由或实测依据，参照 `tools/test_all.py:62-64` 的写法。跨文件必须一致的值互相注明。
5. **给公开函数补 docstring，从 mypy strict 覆盖的 27 个模块开始**。可以对这些模块增量启用 Ruff 的 `D1` 规则，避免一次性大改，也避免与功能提交混在一起。
6. **识别任务元组改为 dataclass**（见下节），字段名本身就是文档。
7. **测试补一行说明或文档链接**，指出所防的回归或对应的 `docs/` 页面；新增测试时执行，不必回补全部存量。
8. **约定注释语言**，新增注释统一一种，存量不必专门翻译。

按 AGENTS.md 的要求，纯注释/格式类修改应与功能提交分开。

## 可维护性与复杂度

以 Ruff 的 `C901`（圈复杂度）作参考，生产与工具代码中复杂度最高的函数：

| 函数 | 位置 | 复杂度 |
| --- | --- | --- |
| `main` | `device_smoke.py:4199` | 112（822 行） |
| `clauses` | `dot_client.py:250` | 78 |
| `generate` | `tools/qa_faults.py:76` | 66 |
| `_audit` | `audit_device_suite.py:317` | 65 |
| `run_device_suite` | `device_smoke.py:3801` | 58 |
| `work` | `recognition_worker.py:91` | 57（约 350 行） |
| `validate_device_event` | `protocol.py:86` | 56（约 290 行） |
| `run` | `chat_controller.py:376` | 49 |
| `handle` | `announce.py:91` | 48 |
| `run_backend` | `app_runtime.py:65` | 47 |
| `_dispatch` | `device_event_router.py:65` | 44 |
| `answer` | `response_worker.py:447` | 43（270 行，含 110 行闭包） |

Java 侧最长的方法：`VoiceSession.handle`（第 960-1550 行，591 行）、`startMicrophone`（2467-2686）、内部类 `Player`（3130-3572）；`MainActivity.build`（417-627）、`editReminder`（1231-1372）、`showSettings`（1374-1472，按下标 0–22 分发）。

建议的拆分方向（属于职责重构，应单独成批，不与功能改动混合）：

- `VoiceSession` 拆成传输路由、采集控制、`Player` 独立文件、看图协调、会议协调；`handle` 改为事件分发表。
- `MainActivity` 拆出提醒对话框、HUD 布局、诊断输出。
- `device_smoke.py`（5024 行，`SmokeRun` 类 2475 行）按探针拆模块，参数解析、编排、报告分离。
- 识别任务的位置元组（`recognition_worker.py:94-99`、`endpoint_pipeline.py:246-262,373-399`）改为 dataclass。
- `bridge.py` 约 60 个纯转发方法可收敛。

其他静态信号（参考性质，不是缺陷清单）：宽泛的 `except Exception` 79 处，集中在 `announcement_controller.py`（7）、`tools/check_failure_ui.py`（6）、`vision_controller.py`（5）、`session_controller.py`（5）；`try/except/pass` 13 处。多数是有意的隔离边界，但没有记录异常类型时会掩盖 H-5 这类问题。循环内闭包捕获循环变量（`B023`）在 `recognition_worker.py:113,417`、`tls_stream.py:85` 各有出现，均在当次迭代内同步调用，当前无害。代码里没有 TODO / FIXME 标记。

## 测试质量

- 规模充足：host 2710 项、tooling 180 项、installer 24 项，运行约 4 分钟。
- Java 侧没有 JUnit。`app/src/test` 的两个文件是 `main()` 式检查，由 host 的 Python 测试用 `javac` 驱动；多数校验类以这种方式被覆盖。完全没有测试的类：`PreferenceMemoryDialog`、`DotChatsDialog`、`TaskStepsView`、`KaraokeTextView`、`PhotoReviewPanel`、`PhotoCropView`，以及检测器 `dump()` 的参数解析。
- 约 42 个 host 测试文件把 Java 源码交给 `javac` / `java` 做真实执行，这是有效的行为测试。另有一部分测试读取源码或配置文本并断言其中包含某个字符串（启发式检索到十余个候选文件，没有逐个确认）；这类断言在重构时会误报，也不能证明行为，建议逐步替换为行为断言。
- 时序依赖见 T-3；显式 opt-in 后仍跳过见 T-7；环境敏感见 T-6。
- 本轮发现对应的测试缺口：H-1、H-2 的跨线程测试，H-3 的超时归属测试，H-5 心跳遇意外异常的测试，F-1、F-2、F-3、F-6、F-7 的反例测试。

## 看过未发现问题的部分

以下方面做了针对性检查，没有发现问题。这是"本轮没找到"，不是"证明不存在"。

- **安装约束**：目标设备判定、参数数组方式调用子进程、快照 → 流式哈希 → 回调 → 已装哈希的校验链、清理与退出码；没有 Root、刷机、网络 ADB、终止 OEM 服务或静默卸载的路径。
- **CI**：5 个 Action 全部固定到 40 位 SHA（SHA 与版本标签的对应关系离线无法核对）；默认只读权限；没有 `pull_request_target`、没有缓存；Gradle 与 CLI 下载校验 SHA-256。
- **敏感内容检索**：仓库里没有发现密钥、私网 IP、个人路径或设备序列号。本地是 50 个提交的浅克隆，更早的历史没有检查。
- **来源读取的 SSRF 防护**：字符串过滤、全部 DNS 结果校验、连接地址钉住、逐跳重新校验、禁用代理和自动重定向。
- **协议边界**：帧长上限、JSON 深度与节点预算、HMAC 与令牌的常量时间比较。
- **访客/主人隔离**与"设备不能选主人"；Codex 审批一律拒绝或空授权。
- **视觉帧**：request ID、generation、哈希、尺寸绑定；JPEG 仅在内存；桌面照片目录 0700、文件 0600，使用 `O_EXCL | O_NOFOLLOW`。
- **持久化**：写盘均为临时文件 + fsync + rename + 目录 fsync，权限 0600/0700；路径只来自正则校验过的标识或已校验的绝对路径。功能与存储模块范围内没有日志或 print 调用。
- **自动语音**：一次性、30 秒等待、手动 Stop 与 `onStop` 取消；重连和普通 `onResume` 不重新武装（A-6 的进程重建场景除外）。
- **Android 生命周期**：Home 停止采集并释放资源；全部通道丢失时停止；TLS 指纹固定；TLS 关闭不在 UI 线程；`DesktopEntryService` 不持有麦克风、相机或网络；minSdk 21 的 API 守卫无遗漏；没有静态持有 Activity。
- **字幕**：三行裁剪和翻页算术逐步核对，没有发现第四行残片或遮挡进度条。
- **任务界面**：没有种子数据；已保存结果有查看、复制、删除入口。
- **检测器**：`DeviceReport` 不采集序列号、IMEI、MAC 或 ANDROID_ID；`RamPcmLease` 仅在内存、读后清零。
- **播放完成与 publication 锁序**、`submit_text` 准入回滚、唤醒子进程 IPC、提醒 revision：未见问题。

## 局限

- 全部是静态阅读加离线测试。并发类发现（H-1、H-2、H-7、H-8、F-8、A-7）没有实测到竞态，是否可触发、频率多高都未知。
- 没有实机验证。A-1 到 A-4、U-1、U-3 的实际影响取决于设备和固件行为。
- host 的诊断/证据类工具（`device_smoke.py`、`audit_device_suite.py`、`service_smoke.py`、`playback_evidence.py`、`speaker_harness.py`、`asr_evaluation.py`、`synthetic_asr.py`、`known_speech_*.py`、`login_probe.py` 等，约 1.5 万行）只做了结构和复杂度统计，以及对 `upgrade_preparation.py`、`diagnostics.py` 的通读，没有逐文件审查。
- `report_voice.py`、`meeting_analysis.py`、`meeting_asr.py`、`dot_document.py`、`composition.py`、`usb_handover.py` 等为略读。
- 没有构建 APK、没有跑 Android lint、Java 格式检查和 Swift 构建。
- 测试套件是抽样评估，没有逐个审查 231 个测试文件。
- 标为"未复核"的条目来自分模块审查的读代码结论，实施前应先写出失败的回归测试来确认。

## 建议的处理顺序

1. **改动小、收益明确的**：S-2（`sys.path` 增长）、H-3（播报超时取消）、F-1（偏好误解析）、U-2（空指针）、T-1（安装回执）、T-5、T-6，以及注释专项里的过期注释。
2. **隐私与安全边界**：H-4（自检文本落盘）、U-1（录音副本）、S-1（认证总期限）、S-3（netrc）、A-1（USB 回环信任，至少先补文档）、T-2（依赖哈希）。
3. **取消与过期数据隔离**：A-2、A-3，与 H-1、H-2 一起补跨线程回归。
4. **存储健壮性**：F-2、F-3、F-4、F-5。
5. **注释与结构**：按注释专项的建议 2–5 分批进行；职责拆分单独成批。

每一项在实施前仍需按 AGENTS.md 的要求补对应的回归测试，并与实机验收分开记录。
