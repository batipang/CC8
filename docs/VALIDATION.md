# 当前验证记录

最初仓库整理检查：Mac 后台源码 0.11.1，261 项后台测试中 257 通过、4 项私人录音测试跳过；Android 0.8.6 与检测器的构建 / lint 通过，详见[第二轮代码检查记录](CODE_REVIEW.md)。后续验证按下文各批次记录解释；候选源码通过检查不等于已替换运行实例。

整理日期：2026-11-01。本页区分此前已经完成的实机观察与本次源码整理检查，不包含设备唯一标识或原始私有日志。

| 项目 | 当前证据与结论 |
| --- | --- |
| APK 安装 | TG_S6B01 / AILABS_S6 / SDK 27 / 固件 6.1.8-RS-20251125.1919；原生 PackageInstaller shell UID 2000 成功，安装后哈希核对通过 |
| 检测器 | 1.0.1 已安装；参考音、录音与 10 秒同时录放实机完成；纯播放录音仍有回声，不能依赖系统 AEC 声明判断消回声效果 |
| 助手 | APK 0.5.0 / Mac 后台 0.8.0；MiniMax ASR → Dot → MiniMax TTS 链路已获用户确认识别与回答正常 |
| 普通插话 | 用户确认普通音量可先暂停、再正确回答新问题；字幕与嘴型同步停，修复后未复现安静时突然停播；不代表所有环境都免误触发 |
| USB / Wi-Fi | 已完成真实局域网 TLS 播报、逻辑 USB 抑制切换及自动返回；物理 USB 拔插过程中的完整语音双工验收仍待用户确认 |
| 显示 | 采用已选定 G1 大黄蜂头像、独立青白粒子声波区、播放头驱动的字幕高亮；嘴型是能量包络近似 |
| 待机 | 900 秒无交互的状态逻辑有回归测试；未完成持续 15 分钟全链路用户验收 |
| 摄像头 | 正常光照下 Camera1 前置、640×480 NV21，20 秒 395 帧、19.8 fps、0 个坏帧；用户确认清晰、方向正确、动作流畅；关闭与后台释放通过；后续桌面拍照已验证私有导出及聊天显示（见下文） |
| 长期运行 | 远场、方言、高音量、多说话人、时钟漂移、长时间稳定性与精确端到端延迟尚无完整验收 |

CC8 未发现可用的标准 Android RecognitionService，尚无设备原厂识别的可比准确率。本机 SenseVoice 与 MiniMax 做过小规模合成样本比较，用户选择 MiniMax ASR；这些样本不构成通用语音识别准确率排名。

## 初次仓库整理检查

独立发布目录复用本机已安装的 Android 工具链 / Gradle 下载缓存，重新生成了开发签名与 APK；没有复用原 APK 或签名私钥。检查结果：

- 安装器：6 项离线测试通过，Java / DEX 助手构建及 `--check` 通过。
- 离线检测器：15 项 Java 音频 / WAV 检查通过，`assembleRelease` / `lintRelease` 通过。
- 语音助手：`assembleRelease` / `lintRelease` 通过。两个 APK 的 v1 / v2 / v3 签名与 zipalign 校验通过。
- 新建 Python 3.13 语音虚拟环境，依赖安装及停止词模型下载 / 哈希校验通过；实际加载本地关键词模型和 AEC3，静音处理通过。
- 新虚拟环境运行 161 项语音后台回归：无失败，4 项因未收录私人设备录音而跳过。
- 文档本地链接与待提交文件的路径 / 密钥模式检查通过。

本次仅对独立发布副本进行构建 / 离线回归，没有重新安装设备应用、开启麦克风 / 摄像头或替换正在运行的后台。私人录音依赖测试在新副本中按设计跳过。

## 全面代码检查后的修复版

源码版本更新为 Android 0.5.1 / Mac 后台 0.8.1；本次没有向设备安装或重启原后台。修改内容、复现方式与验证结果见 [代码检查记录](CODE_REVIEW.md)。此前实机结论仍针对原版本，不视作新版本的设备验收。

## 桌面入口部署

Android 0.5.2 已使用原签名安装到当前 CC8，Mac 后台已更新为 0.8.1；原配对和配置保留。桌面入口、Home / Back 返回及重复打开已通过实机检查，详见 [DESKTOP_ENTRY.md](DESKTOP_ENTRY.md)。这不等同于完成摄像头白天画质或 Wi-Fi 全双工插话验收。

## 打开即开始语音部署

2026-11-01：Android 0.5.3 / versionCode 21 使用原签名覆盖安装到当前 CC8，安装完成与设备 APK SHA-256 一致性检查通过；Mac 后台更新为 0.8.2。保留原配对、音色、MiniMax ASR 与 Dot 配置。

- 从桌面入口打开后，实际麦克风帧持续增长并启动校准；后台重启、双通道重新连接后再从桌面打开同样通过。相同录音 epoch 不重复启动校准。
- 手动停止后，完整停止 / 无界面重启 Mac 后台，连接恢复但不重新开麦；Home 后实际采集与后台 active 均停止。
- 再从桌面打开，自动完成参考音校准；最终界面在前台、麦克风开启、后台 active，calibrating 已结束。此次验证依据设备状态、实际帧数与后台事件，没有取回或保存用户原始录音。
- `--no-auto-voice` 打开页面不启动麦克风；`--no-open` 启动后台不拉起界面。本机只播报技能已改用这些入口，避免默认打开行为顺带开麦。
- 182 项语音回归无失败，4 项因私有录音未纳入仓库而跳过；包括真实 Java 一次性启动门控、短暂暂停 / 恢复、真正后台取消，以及 session_start / session_rebind 顺序竞争。Python 静态错误检查通过；Android release 构建 / lint、签名与 zipalign 校验通过。

本轮没有开启摄像头，也没有替代物理 USB 拔插及完整 Wi-Fi 语音双工验收。首次安装后的系统权限弹窗和真实断网 30 秒等待界面未重新做设备验收；相关门控已覆盖离线回归。

## 正常光照下摄像头实机验收

2026-11-01，Android 0.5.3：在既有检测页主动执行本地预览，Android CAMERA 权限已授予。开始测试前助手麦克风已关闭，测试期间及返回助手后均保持关闭。

- 20 秒预览收到 395 帧 NV21（640×480），平均 19.8 fps，0 个缓冲长度错误，TextureView 更新 396 次；首帧延迟记录为 74 ms。用户确认画面清晰、方向正确，挥手动作流畅。
- 20 秒结束自动释放；手动停止、释放后重新开流、Home 后后台释放均通过。恢复检测页未自动开流，返回助手界面正常。
- 系统报告 1 路前置摄像头；支持列表包含最高 1920×1088 预览及 2560×1920 拍照尺寸。本轮只实测 640×480 预览，其他尺寸和静态拍照均未验证，不能据此推断传感器原生分辨率。
- 图像仅在 CC8 屏幕本地预览，没有取回截图、保存照片或上传图像；只保存非图像检测参数。没有修改应用代码或固件。

结论：已验证当前 CC8 可作为助手图像输入的采集端；尚未接入 Dot 图像理解、语音触发拍照或持续视觉功能。后续接入应明确提供前台拍摄入口、传输范围及停止方式。

## 摄像头图片交给 Dot 分析

2026-11-01：Android 0.6.0 / versionCode 22、Mac 后台 0.9.0 已部署到当前 CC8。保留原签名、配对、ASR、音色、Dot 模型和工具配置；没有启用 Claude、Root、网络 ADB 或持续视频。使用方式和数据边界见 [VISION.md](VISION.md)。

- 实机按钮触发后，从原生相机取得一张 640×480 JPEG，通过当前 USB 应用通道完成分块和哈希校验，Dot 收到内联图片并完成回答，屏幕显示 Dot 结果，原 MiniMax 音色实际渲染播报音频。测试期间麦克风始终关闭；拍摄结束即释放相机，在等待 Dot 和播报期间相机保持关闭。
- 手动停止看图后，摄像头释放、看图状态退出，未出现延迟提交或延迟回答。已拍摄 / 已提交后停止不撤回此前已发出的图片；取消会阻止后续旧结果进入播报。
- 新一轮请求进行中按 Home，实际后台释放和取消通过；用不自动开麦的维护入口返回前台，没有重启摄像头或麦克风。普通启动仍按用户要求自动开启语音。
- 201 项回归无失败，4 项依赖未纳入仓库的私有录音而跳过。新增视觉测试包含明确意图与否定 / 引用 / 历史描述误触发、分块 / 哈希 / 尺寸 / 超时、旧代取消、图片进入 Dot、无麦克风看图、重复音频握手和真正开启新语音时的视觉取消。
- Android release 编译 / lint、Python 静态错误检查、签名和 zipalign 通过。最终安装后的 APK SHA-256 与本机签名产物一致；文档链接和待发布文件检查通过。

实机完成的是 USB 下按次取图、Dot 回答与 TTS，以及取消 / 前后台生命周期。不同实物的语义识别准确率、真实口述视觉触发、Wi-Fi 图片全链路和更高分辨率采集尚未完成用户验收；此前正常光照预览的用户确认不等同于对本次 Dot 识物准确性的确认。没有把原始摄像头图片取回聊天、保存到应用文件或纳入仓库，只记录非图像验收结论。Dot 模型服务处理了本轮主动提交的图片。

## 看图前 5 秒调整预览

2026-11-01：Android 0.6.1 / versionCode 23、Mac 后台 0.9.1 使用原签名更新并完成安装哈希核对。按钮和已确认的视觉语音请求继续进入同一个单帧相机组件；现在从第一帧真正显示时开始 5 秒实时预览和倒计时，再采集当前帧。相机初始化时间不占用这 5 秒。

