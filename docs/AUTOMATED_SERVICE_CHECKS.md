# 自动后台崩溃恢复检查

P0-02 的真实进程验证使用 `verify-service`，与隔离模拟 worker 的 supervisor 单元回归分别记录。默认只打印计划，不查设备/进程、不创建目录、不加载模型。

```sh
./cc8 verify-service --runtime-root /absolute/live/workspace
# 最多十轮；显式终止空闲的本项目后台，由已有守护恢复
/usr/bin/python3 cc8 verify-service --runtime-root /absolute/live/workspace --cycles 10 --execute
```

执行前须由用户授权相关维护，实际运行目录必须是已启用原有 LaunchAgent 的真实绝对路径。共享实际 runtime 的 `automation-test.lock`，不能与自动设备测试并行。不会安装/启用一个停止的服务，不调用任意终端命令，不打开 UI、不开始麦克风或相机。

## 门控和依据

- 正常 Android 模式下唯一 AILABS_S6 / SDK27 / shell UID2000，每次状态操作重核同一 USB target。
- 用户自己的服务启用标志和 LaunchAgent 必须匹配该 runtime；进程列表按 UID、完整脚本路径和参数核对，一个 supervisor、至多一个 backend，额外参数的同脚本进程也拒绝。后台必须是对应 supervisor 的直接子进程，supervisor 私有 ready 回执 PID 匹配；仅有 PID 文件或名字包含 bridge 不作为选取依据。
- 源码/backend/protocol/APK 版本相符；设备前台、双端麦克风关闭、摄像头关闭、无播放/校准；有未完成的诊断请求就拒绝。每轮发信号前再次核对完整空闲快照、实例与进程，不跟随更换的目标。
- 只向核实的本项目后台 PID 发送一次 SIGKILL，模拟异常退出；不向 supervisor、OEM 服务或其他工作区发送信号，不写系统分区。不调整守护退避等待。
- 恢复须同时观察新 PID 和新 runtime instance、worker/model 就绪、USB 连接、supervisor ready、双端仍不开麦；三秒后重新核对稳定快照，再计本轮通过。每次等待上限90秒。

这是用户级空闲后台的故障注入；不是睡眠/登录、任务执行中崩溃、真实物理 USB 拔插或副作用任务不重跑的完整验收。进程身份核对与信号调用是独立系统调用，不能宣称提供 macOS 不具备的原子 PID 句柄保证。遇到所有者开始使用设备或停止服务即中断，不覆盖用户操作。

## 报告与结束

源码目录 `device-access/private/service-checks/` 原子保存状态检查点，目录0700、文件0600。只保留版本、脚本摘要、轮次、固定错误代码、单端恢复时间和结束确认；不保存 PID、runtime instance、设备标识、请求正文、音视频或凭据。恢复时间包括轮询和三秒稳定检查，不是精确进程启动耗时或语音延迟。

异常/取消后仅继续有界观察已有 supervisor 的恢复结果，不另外启动服务、打开 UI 或重放任务。所有者停止/捕获启动/恢复超时不能通过，无法确认恢复则标记 `recovery_unconfirmed`。SIGKILL 或 Mac 断电使测试本身无法执行 finally 时，不保证终态报告；后台是否恢复仍由既有守护负责。

故障测试会使资源观察分段。十轮完成后从新的稳定实例重新开始独立72小时观察，保留之前的含注入记录，不把混合实例时间算成单实例72小时。
