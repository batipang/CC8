# 配置来源、校验与恢复（F-09）

后台候选 0.12.4。此配置层不读取模型权重、不访问设备，也不改桌面 Codex 的模型、技能或工具权限；Dot 仍继承桌面现有配置。当前部署环境不会因修改仓库文件自动升级或重启。

## 优先级与私有文件

**明确的环境变量 > 选定私有 JSON > 仓库 defaults > schema 默认值**。仓库 defaults 为 `cc8-assistant/host/voice-config.json`，当前 ASR 仍是用户选定的 MiniMax，brain 默认 Dot；默认音色、速度、音调和模型保持原值。

私有覆盖默认放在忽略的 `.toolchains/cc8/settings/assistant.json`。可用 CC8_CONFIG_FILE 选择另一个文件；显式选择的文件缺失/为空/损坏时失败，不能悄悄回落。配置和引用中的相对路径均相对于本次代码工作区根目录，不随 shell 当前目录或 JSON 所在目录变化。

首次准备一个最小覆盖文件（cp -n 保留已经存在的文件）：

```sh
mkdir -p .toolchains/cc8/settings
chmod 700 .toolchains/cc8/settings
cp -n cc8-assistant/host/resources/assistant-config.example.json .toolchains/cc8/settings/assistant.json
chmod 600 .toolchains/cc8/settings/assistant.json
python3 cc8-assistant/host/configuration.py check
```

私有配置必须为当前用户的普通文件，不能为符号链接、目录或其他用户/组可读写文件；打开后再次核对文件描述符权限，读取最多 16 KiB。代码只在新建专用目录时使用 0700，不改变用户指定的已有外部目录权限。

普通 JSON **禁止 API key、token、签名私钥内容**。MiniMax 密钥仍从原 CC8_MINIMAX_API_KEY 或 owner-only voice-secrets.env 读取；JSON 只能指定 credential_file 路径引用。签名只保存 signing_keystore 路径引用。配对令牌、证书、登录信息和 Dot 配置不迁入这里。

## 字段与入口

完整 [JSON Schema](../cc8-assistant/host/resources/runtime-config.schema.json) 由 `config_schema.py` 的同一组字段定义生成；[私有样例](../cc8-assistant/host/resources/assistant-config.example.json) 和根目录 [.env.example](../.env.example) 不含秘密。

