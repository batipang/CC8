"""One serial plan, accepted operation receipts and RAM-only results; never retry work."""

from copy import deepcopy
from dataclasses import dataclass
import math
import re
import threading
import time
import uuid

from workflow_definition import definition, fingerprint, schedulable, token

RECEIPTS = dict(
    assistant='assistant_turn',
    web_search='web_response',
    work_report='report_read',
    ocr='vision_turn',
)
RESULT_LIMIT = 8000


class WorkflowCancelled(RuntimeError):
    pass


class WorkflowUnconfirmed(RuntimeError):
    pass


@dataclass(frozen=True)
class StepOutput:
    receipt_kind: str
    operation_id: str
    text: str
    completed: bool
    truncated: bool = False

    def normalized(self, kind):
        if (
            self.receipt_kind != RECEIPTS[kind]
            or self.completed is not True
            or not isinstance(self.operation_id, str)
            or not re.fullmatch('[A-Za-z0-9._:-]{1,128}', self.operation_id)
        ):
            raise WorkflowUnconfirmed('Step operation receipt missing')
        if (
            not isinstance(self.text, str)
            or not self.text.strip()
            or type(self.truncated) is not bool
        ):
            raise WorkflowUnconfirmed('Step result invalid')
        self.text.encode('utf-8')
        if any(ord(c) < 32 and c not in '\n\t' for c in self.text):
            raise WorkflowUnconfirmed('Step result control data invalid')
        return dict(
            receipt_kind=self.receipt_kind,
            operation_id=self.operation_id,
            text=self.text[:RESULT_LIMIT],
            truncated=self.truncated or len(self.text) > RESULT_LIMIT,
            independently_verified=False,
        )


