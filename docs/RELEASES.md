# 版本、构建回执与源码快照

根目录 `release.json` 是独立组件版本与兼容关系的唯一来源。`release_info.py` 做有界/重复字段/类型/范围校验，后台 health / status 读取其版本；两个 Gradle 工程和构建脚本读取同一清单，APK 的 versionName / versionCode 不再各写一份。助手 BuildConfig.CC8_PROTOCOL 与后台握手兼容检查也来自清单。

| foundation-20261102.1 已标记源码组件 | 版本 | 含义 |
| --- | --- | --- |
| 助手 APK | 0.8.7 / code 32 | Android 包版本，升级仍须原签名预检 |
| Mac 后台 | 0.12.5 | 配置、统一入口及运行/版本状态 |
| 检测器 APK | 1.0.1 / code 2 | 独立检测应用，无需随后台升版 |
| 协议 | 2，设备接受范围 2～2 | 包布局和握手不变；不同协议拒绝就绪 |

`known_deployed_pair` 记录原现场助手 0.8.6 / code 31、后台 0.12.2、协议 2；它不是候选已部署的声明。相同协议表示声明的传输兼容，不代表每个可选功能、声学效果或物理切换已验收。破坏协议的改动必须改清单/兼容范围并补回归，不强迫所有组件版本号相同。

工作中候选以 `python3 tools/release.py show` 和 CHANGELOG 的 Unreleased 为准：当前资源观察候选为后台 0.13.2 / runtime-observation-20261102.1，APK / 协议未升版，尚未建立对应 tag 或部署。下方 .1 命令用于 checkout 到该已标记源码时复验其原清单。

## 构建与内容核验

按 [SIGNING.md](SIGNING.md) 引用原签名，运行两个构建脚本。每个 APK 附带 `.apk.sha256` 和 `.apk.build.json`：回执包含源码提交、脏状态、manifest 摘要、组件版本及 APK 摘要，写入前核对源码状态未变化。无 Git 的源码归档能构建候选，但标记 source_commit=null / source_dirty=true，不能成为干净发布。

```sh
python3 tools/release.py show
python3 tools/release.py check-apk assistant artifacts/cc8-assistant-0.8.7.apk
python3 tools/release.py check-apk diagnostics artifacts/cc8-diagnostics-1.0.1.apk
```

check-apk 用 AAPT 比对包名、versionName、versionCode，用官方 apksigner 验证 API 27 签名并核对前后内容哈希。两工程 CI 在 assemble/lint 和真实签名测试后执行同一检查；不访问设备或模型。

## 记录与可追溯 tag

源码提交、CI 通过并合入后，从干净的对应提交重建两个 APK。另一个提交构建的同版本包不能直接写新记录：构建回执必须匹配当前提交、manifest 和内容。

```sh
# 工程基础源码快照，不宣称完整 P0 实机验收
git tag -a foundation-20261102.1 -m 'CC8 foundation source snapshot; device acceptance pending'
git push origin foundation-20261102.1
python3 tools/release.py record --tag foundation-20261102.1
python3 tools/release.py verify artifacts/releases/foundation-20261102.1.json
```

`record --tag` 要求 annotated tag 指向当前干净提交。清单含源码提交、manifest 摘要、独立版本/协议范围、两个 APK 哈希及公开证书摘要，固定 `device_acceptance=not_asserted`。文件在忽略的 artifacts/releases，不含 JKS、密钥、绝对路径、配对资料或设备身份。verify 重查预期本地产物/回执/源码，不跟随记录的任意路径。tag 定位源码，清单定位本机构建内容；这不是可复现构建或第三方供应链签名证明。

开发中可显式 `record --allow-dirty` 保存候选清单；它不能通过 verify 或绑定发布 tag。默认拒绝脏源码、缺失/过期回执，不把编译通过等同于设备验收。首次 tag 建立及实际记录结果见 VALIDATION；本文命令示例以实际存在的 tag 为准。

回退仍须核对协议、原签名和已有 versionCode；Android 的降级限制不会因持有旧 APK 消失。工具不自动卸载、清数据或降级，P1-09 的升级回退演练单独进行。
