"""Validate and dispatch authenticated device events to their state owners."""

from __future__ import annotations
from dataclasses import dataclass
import queue
import re
from typing import TYPE_CHECKING, Callable

from protocol import validate_device_event
from session_ports import EventSink

if TYPE_CHECKING:
    from capture_pipeline import CapturePipeline
    from chat_controller import ChatController
    from device_controller import DeviceController
    from playback_controller import PlaybackController
    from saved_task_controller import SavedTaskController
    from session_controller import SessionController
    from vision_controller import VisionController
    from workspace import Workspace


@dataclass(frozen=True)
class DeviceEventRouter:
    """Keep no event, session or component state of its own."""

    session: SessionController
    capture: CapturePipeline
    chats: ChatController
    device: DeviceController
    playback: PlaybackController
    saved_tasks: SavedTaskController
    visual: VisionController
    workspace: Workspace
    begin_visual: Callable[[str, str, str], object]
    cancel: Callable[[str], int]
    status: Callable[[str, str], None]
    event: EventSink
    publish_workspace: Callable[[], None]
    submit_text: Callable[[object], object]
    reminder_action: Callable[[object], object]
    touch_activity: Callable[[], None]
    log: EventSink
    read_result: Callable[[dict], None] = lambda data: None

    def on_event(self, j):
        validate_device_event(j)
        event = j.get('event')
        if event in (
            'camera_image_begin',
            'camera_image_chunk',
            'camera_image_end',
            'camera_error',
        ):
            self.visual.feed(j)
        elif event == 'vision_request':
            nonce = j.get('nonce')
            if isinstance(nonce, str) and re.fullmatch('[a-f0-9]{32}', nonce):
                self.begin_visual(
                    '请描述当前摄像头画面中的主要事物，说明你能确定的内容；看不清时请说明。',
                    'button',
                    nonce,
                )
        elif event == 'vision_cancel':
            with self.session.guard:
                if self.visual.matches_cancel(j):
                    self.cancel('vision_cancel')
                    self.status('listening' if self.session.active else 'idle', '已停止看图')
        elif event == 'device_ready':
            self.session.ready(j)
            self.chats.publish(ok=True)
        elif event == 'dot_chat_request':
            self.chats.request(j)
        elif event == 'dot_chat_cancel':
            self.chats.cancel(j['request_id'])
        elif event == 'saved_task_request':
            self.saved_tasks.request(j)
        elif event == 'result_read_request':
            self.read_result(j)
        elif event == 'workspace_get':
            self.publish_workspace()
        elif event == 'text_request':
            with self.session.guard:
                generation = self.session.generation
                try:
                    self.submit_text(j.get('text'))
                except (ValueError, RuntimeError, OSError, queue.Full):
                    # Startup failures publish their own generation-bound
                    # guidance after rollback. Input/readiness rejection has
                    # not cancelled work and still uses the existing notice.
                    if self.session.generation == generation:
                        self.event(
                            'workspace_result',
                            ok=False,
                            message='文字指令未提交，请连接 Mac 并等待校准结束',
                        )
        elif event == 'reminder_action':
            try:
                self.reminder_action(j.get('arguments'))
                self.event('workspace_result', ok=True, message='提醒已更新')
            except (ValueError, OSError):
                self.event(
                    'workspace_result', ok=False, message='提醒未保存，请检查时间、内容及 Mac 存储'
                )
        elif event == 'task_cancel':
            # The owner-local text IPC can replace this generation while the
            # device thread is handling an older button. Match and cancel in
            # the same session transaction, as vision cancellation does.
            with self.session.guard:
                task = next(
                    (
                        t
                        for t in self.workspace.snapshot()['tasks']
                        if t['id'] == j.get('id') and t['status'] == 'running'
                    ),
                    None,
                )
                if task and task['generation'] == self.session.generation:
                    self.cancel('task_button')
                    self.status(
                        'listening' if self.session.active else 'idle',
                        '已停止任务等待；已执行的操作不会撤销',
                    )
        elif event == 'session_rebind' and j.get('capture_active'):
            self.session.rebind(j)
        elif event == 'device_control_result':
            self.device.receive(j)
        elif event == 'screen_touch':
            self.touch_activity()
        elif event == 'settings':
            self.session.settings(j)
        elif event == 'session_start':
            self.session.start_capture(j)
        elif event == 'session_stop':
            # A stopped worker can finish after a new foreground capture starts.
            self.session.stop_capture(j)
        elif event == 'interrupt':
            self.session.interrupt(j)
        elif event in (
            'play_done',
            'playback_paused',
            'playback_resumed',
            'playback_error',
            'playout_progress',
        ):
            self.playback.device_event(j)
        elif event == 'capture_gap':
            self.capture.note_read_gap(j.get('epoch'), j.get('sequence'))
            self.log('capture_read_gap', milliseconds=j.get('milliseconds'))
