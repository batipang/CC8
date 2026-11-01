# 签名连续性与更新前核验

已安装应用的长期更新身份是原签名证书。新克隆生成的开发 JKS 与原设备的证书通常不同；知道别名/开发密码不能恢复丢失的私钥。仓库、CI 和发布包均不含原 JKS、私钥或设备数据。

## 保留与导入原签名

在原开发机保留 `cc8-diagnostics/signing/cc8-diagnostics-debug.jks`，将它备份到你管理的离线/加密存储，并记录别名 `cc8-diagnostics` 和原有密码。当前个人开发默认密码是 `android`，不适合作为公开发行的密钥管理方案。不要提交 JKS 或把它放进普通配置、聊天和日志。

更新现有设备前，显式指向原签名：

```sh
export CC8_SIGNING_KEYSTORE=/absolute/path/to/original-keystore.jks
python3 cc8-assistant/tools/build.py
python3 device-access/cc8_install.py --verify-apk artifacts/cc8-assistant-0.8.6.apk
```

也可将非敏感 `signing_keystore` 路径引用放在 owner-only 配置中，见 [CONFIGURATION.md](CONFIGURATION.md)。显式文件缺失时两个构建脚本失败，不生成替代钥。未指定时仍可为独立新安装创建本地开发签名；这不表示能覆盖原设备应用。

`--verify-apk` 只读核验唯一正常 Android USB 设备的型号 / SDK / shell UID、候选 APK 和同包名已装 APK 的签名；不传助手、不写设备、不安装、不启动界面。通过后再执行原来的 `--install`；正式安装会重新做同样核验，不能用旧结果跳过。原签名丢失时停止覆盖更新，不自动卸载、清数据、改包名或生成新钥代替。

## 校验过程与失败处理

候选 APK 先读为有界字节快照，AAPT、签名验证和安装流都使用同一份内容，避免编译产物在核验后被覆盖。Android SDK 35 的官方 [apksigner](https://developer.android.com/tools/apksigner) 通过 JDK 21+ 验证签名；本设备固定按 API 27 校验（`--min-sdk-version 27 --max-sdk-version 27 --print-certs --verbose`，参数语义见 [官方 help](https://android.googlesource.com/platform/tools/apksig/+/master/src/apksigner/java/com/android/apksigner/help_verify.txt)）。

已安装同包名时，只读取得单个 base APK 到 owner-only 临时目录（最多 150 MiB、30 秒），核对读取前后 SHA-256、包路径和包名，验证已装包签名，再比较完整 SHA-256 证书集合。临时文件随操作退出删除，不保留厂商应用或设备代码。证书匹配才允许进入原来的 helper 传输 / PackageInstaller；系统仍有独立最终签名检查，安装回调及装后哈希要求也保留。

无已装包时，候选仍必须签名有效，并由第二个包列表查询确认缺席，回执为 `signature_continuity=new_package`。升级匹配为 `matching_installed_signers`。签名无效、证书不一致、权限/读取失败、变化中的包、split APK 或无法确认缺席时均失败，不自动降级核验。当前只接受完整相同证书集合；不尝试接受其他系统版本的密钥轮换 lineage，也不保证未知固件可用。

| 提示 | 处理 |
| --- | --- |
| `Signing certificate mismatch` | 导入原 JKS 重建；不要卸载原应用 |
| `APK signature verification failed` | 检查 APK 是否完整、签名及 API 27 支持；不继续传包 |
| `Installed APK could not be read` / `hash unreadable` | 恢复正常工程 USB 通道与读取权限；不凭 dumpsys 的短哈希猜测证书 |
| `Installed APK changed while checking` | 等其他安装操作结束，再重新只读核验 |
| `Cannot verify a single installed base APK` | 本路径不支持 split / 歧义输出，不猜测目标 |

## 自动验证边界

SDK-free suite 验证证书集合解析、拒绝条件、候选快照一致和传包前中止。两个 Android CI job 在真实 assemble/lint 后，用构建出的 APK 运行 `tools/test_apk_signing.py`：验证 SDK 27、同证书匹配、临时新钥重签后拒绝、篡改后拒绝；临时密钥与 APK 不上传。它不安装设备，也不证明已备份的原私钥能够覆盖更新；实机只读结果和部署记录单独记录于 [VALIDATION.md](VALIDATION.md)。
