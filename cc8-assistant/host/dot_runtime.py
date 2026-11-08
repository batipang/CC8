"""Resolve the owner Codex command and inspect availability without starting it."""

from pathlib import Path
import shutil
from runtime_env import command_path
from dot_receipt import DotError


def codex_command():
    binary = shutil.which('codex', path=command_path())
    if not binary:
        candidate = Path.home() / '.local/bin/codex'
        if candidate.is_file():
            binary = str(candidate)
    if not binary:
        raise DotError('Codex CLI is unavailable')
    # Inherit the owner's actual configured model, reasoning, skills and tools.
    # Overrides belong to this child only; never rewrite the desktop config.
    command = [binary, 'app-server', '--stdio', '-c', 'web_search="live"']
    return command


def runtime_status():
    # 只报告依赖就绪位，不导出本机路径、环境变量或凭证，也不启动模型查询。
    try:
        binary = codex_command()[0]
        with open(binary, 'rb') as source:
            header = source.read(128)
        node_required = header.startswith(b'#!') and b'node' in header.split(b'\n', 1)[0]
        node_available = shutil.which('node', path=command_path()) is not None
        return {
            'codex_available': True,
            'node_required': node_required,
            'node_available': node_available,
            'ready': not node_required or node_available,
        }
    except (DotError, OSError):
        return {'codex_available': False, 'ready': False}