| 字段 | 含义与约束 |
| --- | --- |
| schema_version | 当前为整数 1；未知版本失败 |
| asr | minimax / sensevoice / whisper；失败不更换引擎，不扩大上传范围 |
| brain | dot / minimax；MiniMax ASR/TTS 与大脑选择分开 |
| capture_frontend | `mono` 默认；`paired` 为 0.15.45／APK 0.9.43 的成对参考候选，通过 `CC8_CAPTURE_FRONTEND` 或私有配置显式选择。仍需设备能力协商，格式不支持时在采样前回退单声道。参考 [协议](PROTOCOL_BOUNDARIES.md#成对参考输入候选)；候选尚需实时实机 A/B，不改变现用默认。 |
| minimax_base | 保留三个官方 HTTPS 域名，不允许凭证、查询参数、第三方端点或其他路径 |
| minimax_model / asr_model / tts_model | MiniMax 对应模型 ID 格式；不等于订阅支持或服务可用性保证 |
| tts_voice / tts_speed / tts_pitch / tts_emotion | 音色及有界数值；默认仍为已选大黄蜂音色参数，emotion 默认 null |
| wake_window_seconds | 唤醒后空闲对话窗口，整数 15～300 秒、默认 60；环境变量 CC8_WAKE_WINDOW_SECONDS，见 [LOCAL_WAKE](LOCAL_WAKE.md) |
| chinese_wake_verifier | 中文唤醒复核候选，默认 `off`，可显式设为 `sensevoice`（原短窗双支路）或 `sensevoice_complete`（主路候选整句本地复核）。两者互斥，见 [中文整句复核](COMPLETE_CHINESE_WAKE.md)。环境变量 `CC8_CHINESE_WAKE_VERIFIER`，两种模式均需本机已校验的 SenseVoice 模型，原 `sensevoice` 另需快速停止收音支路。对话 ASR 仍由 `asr` 控制。 |
| english_wake_verifier | 英语扩展候选整句复核，默认 `off`；显式 `whisper_complete` 使用本机固定 Whisper 缓存，仅在原英文检测未确认、同句存在扩展本地候选时判断名字。环境变量 `CC8_ENGLISH_WAKE_VERIFIER`；就绪检查校验权重，不下载或加载模型，不改变 MiniMax 对话 ASR。见 [英语完成句确认](LOCAL_WAKE.md#英语扩展候选完成句确认)。 |
| tls_port | 已配对 Wi-Fi 的 TCP 端口，1024～65535，默认 18766 |
| connect_timeout / read_timeout | MiniMax 请求的连接/读取秒数，分别限制 1～30 / 1～120，默认 8 / 20 |
| credential_file | 现有 owner-only 密钥文件的引用，校验配置时不读取其内容 |
| signing_keystore | 原签名 JKS 引用或 null；两个构建入口一致，指定文件缺失时不生成替代密钥 |

所有可覆盖的环境变量名称见 .env.example；该文件**不会自动加载**。布尔伪装整数、非有限数、超大数、未知键、重复 JSON 键、非法 UTF-8/代理字符、控制字符和越界值明确失败。错误只给字段名与稳定原因，不包含传入值；按 schema 修正对应字段，再运行 check。

配置在新后台启动时形成快照，修改文件不会在当前对话中途换模型。交互 launcher 在创建后台前校验；后台入口在加载模型/凭证前校验；登录服务在写 plist / 启用前校验。LaunchAgent 默认仍只保存 PATH；若安装时明确给了白名单中的非敏感环境覆盖，则规范化并保存这些覆盖及可选配置路径，保证登录后选择一致，永远不复制整个环境或持久化 API key。UI 当前会话的 provider 切换不自动改私有文件。

USB 本机通道 18765 和广播发现 18767 保持原协议。改 tls_port 后需经原 USB 下发的 lan_config 更新设备已保存端口；旧配对不会接受端口不同的广播而静默切换。广播签名、TLS pin、令牌校验和 USB 优先不变。

## 检查、迁移与恢复

```sh
# 只显示非敏感选项、来源标签及引用是否配置，不显示引用路径或秘密
python3 cc8-assistant/host/configuration.py show
python3 cc8-assistant/host/configuration.py check

# 显式升级旧的仅含 {"asr": "..."} 私有覆盖；保留原选择，不写入临时环境覆盖
python3 cc8-assistant/host/configuration.py migrate

# 明确恢复私有覆盖为 v1 空覆盖；旧文件保留为同目录的随机 .backup、权限 0600
python3 cc8-assistant/host/configuration.py reset

# 生成 schema；没有网络查询或模型加载
python3 cc8-assistant/host/configuration.py schema
```

读取和检查不自动迁移；未知版本或坏文件的迁移失败后原字节不变。写入采用同目录临时文件、fsync 和原子替换，失败不留下半份配置。reset 保留旧字节备份，失败会回滚；不改密钥/签名/配对文件，不启动服务、界面、麦克风或相机。环境变量仍优先，reset 不会替你清除 shell 环境；若曾指定签名引用，构建前需继续明确选择原签名。

验证覆盖来源优先级、真实最小 PATH 子进程、登录环境白名单、迁移/原子写/恢复回滚、文件权限与类型、真实 MiniMax 客户端编码（模拟 HTTP 响应，不调用云端）、签名引用缺失前置失败及 TLS 端口与签名发现一致性。均为软件验证，实际 CI 状态见 [VALIDATION.md](VALIDATION.md)，不代表模型额度、识别准确率、物理设备或长期可靠性已验收。
