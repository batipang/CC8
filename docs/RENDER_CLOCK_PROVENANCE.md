# 播放估算时钟来源诊断

0.14.20／APK0.9.14、code49软件候选，协议仍为2。当前实机冻结0.14.19／code48跑三轮无人值守验收；本功能未部署，不改写已有物理结果。

## 目的与边界

0.14.18声学检测曾遇到播放参考时间戳跳变；0.14.19按稳定片段重建参考，已实际匹配到麦克风中的固定200ms声音窗口。但跳变本身不能区分估算来源转换与实际播放间隙。

Android的[AudioTrack时间戳](https://developer.android.com/reference/android/media/AudioTrack#getTimestamp(android.media.AudioTimestamp))表示音频帧的估计呈现时间，启动时也可能暂不可用。本功能复用既有`getTimestamp`读数，记录当前估算算法究竟采用了哪条分支；来源转换不等于实际欠载，硬件时间戳存在也不证明首个可听采样。输入侧硬件时间戳、物理首声和完整对话延迟仍需独立测量。

## 显式检查

```sh
# 默认只输出计划，标准库Python及最小PATH可运行，不读设备或打开麦克风
./cc8 verify-device --runtime-root /absolute/runtime \
  --capture --playback-evidence --clock-evidence
```

实际执行另加`--execute`，使用该运行目录已准备的环境。该项复用原有有界前台收音、固定公开短句、owner-only IPC、结束停采和失败报告；必须同时指定`--capture`与`--playback-evidence`。后台／设备缺少能力时在首次打开界面或开麦之前拒绝，不把缺测记为零。

设备在`device_ready`声明`render_clock_report_version=1`及`playback_done_id_version=1`。主机仅在显式播放声学probe中，通过该次`play_begin`请求`report_audio_clock=true`；普通问答、普通播报及旧主机不请求。设备还要求现有收音及播放头回执能力。没有新增开麦、拍照、遥控或后台采集入口。

## 数字报告

播放器在20秒内、常量大小的计数器中记录已接受PCM的时钟来源：

| 来源 | 既有估算算法的选择 |
| --- | --- |
| `hardware` | 接受硬件帧位置/时间投影 |
| `clamped` | 接受近过去的硬件投影，但输出时间钳制到当前设备时间 |
| `missing` | 平台暂未给出时间戳，回退当前播放头估算 |
| `invalid` | 时间戳值或帧位置无效，回退播放头估算 |
| `outside` | 硬件投影超出现有过去20ms／未来1s窗口，回退播放头估算 |

40ms步长计数及最大相邻时间偏差使用同设备时钟；上述20ms／1s仍为原算法规则，不是Android准确性保证。只在播放完成回执中附带来源计数、写入次数/帧数、转换次数、时间步长和完整性，未发送PCM、波形、文字或逐帧绝对时间。不增加平台时间戳查询次数，不改变PCM、播放排队或原估算值。

主机按generation及playback ID绑定回执，核对报告帧数与发送前预留的实际PCM总数。拒绝布尔冒充整数、不一致计数、未知字段和不可能的转换次数；取消后及旧播放回执不能覆盖当前结果。坏诊断不会把已经完成的声音改成播放失败。

新端完成/播放失败回执同时携带句ID。已协商该能力时，缺ID或旧ID不会结束当前句；显式旧ID在同一代次内也会被拒绝，避免上一句的迟到完成/失败作用于下一句。旧端缺少该能力时保留原代次兼容路径，不虚构其具有句ID隔离能力。

原有`playback_probe_evidence`增加`render_clock_evidence`：`observed`为有效数字报告，`incomplete`为设备采样超时/无效，`invalid`为报告不一致，`missing`为终态前未收到，`cancelled`与`not_requested`分别表示取消及未请求。`hardware_timestamp_accepted`只描述算法是否接受过硬件读数，`first_audible_onset_measured`仍为false。`--clock-evidence`只在该报告为observed时通过；全部来源为missing仍可形成有效报告，但不会虚构硬件支持。

## 软件验证

离线验证包含真实Java分类/计数器到Python校验器的格式往返、10000组历史估算函数等价对照、超时/帧缺口、实际分包与owner IPC完成链、旧端、同代次迟到完成/失败、帧数不一致、缺报告、普通播报未请求、取消期间迟到结果和最小PATH计划。APK仅在候选目录构建/验签；当前三轮实机继续使用未变的0.14.19／code48。软件通过不等于新APK物理验收，不关闭P0-01。
