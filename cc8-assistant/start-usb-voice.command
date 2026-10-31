#!/bin/zsh
set -e
cd -- "${0:A:h}/.."
python3 cc8-assistant/host/launch.py
print "按回车关闭此窗口；语音服务会继续运行，CC8 的停止按钮可关闭采集。"
read
