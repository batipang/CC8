# 收到的麦克风 PCM 质量

后台 0.15.46 增加 `health.input_quality` 和私有 `status.json.input_quality`。`diagnose` 的 JSON 报告也保留这份 health 快照。查询只读统计，不打开设备、麦克风或摄像头，不上传音频，不调用 ASR／Dot。

```sh
CC8_RUNTIME=/absolute/live/workspace
"$CC8_RUNTIME/.toolchains/cc8/voice-venv/bin/python" "$CC8_RUNTIME/cc8-assistant/host/diagnose.py" --health
"$CC8_RUNTIME/.toolchains/cc8/voice-venv/bin/python" "$CC8_RUNTIME/cc8-assistant/host/diagnose.py"
```

## 测量位置与数据

只统计当前录音代次校验通过、从前瞻缓存进入处理入口的逻辑 16 kHz 麦克风 PCM，位置在 Mac 的高通、AEC／降噪和断句之前。单路取整个 MIC，有成对输入时只取 MIC_PAIRED 的麦克风列；播放参考列不进入统计。统计在整包进入处理入口时更新，不以 AEC 是否输出帧为准。

`schema=1`，`scope=received_mic_pcm_16khz_before_host_dsp`。报告仅含以下非内容标量，不保留采样数组、波形、正文或逐帧历史：

| 字段 | 含义 |
| --- | --- |
| `status`、`frontend` | `observed` 表示本段实际收到有效 PCM；零样本为 `unmeasured`，前端未观测时为 null |
| `samples`、`sample_rate` | 本段逻辑麦克风采样数及 16,000 Hz 采样率 |
| `clipped_samples`、`clipped_ratio` | 绝对值至少 32,760 的样本数／比例，门槛与 Android 检测器相同 |
| `zero_samples`、`zero_ratio` | 恰好为零的样本数／比例 |
| `peak_int16` | 最大绝对整数幅度，范围 0～32,768，避免对 −32,768 取绝对值溢出 |
| `rms_dbfs`、`dc_offset_int16` | 本段 RMS 数字电平及有符号均值，不是环境声压 |
| `max_consecutive_clipped_samples` | 按包序列及设备采样时间连续的最长满幅样本段；时钟容差为 `continuity_tolerance_ns=2000000` |
| `discontinuities` | 包丢失、已确认读停顿、无效帧或超过容差的时间断点；一个有效包同时出现多种断点只计一次 |

没有样本时 RMS、峰值及比例为 null，不能把未测写成静音。采样数和计数仍为零。停麦后只在内存保留最后一段标量，录音是否开启以 health 的 `microphone_active` 为准；新录音或通道切换清空本段统计。停止／重置会撤销统计所有者，较晚完成的旧包不能污染新段。断点会切断连续段，不拼接成更长的削顶。

## 解释边界

`device_adc_clipping_measured=false` 始终保留。成对输入已在 Android 上经过选路和降采样；这里观测到的满幅样本可能出现在设备前端、转换或传输之前的其他处理环节。一次峰值达到满幅不能证明持续削顶，需同时检查比例、连续长度和测试条件，不能据此自动增减音量、修改 VAD 门槛或认定 ASR 根因。

Android 的音源选择还涉及设备自身的增益和预处理配置，不能把 Mac AEC3 中关闭 AGC 理解为控制了设备全部处理。[Android 预处理说明](https://source.android.com/docs/core/audio/implement-pre-processing)、[录音源说明](https://developer.android.com/media/platform/mediarecorder)。该补充只提供定位证据，不改变当前提供方、音色、默认前端、音频样本或语音准入。

## 验证范围

离线回归从实际 Bridge／CapturePipeline 入口覆盖有符号满幅边界、成对参考排除、前 DSP 原样输入、跨包连续段、三种断点、无效帧、旧代次、停止与重开、换路、解码期间重置／关闭，以及 health／私有状态的副本和非内容字段。软件结果不代替真实房间、输入增益、削顶成因或识别准确率验收。
