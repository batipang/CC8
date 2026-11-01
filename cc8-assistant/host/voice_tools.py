"""Client-executed Mac/CC8 actions. No shell interpolation or mic-start action."""

from pathlib import Path
import subprocess
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]


def spec(name, description, properties, required):
    return {
        'type': 'function',
        'name': name,
        'description': description,
        'inputSchema': {
            'type': 'object',
            'properties': properties,
            'required': required,
            'additionalProperties': False,
        },
    }


TOOLS = [
    spec(
        'cc8_control',
        '按用户要求查询 CC8 状态、设置媒体音量（0-100）、停止播报或亮屏。没有开启麦克风、刷机或重启功能。',
        {
            'action': {'type': 'string', 'enum': ['status', 'volume', 'stop', 'wake_screen']},
            'percent': {'type': 'integer', 'minimum': 0, 'maximum': 100},
        },
        ['action'],
    ),
    spec(
        'mac_open',
        '按用户要求在当前 Mac 打开已安装的应用、HTTP(S)网页或在 Finder 显示当前项目文件。只负责打开，不发送消息或运行传入代码。',
        {
            'kind': {'type': 'string', 'enum': ['app', 'url', 'project_file']},
            'target': {'type': 'string', 'maxLength': 2048},
        },
        ['kind', 'target'],
    ),
    spec(
        'cc8_reminder',
        '仅按用户要求创建、查询、修改或取消大黄蜂提醒。due 必须是带时区的 ISO8601 时间，默认北京时间。提醒由当前 Mac 保存，Mac 服务与 CC8 前台连接时才可播报；不可声称已创建系统闹钟。修改/取消须先 list 获取真实 id。',
        {
            'action': {'type': 'string', 'enum': ['create', 'list', 'update', 'cancel']},
            'id': {'type': 'string', 'maxLength': 16},
            'title': {'type': 'string', 'maxLength': 120},
            'due': {'type': 'string', 'maxLength': 40},
            'repeat': {'type': 'string', 'enum': ['none', 'daily', 'weekdays', 'weekly']},
        },
        ['action'],
    ),
]


class VoiceTools:
    # 仅暴露工具白名单中的 Mac/CC8 动作，执行前复核轮次并返回真实结果。
    def __init__(self, device_control=None, run=subprocess.run, reminders=None):
        self.device_control = device_control
        self.run = run
        self.reminders = reminders

    def call(self, name, args, is_current=lambda: True):
        if not is_current():
            return {'ok': False, 'error': 'turn_cancelled'}
        if not isinstance(args, dict):
            raise ValueError('Invalid tool arguments')
        if name == 'cc8_reminder':
            if self.reminders is None:
                raise RuntimeError('Reminder service unavailable')
            return self.reminders(args, is_current)
        if name == 'cc8_control':
            if set(args) - {'action', 'percent'}:
                raise ValueError('Unexpected argument')
            action = args.get('action')
            percent = args.get('percent')
            if action not in ('status', 'volume', 'stop', 'wake_screen'):
                raise ValueError('Unsupported CC8 action')
            if action == 'volume' and (type(percent) != int or not 0 <= percent <= 100):
                raise ValueError('Volume must be 0-100')
            if not self.device_control:
                raise RuntimeError('CC8 tool is not connected')
            return self.device_control(action, percent)
        if name != 'mac_open' or set(args) - {'kind', 'target'}:
            raise ValueError('Unsupported tool')
        kind = args.get('kind')
        target = args.get('target')
        if (
            not isinstance(target, str)
            or not target
            or len(target) > 2048
            or any(ord(c) < 32 for c in target)
        ):
            raise ValueError('Invalid target')
        if kind == 'url':
            parsed = urlsplit(target)
            if (
                parsed.scheme not in ('https', 'http')
                or not parsed.hostname
                or parsed.username
                or parsed.password
            ):
                raise ValueError('HTTP(S) URL required')
            command = ['/usr/bin/open', target]
        elif kind == 'app':
            # Resolve an actual installed bundle; no executable paths/flags.
            if '/' in target or target.startswith('-') or len(target) > 100:
                raise ValueError('Application name required')
            app = target if target.endswith('.app') else target + '.app'
            roots = [
                Path('/Applications'),
                Path('/System/Applications'),
                Path('/System/Applications/Utilities'),
                Path.home() / 'Applications',
            ]
            bundle = next((folder / app for folder in roots if (folder / app).is_dir()), None)
            if bundle is None:
                raise ValueError('Application is not installed at a known location')
            command = ['/usr/bin/open', '-a', str(bundle)]
        elif kind == 'project_file':
            path = (ROOT / target).resolve()
            if not path.is_relative_to(ROOT) or not path.exists():
                raise ValueError('Existing project path required')
            if any(
                part.startswith('.') or part in ('private', 'signing')
                for part in path.relative_to(ROOT).parts
            ):
                raise ValueError('Private project path is not available')
            command = ['/usr/bin/open', '-R', str(path)]
        else:
            raise ValueError('Unknown Mac action')
        if not is_current():
            return {'ok': False, 'error': 'turn_cancelled'}
        result = self.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        return {
            'ok': result.returncode == 0,
            'action': kind,
            'target': target,
            'result': 'opened' if result.returncode == 0 else 'open_failed',
        }