- 当前 CC8 实测：预览已显示 506、1177、1889、2541、3196、3845、4530 ms 时尚未拍摄；实际在 5042 ms 采集一张画面。Dot 图像分析与原音色播报完成，相机随后保持关闭。
- 预览中途取消实测通过：525 ms 时取消，没有生成或提交图片。新一轮会重新等待完整 5 秒，不复用上一轮时钟。
- 测试时麦克风关闭，结束后已恢复更新前开启的语音会话并完成校准，摄像头保持关闭。
- 相机整体上限调整为 11 秒，Mac 取图请求上限为 16 秒，避免 5 秒预览被旧超时提前终止。未启用持续图像上传，只有倒计时结束后的单帧交给 Dot。
- 202 项回归无失败，4 项私有录音测试跳过；新增测试运行真实 Java 计时类，覆盖第一帧前不计时、5000 ms 边界、后续帧不重置、重新开启重置和倒计时。Android 编译 / lint、Python 静态检查、签名和 zipalign 检查通过。

此前用户已反馈一轮看图识别「正确」；这是单轮用户确认，不代表一般识别准确率测评。真实口述入口本轮未重复要求用户发声；语音和按钮共用同一预览 / 采集路径。

## 0.6.1 启动默认开麦复核

2026-11-01：在现有 Android 0.6.1 / Mac 后台 0.9.1 上，分别实测停止本应用进程后以普通 MAIN / LAUNCHER Intent 启动（不附加自动开麦参数），以及 Home 后点击桌面大黄蜂入口。两次均自动连接、开启麦克风并完成校准，全程未点击「开始语音」。最后保持助手在前台、麦克风开启、校准结束，摄像头关闭。本次确认已有默认行为，没有改动应用代码或重新安装；只播报 / 维护的显式不自动开麦入口继续保留。

## 适度降低噪声灵敏度

2026-11-01：仅更新 Mac 后台至 0.9.2，Android 仍为 0.6.1，无需重新安装 APK。近端语音最低能量门槛提高 3 dB、噪声余量提高 2 dB，语音起点改为 140 ms；普通插话的有效语音投票要求增加。保留原句首缓冲、断句尾静音、回声相关性保护及快速停止词参数，详情见 [ARCHITECTURE.md](ARCHITECTURE.md)。

- 206 项回归无失败，4 项因私有录音未纳入仓库而跳过；Python 静态错误检查通过。本轮未改动 Android 源码，未重新构建或安装 APK。
- 新增合成测试：即使 VAD 把 -44 dBFS 弱背景误判为语音，仍不进入识别；130 ms 短促声不形成对话或可信插话；安静底噪下 -38 / -42 dBFS 持续语音仍进入对话。150 ms 短语音与句首缓冲、延迟回声拒绝、独立双讲检测的已有回归通过。这些合成信号不是用户实际音量或真实环境准确率测评。
- 当前运行进程已确认 `bridge_version=0.9.2`、`noise_profile=moderate`、能量门槛 -43 dBFS、噪声余量 12 dB、起点 140 ms；快速停止增益仍为 2.0。更新前开启的麦克风已恢复，连接正常、校准结束。

没有保存本轮原始录音，没有为此开启摄像头。未在真实房间内进行受控噪声 / 轻声 A/B 验收，最终误触发和轻声识别表现仍需用户实际使用反馈。

## 当前 Dot 聊天控制播报与拍照

2026-11-01：Android 0.7.0 / versionCode 24、Mac 后台 0.10.0 已使用原签名部署，安装后的 APK 哈希核对通过。新增 owner-only IPC `camera_snapshot`、私有照片客户端与可复用的桌面拍照技能；已有播报技能继续使用原文朗读入口。使用方法见 [DOT_CONTROL.md](DOT_CONTROL.md)。

- 当前聊天实际请求一张照片：CC8 显示实时预览，5037 ms 后取一张 640×480 JPEG，设备关闭摄像头；Mac 完成请求关联、长度 / 尺寸 / SHA-256 校验，保存为 0600 文件（父目录 0700）。已用图片工具读取并在当前聊天显示实际照片。照片本身、内容描述、绝对路径和指纹不进入仓库。
- 摄像头使用期间断开桌面请求客户端实测通过：225 ms 时取消，设备释放摄像头、没有生成照片。拍照操作保持原麦克风关闭状态，不调用内部 Dot 或 TTS 重复分析 / 播报。更新结束后已恢复用户原来开启的语音状态，完成校准，摄像头关闭。
- 桌面相机授权使用既有配对密钥、当前连接随机挑战、请求 nonce 和递增代次签名。Java 实际授权类回归覆盖错误签名、重复 / 旧代拒绝、连接更新后旧签名拒绝及与 Python 签名一致性。
- 221 项回归无失败，4 项私有录音测试跳过；包含私有 IPC / 文件权限、客户端断开、错误关联 / 哈希 / 大包、过期结果、旧相机任务清理隔离、已有 / 关闭麦克风状态保持、既有播报回执。Android release 构建 / lint 和 Python 静态检查通过；系统 Python 也可导入播报 / 拍照客户端。
- 维护启动入口增加 clear-top，实测从相机检测子页面返回主助手时不会自动开麦；普通启动仍自动开始语音。新的拍照技能、本机既有播报技能和仓库可移植技能均通过 skill-creator 校验。

实测为当前 USB 通道；Wi-Fi 复用同一认证与分块协议，但本轮未另做物理拔线后的照片验收。没有创建定时、后台监控或公开图片链接；只有用户明确要求在聊天看照片的桌面请求才保存 JPEG，原设备端语音 / 按钮分析继续使用内存图片。

## 2026-11-01：0.8.1 方案一界面与任务 / 提醒

- APK 0.8.1 / code26，Mac 后台0.11.0；同签名原生 PackageInstaller 更新及安装后哈希核对通过。APK SHA-256：`9d796b40d8597eebd08517d6e556868e718bedccd01bea046ca9e3414a650614`。
- 247 项后台测试中243项通过，4项缺少私人录音样本而跳过；Android assembleRelease、lintRelease 通过。
- 真实设备验证了 Dot 工具回执、无麦克风文字交办、提醒创建 / 查询 / 取消、到时实际播报完成，以及停止未完成任务。停止朗读不会撤销已完成任务的回执。
- 方案一的排版、短弧光环、独立粒子声波、状态位置和胶囊按钮已用原生截图并排核对；正文42sp保持，其他字号按用户要求放大。原始本机截图保留在忽略目录，完整比对记录在仓库根目录 design-qa.md。
- 正常打开自动开麦和校准通过；最终前台、USB优先、Wi-Fi备用已就绪、麦克风开启、摄像头关闭。此次没有采集相机图像。
- 不以本轮 UI 验证替代物理拔插、远场噪声对照和长期待机验收。

## 2026-11-01：0.8.2 字幕遮挡修复

- 复现 0.8.1 长字幕越出容器、覆盖标题及进度条的情况。0.8.2 / code27 为字幕容器增加显式裁剪，并在播放进度进入新行时保证当前行完整可见；正文保持42sp，方案一的区域坐标不变。
- Android assembleRelease / lintRelease 通过；字幕时间戳、原生播放与字幕时间轴、播放时钟和流式播放共7项相关回归全部通过。
- 原签名、stock PackageInstaller 更新并核对安装后 APK SHA-256：`1cd5574c3faa1fb530b5385af18d8f45cbf15c0a1eaf9b54b11d78f9180ad8b1`。
- CC8 实际完成97字、7行播报，收到 completed；11次状态采样的当前行均完整可见，最大滚动302px，字幕区域与进度条交集均为false。原生截图逐行保存并人工检查首行、中间行和末行，文字没有再越出字幕区域或被进度条覆盖。屏幕证据保留于本机忽略目录，不提交私人界面内容。
- UI验证期间麦克风关闭、摄像头关闭，未更改音频识别、打断或网络协议。
- 更新结束已恢复原先开启的麦克风，校准完成，USB连接、助手前台正常；摄像头保持关闭。

## 2026-11-01：0.8.3 光环时钟与科技风进度

- APK 0.8.3 / code28，后台保持0.11.0；原签名 stock PackageInstaller 更新完成，安装后哈希一致：`412c3cc7d6f0cebc56ac8598cfefd24bb69a03fcbc428f540fa72ecade663183`。
- CC8 当前系统时区 Asia/Shanghai，自动校时开启。原生截图与系统时间核对一致，真实观察到17:26→17:27跨分钟更新和外圈秒点移动。小时/分钟短弧替代原本自由旋转的装饰弧；数字时间位于头像下方且未进入独立声波区域。
- 实际97字、7行播报收到 completed，32段光轨及菱形游标从开头推进到结束。8次状态采样显示字幕当前行完整可见、字幕和光轨无交集、正文42sp；逐行截图保留在忽略目录，检查首行及末行无进度条遮挡。
- Android assembleRelease、lintRelease 通过；字幕时间戳、原生播放与字幕时间轴、播放时钟、流式播放相关7项回归全部通过。未修改识别、打断或网络协议；没有重做真实远场/噪声验收。
- UI验证期间麦克风和摄像头均关闭，没有采集相机图像。
- 结束后已恢复原先开启的语音状态，校准完成，助手前台、USB连接正常；摄像头保持关闭。

## 2026-11-01：0.8.4 长短黄色时钟标记

- Android 0.8.4 / code29，后台保持0.11.0；原签名 stock PackageInstaller 安装完成并核对安装后哈希：`7b1ca52b726310b6b5319bb9a18b13d97129f17b2462ff21dcb3008f20433a0a`。
- 实机1280×800截图检查：17:31时，短粗黄线指向5点与6点之间，长黄线指向31分钟位置；时间来源为Asia/Shanghai系统时钟。44秒、49秒的两帧可见外圈秒点及拖尾前进。可见数字时间与数字底板已移除；线段留在环带内，不遮挡头像，独立声波与其余布局保持。
- Android assembleRelease、lintRelease通过。此次只修改时钟绘制和诊断样式字段，不改变音频/字幕时序或联网逻辑，没有以绘制检查替代语音准确率验收。维护截图期间麦克风关闭、摄像头关闭。
- 更新结束已恢复原先开启的语音状态，校准完成；助手前台、USB连接正常，摄像头关闭。

## 2026-11-01：0.8.5 跑秒增强与整分对齐

