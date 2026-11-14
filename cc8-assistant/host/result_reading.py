"""RAM-only result narration checkpoints; never a provider or task retry."""

from collections import OrderedDict
from dataclasses import dataclass
import re
import threading
from web_search import SpeechClauses

MAX_TEXT = 10000


class ReadingUnavailable(ValueError):
    pass


def reading_command(text):
    if not isinstance(text, str) or len(text) > 100:
        return None
    value = re.sub(r'^(?:大黄蜂|bumblebee)[，,：:\s]*', '', text.strip(), flags=re.I)
    value = ''.join(value.split()).rstrip('。.!！?？')
    if value in ('继续播报', '继续朗读', '继续朗读结果', '继续播报刚才的结果'):
        return False
    if value in ('从头朗读结果', '从头朗读刚才的结果', '重新朗读结果', '重新播报刚才的结果'):
        return True
    return None


@dataclass(frozen=True)
class ReadingPlan:
    ident: str
    serial: int
    offset: int
    text: str
    truncated: bool


class ResultReading:
    def __init__(self):
        self.lock = threading.RLock()
        self.entries = OrderedDict()
        self.serial = 0
        self.last_read = None

    def begin(self, ident, generation):
        with self.lock:
            self.last_read = None
            self.entries[ident] = dict(
                generation=generation,
                text=None,
                heard='',
                original_gap=False,
                aligned=True,
                offset=0,
                serial=0,
                truncated=False,
            )
            while len(self.entries) > 12:
                self.entries.popitem(last=False)

    def complete(self, ident, text, truncated=False):
        with self.lock:
            entry = self.entries.get(ident)
            if entry is None or entry['text'] is not None:
                return
            # Reuse the speech renderer's URL/markup cleaning, without reading
            # files, interpreting a command, or retaining provider tool output.
            entry['text'] = ''.join(SpeechClauses().feed(text[:MAX_TEXT], final=True))
            entry['truncated'] = bool(truncated or len(text) > MAX_TEXT)
            entry['aligned'] &= entry['text'].startswith(entry['heard'])
            entry['offset'] = len(entry['heard']) if entry['aligned'] else 0

    def original_ack(self, ident, generation, clause, *, confirmed=True):
        with self.lock:
            entry = self.entries.get(ident)
            if entry is None or entry['generation'] != generation or entry['serial']:
                return
            # Offset counts only the contiguous confirmed prefix. A later
            # matching clause may repeat the exact missing words.
            if not confirmed:
                entry['original_gap'] = True
            if entry['original_gap']:
                return
            heard = entry['heard'] + clause
            if len(heard) > MAX_TEXT:
                entry['aligned'] = False
                return
            entry['heard'] = heard
            if entry['text'] is not None:
                entry['aligned'] &= entry['text'].startswith(heard)
                entry['offset'] = len(heard) if entry['aligned'] else 0

    def available(self, ident, restart=False):
        with self.lock:
            entry = self.entries.get(ident)
            if not entry or not entry['text']:
                raise ReadingUnavailable(
                    '没有可朗读的已完成结果，请先查看任务状态；不会重新执行任务。'
                )
            if not restart and not entry['aligned']:
                raise ReadingUnavailable(
                    '原播报与结果无法精确对齐，请在任务详情选择“从头朗读”；不会猜测续读位置。'
                )
            if not restart and entry['offset'] >= len(entry['text']):
                raise ReadingUnavailable('这份结果已确认读完；如需再听，请选择“从头朗读”。')

    def coverage(self, ident):
        with self.lock:
            entry = self.entries.get(ident)
            if entry and entry['text'] and entry['aligned'] and entry['offset']:
                if entry['offset'] >= len(entry['text']):
                    return 'excerpt' if entry['truncated'] else 'complete'
                return 'partial'
        return None

    def plan(self, ident, restart=False):
        with self.lock:
            self.available(ident, restart)
            entry = self.entries[ident]
            if restart:
                entry['offset'], entry['aligned'] = 0, True
            self.serial += 1
            entry['serial'] = self.serial
            self.last_read = ident
            return ReadingPlan(
                ident,
                self.serial,
                entry['offset'],
                entry['text'][entry['offset'] :],
                entry['truncated'],
            )

    def target(self, tasks):
        with self.lock:
            if self.last_read is not None:
                return next(
                    (t for t in tasks if t['id'] == self.last_read and t['status'] == 'completed'),
                    None,
                )
            return tasks[0] if tasks and tasks[0]['status'] == 'completed' else None

    def select(self, ident):
        with self.lock:
            if ident in self.entries:
                self.last_read = ident

    def ack(self, plan, consumed, clause):
        with self.lock:
            entry = self.entries.get(plan.ident)
            if (
                entry is None
                or entry['serial'] != plan.serial
                or type(consumed) is not int
                or consumed < 0
                or consumed + len(clause) > len(plan.text)
                or plan.text[consumed : consumed + len(clause)] != clause
                or entry['offset'] != plan.offset + consumed
            ):
                return False
            entry['offset'] += len(clause)
            return True
