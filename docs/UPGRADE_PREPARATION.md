# P1-09：离线升级准备与非密钥配置恢复预生成

后台工具候选 0.14.1，APK 仍为 0.8.9 code 34，协议 2。P0 待测源码固定在 p0-validation-base-20261102.1。以下命令不安装、不启停服务、不联系设备/模型，也不会替换配置；当前运行安装未改变。

## 版本规划

```sh
./cc8 upgrade plan --from-root /absolute/baseline-source --to-root /absolute/candidate-source
# 可选：使用已有 Android 工具实际检查候选包版本与 API-27 签名
./cc8 upgrade plan --from-root /absolute/baseline-source --assistant-apk /absolute/candidate.apk
```

两个目录必须各有经现有 release schema 校验的 release.json。报告独立 APK/后台/协议版本、已校验版本声明摘要，分别检查“旧设备协议配新后台”“新设备协议配旧后台”，标明 APK versionCode 降级。这里比较的是**源码声明，不是已装版本探测**。选填 APK 检查复用 tools/release.py，不调用 adb；校验候选签名不等于已核对设备原签名。

结果固定 ready_for_installation=false、installed_signer_checked=false、device_acceptance=not_asserted。协议任一方向不兼容时写报告并退出 2；退出 0 只表示声明检查完成。原签名/实际已装版本、服务所有权、配对/资源和物理切换仍须在正式部署流程核对，不能由一份 JSON 自动批准升级。Android 降级仍有平台限制，工具不会卸载、清数据或强制降级。

## 校验升级和恢复包

`verify-transition` 比较三个干净的 Git 工作区。基线 APK 的 SHA-256 必须与已核实的安装记录一致。工具校验三个 APK 的包名、版本和 Android 27 签名，要求签名证书一致、升级和恢复的 versionCode 逐步增加。它还核对两份新包的构建回执、双向协议兼容，以及恢复源码相对基线只改 `release.json`。

```sh
./cc8 upgrade verify-transition --from-root /absolute/running-source \
  --to-root /absolute/candidate-source --recovery-root /absolute/recovery-source \
  --baseline-apk /absolute/verified-baseline.apk --baseline-sha256 <installed-apk-sha256>
```

候选与恢复包从各自工作区的 `artifacts` 读取。输出为私有报告。命令只检查本地文件，无需签名钥匙、ADB 或运行中的设备。安装前仍须独立核实当时设备上实际包的版本、摘要和证书。

若需要把 72 小时观察作为升级前置，在同一命令追加 `--observation-report /absolute/private/window.json`。工具按基线源码的后台版本复核私有报告，重算采样和时间完整性，并在升级报告中记录观察摘要及其规范化 SHA-256。窗口仍在运行、版本不符或采样有缺口时，输出失败原因并退出 2。旧版观察只绑定后台版本及单实例，实际设备 APK 的身份仍在安装前独立核对。

本次使用原签名完成后台 0.14.33／APK 0.9.18 code53 候选与后台 0.14.20／APK 0.9.19 code54 恢复包的编译、静态检查和离线校验。恢复包保留现用功能源码，版本号高于 code53，满足恢复覆盖的版本顺序。现场仍运行 0.14.20／code49，升级和恢复均未执行。

## 显式备份与校验

```sh
./cc8 upgrade backup --profile /absolute/original/.toolchains/cc8/settings/assistant.json --source-root /absolute/original
./cc8 upgrade verify /absolute/private/backup.json
```

只读这份显式选择的 owner-private 非密钥配置文件，复用原 schema、大小、重复/未知字段与权限校验。原 asr-only legacy 格式可规范化到 schema 1；其他缺版本输入仍拒绝，不放宽原配置规则。

快照包含当前 schema 的默认值加文件内覆盖；**不采集环境覆盖或服务定义**，因此部署前仍需按 CONFIGURATION 核对原服务中的显式配置。source-root 指原运行工作区，用于固定相对凭据/原签名引用；不读取所引用文件。原默认签名路径也固定为原工作区绝对引用，避免换目录后生成或使用另一套钥匙。备份与恢复不能证明这些引用文件存在或符合原证书。

不复制凭据、私钥、配对材料、模型、录音、提醒或任务结果。输出含私有路径引用，因此只能保留本机私有目录，不提交 Git。默认写忽略的 device-access/private/upgrade-preparation，目录 0700 / 文件 0600；可用 --output 指定已存在的私有父目录中的新 JSON。拒绝已有文件、链接或公开父目录，不自动 chmod 外部目录。

## 恢复文件预生成

```sh
./cc8 upgrade stage-restore /absolute/private/backup.json --output /absolute/private/staged-settings.json
```

先校验 backup schema、规范化 settings 和 SHA-256，再创建**新** settings 文件。SHA-256 用于内容完整性检查，不是发布者签名。备份中的数据不决定恢复路径，输出只能来自命令明确指定；不允许覆盖原配置或已有文件。损坏、篡改、权限错误、未知 schema 和写失败不会改动原配置。

本命令不应用 staged 文件。真正升级/恢复仍须先明确停止原服务，核对原签名/配对/资源和配置环境，将核对后的配置应用到目标，再按 SIGNING / RELEASES / TODO 做实机验收。没有自动回放工具任务、播报或开启采集。

## 软件验证范围

十项离线回归覆盖双向协议、降级标记、复用 APK 检查、原配置不变、引用固定且不读钥匙/环境密钥、legacy 规范化、权限/链接/不覆盖、篡改/重复/超长输入、只读 verify 及最小 PATH CLI。所有备份使用合成配置。

另在系统 Python 3.9.6、PATH=/usr/bin:/bin 实际跑 root CLI：从固定 P0 source tag 的 manifest 到候选规划，对本机原签名 APK 0.8.9 实际 AAPT/API-27 检查；再用合成配置 backup → verify → stage-restore。全部通过，文件 0600，原合成文件字节不变，未读取真实运行配置或操作设备。完整批次见 [VALIDATION](VALIDATION.md)。

P1-09 仍开放：真实升级/回退演练、服务配置应用与设备验收尚未执行。