- Android 0.8.5 / code30，后台保持0.11.0。原签名 stock PackageInstaller 更新完成并核对安装后哈希：`56c017e8132aedbdab132aa610431354609438da2f571770ff375d2515782e96`。Android assembleRelease / lintRelease通过。
- 跑秒前端亮线由3单位增至6单位，尾迹从11度扩至36度，分三段递减亮度与粗细，秒点加入更大的亮芯。长短黄色时分标记不变，仍不显示数字时钟。
- 真机截图跨过17:37→17:38：截图前后系统时间分别为17:37:59—17:38:01及17:38:01—04，亮点从顶部左侧（约357.2度）到顶部右侧（约12.7度）；后续继续到27.6度、42.5度。角度由截图亮芯相对光环中心估算，截图采集有时间跨度，并非精密计时测量。与00秒12点、顺时针一圈60秒、过12点进入下一分钟的规则一致。
- 49字实际测试播报 completed，字幕42sp、字幕/进度区域无交集。进度条保持已安装的分段光轨；三个新进度样式仅生成待选设计图。
- 本轮维护和截图期间麦克风关闭、摄像头关闭；未采集相机图像，原生截图保留于忽略目录。
- 维护结束后已恢复原先开启的麦克风并完成校准；助手前台、USB连接正常，摄像头保持关闭。

## 2026-11-01：0.8.6 方案二进度条、三行字幕与简洁跑秒

- Android 0.8.6 / code31，后台保持0.11.0；最终原签名 stock PackageInstaller 更新完成，安装后 APK 哈希一致：`40c6721324f3bed2ea413586c4f83df8dfcf0c73b75315023a36ba9458b47278`。
- Android assembleRelease、lintRelease通过。字幕时间戳、原生播放与字幕时间轴、播放时钟、流式播放相关7项回归全部通过。改动不涉及识别、AEC、打断或网络协议，没有重新做远场/噪声语音验收。
- 用户选定原三图中实际显示的第二张“斜切能量舱”。20个青色斜切单元、空心未播部分、切角细框、黄色斜向游标已原生实现，仍沿真实字幕播放进度推进；没有写入设计图的示例任务或提醒。选图和修订图保存在 docs/design/，原界面布局锚点和其它辅助字号保留。
- 实际97字测试自动换成6行、两页并收到 completed；12次采样均为3个完整可见行、当前朗读行可见、字幕/进度区域无交集。实际字体36sp，可见窗口147px，滚动位置0→144。首行、约60%进度及末行截图均检查通过，没有露出第四行残片、没有末页前行碎片，进度条与字幕分离。
- 最终安装包另播报显式换行的固定三行文字，收到 completed，实机状态为36sp、3个完整行、153px窗口、当前行可见、无进度区域交集；已人工检查最终截图。
- 跑秒使用单个连续12度渐变短弧与小亮点，上一版三段式36度拖尾和大光晕已移除。黄色长短时分标记、00秒12点及顺时针真实系统时钟语义保留，没有数字时间。实际帧中的短弧方向和位置随时间移动。
- 所有维护播放和截图检查均保持麦克风关闭、摄像头关闭；截图保留于本机忽略目录，没有采集相机图像。
- 结束后已恢复原先开启的麦克风并完成校准；助手前台、USB连接正常，摄像头保持关闭。

## 2026-11-01：后台 0.11.1 替换运行服务

- 按用户要求，将提交 `1654e90` 的 `bridge.py`、`dot_client.py`、`workspace.py` 部署到原运行目录。部署前核对三份旧文件与 `b70bf3c` 一致，其余运行模块与新仓库一致；使用实际运行的 Python 环境重跑相关 40 项回归，全部通过。
- 在本机忽略的私有目录保存旧源文件及回退清单，停止已核验 UID 和绝对脚本路径的旧服务，再启动新服务。未重复启动另一克隆的后台，运行进程检查为一份；配对、证书和凭证文件哈希保持一致，音色与模型配置沿用原环境。
- 实际运行状态为后台 0.11.1；设备包管理器确认 Android 0.8.6 / code31，没有安装、卸载或清除 APK 数据。设备控制请求收到真实回执：助手在前台、USB 为当前通道、已配对 Wi-Fi 备用在线、媒体音量保持 69%。本轮没有执行物理拔插切换。
- 重启后先确认麦克风关闭，再按更新前已开启的状态，通过正常打开入口恢复语音。校准完成，麦克风帧持续增长，采样期间序号缺口为 0；没有保存原始录音，摄像头与桌面拍照任务保持关闭。新启动日志中没有异常回溯或错误 / 失败 / 重试事件。
- 本轮验证范围为部署、连接往返、既有语音会话恢复及校准；没有重新执行真实问答、自然插话、摄像头识物、提醒送达或长期运行验收。上述离线回归不能替代这些场景的实机结果。


## 2026-11-01：P0 第一批后台 0.12.0

- P0-01：接入有界无内容的阶段诊断、request / generation / Dot turn 关联、任务与朗读结果分离、按来源/实际工具场景统计的样本数与 p50 / p95，以及私有 JSON 导出。所有时间差来自 Mac 单调时钟；播放参考回执明确标为非实际出声时刻。尚未取得真人语音基线，不报告合成或模拟数据为实际准确率/时延。
- P0-02：已在实际运行目录安装并启用当前用户 LaunchAgent，加入覆盖模型加载的 OS 单实例锁、监督进程锁、退出退避、健康检查、日志轮转和明确停止。未从第二个克隆启动服务。正常状态是一份后台进程，识别/回答工作线程均存活，USB 主用与已配对 Wi-Fi 备用在线。
- 当前真实后台进程在麦克风关闭、无在途任务时接受一次受控 SIGKILL。监督服务约 3.97 秒内重建后台并恢复连接；没有打开麦克风、自动播报或重新执行任务。随后明确停止使 enabled=false，观察 6 秒没有自动复活；重新启用后恢复连接。这是单次故障与一次停止/启用验证，不代替十组完整联调。
- 285 项后台回归：281 通过、4 项私人录音依赖跳过、0 失败；包含 24 项新增测试。十轮真实子进程崩溃验证 OS 锁释放，十轮模拟后台故障验证退避和停止，另覆盖日志轮转、诊断脱敏/私有文件、阶段缺失、首包/任务回执、后续句 TTS 错误归因及队列丢弃终态。Ruff F/E9 和 Git 空白检查通过。
- 首次实际导出发现 CLI 文件创建参数不兼容，修正后新增完整导出回归；实际 JSON 已导出到忽略的私有目录，并核对文件 0600、目录 0700。没有把空白实机报告当成语音评测成绩。
- Android 保持已安装的 0.8.6 / code31，本批无 Android 源码或 APK 变更；不重装、不清数据。原模型/音色、凭证、证书、配对及提醒文件哈希保持。结束时麦克风维持更新前关闭状态，相机未打开，未保存原始音视频，也未调用模型测试问答。
- P0-01 / P0-02 仍保留固定真人样本、实际起声时间、十组完整重启及睡眠/登录实测等验收项；P0-03 本地双语唤醒、P0-04 调优、P0-05 物理切换/待机/72 小时及 P0-06 完整错误恢复继续列为未完成。功能与操作说明见 [P0_FOUNDATION.md](P0_FOUNDATION.md)。


## 2026-11-01：0.12.1 去除启动女声

- 按用户要求，启动不再读取固定女声参考，改用当前 MiniMax 大黄蜂音色合成“汽车人，变形！”；该句同时作为校准参考，只播放一次。保留自实际播放开始计时的约 6.4 秒采样/尾音窗口，剩余时间安静等待，不重复口号。检测器和离线回放所需原参考文件保留。
- 合成等待期间的收音不进入对齐窗口；取消后的合成结果不能清掉新会话校准帧或启动旧播放。空白、过短/过长、静音或格式错误 PCM 会明确失败，不回退到女声。
- 290 项后台回归：286 通过、4 项私人录音依赖跳过；新增 5 项启动测试，覆盖两种模式仅说一次、采样窗口、取消、失败和 PCM 边界，并更新原校准竞态回归以继续进入对齐路径。Ruff F/E9 与 Git 空白检查通过。
- 已部署当前后台并恢复更新前开启的语音会话。单次实机启动返回 38093 帧（约 2.38 秒）播放参考，完成时间对齐和校准，未出现播放/运行错误，也没有第二段提示；麦克风开启、USB 正常、无原始录音保存。对齐相关系数约 0.325 是本次定位信息，不作为一般场景 AEC 或插话质量的验收结论。


## 2026-11-01：0.12.2 修复登录后台的 Dot 启动环境

- 最近真实请求均在连接 Dot 阶段失败，ASR 与 TTS 正常。复现发现，当前 Codex 入口是依赖 /usr/bin/env node 的 npm 包装器；launchd 精简 PATH 不含本机 Node 目录，执行该入口实际返回 127，stderr 指示 Node 不存在。此前统一错误文案隐藏了这一区别。
- 登录服务仅持久化补齐后的 PATH，Codex 子进程也独立补齐 PATH；保留已有目录顺序与当前模型/推理/工具配置，不复制环境密钥进 LaunchAgent。健康报告新增不含路径/密钥的 dot_runtime 依赖就绪位。
- 297 项后台回归：293 通过、4 跳过、0 失败，新增 7 项环境回归。覆盖缺少 Node 的包装器复现、继承顺序、相对目录剔除、不修改原环境、plist 不泄露环境密钥、子进程环境及原生二进制无需 Node 的情形；Ruff F/E9 和 Git 空白检查通过。
- 停止并重新启用实际登录服务后，0.12.2 健康状态显示 Codex / Node 依赖就绪、USB 连接正常。通过当前后台提交一次明确不调用工具的只读连接检查：收到真实 Dot 完成结果、TTS 首包、设备播放参考及播放完成回执，整个检查约 15.2 秒。此为单次连通性检查，不是语音时延基线或普遍性能保证。
- 本轮测试期间麦克风关闭，未保存原始音视频、未重放此前用户请求或执行有副作用任务；测试结束恢复更新前开启的语音会话。保留单次“大黄蜂音色”开场和回声校准；Android 未修改、未重装。

