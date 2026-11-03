# P0-03：Mac 本地双唤醒词（0.13.0 候选）

大黄蜂 / Bumblebee 由 Mac 的专用流式 KWS 检测，位于云端 ASR 之前。MiniMax 仍做有界话语识别和播报，Dot 仍做大脑；没有更换用户选择的服务、音色或工具权限。当前运行安装没有因源码合入而更新。

## 等待、对话与停止

唤醒模式的状态为：开始语音并完成校准 → 等待唤醒 → 本地检测到名字 → 正在听 / 处理中 → 空闲窗口超时回到等待。窗口默认 60 秒，可用非敏感配置 `wake_window_seconds` 或 `CC8_WAKE_WINDOW_SECONDS` 设为整数 15～300 秒。收音中的话语、识别、任务、播报、暂停和显式取景都会延长窗口；不在工作中途超时。说“休息”仍可结束唤醒对话。

等待期间的声音只在现有本地有界 AEC / NS / VAD / KWS 路径处理。断句入口和识别 worker 都校验 awake；未经本地唤醒的话语不进 ASR 队列、不调用识别提供方。没有云端文本找名字的降级路径，缺少本地模型时启动明确失败。正常启动也不调用 ASR 做空白预热；旧识别器原本会丢弃该空白，这不是对历史上传行为的变更声明。显式 `--speech-smoke` 仍属于提供方测试。

保留原有句首缓冲，支持“名字＋问题”同一句；只在这次本地唤醒所属话语开头去掉识别出的名字。仅说名字不向 Dot 虚构“你好”请求；随后问“大黄蜂是什么车型”不会删除句中的主题。自播报和尾音参考含名字时拒绝对应回声风险触发，仍需真实房间负样本验证。

无唤醒词模式启动后直接听取有效话语；应用内 15 分钟黑屏后，仍可用本地名字唤醒并亮屏。Home、手动停止及全部认证通道丢失仍停止采集；连接恢复不会自动重开麦。会话/通道代次使旧检测和排队帧失效。显式回声自检在等待唤醒时拒绝运行，需先唤醒或选无唤醒词模式；不伪造自检成功。

## 模型、资源与锁

中文流使用 `sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20` 的独立 stream，与既有停止检测共享模型和 native engine lock。关键词资源为 `d à h uáng f ēng @大黄蜂`。停止检测的 gain、threshold、trailing blanks 和线程数保持不变。

原双语模型对本轮英语合成音效果不足，英文改用独立的 `sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01`，资源为 `▁BU M B LE B E E @Bumblebee`。两支均 CPU 单线程、score 1、threshold 0.25、trailing blanks 1、输入 gain 2 / clip ±0.98。英文使用 int8 encoder / joiner 与原 fp32 decoder；未新增运行 Python 包。SentencePiece 只在隔离开发环境核对一次 BPE，不是运行依赖。

`./cc8 setup --profile voice` 仍通过原全局运行所有权检查，再按两个清单下载/核对精确长度与 SHA-256、拒绝路径穿越/链接后解包。`doctor` 对照两个固定归档逐文件核验已提取内容。权重、下载缓存、生成音频不进入 Git；已运行另一份后台时不要从第二个克隆准备或启动模型。

英文归档 17,626,723 字节，SHA-256 `f170013b4716e41b62b9bfd809687c207cef798ef9bc6534d524e17af9b6561a`。此 SHA 从官方 HTTPS 资产本机计算，上游旧资产 API 未提供 digest，清单明确记录来源；不是官方数字签名。中文固定清单沿用已核验的归档。

native 调用不进入 session 回调；reset 按 session guard → wake native lock → engine lock 顺序，feed 释放 native 锁后才重新核对代次。进入 native 前再检查 serial，防止已失效帧污染新 stream。两个受控并发回归覆盖重置发生在进入 native 前及解码中两个窗口。

## 许可与上游依据

- [官方预训练 KWS 模型说明](https://k2-fsa.github.io/sherpa/onnx/kws/pretrained_models/index.html)：模型族、中文拼音/英文音素与 GigaSpeech BPE 区别。
- [sherpa-onnx 2.0.1 KeywordSpotter 接口](https://github.com/k2-fsa/sherpa-onnx/blob/v2.0.1/sherpa-onnx/python/sherpa_onnx/keyword_spotter.py)：独立 stream / 额外关键词接口。
- 英文官方归档 README 的 model card 标为 Apache-2.0；中文模型许可经[上游问题 #3802](https://github.com/k2-fsa/sherpa-onnx/issues/3802)维护者 2026-10-26 确认 Apache-2.0。这里只记录本轮使用的模型来源，不改变本仓库其他资产的许可范围。

最新隔离候选的反例、固定模型/依赖与后续门槛见 [WAKE_RESEARCH](WAKE_RESEARCH.md)。这些实验均未替换运行KWS，不能用整句编码器输出或开发集通过代替唤醒验收。

## 软件验证与尚未完成的验收

本机全套：installer 16、host 402（4 项明确依赖私人音频跳过）、tooling 51、Java 15 checks；Ruff / Java formatting / strict mypy 六模块通过。新增 native 适配和动态控制模块尚未声称 strict 全覆盖。模型准备的离线回归覆盖固定缓存、两个归档、链接/穿越/错误根目录、下载大小与校验拒绝；测试不访问网络。

独立 eval venv 使用运行锁相同版本的 numpy 2.3.4 / sherpa-onnx 2.0.1。真实权重＋Mac 本地 say 合成音（无麦克风、无云请求、临时音频已删除）结果：5 条名字正样本命中、3 条普通问句负样本未命中，10 秒 seed 固定低电平噪声无命中。观察到加载 0.571 秒、进程峰值 RSS 108.0 MiB，10 秒噪声的两支 native 累计计算 0.2123 秒；这是一次开发机合成检查，不是持续 CPU、整条语音时延或 CC8 真人召回率承诺。

P0-03 保持未勾选。尚需真人两词、1/3 米、安静/电视背景和自播报负样本、4 小时误唤醒观察；P0-04 普通音量插话及 P0-05 真实黑屏/物理切换一起验收。未安装 APK、替换后台、启停服务或开启设备采集。

补充持续输入检查：隔离真实双模型处理 1800 秒 seed 固定合成噪声（挂钟约 40.6 秒），无触发，最后三个采样点 RSS 稳定约 88.5 MiB；见 [资源观察记录](RUNTIME_OBSERVATION.md)。这不替代真人/实际时长验收。
