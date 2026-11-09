# CC8 提醒的 Mac 桌面渠道

0.16.10／原签名助手0.9.69（code106）加入显式的桌面静默通知和设备端送达记录；实际部署及验收范围见[当前部署](CURRENT_DEPLOYMENT.md)。现有提醒默认仍为CC8播报，每条提醒单独选择渠道，不会双发或把失败的桌面通知改成语音。

## 一次准备与使用

Mac需macOS12以上、Apple命令行开发工具及Python3.11以上。用项目已有dev／voice解释器执行：

```sh
python3.13 cc8 notifications setup
python3.13 cc8 notifications authorize
python3.13 cc8 notifications status
```

`setup`只编译、以本地临时证书签名并安装用户私有的辅助App，不请求通知授权或发试通知。App位于当前用户`Library/Application Support/CC8/Notifications`下，安装不会修改Dot／MiniMax或LaunchAgent配置；遇到身份不明的既有文件会停止，不删除它们。签名仅用于本机辅助App，和CC8的Android原签名是不同用途。当前是个人本地构建方案，未作Apple公证或跨机器分发承诺。

只有显式执行`authorize`才请求系统通知权限。在Mac系统对话框选择允许；以后可在系统设置里调整。查询`status`不弹授权框、不发送通知；`not_determined`表示尚未授权，`denied`表示已拒绝。开发构建和CI均不执行授权。系统专注模式、通知样式、登录与睡眠仍会影响实际可见性，不保证横幅即时出现。

CC8“添加提醒／修改提醒”选择“Mac桌面静默通知”，或明确说“在Mac桌面提醒我……”。默认使用CC8语音；编辑保留原渠道。新版界面依据后台能力显示可用渠道；连接旧后台时保持原CC8请求格式，不发送未知字段，也不把不支持的桌面事项改成播报。两者仍遵守已有CC8前台认证连接、空闲管线、明确安静时段与迟到规则；不启动后台设备、不打开麦克风或摄像头。Mac／CC8离线时不保证准时。这版没有新增脱离CC8的独立系统闹钟。

设置中的“提醒送达记录”显示最多12条最近状态，正文标为当前事项内容，不冒充原播报／通知的历史原文；最近尝试的渠道与原定时间、当前下一次安排分别显示。只保留用户明确保存的提醒，不新增原文版本库。语音工具的`history`可查询相同记录。重复事项保留原时钟相位，并在一轮确认后移到下一未来发生次。

## 回执的含义

- CC8：只有对应发生次收到实际`completed`播放回执才记“已播完”。
- Mac：`UNUserNotificationCenter.add`成功只表示提交；只有查询到同一通知ID仍在通知中心，才记“已进入通知中心，未确认阅读”。这不证明用户看过，不冒充CC8播报。
- 未授权、未安装、系统拒绝、辅助程序退出或提交后未查询到显示：记录“送达未确认”和有界原因；不自动重发，可明确重新安排或取消。

发送前先原子认领并保存发生次／渠道／尝试ID。同一批最多三条且必须同渠道，桌面通知只有静默文字。系统请求在独立有界工作线程内执行，接收／采集读线程不等通知服务；实际发送前复核原任务、线路和提醒归属。请求已交给系统后不能保证撤回，迟到旧回执不能结束后来编辑的提醒。重启遗留认领保持未确认，不自动补发。

## 本机边界与存储

辅助App只处理一次请求后退出，用AppKit与UserNotifications原生接口，通过权限0700临时目录中的owner-only Unix套接字接收固定结构JSON。主机不把正文放入命令行、脚本、额外文件或日志。已交付的通知正文由macOS通知中心按系统规则保留，锁屏显示与清除也受系统设置管理。辅助App和父进程均核对本机用户身份；每次使用检查源码摘要、确切文件清单和签名，发现更改停止。父进程只清理自己的启动器，不按进程名称终止其他程序。

提醒格式升级为schema3，读取schema1／2时不写回；明确修改才写3，保留默认CC8渠道。reader2不能读取3，升级／恢复门控拒绝这类降级，不还原旧提醒文件覆盖新数据。schema2仅加标头改称schema1不能绕过新字段校验。配置备份继续排除提醒正文。

## 检查范围

自动回归覆盖实际原子存储、同渠道合并、错误渠道／旧回执、未确认不重试、重启、安静／忙碌、重复发生次、设备保存回调与真实本地套接字、畸形响应、其他UID、辅助App文件与未知安装保护。Mac CI编译签名但不安装、不授权、不发通知。

开发已实际通过原生App的权限状态查询，结果尚未授权；真实授权后的通知中心正向送达仍列环境／最终验收，不用fixture补判。真人体感、真实登录／睡眠及最终72小时不因软件检查通过而关闭；它们不阻塞其他独立功能。

接口依据：[Apple通知授权](https://developer.apple.com/documentation/usernotifications/asking-permission-to-use-notifications)、[提交本地通知](https://developer.apple.com/documentation/usernotifications/scheduling-a-notification-locally-from-your-app)、[查询仍在通知中心的已送达通知](https://developer.apple.com/documentation/usernotifications/unusernotificationcenter/getdeliverednotifications(completionhandler:))。