## 2026-11-01：F-04 轻量测试与 F-07 本地质量门槛

- 纯格式基线独立提交 `b9d38752cdbf95cd8ecd169238197a9917391a7b`，登记于 `.git-blame-ignore-revs`。57 个 Python 文件在将多名称 import 拆分归一后 AST 相同，30 个 Java 文件忽略空白/注释后的有效 token 相同；没有混入业务改动。开发依赖、测试入口、钩子和文档使用另一个提交。
- 独立 Python 3.13 开发环境仅安装 `requirements-dev.txt`，`pip check` 通过；没有 MLX、Whisper/SenseVoice 模型或原生 AEC3 依赖。统一入口运行安装器 11 项、后台 297 项（293 通过、4 项私人录音回放显式跳过）、开发工具 15 项，均无失败；另运行真实 Java 音频/编解码 15 项检查。可选依赖不再导致核心 Java 用例因缺少本机 Android 配置而跳过。
- 在不含 `.toolchains`、私人录音、模型或本机配置的干净源码副本中，以仅开发依赖的新虚拟环境、显式 JDK 21、精简 PATH、移除模型 API 环境变量运行统一测试及质量检查，通过。提交候选代码后另做实际 `git clone --no-hardlinks`，同样通过 11 项安装器、297 项后台（4 skip）、15 项工具回归与 15 项 Java 检查，且源码 diff 为空。首次显式下载 Java formatter 后核对固定大小和 SHA-256，后续 check 不下载。当前实测平台仍为 macOS / Apple Silicon，不能据此宣称 Linux CI 已通过。
- 实际负向命令验证：关闭 site-packages 导致缺核心依赖时退出 2；显式错误 JAVA_HOME 退出 2；临时加入多语句坏格式文件使只读检查失败，文件内容不变。工具回归另覆盖缺失/损坏 JAR 不联网、下载校验失败清理、超大下载、运行超时、前一组失败后继续其他组，以及 Java 格式化两遍后仍不稳定必须失败。
- Ruff 0.17.3 lint / format check、google-java-format 1.35.0 check、`pre_commit run --all-files` 两个真实隔离钩子与 Git 空白检查通过。已在当前仓库安装 pre-commit hook；没有覆盖原有 hook。Java 注释二次格式化行为采用有界两遍加最终只读复查，避免 format 返回成功但紧接的 check 失败。
- 格式基线后的最终 Android `assembleRelease` / `lintRelease` 两工程均通过；安装器 helper 本机构建和 `cc8_install.py --check` 通过，未调用设备安装。本轮没有部署后台、安装 APK、操作设备、打开麦克风/摄像头或调用模型；实际运行版本不变。
- F-04 的轻量入口交付；F-07 的本地规则/格式基线/钩子交付。CI 工作流尚不存在，本地通过不写成 CI 通过；F-07 的 CI 验收继续关联 F-05。

## 2026-11-01：F-05 / F-19 真实 CI、安全门槛与 F-07 验收

