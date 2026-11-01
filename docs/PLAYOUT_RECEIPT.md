# 首个信号的设备播放进度回执

后台 0.13.6 / 助手 APK 0.8.8 code 33，协议仍为 2。本功能不启动播放、录音或摄像头，不改变音量、字幕布局和打断阈值，也没有升级当前现场安装。

## 能证明什么

`render_ack` 表示 AudioTrack 接受 PCM；新增 `playout_ack` 表示设备播放头越过本句首个非零 PCM 帧后，Mac 收到一次回执。它能帮助区分“数据已接收”和“播放头已推进”。两者都不是扬声器实际起声：静音、HAL、功放和房间声学还需要实测。

Android 官方定义 [getPlaybackHeadPosition](https://developer.android.com/reference/android/media/AudioTrack#getPlaybackHeadPosition()) 返回帧位置，须按无符号 32 位读取，停止/flush 会重置。本实现每句新建 track，使用原有播放线程查询，暂停、取消或无有效首帧不发回执；只在协商启用时扫描已接受 PCM，并在首次回执后停止查询。全零音频没有此标记，不能因此认定播放失败，原 play_done 语义保持。

## 协商和准入

- device_ready 广告整数 `playout_receipt_version=1`；后台只接受精确整数 1，并在 play_begin 请求同样的版本。旧后台不请求，新 APK 不发送；旧 APK 不广告，新后台保留原流程。无需新权限或修改二进制帧布局。
- 设备只扫描 AudioTrack 实际短写返回的样本，保存首个非零样本索引。播放头严格越过该索引且不超过已接受帧数后，发送一次 `playout_progress`。
- 必填白名单数值为 generation、playback_id、first_signal_frame、played_frames、accepted_frames、nanos。类型/非负/位宽在协议入口检查；布尔、浮点、字符串不代替整数。事件 body 的播放 generation 独立于控制包 header 的采集 epoch。
- 后台按当前 generation、当前句 ID、本地已发送 PCM 的首个非零位置，以及 `first_signal < played <= accepted <= reserved` 核对。先预留再发送，兼容读取线程在发送调用返回前收到回执。未经请求、过期、重复、取消后和上一句回执均不记账。
- 取消与发布只用短时组件锁，不在该锁中发送网络数据或等待播放完成；设备断连清除能力及探针。不会重播、重交办任务或重新开启采集。

## 单端计时与缺口

设备 nanos 仅是同端观察值，不参与跨端相减。后台在准入时用 Mac 单调时钟写 `playout_ack`，新增 `utterance_to_playout_ack_ms` 和 `pcm_to_playout_ack_ms`；它们包含设备轮询、回传和调度开销，不能叫声学时延。报告明确 `playout_ack_is_audible_onset=false`，measure 拒绝宣称其为 true 的快照。health 中 `playout_receipt_supported` 只表示能力协商，不能证明已播放。

未收到回执不填零，也不将它改成播放失败。现有 play_done / playback_error / 排空超时仍负责播放结果。旧版诊断消费端可能不认识新阶段，采集新后台请用配套源码的 measure；新工具可读旧后台缺字段的快照。

## 验证边界

离线回归覆盖短写偏移、前导零/全零、播放头恰在首帧、暂停/未播放、已排空短 buffer、无效字段、帧边界、一次性、旧端协商、取消/断连/换句，以及真实 Python 读线程的提前回执和发布/取消并发。原取消死锁回归保持不变。Java 探针在无 Android 设备环境独立编译执行，Android 主工程仍须实际 assemble/lint。

这不是 CC8 扬声器实测、静音检测、端到端延时达标或 P0 结项；真人、USB/Wi-Fi 物理切换、待机与长时验收继续按 [TODO](TODO.md)。
