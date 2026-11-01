# 大黄蜂（Bumblebee）语音助手

完整源码快照：Android APK **0.6.1**（versionCode 23）、Mac 后台 **0.9.2**。包名 `com.happyhome.cc8assistant`。默认 **Dot / Codex 做大脑，MiniMax ASR 做耳朵，MiniMax TTS 做嘴巴**，Mac 执行消回声与即时打断，CC8 负责录放和显示。

## 构建与安装

先按[开发说明](../docs/DEVELOPMENT.md)准备 Android 工具链，再从仓库根目录执行：

```sh
python3 device-access/installer-helper/build.py
python3 cc8-assistant/tools/build.py
python3 device-access/cc8_install.py --probe
python3 device-access/cc8_install.py --install artifacts/cc8-assistant-0.6.1.apk --launch --shared-staging
```

`--shared-staging` 的共享存储替换风险、安装条件和更新签名问题见[安装步骤](../docs/APK_INSTALL.md)。新克隆不会拥有原开发机签名，首次构建生成独立本地开发证书。

## Mac 运行环境

使用 Apple Silicon Mac 上的 CPython 3.13：

```sh
python3.13 scripts/setup-voice.py
```

脚本创建 `.toolchains/cc8/voice-venv`、安装 `host/requirements-runtime.lock`，校验 AEC3 轮子，并下载和校验本地停止词模型。依赖和模型不进入 Git。可选本机 ASR：增加 `--sensevoice` 下载固定版本 SenseVoice 文件。

Dot 模式还需要本机已安装并登录的 `codex` CLI；后台沿用其当前模型、推理、技能、插件 / MCP 和审批配置，创建独立临时语音上下文，不复制桌面当前聊天。没有安装 CLI 或服务无权限时明确报错，不偷偷换模型。

配置 MiniMax 专用订阅 Key，**不要写进源码或 APK**：在本机 `.toolchains/cc8/voice-secrets.env` 保存一行 `CC8_MINIMAX_API_KEY=你的专用密钥`，文件权限设置为 `0600`；或者通过同名环境变量提供。复制本机文件编辑，不要把真实密钥输入聊天或提交 Git。

当前适配器针对 `sk-cp-` 专用订阅密钥，ASR / TTS / 搜索的可用性取决于账户实际权限与额度；不保证每个 Coding Plan 都同时拥有全部能力。

## 启动与停止

```sh
python3 cc8-assistant/host/launch.py
# 停止后台
python3 cc8-assistant/host/launch.py --stop
```

也可双击 `start-usb-voice.command`。首次连接 USB 完成局域网配对；Mac 后台只运行一份，勿同时运行两个源码目录的后台。APK 安装后也可在设备应用列表手动打开“大黄蜂”。

从 CC8 桌面入口或应用图标打开大黄蜂，已授权时会在连接后自动开始语音；首次允许麦克风后，安静等约 6 秒校准及提示语结束。开启唤醒模式时说“大黄蜂，介绍一下你自己”或“Bumblebee，常州属于哪个省？”。无唤醒模式可直接说话。正常音量插话应先暂停播报，识别新内容后回答；“停一下 / 停止 / 暂停”另有本地快速路径。

点停止、离开前台或全部通道断开会停止采集。重连不会自动开麦。15 分钟无交互进入应用内黑屏待机，需已开启麦克风与 Mac 服务才能语音唤醒。

## 桌面入口

返回原厂桌面后，点击右侧“大黄蜂”悬浮按钮即可重新打开助手，不需要 USB 拉起；打开后默认自动开始语音；手动停止、后台及重连规则见[自动开始说明](../docs/AUTO_VOICE.md)。按钮可上下拖动，设置位于顶部连接状态 → 桌面入口。详见[桌面入口说明](../docs/DESKTOP_ENTRY.md)。

## ASR 与音色

`host/voice-config.json` 默认为 `{"asr":"minimax"}`；可将 `asr` 改为 `sensevoice`，或通过 `CC8_ASR_ENGINE` 显式覆盖。SenseVoice 必须先下载模型；本机 Whisper 对照路径保留，但需自行准备 MLX 缓存，不会自动替代失败的云端识别。

TTS 使用 `speech-2.8-turbo`、`Chinese (Mandarin)_Straightforward_Boy`、speed 1.03、pitch +1，16 kHz 单声道流式 PCM。目标是年轻、明亮、活泼的中文大黄蜂感觉，是系统音色近似，不是原动画配音的精确复刻。相关适配在 `host/minimax_client.py`。

顶部可手动切换 MiniMax 对话模式；默认 Dot 可使用其本机工具做联网、文件与项目任务。客户端另提供 Mac 打开应用 / 网页 / 项目文件及 CC8 状态、媒体音量、停播与亮屏工具，等待真实执行结果；没有远程开麦工具。

只启动后台可附加 `launch.py --no-open`；只打开页面而不新开麦可用 `launch.py --no-auto-voice`。只播报和维护流程必须显式选择相应选项。

## 从 Mac 指定播报

应用前台且已连接时：

```sh
python3 cc8-assistant/host/announce.py --text '你好，我是大黄蜂。'
```

也支持从标准输入或 `--file` 读取。直接 TTS 原文，不调用对话模型，最多 500 字，等待实际播放完成回执。麦克风原来关闭时继续关闭，可点击设备按钮打断；不会为播报自动开启麦克风。

## 代码导航

| 文件 | 职责 |
| --- | --- |
| `host/bridge.py` | 流式音频、轮次状态、插话、待机和设备事件 |
| `host/dsp.py` / `fast_stop.py` | AEC3、VAD、回声证据、停止关键词 |
| `host/recognition.py` | MiniMax / SenseVoice / Whisper 识别适配 |
| `host/dot_client.py` / `brain_router.py` | Dot app-server、工具与对话路由 |
| `host/minimax_client.py` / `web_search.py` | ASR、流式 TTS、可选 MiniMax 对话与搜索 |
| `host/transport.py` / Android `LanConnector.java` | USB 优先、认证 TLS 局域网备用与发现 |
| `host/caption_timing.py` / Android `CaptionTimeline.java` | 词时间戳与实际播放进度 |
| Android `PcmPump.java` / `PlaybackClock.java` | 短写入重试、暂停与参考时钟 |
| Android `BumblebeeAvatar.java` / `WaveformView.java` | 头像表情、口型和独立粒子声波 |
| Android `CameraTestActivity.java` | 主动启动的 20 秒本地相机预览检测 |

回归命令见[开发说明](../docs/DEVELOPMENT.md)。默认不保存原始麦克风音频，状态 / 日志只在忽略的 `device-access/private/voice-runtime/`；ASR 的有界讲话片段发 MiniMax，Dot 收识别文字，TTS 收待播文字。局域网通信、已验证范围与未验收项见[架构](../docs/ARCHITECTURE.md)及[验证记录](../docs/VALIDATION.md)。

## 摄像头看图

点「看一看」，或在语音开启后说「大黄蜂，看看我手里是什么」，先显示 5 秒实时预览供调整，再把当次图片交给 Dot 分析。详见[视觉功能说明](../docs/VISION.md)。