- [PR #1](https://github.com/batipang/CC8/pull/1) 的 [真实 CI 运行](https://github.com/batipang/CC8/actions/runs/41319975312) 全部 8 个 job 成功：Ubuntu 24.04 与 macOS 15 离线回归、同版 Python/Java/工作流检查、两个 Android 工程编译/lint、脱敏 Git 历史密钥扫描、Python 清单审计，以及 Required checks 汇总。F-07 的原 CI 缺口补齐；不以本轮 Ubuntu 回归宣称完整 Linux 语音运行或实机验收完成。
- 本机轻量测试：安装器 11 项、后台 297 项（293 通过、4 项私人录音显式 skip）、工具 24 项，无 errors/failures；Java 音频 15 项检查、Ruff、Java formatter、actionlint 与真实提交钩子通过。新增 9 项离线回归覆盖审计失败/缺失或陈旧报告/无法查询包、分环境继续执行、扫描脱敏及全历史参数、CLI 校验和/符号链接/缓存篡改边界。
- 首轮 CI 真实暴露 SDK manager 不在 PATH、macOS 14 退役 brownout；改为显式 SDK 路径和 macOS 15 后复跑全绿。保留 [首轮失败记录](https://github.com/batipang/CC8/actions/runs/41319754527)。在这次失败状态下，main 已启用 Required checks 严格必需检查（绑定 GitHub Actions、管理员适用），API 实际确认 PR 的 merge state 为 BLOCKED；未调用 merge 尝试绕过。修复后才允许合入。另启用仓库 Actions SHA 固定策略，默认 token 保持只读。
- gitleaks 8.30.1 全历史扫描仅发现一处误报：SenseVoice 公共词表 `tokens.txt` 的 SHA-256。`.gitleaksignore` 只登记原提交/文件/规则/行号指纹。隔离临时 Git 仓库放入随机生成的模拟 API key，扫描返回 1，verbose 输出也未包含原值；没有把模拟值或私人密钥加入本仓库，也没有上传泄露报告。
- pip-audit 2.10.1 首次在源码声明的 MLX 0.29.3 检出两个独立已知漏洞（服务返回 3 条记录，包含一条重复）。源码锁将 mlx / mlx-metal 同步修补到 0.29.4，不加漏洞忽略项；开发 19、审计工具 27、运行清单 26 个包共 72 条环境记录再次审计，零已知漏洞、零查询 skip。不同环境重复包分别审计，不把版本冲突合并成一个环境。审计范围限定声明包，不代表所有间接依赖、Gradle 或模型内容均已扫描。
- 在独立 Apple Silicon / CPython 3.13 环境安装 mlx / mlx-metal 0.29.4 与 numpy 2.3.4，pip check、正常 NPY 数组加载和求和结果核对通过，没有加载模型或采集录音。当前运行目录和实际 voice venv 未修改，语音模型兼容、真实问答与运行环境补丁部署留待单独验证。
- Dependabot 配置为每周检查 pip、GitHub Actions 和两个 Gradle 工程，只生成更新 PR、不自动合并/发布。工具与模型下载来源、固定校验值、唯一误报证据及当前覆盖限制见 [CI_SECURITY.md](CI_SECURITY.md)。CI 仅保留 lint 和依赖审计公开信息报告 7 天，不上传 APK、开发签名、用户数据或本机配置。
- 本轮未操作设备、打开麦克风/相机、部署后台、安装 APK或调用任何模型；当前实际运行助手不变。F-05 / F-07 / F-19 按上述范围完成，后续 F-20 / F-08 / F-09 与运行补丁部署仍在 TODO。

### F-19：首次真实 Dependabot 更新补验

main 的 [CI 运行](https://github.com/batipang/CC8/actions/runs/41320525409) 已全绿。GitHub Actions / Gradle 更新器成功并产生待审 PR，但首次 pip updater 报错，原因是上游文件发现器只递归取回 .txt/.in，未取回原跨目录 .lock 引用；此时将 F-19 恢复为未完成。

[PR #5](https://github.com/batipang/CC8/pull/5) 保留原 .lock 为 -r 兼容入口，将唯一版本列表移到标准 `cc8-assistant/host/requirements-runtime.txt`，版本未改变；三份清单审计及 PR CI 再次通过后合入。通过已登录的仓库网页 Check for updates 触发真实 pip 更新，[复跑 41321754075](https://github.com/batipang/CC8/actions/runs/41321754075) 成功，F-19 才重新关闭。没有用普通 Actions 重跑失败 API 的 403 作为成功证据，也没有忽略运行清单或自动合入 bot 更新。

2026-11-02 补验发现：此前审计兼容 -r 入口时，--disable-pip 返回空依赖列表，退出码与零告警没有揭示漏扫。该阶段的“审计通过”不代表实际覆盖运行清单。[PR #12](https://github.com/batipang/CC8/pull/12) 改为直接审计标准清单，逐包核对规范化名称、固定版本和数量，并拒绝空/未固定/include 清单；新增 3 项回归覆盖空报告与错误包/版本等漏扫情形。[真实 CI 41324197547](https://github.com/batipang/CC8/actions/runs/41324197547) 通过，运行清单实际返回 26 个包，非零覆盖核验通过，不再仅凭退出码结项。

## 2026-11-01：F-20 协议边界与 TLS 生命周期

- [PR #11](https://github.com/batipang/CC8/pull/11) 已在 [真实 PR CI](https://github.com/batipang/CC8/actions/runs/41322860864) 全部 8 个 job 通过后合入，随后 [main CI](https://github.com/batipang/CC8/actions/runs/41323092681) 全绿。后台候选 0.12.3，未部署到实际运行目录。
- 本机安装器 11、后台 315（311 通过、4 私人录音 skip）、工具 24、Java 音频 15 项检查通过；8 组固定种子 Hypothesis 属性测试各最多 150 个有效生成例，加显式包长/JSON/字段边界例。真实 CI 同时覆盖 Ubuntu / macOS 与两工程 Android 编译/lint。开发依赖新增 Hypothesis 及 sortedcontainers，仍不安装模型。
- 首轮 Linux 并发连接替换复现 SSL 握手异常；修复外部 close 先 shutdown 唤醒、读线程退出后才最终释放描述符的顺序，防止 SSL_read 尚在使用的 FD 被新连接复用。确定性阻塞流回归验证不提前释放描述符；3 个并发客户端改为 20 轮替换后实际 Ubuntu/macOS 均通过，没有降低为串行或取消 Linux 验收。
- 新增严格 UTF-8、重复字段/非有限值/孤立代理字符拒绝、JSON 深度/节点预算、会话字段真实整数/布尔校验、协议版本 2 校验。UDP 深层数组之后合法发现仍响应；认证歧义拒绝后合法认证成功；队列饱和读线程及时退出；未完成 TLS 握手期间关闭后的受管线程在预算内退出。没有将异常正文、令牌或原始音视频加入日志。
- 本轮 Linux 本地 Docker 辅助复现的启动未完成并已终止，不报告为通过；Linux 验收证据来自上面的真实 GitHub Ubuntu job。资源预算、固定 seed 和最小回归见 [PROTOCOL_BOUNDARIES.md](PROTOCOL_BOUNDARIES.md)。旧代播放/交接/全断停止/重连不启用的既有测试继续通过；上述有限自动测试不代表 20 次物理切换、声学或长时运行已完成。
- 用户已说明稍后配合设备旁实测。当前没有打开麦克风/相机、连接设备、改动运行环境、调用模型或部署 APK；P0 的真人与长时验收继续保留待完成。

## 2026-11-02：F-08 严格类型验收

- [PR #13](https://github.com/batipang/CC8/pull/13) 的 [真实 CI](https://github.com/batipang/CC8/actions/runs/41324463866) 8 个 job 全绿后合入 main。mypy 2.4.1 strict 覆盖 protocol / transport / workspace；同一检查进入 quality.py、本地独立 hook 和 CI，无全局忽略或显式 Any。JSON / 流 / 事件 / 回执类型与运行时校验相接，cast 限定在已验证的反序列化边界并附说明。
- 本机安装器 11、后台 316（312 通过、4 私人录音 skip）、工具 27、Java 音频 15 项通过；真实 mypy 负向子进程以 1 退出，分别拒绝字符串 generation、错误任务 generation 和字符串音频流。新增 JSON 非值对象/非字符串键、配对文件类型/重复键/长度边界回归；没有以注解替代这些动态检查。
- 新配置模块继续使用相同 strict 规则；不宣称 bridge、DSP、所有调用方或第三方原生库已有全量类型证明。本轮未部署、采集或访问模型。

## 2026-11-02：F-09 配置软件候选（待 CI）

- 候选后台 0.12.4 新增同源非敏感 schema、不可变快照、来源优先级、明示错误、私有覆盖迁移/恢复；接入 ASR / TTS / 大脑选择、交互 launcher、登录服务和两个构建入口。Dot 模型/工具仍继承桌面，MiniMax 密钥、配对与原签名文件不迁移到普通配置。
- 本机安装器 11、后台 335（331 通过、4 私人录音 skip）、工具 27、Java 音频 15 项通过；配置专项 19 项检查通过。包含来源/数值与秘密不入日志、权限/打开后再次核验、目录/符号链接/大小/深度/重复字段、迁移失败保留、写入/重置回滚、已有外部目录权限保留、真实最小 PATH 子进程和模型加载前失败。两份配置模块使 strict 范围增至 5，检查与真实提交钩子使用相同入口。
- 使用真实 MiniMax 客户端编码与模拟 HTTP 响应核对模型、voice、speed、连接/读取 timeout；没有调用云端。登录 payload 只继承明确的非敏感白名单覆盖，API key 与其他环境秘密不进入 plist。已验证的 TLS 发现与 USB 配置返回实际绑定端口，改变端口仍要求原 USB 配对下发。
- 两个 Android `assembleRelease` / `lintRelease` 本机通过，未安装；两构建器对缺失的显式签名引用均在生成证书前失败。335 项总数不包含模型准确率、真人语音、设备旁操作或长期观察；实际运行目录、进程与已装 APK 均未更新。
# F-15 统一入口（2026-11-02）

新增 `./cc8 setup / doctor / run`、dev/android/voice 三类 profile、只读就绪报告，以及跨工作区 OS 排他锁；旧版 bridge / enabled supervisor 也会被只读识别。直接 voice setup 和 bridge 都使用同一运行锁，不在模型准备/加载时互相修改环境。

本机全套离线检查：installer 11、host 341（4 项私人录音明确 skip）、tooling 38、Java 15 checks；之后新增的显式签名引用 / 前台与停止路由两项连同原 CLI 共 13 项通过。Ruff / Java formatter / 5 模块 strict mypy 通过。锁竞争用两个真实 Python 子进程、两个临时 checkout 和临时 owner 锁验证，SIGKILL 后锁由 OS 释放；不使用当前助手的锁或进程。

把当前候选的公开源码复制到临时干净目录，使用全新 owner HOME / venv 实际执行 `setup --profile dev`，27 项固定依赖和校验 Java formatter 安装成功；随后从 `/`、`PATH=/usr/bin:/bin`、显式 JDK 的条件运行 dev doctor，退出 0。原仓库 dev / Android doctor 通过；voice doctor / run 准确报告本仓库凭证/包版本不足及另一目录运行中，退出 2，不创建第二个后台。

未在本轮重新安装 Android/voice 环境、验证云端请求或打开设备采集。当前运行实例仍是原目录后台 0.12.2 / APK 0.8.6，未升级或重启；此项不关闭 P0 的真人、物理切换、待机和长期运行验收。

[PR #15](https://github.com/batipang/CC8/pull/15) 的 [真实 CI](https://github.com/batipang/CC8/actions/runs/41330787441) 八项全部通过并合入主分支；实际全套包含 installer 11、host 341（4 private skip）、tooling 40 与 Java 15 checks。

# F-14 签名预检（2026-11-02）

安装前强制验证候选签名和同包名已装 APK 的完整证书集合；候选快照与安装字节绑定，已装包读取前后核对哈希/路径，任何读取失败、变化或签名不一致都在 helper / APK 写入前停止。新增 `--verify-apk` 只读入口和 [SIGNING.md](SIGNING.md)。

本机全套离线检查：installer 16、host 341（4 private skip）、tooling 43、Java 15 checks；Ruff / Java formatting / 5 模块 strict mypy / actionlint 通过。真实 SDK 工具以构建出的 0.8.6 APK 验证 API 27、同证书匹配、临时新钥重签后拒绝、签名内容篡改后拒绝；未使用设备或原私钥做这些临时测试。两个 Android CI job 也加入这一真实 APK 检查。

随后对当前正常 Android CC8（UID 2000 / AILABS_S6 / SDK 27）执行两轮只读预检：新仓库临时开发证书被正确拒绝；原运行目录的历史 0.8.1 APK 证书与现装助手匹配。后者只用于读证书，不安装或降级。临时设备 APK 已清理，未取回图像、录音或用户数据，当前服务/界面/采集状态未改动。待补原钥重建候选预检与真实 CI。

再通过 `CC8_SIGNING_KEYSTORE` 引用原目录 JKS 构建本仓库助手候选：assembleRelease / lintRelease 通过，产物 SHA-256 为 `4c8ec602ecd5d4cada10ebda91be8eb5aa9e6e965fa8634e4f37aa85b4bf5198`；第三轮设备只读预检确认 `matching_installed_signers`。未复制或修改原私钥，未安装 APK。检测器 1.0.1 APK 也通过真实 SDK 同签名 / 换钥 / 篡改检查。剩余为 PR CI；实际升级与回退演练仍归 P1-09。

[PR #16](https://github.com/batipang/CC8/pull/16) 的 [真实 CI](https://github.com/batipang/CC8/actions/runs/41331554254) 八项全部通过并合入主分支，含两个工程实际 APK 的换钥 / 篡改拒绝；F-14 软件与原签名只读验收完成，未安装/降级。

# F-10 独立版本与构建清单（2026-11-02）

新增根 release.json：助手 0.8.7 / code 32、后台 0.12.5、检测器 1.0.1 / code 2、协议 2（设备接受 2～2）。Gradle、BuildConfig、Python 构建脚本、后台 health / status 和握手读取同一版本来源；原运行对 0.8.6 / 0.12.2 只是已知现场记录，不表示候选已部署。

新增源码提交 / 脏状态 / manifest / APK 摘要的构建回执、实际包版本与签名核验、annotated tag / 本地 inventory 的 record / verify 工具。脏候选记录不通过 verify，旧提交/旧产物回执或修改的记录字段/路径拒绝；所有记录固定不宣称设备验收。版本信息纳入 strict mypy，第六模块无全局忽略。

本机完整离线检查 installer 16、host 345（4 private-audio skip）、tooling 47、Java 15 checks；Ruff / Java formatting / strict mypy 6 模块 / actionlint 通过。两个 APK 均经原签名引用 assemble/lint，AAPT 实际版本和 SDK-27 签名与清单一致，脏源码候选 inventory 成功生成，尚不能算干净发布。后续在通过 CI 的合入提交上重建、建立 annotated 源码 tag 并复验 inventory；未部署或打开采集。

[PR #17 CI](https://github.com/batipang/CC8/actions/runs/41332607678) 和 [合入 CI](https://github.com/batipang/CC8/actions/runs/41332798135) 全绿；干净提交 `3850f3e9df6a81eb6288e91832d2c3e3022dfc7e` 已用原钥引用重建两工程，通过实际 record / verify。annotated 源码 tag [foundation-20261102.1](https://github.com/batipang/CC8/tree/foundation-20261102.1) 已推送并核对远端解引用提交；tag inventory 位于本机忽略的 artifacts/releases/foundation-20261102.1.json，带 tag 的 record / verify 通过。助手 APK SHA-256 `05520cd42f480c4ac9fc9c5aac0c13008128c313f014a3e683a81d3dba440262`；检测器 `830290358b0f64b1b12c9545f2b9792cbc66336ee88839b1146ac662809f84df`。未发布 APK 或创建 GitHub Release，未部署、验收设备。后续 main 改动后复验该清单需在对应 tag 源码下进行。

# F-06 第一步候选（2026-11-02，待 CI）

提取 provider 自有资源、USB 子进程流、工作项类型三个模块，保留 bridge 导出；12 个移动定义和未移动的 Bridge / main / run_backend 共全部顶层定义，拆分前后 AST 相同。共享会话/DSP/播放/视觉状态尚留 Bridge，本项保持未完成。所有原离线回归同样通过（16 / 345[4 skip] / 47 / 15 checks），六模块 strict mypy 与格式检查通过；未运行设备或声音对照。线程/资源边界及下一步见 [BRIDGE_REFACTOR.md](BRIDGE_REFACTOR.md)。

[PR #18](https://github.com/batipang/CC8/pull/18) 的 [真实 CI](https://github.com/batipang/CC8/actions/runs/41333342689) 八项通过并合入主分支。

# F-06 视觉生命周期候选（2026-11-02，待 CI）

由 249 行 VisionController 拥有拍照/看图代次、nonce、挑战、inbox 与桌面 waiter，通过 frozen typed callback 接口协调会话，不传整个 Bridge 或共享指标字典。Bridge 入口保留薄转发；原全部 345 项 host 通过（4 私人录音 skip），视觉/桌面照片/取消/断线/异常收尾断言保留，仅更新内部状态路径。Ruff / Java formatting / 六模块 strict mypy 通过（视觉模块本身尚不在 strict 清单）。无摄像头、麦克风、模型调用或当前运行实例操作，F-06 整体未结项。

[PR #19](https://github.com/batipang/CC8/pull/19) 的 [真实 CI](https://github.com/batipang/CC8/actions/runs/41334082851) 八项全部通过并合入。

# F-06 输出/暂停所有权候选（2026-11-02，待 CI）

纯拆分保持参数、协议和行为：输出/字幕/完成 Condition、近期播报与暂停状态集中 PlaybackController，Bridge 保留同签名转发；设备事件和时钟通过明确端口协调。内部测试状态路径迁移后，同套 host 345 项通过（4 private skip），Ruff / Java formatting / 六模块 strict mypy 通过；未接设备、调用云端或读私人音频。

审计完成等待的锁路径时，在基线 `462f20d6c4330984db3ed22f5c19d742cc157afd` 和纯拆分候选两份源码，均以两个受控线程、合成零 PCM 和屏障复现 LOCK_CYCLE：持有 done 的 pause timeout 等 guard，同时外层持有 guard 的 cancel 等 done。每个复现进程约 2 秒退出，未干扰运行实例。这是原问题而非拆分引入；后续独立修复提交将通过同一复现及有上限子进程回归验收，不把既有 345 项通过当成已解除死锁。

纯重构提交 `6f01187` 后独立修复：pause wait / cancel 移出完成 Condition 临界区，重新检查 ACK；暂停超时在 session guard 内重新核对 generation，旧超时不能取消新请求。新增子进程回归在纯重构上有界失败（6.2 秒，锁循环），修复后通过（约 0.17 秒），并验证只由 owner cancel 推进一次 generation。测试子进程上限 12 秒、检查线程异常/未退出，使用合成零 PCM、假发送，不启动 provider 或设备。

修复后完整离线 installer 16、host 346（4 private skip）、tooling 47、Java 15 checks；Ruff / Java formatting / strict mypy 六模块通过。后台候选版本独立升至 0.12.6 / foundation-20261102.2，APK 0.8.7 / 协议 2 不变，旧 .1 tag 仍指原 0.12.5 快照。本轮未构建/安装设备包、升级/重启运行实例或打开采集；待真实 CI，不关闭 F-06 和 P0。

[PR #20](https://github.com/batipang/CC8/pull/20) 的 [真实 CI](https://github.com/batipang/CC8/actions/runs/41336074661) 八项全部通过并合入，Linux / macOS 均运行新死锁回归；不代表旧运行实例已经获得修复。

# F-06 收音三层候选（2026-11-02，待 CI）

有界包调度、AEC/参考资源、VAD/断句证据分别归 CapturePipeline / EchoPipeline / EndpointPipeline，通过 frozen hooks 和 CleanFrame 交互。原协议验证、纳秒时钟、80/120 ms lookahead、64 包上限、校准播放窗口、stop AEC/NS 分支及所有门槛保留；Bridge 入口和校准提示流程保持。新模块 103 / 140 / 180 行，Bridge 1547 行，F-06 仍未完成。

基线 `12c218ca5a1d76377e36d6c9786304a0fec4796c` 与候选输入相同的 64 对合成包、固定时钟、假 AEC / VAD：7 段话语与参考内容摘要、generation、echo/pause 证据和计数完全相同（mic 8960、render 10240 samples、0 gap、7 utterances）。这是离线行为对照，不是实机 AEC 或真人准确率结论。没有读取私人音频或调用 provider。

当前完整离线 installer 16、host 346（4 private skip）、tooling 47、Java 15 checks，以及 Ruff / Java formatting / strict mypy 六模块通过；新动态/native 组件尚未加入 strict 清单，未冒充全模块类型覆盖。未部署、重启或打开设备采集，等待真实 CI。

[PR #21](https://github.com/batipang/CC8/pull/21) 的 [真实 CI](https://github.com/batipang/CC8/actions/runs/41338562794) 全部通过并合入。首次 actionlint 下载 HTTP 504（未到代码检查）后，只重跑失败任务成功；校验版本/摘要与门槛未变。

# F-06 识别队列/worker 候选（2026-11-02，待 CI）

由 RecognitionWorker 拥有队列/溢出取消/丢弃、busy 与识别线程，Bridge 保留原入口，跨会话、provider、播放、指标和回复仅用明确端口。保留原现场七字段与旧回放路径、回声/停止/文本唤醒/恢复行为，未改门槛/上传规则、未把 KWS 误标已完成。原 host 347 项通过（4 private skip），Ruff / Java formatting / strict 六模块通过；新识别组件尚未在 strict 范围。

线程故障、队列溢出、忙闲等待、旧代次和误暂停恢复的原断言保持，仅迁移内部字段路径；单独溢出用例冻结 Thread.start 后构建真实 façade，避免未初始化对象伪装队列拥有者。Bridge 1387 行，F-06 保持未完成。无设备、云端、私人音频或当前服务操作，待真实 CI。


识别组件新增 one-shot 启动锁和两个并发启动的回归：只创建一个 worker、不调用 provider，另一个启动明确拒绝。加入后全 host 347 项通过（4 private skip）。


### F-06 第六步：回复所有权（未部署）

ResponseWorker 接管有界队列、单消费者、busy 与 RAM-only 历史；任务状态继续通过 Workspace 方法维护，接口动态查询 provider 并保留原 façade，未改变工具权限、调用次数、音色或音频参数。并发启动明确拒绝第二个消费者。实际本地 installer 16、host 348（4 private skip）、tooling 47、Java 15 checks、Ruff / Java / strict 六模块全部通过。原 text_done 断线后第二条请求仍执行、已完成真实任务在停止朗读后仍完成、视觉/提醒忙闲与旧代次隔离均保持。未进行真人/设备或私有音频验证；F-06 和所有实体 P0 验收仍开放，待 CI。


F-06 独立缺陷回归：实际注册的 SIGUSR1 handler 在第三步之后漏迁移 bridge.playing，负向测试两处确认 AttributeError；修复查询 playback.playing 后，新测试验证四种应忽略状态和一个允许状态，线程/语音提供方与设备均替身，未发送真实信号或启用采集。此修复与第六步纯重构分开提交。


### F-06 第七步：校准 / 显式自检（未部署）

参考 PCM 校验、开场音、真正开播后的采样窗口和旧 generation 隔离保留；CalibrationController 通过明确端口管理状态，不打开捕获或读取声音文件。新增有界合成时钟测试完整运行通过/残留候选失败/识别排空不完整/取消/异常路径，无真实 sleep 或提供方。installer 16、host 353（4 private skip）、tooling 47、Java 15 checks、Ruff / Java / strict 六模块通过。校准新模块本身未声称 strict 验收，真实 AEC/唤醒/声音体验仍待用户配合。Bridge 1159 行，F-06 未结束，待 CI。


第七步另一个纯重构提交集中六组件装配：六份绑定构造 AST 在明确名称映射后完全一致，线程启动顺序保持全部组件发布后再启动。全套离线与质量复验通过，计数同上；Bridge 970 行，composition.py 236 行。未部署。


### F-06 第八步：播报 / 提醒 / 控制回执（未部署）

三个独立所有者经明确接口收拢 announcement generation、提醒 pending/revision 与设备请求 waiter；原事务/忙闲门槛、120 字分段、队列满拒绝、5 秒超时、finally waiter 释放保留。新增虚拟通道并发倒序确认/白名单、超时/断线清理、非法动作拒绝回归。原提醒改版旧回执、停止播报不撤销已完成工作、IPC 播报完成和磁盘异常回归保留。installer 16、host 356（4 private skip）、tooling 47、Java 15 checks 及质量通过；严格范围仍六模块。Bridge 829 行，F-06 / 物理 P0 保持开放，待 CI；未更动运行安装。


### F-06 第九步：进程运行入口（未部署）

run_backend 原体经显式工厂/目录/描述绑定后 AST 等价，原 façade 和锁覆盖保留；app_runtime 191 行、Bridge 699 行。installer 16、host 356（4 private skip）、tooling 47、Java 15 checks 和质量通过，含实际信号注册、启动错误先于 provider、最小 PATH 和 USB 排除网络/多设备。动态入口新模块不计入 strict 六模块。没有运行新后台或更动部署，F-06 保持开放，待 CI。


### F-06 第十步：会话与只读报告（未部署）

SessionController 唯一拥有会话/代次/连接/待机与两把原锁；兼容 façade 方法和 IPC 只读 generation / guard。Bridge/入口/其他组件只调用所属命令，没有另一份会话状态；ACK generation 读取保持无锁，未引入新的锁顺序。合入基线 830454e 对同一合成操作的 16 份状态、12 条控制事件及全部指标完全一致；不是真实待机/拔插。另一个纯提交移入只读报告，合成固定时钟下 health 18 字段、status 48 字段和脱敏异常日志与会话基线一致，健康不带识别正文、异常 sentinel 不进日志。

installer 16、host 356（4 private skip）、tooling 47、Java 15 checks、Ruff / Java / strict 六模块通过。Bridge 403 / Session 349 / 装配 363 / 报告 163 行；新动态模块不计入 strict。F-06 软件所有权已收敛、待 CI，声音同样本与实机部分仍开放；候选未替换当前助手，无设备、云服务或私人音频。P0 所有物理/真人项目仍须单独验收。


### P0-01：分轮诊断软件（未更新运行安装）

新增 DiagnosticRun / cc8 measure：默认排除第一窗口、200 个合成请求跨小窗口仍去重保留、阶段/终态冲突原子拒绝、10000 容量有界、白名单脱敏、覆盖缺口和任务/朗读终态分立。共享原汇总函数为独立纯重构提交；整体 IPC 截止时间为独立修复提交，持续滴流回归约 80 ms 内按总预算终止，不受每片重置延长。采集每 30 秒及退出时原子写 owner-only 文件，拒绝已有/替换/不私有目录，不开任何采集资源。

installer 16、host 376（4 private skip）、tooling 47、Java 15 checks 与质量通过，新增 20 项覆盖跨窗口/去重/拒绝/容量、整体 deadline、真实虚拟 Unix IPC / 最小 PATH root CLI、文件权限/替换、Ctrl+C 和查询间断。另在 `/` 和 `/usr/bin:/bin`、系统 Python 3.9 下，对原 TmallGenie 运行后台仅执行 0.2 秒两次 diagnostics 读取：退出 0，排除历史 48 项，新样本 0，报告 0600；临时完整报告已删除，仅保留无 ID/正文的烟测摘要。没有升级或重启后台、没有发送开麦或设备指令。此结果不是真人样本或设备验收；P0-01 和其他物理项目仍开放，待 CI。


P0-01 分轮诊断已由 [PR #28](https://github.com/batipang/CC8/pull/28) 合入，[真实 CI](https://github.com/batipang/CC8/actions/runs/41345296627) 八项通过；不改变此前明确的真人/实机缺口。

### P0-02：隔离真实模拟进程生命周期（未动运行服务）

新增三项回归，运行真实 Supervisor / Popen / OS worker lock / owner health IPC / PID 路径核验 / SIGTERM 与 child wait：十次异常退出串行恢复，disabled 标记终止且不再拉起，存活但三次不健康后替换并由健康实例按 disabled 停止。每次启动检查全部已创建 fixture PID 均已退出，worker OS 锁额外捕获重叠；结束核验无遗留、无 overlap、状态 stopped。所有子进程只运行临时合成脚本，最小环境无凭证/模型配置，临时进程结束后目录清除。

wait gate 加速但退避请求仍断言为 1/2/4/8/16/30/30/30/30/30；不健康用例前移初始化计时预算。全局模型锁检查在此 fixture 明确替换，避免与现有真实后台争用；该检查的真实覆盖仍是 F-15 单独测试。本轮约 3 秒是加速软件回归，不是生产恢复时延。没有 launchctl、模型加载、UI/麦克风/相机、真实后台重启或私有音视频。installer 16、host 379（4 private skip）、tooling 47、Java 15 checks 与质量通过；待 CI。P0-02 的真实登录/睡眠/完整十组验收仍开放。


### P0-03：本地双唤醒词软件候选（未部署）

后台候选 0.13.0 / local-wake-20261102.1，APK 0.8.7 与协议 2 不变。中文独立 stream 共享既有停止模型和 engine lock，英文专用 GigaSpeech BPE KWS；原停止参数未改变。双重云识别门控、有界空闲窗口、同句缓冲、无唤醒词/应用内黑屏、旧代次与 native 重置竞态均有合成流水线/并发回归。英文固定归档 SHA 为本机官方 HTTPS 下载计算，来源明确记录，权重不入 Git。

实际本机全套 installer 16、host 402（4 private-audio skip）、tooling 51、Java 15 checks；Ruff / Java formatting / strict 六模块通过。真实两个权重在隔离 eval 环境执行 5 正样本 / 3 负样本和 10 秒低电平噪声检查；合成音而非真人，无麦克风、云请求或私有音频。加载 0.571 秒、进程峰值 RSS 108.0 MiB、10 秒 native 计算 0.2123 秒仅为本轮开发机观测。模型许可、资源、断线/停止规则和待补真人矩阵见 [LOCAL_WAKE](LOCAL_WAKE.md)。未运行新后台、安装 APK 或启用设备采集；待 CI，P0-03 及实体验收均不关闭。


P0-03 已通过 [PR #30](https://github.com/batipang/CC8/pull/30) 的 [真实 CI 八项](https://github.com/batipang/CC8/actions/runs/41352092517)，合入 c260111。仍未部署，真人/设备缺口保持开放。


### P0-06：分类失败、任务与播报分立（未部署）

候选后台 0.13.1 / failure-feedback-20261102.1。两个定向用例对 44eaff7 基线分别复现 Dot 错误被普通回答吞掉、播放 ACK 缺失无分类反馈，候选通过。新增 11 项回归含大脑 6 类、ASR/TTS 5 类故障各 3 次，真实 MiniMax Transport 三入口超时/断连各 3 次；检查请求一次、busy 清理、失败任务/已完成工作分立、固定恢复文案、旧代次和秘密 sentinel 隔离。RPC waiter 超时清理、后半段 TTS 仍正确标为 TTS、设备错误及 ACK 截止时间均验证。

本机全套 installer 16、host 413（4 private-audio skip）、tooling 51、Java 15 checks；Ruff / Java formatting / strict 六模块与脱敏历史扫描通过。无实际模型、麦克风/相机或设备操作；详见 [FAILURE_FEEDBACK](FAILURE_FEEDBACK.md)。待 CI，不关闭部署/实机验收。


P0-06 经 [PR #31](https://github.com/batipang/CC8/pull/31) 的 [真实 CI 八项](https://github.com/batipang/CC8/actions/runs/41352856361) 通过并合入 e2a274a，未更新运行安装。

### P0-04：固定语料和私有评分软件（未调整音频门槛）

新增 75 条通用语料：五类各 3 调参 / 12 验收，四种距离/背景组合 300 位；空模板全部 unmeasured。CER/WER S/D/I、关键字段和人工意图标注分开，缺测和未标注不当正确；A/B 要求样本/场景/source/标注覆盖一致。输出只留静态语料编号、计数/差值，无转录或实际字段值。

12 项新增回归覆盖已知编辑距离和小字串穷举不变量、负数/小数及纠错句、缺测/失败/缺标注、分组隔离、A/B 拒绝、schema/预算、owner 文件/链接/FIFO/JSON、真实 root CLI 私有结果与不覆盖。实际系统 Python 3.9.6 在 / 与最小 PATH 执行空模板→评分，300 missing、0 observed、0600，无设备/模型/网络；临时文件清理。完整 installer 16、host 425（4 private-audio skip）、tooling 51、Java 15 checks；质量/strict 六模块/脱敏扫描通过。待 CI，未采样真人或调整阈值，P0-04 不结项；见 [ASR_EVALUATION](ASR_EVALUATION.md)。


P0-04 评分软件经 [PR #32](https://github.com/batipang/CC8/pull/32) 的 [真实 CI 八项](https://github.com/batipang/CC8/actions/runs/41353667824) 通过并合入 9806b27。没有新增真人成绩。

### P0-05：只读资源曲线与 native 持续输入（未部署）

0.13.2 / runtime-observation-20261102.1 候选 health 增加仅自身资源快照和随机实例标识；cc8 observe 最长 72 小时 / 6000 采样，原子私有检查点、重启分段、完整/缺口/取消状态。只读取 health，不发设备/工具/服务管理指令。旧后台缺资源字段明确 partial；Python 线程不冒充 native 总线程，探测 FD 误差/轮询间隙/休眠或调时限制均记录。11 项新回归含实际自身进程数值与真实 Unix fixture / 最小 PATH CLI。完整 installer 16、host 436（4 private-audio skip）、tooling 51、Java 15 checks 及质量/strict 六模块通过，待 CI。

隔离 actual dual-KWS eval 额外输入 1800 秒合成低电平噪声，40.589 秒挂钟、native 40.061 秒，无触发；峰值约 112.3 MiB，模拟 600/1200/1800 秒点 RSS 均约 88.5 MiB。不是实时 30 分钟或 72 小时验收，未采麦、调用云端或保存声音。详见 [RUNTIME_OBSERVATION](RUNTIME_OBSERVATION.md)，未更动运行助手。


P0-05 观察软件经 [PR #33](https://github.com/batipang/CC8/pull/33) 的 [真实 CI 八项](https://github.com/batipang/CC8/actions/runs/41354504360) 通过并合入 86a9102，未更新生产助手或开始 72 小时观察。

### P0 兼容性与交接文档（F-03 / F-13 / F-16 / F-17 / F-18）

新增来源分级兼容表、仅四字段 probe 的公开 issue 表单、明确为示意的安装路线/分阶段排障、文档导航/历史索引/英文入口，以及贡献和安全策略。README/安装页补齐停止条件、共享装载剩余风险及当前 APK 示例。GitHub issues 已核对可用，private vulnerability reporting 由 API 启用并再次读回 enabled；没有发送报告/通知。

文档 147 个本地链接存在性与 diff whitespace 通过；兼容 issue form 经系统 Ruby/Psych YAML 解析和字段/唯一 ID 校验通过（旧 Ruby 无 filter_map 的检查脚本已换成 map/compact，不是表单错误）。未操作设备或采集截图。F-16 真实脱敏截图/GIF 仍开放；其他文档条目只按本轮已完成内容关闭。安装报告保存权限问题另行独立修复，不混入本次纯文档提交。


文档前置工作经 [PR #34](https://github.com/batipang/CC8/pull/34) 的 [真实 CI 八项](https://github.com/batipang/CC8/actions/runs/41355184899) 通过并合入 66fa6ee；F-16 的真实截图仍开放。

### 安装报告 owner-only 与未知结果处理（无真实安装）

先独立纯提交共享原 PrivateOutput，定义 AST 完全一致，保留 host 兼容导出；安装 CLI 仅提取 main，installer 16 / host 436 / tooling 51 / Java 15 旧全套通过。随后在旧保存实现上新增两项回归，确认宽松 umask 产生非私有目录/文件，且已有公开目录仍继续设备操作，两项都失败；独立功能修复后通过。

现在安装前预留 0700/0600 私有独立报告，拒绝公开/链接目录且不 chmod；探测/验签/本地 check 不建报告；安装失败/最后保存失败保留 unconfirmed，明确先核对已装包而非重装。新增共 6 项还验证多轮不覆盖历史、链接拒绝、探测前已预留、结果不明和保存失败不重试。无 USB 或实际 APK 安装，target/probe/install 全替身。最终 installer 22、host 436（4 private-audio skip）、tooling 51、Java 15 checks，以及质量/strict 六模块和脱敏历史扫描通过，待 CI。P0 软件候选统一为后台 0.13.3 / p0-software-20261102.1，APK/协议保持。


### P0-06 补充：真实 MiniMax 搜索路径的失败回执（未部署）

沿实际 MiniMaxBrain→BrainRouter→ResponseWorker 复查，发现检索异常仍可能作为普通回答关闭成完成，failed 进度 payload 也曾整包进日志。新增两个测试方法：五类检索失败各 3 次、任意正文进度注入一次；正确 fixture 对旧实现确认 16 个预期断言失败且无 fixture error，修复后通过。现在检索失败上抛并保留 completed=false，仅记录白名单 kind，任务/search step 明确失败；不重试、不调用其他大脑或模型事实替代新查结果。查询准备的原关键词兜底和真实空结果语义保持。

本机 installer 22、host 438（4 private-audio skip）、tooling 51、Java 15 checks，质量/strict 六模块通过；34 项定向错误/检索回归也通过。全为假提供方，无设备/网络/采集；候选后台 0.13.4 / p0-software-20261102.1，待 CI。


安装报告修复经 [PR #35](https://github.com/batipang/CC8/pull/35) 的 [真实 CI 八项](https://github.com/batipang/CC8/actions/runs/41355759403) 通过并合入 25c24f2；没有进行真实安装。

### P0-05 / P0-06：通知失败仍释放资源与正确错误阶段（未部署）

旧实现的取消通知/提供方取消异常可跳过本地收尾，首次 play_begin 发送失败还会留下 playing。五项定向方法在旧实现复现七处失败/异常；另单项复现开场发送失败后残留 calibrating。修复先通知本地 waiter，普通通知失败只记录类型；Stop 用 finally 关闭采集/保存状态，未预期提供方错误仍抛出。输出的 begin/PCM/end/字幕 IO 分为 transport；iterator 内已分类错误不再包装成 TTS。仅输出失败结束校准，handover 保留校准重绑语义。

六项新方法包括三个发送阶段、字幕回调/iterator 关闭、普通取消与手动停止、异常提供方取消以及开场失败/手动 transport 保留校准。没有真实通道、模型或采集，修复后 installer 22、host 444（4 private-audio skip）、tooling 51、Java 15 checks，格式/质量/strict 六模块通过。候选后台 0.13.5 / p0-software-20261102.1；待 CI，未替换当前助手，不把模拟故障当物理拔插验收。


P0-06 搜索回执补充经 [PR #36](https://github.com/batipang/CC8/pull/36) / [真实 CI](https://github.com/batipang/CC8/actions/runs/41356493257) 通过并合入 72fa997；通知/资源收尾经 [PR #37](https://github.com/batipang/CC8/pull/37) / [真实 CI](https://github.com/batipang/CC8/actions/runs/41357130621) 八项通过并合入 327871a。

### P0 软件源码标记及原签名产物核验

CI 通过的干净源码 5f1c087cfa8c4cdea42758dd26b7f0597b1c31ef 已包含在 main；两个 APK 实际原钥引用 assemble/lint 通过，独立版本/SDK27 签名及旧原证书集合一致。annotated tag p0-software-20261102.1 固定该源码，实际 record / verify 通过，远端解引用同 SHA。Git 归档源码包 7075058 字节，SHA-256 为 57bb5533cb415850e2ade89cfd52e0a10e92d912b70d51912f31b6e9e141485c；逐项检查不含工具链、private、密钥文件或 Git 目录。所有产物/回执仍在忽略目录，没有上传 APK 或发布 GitHub Release；未部署、重启或打开采集。完整版本/产物和待补输入见 [软件交接](P0_SOFTWARE_HANDOFF.md)，P0 实体/真人验收保持开放。


### P0-01：可选设备播放头回执（未部署）

后台 0.13.6 / APK 0.8.8 code 33 增加双端能力协商、首个非零 PCM 帧播放进度和 Mac 单端回执阶段。九项新回归含平台无关 Java 编译执行、真实读线程提前回执、发布/取消并发、旧端和非法/过期/重复事件。新增同步曾被原取消回归发现会阻塞，已改为组件短时锁并保留原用例。全套 installer 22、host 453（4 private skip）、tooling 51、Java 15 checks，格式/lint/strict 七模块通过。没有访问设备、私人录音或云服务；两个 Android 工程已使用原 JKS 路径引用完成 assembleRelease / lintRelease，check-apk 实际核对包版本、API 27 签名与旧原证书集合一致；没有安装。合入由 [PR #39](https://github.com/batipang/CC8/pull/39) 必需 CI 把关，结果以该 PR 为准。方法和计时限制见 [PLAYOUT_RECEIPT](PLAYOUT_RECEIPT.md)，不作为声学起声或物理验收证据。

真实提交钩子额外检出新 strict 模块的 numpy 类型依赖未列入独立 hook 环境；已补同 requirements-dev 的 numpy 2.3.4 固定版本，不改变运行依赖、关闭规则或跳过检查。


### 2026-11-02 代码检查：播放完成与失败收尾并发

基于 main 84ff49f，发现并修复完成 ACK 覆盖后继句 playing、旧超时/设备/iterator 错误误取消新请求、插话超时 listening 覆盖新状态。新增四项有界真实线程测试，在基线方法上确认五处预期失败且零 fixture error；修复后定向通过。原取消锁回归未改动。后台候选 0.13.7，APK 0.8.8 / 协议 2 不变；没有设备/云调用、安装或重启。最终本机 installer 22、host 457（4 项私人音频依赖跳过）、tooling 51、Java 15 checks，以及 Ruff / Java format / strict 七模块通过。合入须通过对应 PR 的全部必需 CI，真实状态以 GitHub 为准；详见 [代码检查](CODE_REVIEW_20261102.md)。


### P1-01：独立聊天摘要接续候选（未部署）

基于 main d359552 独立开发，后台 0.14.0 / APK 0.8.9 code 34 / 协议 2。本机 CLI 0.166.0 实际 read-only list 返回十条元数据，与桌面 API 列出的五条聊天 ID 全部匹配；未读取正文、resume 或 start turn。proxy/受管 daemon 不可用，独立进程全为 notLoaded，不把它当桌面空闲。按计划采用明确来源的摘要交接，不追加原聊天。功能、隐私、工具范围及待验收见 [DOT_CHAT_HANDOFF](DOT_CHAT_HANDOFF.md)。

十六项新方法覆盖只读元数据/显式选定、分页容量和过期、文本筛选/截断、资源关闭、取消/代次/断连/忙碌、失败保留、不落盘上下文与 scoped interrupt。原 Dot 二十六项定向回归保留并通过；Android 原签名引用 assemble/lint 实际通过（首次遗漏的旧清空回调参数已补齐）。全套最终结果以本次提交/PR 检查为准，不将合成五聊天 fixture 或同 ID 发现写成真实端到端验收。没有安装、重启、调用模型、采音或摄像。

本批最终本机 installer 22、host 473（4 项私人音频依赖跳过）、tooling 51、Java 15 checks，固定格式/lint/strict 七模块通过，原签名 APK 构建/lint 通过。新增来源项目目录隔离回归，远端/失效目录不冒充本机已接入。P0 原源码 d359552 的 main CI 41366676544 已实际成功，已固定 source-only annotated tag p0-validation-base-20261102.1；该标记不声称设备验收或新构建清单。


### P1-09：离线版本规划与配置恢复预生成（未部署）

后台工具 0.14.1 / APK 0.8.9 不变。十项新离线方法覆盖两向协议、降级、实际检查入口复用、owner-only/链接/公开目录拒绝、原文件不覆盖、备份完整性、原引用固定且不读取秘密、legacy 白名单和最小 PATH 根 CLI。全套 installer 22 / host 483（4 private skip）/ tooling 51 / Java 15 checks，固定格式/lint/strict 七模块通过。首个 legacy 测试错误使用非白名单旧格式，修正为原 asr-only fixture，未放宽 schema。

系统 Python 3.9.6 / PATH=/usr/bin:/bin 实际 root CLI：固定 P0 source manifest → 当前候选的声明规划，真实原签名助手 0.8.9 做 AAPT/API-27 包检查；合成配置 backup/verify/stage-restore，原字节不变，输出 0600。没有读真实配置/凭据/配对，没有设备通信、安装、服务重启或模型请求。详见 [UPGRADE_PREPARATION](UPGRADE_PREPARATION.md)；真实升级/回退仍未验收。

P1-09 首次 CI 其余六项通过，密钥扫描将 backup() 的单行英文注释误判。已检查 redacted finding 的提交/文件/起止位置和该源代码段，未发现凭据；改写当前注释并仅加入 aa61861:upgrade_preparation.py:generic-api-key:81 的不可变历史指纹例外，未忽略文件或默认规则。全历史脱敏扫描复验随后运行，CI 仍强制通过后才合入。

误报范围复核：redacted finding 的 StartLine 与 EndLine 均为 81（columns 32–58）；之前关于跨行的初步判断不准确，现以注释单行命中记录为准，例外指纹范围未扩大。

P1-09 后续 CI 安全扫描成功，但 macOS 的既有 measure CLI 集成用例在 0.2 秒窗口只观察到一次 IPC，未达到该用例要求的两次；Linux 同项成功。将该真实 IPC 集成窗口改为 2 秒，保留至少两次读取、仅 diagnostics、单样本去重、0600 与 worker 收尾断言及 10 秒进程上限；生产计时和虚拟时钟截止回归不变。此处不把 CI 机器调度/文件同步抖动作为语音时延指标。
