"""Keep login-service tool discovery consistent with the owner's local installs."""

import os
from pathlib import Path

FALLBACK_DIRS = (
    str(Path.home() / '.local/bin'),
    str(Path.home() / '.npm-global/bin'),
    '/opt/homebrew/bin',
    '/opt/homebrew/sbin',
    '/usr/local/bin',
    '/usr/local/sbin',
    '/usr/bin',
    '/bin',
    '/usr/sbin',
    '/sbin',
)


def command_path(value=None):
    # launchd 不读取交互 shell 配置；保留已有顺序，再补常见本机工具目录。
    # 排除空项/相对目录，避免当前工作目录被当作可执行程序搜索目录。
    original = os.environ.get('PATH', '') if value is None else value
    paths = []
    for part in (*original.split(os.pathsep), *FALLBACK_DIRS):
        if part and os.path.isabs(part) and part not in paths:
            paths.append(part)
    return os.pathsep.join(paths)


def runtime_environment(source=None):
    # 仅对子进程补 PATH，不改全局 shell，也不把环境密钥写进 LaunchAgent。
    result = dict(os.environ if source is None else source)
    result['PATH'] = command_path(result.get('PATH', ''))
    return result