class WorkflowRun:
    def __init__(
        self, flow, *, origin='manual', clock=time.monotonic, owner_guard=None, attempt=None
    ):
        self.flow = deepcopy(flow)
        definition(self.flow)
        if origin not in ('manual', 'scheduled'):
            raise ValueError('Invalid workflow origin')
        self.ident = uuid.uuid4().hex
        self.definition_hash = fingerprint(self.flow)
        if origin == 'scheduled':
            if (
                not isinstance(attempt, dict)
                or attempt.get('workflow_id') != self.flow['id']
                or attempt.get('definition_hash') != self.definition_hash
                or attempt.get('execution') != 'claimed'
                or attempt.get('delivery') != 'not_started'
            ):
                raise ValueError('Scheduled workflow requires its durable matching claim')
            self.ident = token(attempt.get('id'))
        elif attempt is not None:
            raise ValueError('Manual workflow cannot borrow a scheduled claim')
        self.origin, self.clock = origin, clock
        # Production supplies the session owner RLock. Admission callbacks run
        # in that same transaction, never under a separate private plan lock.
        self.guard = owner_guard if owner_guard is not None else threading.RLock()
        self.state = 'prepared'
        self.reason = None
        self.used = False
        self.deadline = None
        self.current = None
        self.rows = [
            dict(id=s['id'], title=s['title'], kind=s['kind'], state='pending', result=None)
            for s in self.flow['steps']
        ]
        self._owned = lambda: False
        self._publish = lambda state: None

    def live(self):
        with self.guard:
            return (
                self.state in ('running', 'completed')
                and self.current is not None
                and self.clock() < self.deadline
                and self._owned() is True
            )

    def check(self):
        with self.guard:
            if self._owned() is not True or self.state == 'cancelled':
                raise WorkflowCancelled('Workflow owner changed')
            if self.clock() >= self.deadline:
                raise TimeoutError('Workflow deadline exceeded')

    def snapshot(self, *, include_results=False):
        with self.guard:
            rows = []
            for row in self.rows:
                value = {k: deepcopy(v) for k, v in row.items() if k != 'result'}
                if include_results:
                    value['result'] = deepcopy(row['result'])
                rows.append(value)
            return dict(
                id=self.ident,
                workflow_id=self.flow['id'],
                name=self.flow['name'],
                definition_hash=self.definition_hash,
                origin=self.origin,
                state=self.state,
                reason=self.reason,
                steps=rows,
                independently_verified=False,
            )

    def publish(self):
        # Production uses the same reentrant session lock for run and parent
        # task admission. No separate component lock may invert that order.
        self._publish(self.snapshot())

    def cancel(self):
        with self.guard:
            if self.state in ('completed', 'failed', 'cancelled'):
                return False
            self.state = 'cancelled'
            self.reason = 'owner_cancelled'
            for row in self.rows:
                if row['state'] == 'running':
                    row['state'] = 'unconfirmed'
        self.publish()
        return True

    def accept(self, step_id, output):
        if not isinstance(output, StepOutput):
            raise WorkflowUnconfirmed('No operation receipt')
        with self.guard:
            row = next((item for item in self.rows if item['id'] == step_id), None)
            if row is not None and row['state'] == 'completed':
                return False
            self.check()
            if row is None or row['id'] != self.current:
                raise WorkflowUnconfirmed('Wrong workflow step receipt')
            if self.state != 'running' or row['state'] != 'running':
                raise WorkflowUnconfirmed('Step is no longer current')
            result = output.normalized(row['kind'])
            row.update(state='completed', result=result)
            if all(item['state'] == 'completed' for item in self.rows):
                self.state = 'completed'
            # Publish the authoritative parent completion before releasing the
            # owner transaction; Stop cannot cancel it between these commits.
            self.publish()
        return True

    def run(
        self,
        execute,
        *,
        current,
        authorize_camera=lambda flow, step: False,
        publish=lambda state: None,
        budget=300,
    ):
        if (
            not callable(execute)
            or not callable(current)
            or type(budget) not in (int, float)
            or not math.isfinite(budget)
            or not 0 < budget <= 300
        ):
            raise ValueError('Invalid workflow execution boundary')
        with self.guard:
            if self.used or self.state != 'prepared':
                raise ValueError('Workflow attempt already consumed')
            self.used = True
            self._owned = current
            self._publish = publish
            self.deadline = self.clock() + budget
            self.state = 'running'
        try:
            self.check()
            if self.origin == 'scheduled' and (
                not schedulable(self.flow)
                or self.flow['schedule'] is None
                or self.flow['schedule']['enabled'] is not True
            ):
                raise WorkflowUnconfirmed('Unsafe scheduled workflow')
            # Resolve all report scopes before a network request or camera.
            if any(s['kind'] == 'work_report' and s['source'] is None for s in self.flow['steps']):
                raise WorkflowUnconfirmed('Report scope must be explicitly frozen')
            self.publish()
            for item, row in zip(self.flow['steps'], self.rows):
                self.check()
                if item['kind'] == 'ocr' and (
                    self.origin != 'manual'
                    or authorize_camera(self.definition_hash, item['id']) is not True
                ):
                    raise WorkflowUnconfirmed('Fresh visible camera consent required')
                self.check()
                with self.guard:
                    self.check()
                    self.current = item['id']
                    row['state'] = 'running'
                self.publish()
                output = execute(
                    deepcopy(item),
                    self.live,
                    self.deadline,
                    lambda receipt, ident=item['id']: self.accept(ident, receipt),
                )
                # Receive-path admission can precede generator cleanup. Once
                # accepted, completed operations survive stop/narration failure.
                with self.guard:
                    accepted = row['state'] == 'completed'
                if not accepted:
                    self.accept(item['id'], output)
            return self.snapshot(include_results=True)
        except Exception as error:
            with self.guard:
                if self.state != 'completed':
                    if isinstance(error, WorkflowCancelled) or self.state == 'cancelled':
                        self.state = 'cancelled'
                        self.reason = 'owner_cancelled'
                    else:
                        self.state = 'failed'
                        self.reason = (
                            'deadline'
                            if isinstance(error, TimeoutError)
                            else 'operation_unconfirmed'
                        )
                    for row in self.rows:
                        if row['state'] == 'running':
                            row['state'] = 'unconfirmed'
            self.publish()
            return self.snapshot(include_results=True)
        finally:
            with self.guard:
                self.current = None
