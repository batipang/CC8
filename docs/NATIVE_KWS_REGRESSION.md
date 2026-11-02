# P0-03 原生关键词源码回归

`python tools/native_kws.py` 只编译固定源码的复现程序，不载入模型、不联网、不读配置或凭证、不启动设备采集。需要 C++17 编译器；Mac 使用 Xcode Command Line Tools，Linux 使用支持 C++17 的工具链。dev doctor 检查编译器存在性，完整测试缺少编译器时失败，不跳过。

```sh
# 编译原始和修正后的 C++，输出 source-only JSON
python tools/native_kws.py
# 可选：在一个不存在的新目录导出两处源码修改及许可
python tools/native_kws.py --overlay /absolute/new/patch-overlay
```

## 已复现的问题

固定上游 2.0.1，commit `af2d8d81dbfa61c4cea4c568da78f3982edca005`。源码归档 SHA-256 为 `afcafe3c34bc2f15a363ad791fa1644a69bf0977a58c1b4f617468976c2faeb5`。七个未修改的原文件及 Apache-2.0 许可保存在 [固定片段](../tools/resources/sherpa-kws/README.md)，逐文件长度/摘要校验后才用于编译；不在运行时获取浮动版本。

1. 自动静音重置连同 encoder 状态一起清空，可能丢掉刚开始的词。已有 [上游 issue 3990](https://github.com/k2-fsa/sherpa-onnx/issues/3990)；补丁让自动静音分支只重置关键词结果，显式生命周期 Reset 仍清空 encoder。回归编译原始/修正后的实际 Reset、InitOnlineStream、结果清理方法和自动分支，使用最小状态接口验证自动处理保留 encoder、显式 Reset 重建 encoder。它不是完整神经模型或已安装二进制实测。
2. ContextGraph 回退后，输出的匹配词取概率历史的末尾词元，置信度却从历史开头取数。真实上下文图输入 `1,2,1,2,3`，目标为末尾 `1,2,3`，历史长度5、匹配长度3：高概率旧前缀把真实0.01算成0.663333，低概率旧前缀又把真实0.30算成0.106667。修正为取末尾匹配长度后，两例分别为0.01、0.30；无回退对照保持0.40。

复现编译的是原 ContextGraph、实际重置方法和从原 decoder 文件提取的置信度表达式，不以 Python 重写一个类似算法充当原生验证。完整补丁同时包含结果清理 helper 的定义；两个修正文件的正文摘要与本机已完成隔离构建的源码绑定，避免只替换调用却漏掉定义。导出时另加入显著的CC8修改标识注释，完整导出摘要包含该注释，已评估正文摘要单独保留。合成 token 概率不是人声录音，不代表唤醒准确率。结果固定 `installed_native_tested=false`、`runtime_changed=false`、`full_p0_acceptance=false`。

## 补丁边界

两个修改只输出到新的 overlay：按完整文件摘要及唯一上下文匹配；缺失、改动、符号/硬链接、非普通文件拒绝；原始输入不写入，既有输出不覆盖。新 overlay 最后才写 complete 清单，失败而未完成的目录不能用于构建。只校验列出的七个文件，不证明完整源码树、编译脚本或下载依赖可信。

这为后续原生构建提供可审查的输入，**不会替换当前安装的 sherpa-onnx**。正式 wheel 的平台、Python ABI、依赖/许可、二进制摘要、已安装内容及中文唤醒/停止流仍需分别验证；暂不把隔离研究用统一 wheel 安装进当前语音环境。

## 当前模型研究结果

更大的英文 transducer 固定到模型仓库 `csukuangfj/sherpa-onnx-streaming-zipformer-en-2023-06-26` 的 `672fbf1b30579d6585301139bb363f42a0ad4a24`，模型卡标 Apache-2.0，int8/fp32 文件均按上游摘要校验。新模型对公开合成样本的召回有所改善，但扩展45条矩阵仍有漏检及近音命中；增加尾部 blank 或换 fp32 并未通过全矩阵。新置信度补丁修正了确定的算法问题，也没有使该矩阵自动通过。

所有实验权重、wheel、公开合成音和原始报告仍在忽略目录；没有删除失败样本、放宽通过条件或更换正在运行的助手。P0-03 保持开放；真人、距离、电视及自播报仍需独立证据。

## 隔离安装验证

源码回归已通过 [PR #51](https://github.com/batipang/CC8/pull/51) 的八项必需CI并合入。后续在全新隔离环境中验证了一个Python绑定补丁wheel：保留上游2.0.1版本及其core2.0.1依赖，额外Build标识为`1cc8kws1`，独立构建清单记录修改源码与扩展SHA。原始官方facade逐文件保留，仅替换Python扩展和wheel元数据、重建RECORD并增加构建清单；实际安装的扩展SHA与清单相同，官方core的ONNX Runtime内容相同。

这项区别很关键：Python扩展静态包含关键词实现，不链接core的C API库；只替换core动态库不能证明Python路径已修正。本次只验证Python绑定，未修改官方core的C/C++ API库。wheel保守限定当前Mac arm64 / CPython3.13平台，尚无跨平台兼容结论；本地打包原型也不是正式可复现bootstrap。

该分包安装实际完成上游公开LOVELY CHILD音频的65个前导时点检查，65/65检出；扩展SHA为`6300ddc7a982a73a7f7a7630746691ae8a834ca42928ba781d904dc92534685e`。这独立于默认source-only回归，不改写其`installed_native_tested=false`。中文/停止完整组件矩阵见 [自动检查](AUTOMATED_DEVICE_CHECKS.md#中文唤醒与停止指令的数字回归)，不能以单个上游复现通过替代整套语音验收。
