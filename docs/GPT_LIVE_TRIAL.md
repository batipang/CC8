# 大黄蜂 GPT-Live-1 试用

MiniMax 仍是默认语音模式。切到 GPT-Live-1 后，主人在唤醒词模式说“大黄蜂”或“Bumblebee”，由现有本地检测器确认唤醒。随后 Mac 把经过回声消除的音频发到 OpenAI，接回转写和语音。普通问候可由 GPT-Live 应答。实质问题与操作通过客户端委托交给 Dot，沿用原有权限、确认和任务回执。Dot 的屏幕任务回执是执行结果的依据。偏好记忆和本地工作汇报的正文仅显示在 CC8 屏幕。

访客、无唤醒词模式、旧版 APK、开场语音、精确文字播报、独立拍照和看图结果沿用原有链路。Live 试用需要支持流式字幕的新版 APK 和 Mac 后台。暂停、退出和切换传输时，旧会话失效。空闲 30 秒或会话满 3 分钟后需要再次唤醒。OpenAI 按会话时长计费，官方标价为每分钟 0.05 美元，按秒计费，Dot 费用独立。

主人说“追问这张照片”并附上问题后，已通过本地准入的 Live 委托会交给照片追问控制器。它只使用明确允许保留、同一收音归属且未过期的照片。照片和看图结果沿用原 Dot 与 MiniMax 链路，本次不会重新打开摄像头。

## 切换

1. 从同一试用分支构建 APK 并更新 Mac 的语音运行时。使用项目原有安装方式和签名。Mac 端使用具有 GPT-Live API 权限的 OpenAI 项目密钥。交互启动可通过 `OPENAI_API_KEY` 提供，登录服务从工作区 `.toolchains/cc8/openai-api-key` 读取，文件必须仅当前用户可读写（`0600`）。密钥仅保存在 Mac。`./cc8 doctor --profile voice` 会报告试用密钥是否可读取，只检查格式和文件权限。缺少可读取的密钥时，后台继续使用现有语音并输出诊断提示。
2. 在试用工作区运行 `.toolchains/cc8/voice-venv/bin/python cc8-assistant/host/configuration.py voice-mode gpt-live-1`。重启试用后台后生效。若已用 `CC8_VOICE_MODE` 配置服务，先按服务安装方式更新该覆盖。配对、MiniMax 密钥、原有设置按部署流程提供。
3. 在 CC8 选择唤醒词模式，正常启动语音并等待校准。隐私说明会标示 GPT-Live 试用及 OpenAI 音频处理。音色默认 `marin`，可通过私有配置 `gpt_live_voice` 或 `CC8_GPT_LIVE_VOICE` 选择 GPT-Live 内置音色。
4. 切回时运行同一个命令，将参数改为 `minimax`，再重启后台。

## 验证

Mac 上运行 `python3 cc8-assistant/host/diagnose.py --health` 可以查看后台实际报告的 `gpt_live_state`。它区分未启用、配置不可用、设备未连接、非主人、设备版本不支持、未启用唤醒词、本地唤醒不可用、麦克风暂停、需要完成校准、等待本地唤醒、连接中及已建立会话。`gpt_live_sessions` 统计尝试打开的会话，另提供失败、近端打断和接受的转写片段计数。此命令不打开麦克风、不调用 OpenAI，也不输出密钥、转写或音频。实际连接状态和计数只来自本次后台实例。

同时看 `transport_routes` 的 USB／Wi-Fi 就绪位与当前选中线路。换线时 `capture_route_pending` 表示设备收音归属尚未确认。已经本地唤醒、等待后续语音时显示 `ready_for_speech`，占用收音或响应资源时可显示 `capture_busy`，旧会话收尾时显示 `session_ending`。线路就绪只反映本机传输状态。

| 项目 | 自动化检查 | 真机或云端检查 |
| --- | --- | --- |
| 中文识别 | 本地唤醒前没有 Live 连接。流式唤醒命中后，同一句已捕获且无播放回声风险的开头先于新音频发送。握手期间只在 Mac 内存中保留有上限的待发送帧，服务端确认 16 kHz PCM 和客户端委托且收音归属仍有效后才发送。中文片段按顺序合并，委托只提交当前问题。 | 用固定语料比较两种模式的中文字错率、地名、连着唤醒词说的首句与中英混说识别。 |
| 打断 | 用连续近端语音帧触发停播，丢弃旧 PCM 和字幕，保持录音。即使旧 PCM 在网络等待时和插话同时到达，也不能重新启动旧播报。Dot 任务仍有独立回执。 | 用相同音量回放插话和安静对照，记录实际停播延迟及误打断。 |
| 音色 | 音色设置传到 Live 会话，握手确认该音色后才上传音频。 | 对同一段中文试听 `marin` 与现有 MiniMax 大黄蜂音色。 |
| 字幕同步 | 按 GPT-Live 字幕片段时间生成 UTF-16 区间，用 CC8 的播放头推进并绑定播放 ID。 | 对照屏幕与出声时间。主 WebSocket 缺少音频块时间戳和结束事件，片段级对齐和输出分段均为近似。 |

[GPT-Live-1 模型与计费](https://developers.openai.com/api/docs/models/gpt-live-1) · [OpenAI WebSocket 音频协议](https://developers.openai.com/api/docs/guides/voice-websockets) · [客户端委托](https://developers.openai.com/api/docs/guides/live-delegation?delegation-mode=client) · [字幕和音色](https://developers.openai.com/api/docs/guides/live-conversations)
