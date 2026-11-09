"""Real response dispatch and extracted Java event branches preserve terminal errors."""

from pathlib import Path
from test_memory_fixture import isolated_memory
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
import numpy as np

from bridge import Bridge
from dot_client import DotError
from dot_receipt import UnconfirmedTurnStart
from failure_feedback import feedback
from reply_types import Reply, TextReply
from test_failure_feedback import Brain, SECRET
from tools.java_toolchain import java_bin
from test_interrupted_task_notice import method

ROOT = Path(__file__).resolve().parents[2]
JAVA = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'


class TextFailureTests(unittest.TestCase):
    def bridge(self, brain):
        with patch('threading.Thread.start'):
            b = Bridge(None, brain, preference_memory=isolated_memory())
        b.session.connection = object()
        b.save_status = lambda: None
        b.events, b.logs = [], []
        b.event = lambda name, **fields: b.events.append((name, fields))
        b.log = lambda name, **fields: b.logs.append((name, fields))
        return b

    def work(self, b, jobs):
        with patch.object(b.response.queue, 'get', side_effect=[*jobs, SystemExit]):
            with self.assertRaises(SystemExit):
                b.response.work()

    def test_nonrecording_text_errors_reach_ui_before_text_done_each_three_times(self):
        for kind in (
            'timeout',
            'rate_limit',
            'quota',
            'credential_or_permission',
            'transport_or_io',
            'approval_declined',
        ):
            for _ in range(3):
                with self.subTest(kind=kind):
                    brain = Brain(DotError(SECRET, kind=kind))
                    b = self.bridge(brain)
                    result = b.submit_text('固定公开测试指令')
                    job = b.response.queue.get_nowait()
                    self.assertIsInstance(job, TextReply)
                    self.assertFalse(b.session.active)
                    self.work(b, [job])
                    errors = [
                        i
                        for i, (event, data) in enumerate(b.events)
                        if event == 'status' and data.get('phase') == 'error'
                    ]
                    ends = [i for i, (event, data) in enumerate(b.events) if event == 'text_done']
                    self.assertEqual(
                        len(errors), 1, 'Failure was lost after clearing text_generation'
                    )
                    self.assertEqual(len(ends), 1)
                    self.assertLess(errors[0], ends[0])
                    self.assertEqual(
                        b.events[errors[0]][1]['message'], feedback('brain', kind).message
                    )
                    self.assertEqual(brain.calls, 1)
                    self.assertIsNone(b.session.text_generation)
                    self.assertFalse(b.response.busy)
                    self.assertEqual(b.workspace.tasks[-1]['status'], 'failed')
                    self.assertEqual(result['generation'], job.generation)
                    self.assertNotIn(SECRET, str((b.events, b.logs)))

    def test_workspace_send_failure_preserves_unconfirmed_start_guidance(self):
        for send_error in (BrokenPipeError, EOFError):
            with self.subTest(send_error=send_error.__name__):
                brain = Brain(UnconfirmedTurnStart(kind='timeout'))
                b = self.bridge(brain)
                b.submit_text('固定公开测试指令')
                job = b.response.queue.get_nowait()
                publish = b.publish_workspace

                def send_workspace():
                    if b.workspace.tasks[-1]['status'] == 'failed':
                        raise send_error(SECRET)
                    publish()

                with patch.object(b, 'publish_workspace', side_effect=send_workspace) as send:
                    self.work(b, [job])
                self.assertEqual(send.call_count, 2)
                message = feedback('brain', 'timeout', unconfirmed=True).message
                self.assertEqual(b.workspace.tasks[-1]['result'], message)
                errors = [
                    fields['message']
                    for event, fields in b.events
                    if event == 'status' and fields.get('phase') == 'error'
                ]
                self.assertEqual(errors, [message])
                self.assertIn(
                    (
                        'failed_response_workspace_undelivered',
                        {'error_type': send_error.__name__},
                    ),
                    b.logs,
                )
                self.assertEqual(brain.calls, 1)
                self.assertIsNone(b.session.text_generation)
                self.assertNotIn(SECRET, str((b.events, b.logs)))

                brain.error = None
                b.speak = lambda text, generation: True
                b.submit_text('新的固定公开测试指令')
                self.work(b, [b.response.queue.get_nowait()])
                self.assertEqual(brain.calls, 2)
                self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')

    def test_failure_publication_and_new_owner_admission_are_one_transaction(self):
        b = self.bridge(Brain(DotError(SECRET, kind='timeout')))
        b.session.active = True
        b.submit_text('固定公开测试指令')
        job = b.response.queue.get_nowait()
        admitted, release = threading.Event(), threading.Event()
        errors = []
        original = b.valid
        armed = [True]

        def valid(generation):
            result = original(generation)
            if (
                armed[0]
                and result
                and generation == job.generation
                and not b.response.busy
                and b.brain.calls > 0
            ):
                armed[0] = False
                admitted.set()
                if not release.wait(2):
                    raise RuntimeError('publication barrier')
            return result

        b.valid = valid

        def worker():
            try:
                self.work(b, [job])
            except BaseException as error:
                errors.append(type(error).__name__)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        owner_done = threading.Event()

        def owner():
            try:
                b.submit_text('新的固定公开指令')
                owner_done.set()
            except BaseException as error:
                errors.append(type(error).__name__)

        next_owner = threading.Thread(target=owner, daemon=True)
        try:
            self.assertTrue(admitted.wait(1))
            next_owner.start()
            # The owner must wait while the admitted failure is being published.
            self.assertFalse(owner_done.wait(0.1))
        finally:
            release.set()
            thread.join(2)
            if next_owner.ident is not None:
                next_owner.join(2)
        self.assertEqual(errors, [])
        self.assertTrue(owner_done.is_set())
        new_begin = max(i for i, (event, _) in enumerate(b.events) if event == 'text_begin')
        self.assertFalse(
            any(
                event == 'status' and data.get('phase') == 'error'
                for event, data in b.events[new_begin + 1 :]
            )
        )
        self.assertEqual(b.session.text_generation, b.generation)

    def test_replaced_answer_does_not_create_a_task_or_publish_thinking(self):
        brain = Brain()
        b = self.bridge(brain)
        generation = b.submit_text('旧的固定公开测试指令')['generation']
        job = b.response.queue.get_nowait()
        admitted, release = threading.Event(), threading.Event()
        errors = []
        original = b.valid

        def valid(candidate):
            result = original(candidate)
            if result and candidate == generation and not admitted.is_set():
                admitted.set()
                if not release.wait(2):
                    raise RuntimeError('response admission barrier')
            return result

        b.valid = valid

        def worker():
            try:
                self.work(b, [job])
            except BaseException as error:
                errors.append(type(error).__name__)

        thread = threading.Thread(target=worker, daemon=True)
        thread.start()
        try:
            self.assertTrue(admitted.wait(1))
            successor = b.submit_text('新的固定公开测试指令')['generation']
        finally:
            release.set()
            thread.join(2)
        self.assertEqual(errors, [])
        self.assertFalse(thread.is_alive())
        self.assertFalse(b.workspace.tasks)
        self.assertFalse(any(name == 'status' for name, _ in b.events))
        self.assertEqual(brain.calls, 0)
        self.assertEqual(b.session.text_generation, successor)

    def test_answer_status_cannot_overtake_a_new_owner(self):
        b = self.bridge(Brain())
        b.speak = lambda *args: True
        generation = b.submit_text('固定公开测试指令')['generation']
        job = b.response.queue.get_nowait()
        admitted, release, owner_done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        original = b.valid

        def valid(candidate):
            result = original(candidate)
            if (
                result
                and candidate == generation
                and b.response.busy
                and not admitted.is_set()
                and b.workspace.tasks
                and b.workspace.tasks[-1]['status'] == 'completed'
            ):
                admitted.set()
                if not release.wait(2):
                    raise RuntimeError('answer completion barrier')
            return result

        b.valid = valid

        def worker():
            try:
                self.work(b, [job])
            except BaseException as error:
                errors.append(type(error).__name__)

        def owner():
            try:
                b.submit_text('新的固定公开指令')
                owner_done.set()
            except BaseException as error:
                errors.append(type(error).__name__)

        first, second = (
            threading.Thread(target=worker, daemon=True),
            threading.Thread(target=owner, daemon=True),
        )
        first.start()
        try:
            self.assertTrue(admitted.wait(1))
            second.start()
            owner_ran_early = owner_done.wait(0.1)
        finally:
            release.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)
        self.assertEqual(errors, [])
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertTrue(owner_done.is_set())
        new_begin = max(i for i, (event, _) in enumerate(b.events) if event == 'text_begin')
        answer_done = next(
            i
            for i, (event, fields) in enumerate(b.events)
            if event == 'status' and fields.get('message') == '回答已完成 · 麦克风关闭'
        )
        self.assertLess(answer_done, new_begin)
        self.assertFalse(owner_ran_early)
        self.assertEqual(b.workspace.tasks[-1]['status'], 'completed')

    def test_text_completion_cannot_overtake_a_new_owner(self):
        b = self.bridge(Brain())
        old = b.submit_text('固定公开测试指令')['generation']
        entered, release, owner_done = threading.Event(), threading.Event(), threading.Event()
        errors = []
        output = b.event

        def event(name, **fields):
            if name == 'text_done':
                entered.set()
                if not release.wait(2):
                    raise RuntimeError('text completion barrier')
            output(name, **fields)

        b.event = event

        def finish():
            try:
                b.finish_text_response(old)
            except BaseException as error:
                errors.append(type(error).__name__)

        def owner():
            try:
                b.submit_text('新的固定公开指令')
                owner_done.set()
            except BaseException as error:
                errors.append(type(error).__name__)

        first, second = (
            threading.Thread(target=finish, daemon=True),
            threading.Thread(target=owner, daemon=True),
        )
        first.start()
        try:
            self.assertTrue(entered.wait(1))
            second.start()
            self.assertFalse(owner_done.wait(0.1))
        finally:
            release.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)
        self.assertEqual(errors, [])
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertTrue(owner_done.is_set())
        self.assertEqual(b.session.text_generation, b.generation)
        self.assertEqual(
            [n for n, _ in b.events if n in ('text_done', 'text_begin')],
            ['text_begin', 'text_done', 'text_begin'],
        )

    def test_cleanup_disconnect_does_not_kill_worker_or_retry_either_task(self):
        brain = Brain()
        b = self.bridge(brain)
        b.session.active = True
        b.speak = lambda *args: True
        generation = b.submit_text('固定公开测试指令')['generation']
        job = b.response.queue.get_nowait()
        finish = b.session.finish_text_response

        def disconnected(generation):
            finish(generation)
            raise ConnectionError(SECRET)

        b.session.finish_text_response = disconnected
        self.work(b, [job, Reply(generation, '第二条固定指令')])
        self.assertEqual(brain.calls, 2)
        self.assertFalse(b.response.busy)
        self.assertIsNone(b.session.text_generation)
        self.assertIn(('text_finish_error', {'error_type': 'ConnectionError'}), b.logs)
        self.assertNotIn(SECRET, str((b.events, b.logs)))

    def test_asr_and_announcement_error_publication_cannot_cross_new_owner(self):
        for mode in ('asr', 'announcement'):
            with self.subTest(mode=mode):
                b = self.bridge(Brain())
                failed, entered, release, owner_done = (
                    threading.Event(),
                    threading.Event(),
                    threading.Event(),
                    threading.Event(),
                )
                errors = []
                if mode == 'asr':
                    b.session.active = True
                    b.session.awake = True

                    class Speech:
                        def recognize(self, pcm):
                            failed.set()
                            raise TimeoutError(SECRET)

                    b.speech = Speech()
                    worker = b.recognition
                    trace = b.diagnostics.begin('voice')
                    jobs = [
                        (b.generation, np.ones(320, dtype=np.float32), False, [], None, None, trace)
                    ]
                else:

                    def speak(*args):
                        failed.set()
                        raise TimeoutError(SECRET)

                    b.speak = speak
                    b.announce('固定公开播报')
                    worker = b.response
                    jobs = [worker.queue.get_nowait()]
                valid = b.valid
                armed = [True]

                def check(generation):
                    value = valid(generation)
                    if value and failed.is_set() and armed[0]:
                        armed[0] = False
                        entered.set()
                        if not release.wait(2):
                            raise RuntimeError('failure admission barrier')
                    return value

                b.valid = check

                def work():
                    try:
                        with patch.object(worker.queue, 'get', side_effect=[*jobs, SystemExit]):
                            with self.assertRaises(SystemExit):
                                worker.work()
                    except BaseException as error:
                        errors.append(type(error).__name__)

                def owner():
                    try:
                        b.submit_text('新的固定公开指令')
                        owner_done.set()
                    except BaseException as error:
                        errors.append(type(error).__name__)

                first, second = (
                    threading.Thread(target=work, daemon=True),
                    threading.Thread(target=owner, daemon=True),
                )
                first.start()
                try:
                    self.assertTrue(entered.wait(1))
                    second.start()
                    self.assertFalse(owner_done.wait(0.1))
                finally:
                    release.set()
                    first.join(2)
                    if second.ident is not None:
                        second.join(2)
                self.assertEqual(errors, [])
                self.assertTrue(owner_done.is_set())
                self.assertFalse(first.is_alive() or second.is_alive())
                begin = max(i for i, (event, _) in enumerate(b.events) if event == 'text_begin')
                self.assertFalse(
                    any(
                        event == 'status' and fields.get('phase') == 'error'
                        for event, fields in b.events[begin + 1 :]
                    )
                )
                self.assertNotIn(SECRET, str((b.events, b.logs)))


def branch(source, event):
    signature = 'if ("' + event + '".equals(event)) {'
    if source.count(signature) != 1:
        raise AssertionError('Event branch is no longer unique')
    start = source.index(signature) + len(signature)
    end, depth = start, 1
    while depth:
        depth += (source[end] == '{') - (source[end] == '}')
        end += 1
    return source[start : end - 1]


class DeviceFeedbackTests(unittest.TestCase):
    def compile_and_run(self, body):
        source = (JAVA / 'VoiceSession.java').read_text()
        events = (
            'hello',
            'status',
            'text_begin',
            'text_done',
            'announcement_begin',
            'announcement_done',
            'cancel',
            'vision_done',
        )
        # Compile the real event branch bodies, not a Python reimplementation.
        # Minimal interfaces exclude Android I/O; actual device rendering remains
        # a separate deployment check.
        methods = '\n'.join(
            'void '
            + event
            + '(int gen, String next, String message){Packet p=new Packet(gen);Data j=new Data(next,message);j.startFailure=suppliedStartFailure;j.provenance=suppliedResultProvenance;j.memory=suppliedPreferenceMemory;j.modes=suppliedVisionModes;j.photos=suppliedPhotoFollowup;j.listening=suppliedListeningState;j.captureId=suppliedCaptureId!=null?suppliedCaptureId:captureWindow==null?null:captureWindow.id;j.epoch=suppliedCaptureEpoch!=null?suppliedCaptureEpoch:Integer.valueOf(epoch);'
            + branch(source, event)
            + '}'
            for event in events
        )
        methods += '\n' + method(source, 'boolean currentStatusPresentation(')
        helper = JAVA / 'StatusFeedback.java'
        field = (
            'final StatusFeedback statusFeedback=new StatusFeedback();' if helper.exists() else ''
        )
        harness = (
            """package com.happyhome.cc8assistant;
class FeedbackCheck {
 boolean foreground=true,recording=false,announcement=false,visual=false,textSession=false;
 int minimumGeneration=0,textGeneration=0,announcementGeneration=0,visualGeneration=0,epoch=1,lifecycle=1;
 final Object routeLock=new Object();long route=5, activeRoute=5,routeEpoch=5;
 static class CaptureWindow{static int count;final String id="capture"+(++count);boolean startConfirmed=true;}
 CaptureWindow captureWindow=new CaptureWindow();
 boolean dotChatSupported,savedTasksSupported,preferenceMemorySupported,resultProvenanceSupported,resultReadingSupported,visionModesSupported,photoFollowupSupported;
 final PhotoFollowupWindow photoFollowup=new PhotoFollowupWindow();
 String pendingPhotoId="",pendingPhotoForgetId="";
 // The transport handshake itself is covered by the capture route tests.
 static class Link {}
 final Link source=new Link();
 boolean captureHello(Link link,Data hello){return true;}
 // The typed settings receipt is exercised by WorkspaceRouteTests.
 void acceptVoicePreferences(Object state){}
 void clearSpeechCorrection(long route){}
 String recordingPhase(){return recording?"listening":"idle";}
 boolean currentRoute(long wanted){return activeRoute==wanted;}
 String phase="idle";
 Object suppliedListeningState="unknown";
 Object suppliedCaptureId,suppliedCaptureEpoch;
 Object suppliedStartFailure;
 Object suppliedResultProvenance,suppliedPreferenceMemory,suppliedVisionModes,suppliedPhotoFollowup;
 String pendingVisionNonce="",desktopVisionNonce="";
 static class Packet{int generation;Packet(int g){generation=g;}}
 static class Data{String next,message;Object listening="unknown",captureId,epoch,startFailure,provenance,memory,modes,photos;Data(String n,String m){next=n;message=m;}
  Object optJSONObject(String key){return null;}
  String optString(String k){return "phase".equals(k)?next:"listening_state".equals(k)?String.valueOf(listening):message;}
  String optString(String k,String fallback){return optString(k);}
  Object opt(String k){
   if("supports_preference_memory".equals(k))return memory;
   if("supports_vision_modes".equals(k))return modes;
   if("supports_photo_followup".equals(k))return photos;
   if("supports_result_provenance".equals(k)||"supports_result_reading".equals(k))return provenance;
   if("request_start_failed".equals(k))return startFailure;
   if("listening_state".equals(k))return listening;
   if("capture_id".equals(k))return captureId;
   if("epoch".equals(k))return epoch;
   if(!"playback_interrupted_by_transport".equals(k))return null;
   return "notice".equals(next)?Boolean.TRUE:"string-notice".equals(next)?"true":null;
  }}
 class Listener{String phase="idle",message="";boolean playing;int visionCalls;boolean preserveVisionError;
  final ListeningState listeningState=new ListeningState();
  void state(String p,String m){listeningState.localPhase(p);phase=p;message=m;}
  void status(StatusPresentation update){
   if(!currentStatusPresentation(update))return;
   if(listeningState.hostStatus(update.phase,update.listeningState,foreground&&recording,update.generation))state(update.phase,update.message);
  }
  void playback(boolean active){playing=active;}
  void vision(boolean active,String message){vision(active,message,false);}
  void vision(boolean active,String message,boolean preserve){visionCalls++;preserveVisionError=preserve;}
  void recognition(String engine){} void heard(String text){} void caption(String text,String mood){} void announcement(){}}
 final Listener listener=new Listener();
 static class Output {void endTask(int generation){}void endTasksThrough(int generation){}}
 final Output speakerOwner=new Output();
 void beginTaskOutput(String role,int generation){}
 void cancelPlaybackForCommand(int generation,long route){}
 static void yes(boolean value,String message){if(!value)throw new AssertionError(message);}
"""
            + field
            + methods
            + '\n public static void main(String[] args){FeedbackCheck c=new FeedbackCheck();'
            + body
            + '}}'
        )
        java = java_bin()
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary)
            test = folder / 'FeedbackCheck.java'
            test.write_text(harness)
            command = [str(java / 'javac'), '-encoding', 'UTF-8', '-d', str(folder), str(test)]
            if helper.exists():
                command.append(str(helper))
            command.append(str(JAVA / 'ListeningState.java'))
            command.append(str(JAVA / 'StatusPresentation.java'))
            command.append(str(JAVA / 'PhotoFollowupWindow.java'))
            compiled = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run(
                [
                    str(java / 'java'),
                    '-cp',
                    str(folder),
                    'com.happyhome.cc8assistant.FeedbackCheck',
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_explicit_waiting_and_awake_status_reach_the_real_presentation_helper(self):
        self.compile_and_run("""c.recording=true;c.suppliedListeningState="waiting";
 c.status(8,"listening","translated waiting detail");
 yes(c.listener.listeningState.label().startsWith("等待唤醒"),"waiting label hidden");
 c.suppliedListeningState="awake";c.status(9,"listening","translated awake detail");
 yes(c.listener.listeningState.label().equals("正在倾听 · 可以直接说"),"awake label hidden");
 c.suppliedListeningState="waiting";c.status(8,"listening","old waiting");
 yes(c.listener.listeningState.state().equals("awake"),"stale status changed wake label");""")

    def test_local_output_preserves_state_but_new_capture_drops_old_ownership(self):
        self.compile_and_run("""c.recording=true;c.suppliedListeningState="waiting";
 c.status(8,"listening","waiting");c.listener.state("speaking","output");
 c.listener.state("listening","local completion");
 yes(c.listener.listeningState.state().equals("waiting"),"output guessed wake state");
 c.listener.state("idle","stopped");
 yes(c.listener.listeningState.state().equals("unknown"),"stop retained old capture");
 c.suppliedListeningState="awake";c.status(9,"listening","awake");
 c.listener.state("calibrating","new capture");
 yes(c.listener.listeningState.state().equals("unknown"),"new capture retained old state");
 c.suppliedListeningState="waiting";c.foreground=false;c.status(10,"listening","background");
 yes(c.listener.listeningState.state().equals("unknown"),"background published wake state");""")

    def test_missing_or_invalid_host_state_remains_legacy_unknown(self):
        self.compile_and_run("""c.recording=true;c.suppliedListeningState="waiting";
 c.status(8,"listening","waiting");c.suppliedListeningState=null;
 c.status(9,"listening","legacy");
 yes(c.listener.listeningState.state().equals("unknown"),"legacy status borrowed old state");
 c.suppliedListeningState="unexpected";c.status(10,"listening","invalid");
 yes(c.listener.listeningState.label().equals("正在倾听 · 大黄蜂 / Bumblebee"),"invalid state invented authorization");""")

    def test_wrong_metadata_types_cannot_publish_a_listening_state(self):
        self.compile_and_run("""c.recording=true;c.suppliedListeningState="waiting";
 c.status(8,"listening","waiting");c.suppliedListeningState=Boolean.TRUE;
 c.status(9,"listening","wrong type");
 yes(c.listener.listeningState.state().equals("waiting"),"nonstring state changed label");
 c.suppliedListeningState="awake";c.suppliedCaptureId=Boolean.TRUE;c.status(9,"listening","wrong owner type");
 yes(c.listener.listeningState.state().equals("waiting"),"nonstring capture owner changed label");
 c.suppliedCaptureId=null;c.suppliedCaptureEpoch="1";c.status(9,"listening","wrong epoch type");
 yes(c.listener.listeningState.state().equals("waiting"),"string epoch changed label");""")

    def test_queued_listening_callback_cannot_reappear_after_stop_or_background(self):
        self.compile_and_run("""c.recording=true;c.suppliedListeningState="waiting";
 c.status(8,"listening","waiting");
 StatusPresentation queued=new StatusPresentation("listening","queued","awake",8,c.epoch,c.lifecycle,c.route,c.captureWindow);
 c.listener.state("idle","stopped");
 c.recording=false;c.listener.status(queued);
 yes(c.listener.phase.equals("idle")&&c.listener.listeningState.state().equals("unknown"),"queued callback revived stopped capture label");
 c.recording=true;c.foreground=false;c.listener.status(queued);
 yes(c.listener.phase.equals("idle")&&c.listener.listeningState.state().equals("unknown"),"queued callback revived background label");
 c.foreground=true;c.recording=false;c.announcement_begin(9,"","");c.status(9,"error","announcement error without microphone");
 yes(c.listener.phase.equals("error"),"capture guard hid unrelated output error");""")

    def test_queued_callback_cannot_borrow_new_capture_route_or_lifetime(self):
        self.compile_and_run("""c.recording=true;c.suppliedListeningState="waiting";
 c.status(8,"listening","waiting");
 StatusPresentation old=new StatusPresentation("listening","old","awake",8,c.epoch,c.lifecycle,c.route,c.captureWindow);
 c.listener.state("calibrating","new capture");CaptureWindow before=c.captureWindow;
 c.captureWindow=new CaptureWindow();c.epoch++;c.listener.status(old);
 yes(c.listener.phase.equals("calibrating")&&c.listener.listeningState.state().equals("unknown"),"old capture borrowed new recording");
 c.captureWindow=before;c.epoch--;c.routeEpoch++;c.listener.status(old);
 yes(c.listener.phase.equals("calibrating"),"old route overwrote current UI");
 c.routeEpoch--;c.lifecycle++;c.listener.status(old);
 yes(c.listener.phase.equals("calibrating"),"old activity lifetime overwrote current UI");
 c.lifecycle--;c.minimumGeneration=9;c.listener.status(old);
 yes(c.listener.phase.equals("calibrating"),"old generation overwrote current UI");""")

    def test_new_request_cannot_inherit_the_previous_request_waiting_label(self):
        self.compile_and_run("""c.recording=true;c.suppliedListeningState="waiting";
 c.status(8,"listening","waiting");c.status(9,"thinking","new request");
 c.listener.state("listening","local output completed");
 yes(c.listener.listeningState.state().equals("unknown"),"new turn borrowed old waiting label");
 c.suppliedListeningState="awake";c.status(9,"listening","confirmed");
 yes(c.listener.listeningState.state().equals("awake"),"current turn could not publish state");""")

    def test_delayed_wire_status_cannot_bind_to_a_new_capture_or_unconfirmed_start(self):
        self.compile_and_run("""c.recording=true;c.suppliedListeningState="waiting";
 c.status(8,"listening","current");String old=c.captureWindow.id;
 c.captureWindow=new CaptureWindow();c.epoch++;c.listener.state("calibrating","new capture");
 c.suppliedListeningState="awake";c.suppliedCaptureId=old;c.status(9,"listening","old wire capture");
 yes(c.listener.phase.equals("calibrating"),"old wire capture borrowed new window");
 c.suppliedCaptureId=null;c.suppliedCaptureEpoch=Integer.valueOf(c.epoch-1);c.status(9,"listening","old epoch");
 yes(c.listener.phase.equals("calibrating"),"old wire epoch changed UI");
 c.suppliedCaptureEpoch=null;c.captureWindow.startConfirmed=false;c.status(9,"listening","not confirmed");
 yes(c.listener.phase.equals("calibrating"),"pending native start advertised listening");
 c.captureWindow.startConfirmed=true;c.status(8,"listening","fresh receipt");
 yes(c.listener.listeningState.state().equals("awake"),"rejected old metadata consumed rightful status");""")

    def test_provenance_capability_requires_current_foreground_generation(self):
        self.compile_and_run("""c.minimumGeneration=9;c.suppliedResultProvenance=Boolean.TRUE;
 c.hello(8,"","late hello");
 yes(!c.resultProvenanceSupported&&!c.resultReadingSupported,"old generation enabled file control");
 c.hello(9,"","current hello");
 yes(c.resultProvenanceSupported&&c.resultReadingSupported,"current capability unavailable");
 c.suppliedResultProvenance=Boolean.FALSE;c.hello(8,"","late downgrade");
 yes(c.resultProvenanceSupported&&c.resultReadingSupported,"old generation disabled current capability");
 c.hello(9,"","current downgrade");
 yes(!c.resultProvenanceSupported&&!c.resultReadingSupported,"current route cannot disable capability");
 c.foreground=false;c.suppliedResultProvenance=Boolean.TRUE;c.hello(9,"","background");
 yes(!c.resultProvenanceSupported&&!c.resultReadingSupported,"background enabled file control");
 c.foreground=true;c.activeRoute=6;c.hello(9,"","stale route");
 yes(!c.resultProvenanceSupported&&!c.resultReadingSupported,"old route enabled file control");""")

    def test_preference_capability_requires_current_generation_route_and_boolean(self):
        self.compile_and_run("""c.suppliedPreferenceMemory="true";c.hello(8,"","string");
 yes(!c.preferenceMemorySupported,"string advertised memory capability");
 c.suppliedPreferenceMemory=Integer.valueOf(1);c.hello(8,"","number");
 yes(!c.preferenceMemorySupported,"number advertised memory capability");
 c.suppliedPreferenceMemory=Boolean.TRUE;c.hello(8,"","current");
 yes(c.preferenceMemorySupported,"current capability missing");
 c.minimumGeneration=9;c.suppliedPreferenceMemory=Boolean.FALSE;c.hello(8,"","old");
 yes(c.preferenceMemorySupported,"old generation disabled current memory capability");
 c.hello(9,"","current off");yes(!c.preferenceMemorySupported,"current disable ignored");
 c.foreground=false;c.suppliedPreferenceMemory=Boolean.TRUE;c.hello(9,"","background");
 yes(!c.preferenceMemorySupported,"background enabled memory capability");
 c.foreground=true;c.activeRoute=6;c.hello(9,"","retired route");
 yes(!c.preferenceMemorySupported&&!c.recording,"retired route enabled memory or capture");""")

    def test_ocr_capability_keeps_typed_current_generation_and_route(self):
        self.compile_and_run("""c.suppliedVisionModes="true";c.hello(8,"","bad");
 yes(!c.visionModesSupported,"string enabled OCR");
 c.suppliedVisionModes=Boolean.TRUE;c.hello(8,"","current");yes(c.visionModesSupported,"current OCR capability missing");
 c.minimumGeneration=9;c.suppliedVisionModes=Boolean.FALSE;c.hello(8,"","old");yes(c.visionModesSupported,"old generation changed OCR");
 c.hello(9,"","off");yes(!c.visionModesSupported,"current OCR off ignored");
 c.foreground=false;c.suppliedVisionModes=Boolean.TRUE;c.hello(9,"","background");yes(!c.visionModesSupported,"background enabled OCR");
 c.foreground=true;c.activeRoute=6;c.hello(9,"","old route");yes(!c.visionModesSupported&&!c.recording,"old route enabled OCR/capture");""")

    def test_photo_capability_cannot_be_revived_by_old_or_background_hello(self):
        self.compile_and_run("""c.suppliedPhotoFollowup="true";c.hello(8,"","bad");
 yes(!c.photoFollowupSupported,"string enabled photo follow-up");
 c.suppliedPhotoFollowup=Boolean.TRUE;c.hello(8,"","on");yes(c.photoFollowupSupported,"typed capability missing");
 c.minimumGeneration=9;c.suppliedPhotoFollowup=Boolean.FALSE;c.hello(8,"","old");yes(c.photoFollowupSupported,"old hello changed capability");
 c.hello(9,"","off");yes(!c.photoFollowupSupported,"current revoke ignored");
 c.foreground=false;c.suppliedPhotoFollowup=Boolean.TRUE;c.hello(9,"","background");yes(!c.photoFollowupSupported,"background enabled photo follow-up");
 c.foreground=true;c.activeRoute=6;c.hello(9,"","old route");yes(!c.photoFollowupSupported&&!c.recording,"old route enabled photo or microphone");""")

    def test_cancel_revokes_picture_but_preserves_the_inflight_explicit_request_nonce(self):
        self.compile_and_run("""String id="a".repeat(32);c.pendingPhotoId=id;c.pendingVisionNonce="b".repeat(32);
 c.pendingPhotoForgetId="c".repeat(32);yes(c.photoFollowup.offer(id,8,640,480,120000,5,0),"fixture offer failed");
 c.cancel(9,"","cancel before begin");
 yes(c.photoFollowup.current(5,8,1)==null&&c.pendingPhotoForgetId.isEmpty(),"cancel kept photo or stale delete receipt");
 yes(c.pendingPhotoId.equals(id)&&!c.pendingVisionNonce.isEmpty(),"host cancellation successor lost explicit photo request");""")

    def test_route_interruption_notice_survives_rebind_and_clears_for_new_work(self):
        self.compile_and_run("""c.recording=true;c.hello(8,"notice","连接切换中断播报");
 c.status(8,"listening","连接已切换，我在听");c.hello(8,"","duplicate handshake");
 yes(c.listener.phase.equals("error")&&c.listener.message.equals("连接切换中断播报"),
     "ordinary reconnect erased interruption");
 c.text_begin(9,"","");c.status(9,"thinking","new work");c.hello(8,"notice","late");
 yes(c.listener.phase.equals("thinking"),"stale interruption overwrote new work");
 yes(c.recording&&c.listener.playing,"interruption prevented a new request");""")

    def test_interruption_uses_only_current_foreground_route_and_boolean_flag(self):
        self.compile_and_run("""c.activeRoute=6;c.routeEpoch=6;c.hello(8,"notice","stale route");
 yes(!c.listener.phase.equals("error"),"stale route reported interruption");
 c.route=6;c.foreground=false;c.hello(8,"notice","background");
 yes(!c.listener.phase.equals("error"),"background notice changed UI");
 c.foreground=true;c.hello(8,"string-notice","malformed");
 yes(!c.listener.phase.equals("error"),"string spoofed interruption");
 c.hello(8,"notice","fixed interruption");
 yes(c.listener.phase.equals("error")&&!c.recording,"notice rearmed mic or was hidden");
 c.text_begin(9,"","");c.status(9,"thinking","next");
 yes(c.listener.phase.equals("thinking")&&!c.recording,"new task did not replace notice");""")

    def test_text_cleanup_keeps_error_and_releases_playback_ui(self):
        self.compile_and_run("""c.text_begin(8,"","");c.status(8,"error","固定错误与恢复指引");
 c.text_done(8,"","");yes(c.listener.phase.equals("error"),"text_done overwrote error");
 yes(c.listener.message.equals("固定错误与恢复指引"),"guidance was lost");
 yes(!c.textSession&&!c.listener.playing,"text cleanup stuck");
 c.text_begin(9,"","");c.status(9,"thinking","new work");c.text_done(9,"","");
 yes(c.listener.phase.equals("idle"),"new successful task kept old failure");""")

    def test_real_cancel_then_begin_sequence_accepts_one_request_in_that_generation(self):
        self.compile_and_run("""c.cancel(8,"","");c.text_begin(8,"","");
 yes(c.textSession&&c.listener.playing,"cancel-before-text_begin rejected legitimate owner");
 c.status(8,"error","fixed error");c.text_done(8,"","");
 c.text_begin(8,"","");yes(!c.textSession&&!c.listener.playing,"duplicate reopened failed request");
 c.cancel(9,"","");c.announcement_begin(9,"","");
 yes(c.announcement&&c.listener.playing,"cancel-before-announcement_begin rejected legitimate owner");
 c.status(9,"thinking","new");c.status(9,"speaking","first clause");
 c.status(9,"speaking","second clause");c.announcement_done(9,"","completed");
 yes(!c.announcement&&!c.listener.playing&&c.listener.phase.equals("idle"),"successful multi-clause cleanup failed");""")

    def test_announcement_cleanup_keeps_error_without_stuck_speaking(self):
        self.compile_and_run("""c.announcement_begin(8,"","");c.status(8,"error","固定合成失败指引");
 c.announcement_done(8,"","failed");yes(c.listener.phase.equals("error"),"announcement_done overwrote error");
 yes(!c.announcement&&!c.listener.playing,"announcement cleanup stuck");""")

    def test_late_or_background_status_cannot_replace_current_state(self):
        self.compile_and_run("""c.recording=true;c.minimumGeneration=8;c.status(9,"thinking","new");
 c.status(7,"error","obsolete");yes(c.listener.phase.equals("thinking"),"status below cancel floor accepted");
 c.status(8,"error","obsolete");yes(c.listener.phase.equals("thinking"),"status older than newest accepted");
 c.foreground=false;c.status(10,"error","background");
 yes(c.listener.phase.equals("thinking"),"background status accepted");""")

    def test_status_requires_the_exact_nonrecording_request_generation(self):
        self.compile_and_run("""c.text_begin(8,"","");c.status(8,"thinking","current");
 c.status(9,"error","unknown future request");
 yes(c.listener.phase.equals("thinking"),"status from unrelated request accepted");""")

    def test_playback_failure_error_survives_cancel_when_microphone_is_off(self):
        self.compile_and_run("""c.text_begin(8,"","");c.status(8,"speaking","old");
 c.cancel(9,"","");c.status(9,"error","固定播放失败指引");
 yes(c.listener.phase.equals("error"),"post-cancel playback error was discarded");
 yes(!c.textSession&&!c.listener.playing,"cancel did not release controls");
 c.cancel(9,"","");yes(c.listener.phase.equals("error"),"duplicate cancel hid error");
 c.text_begin(10,"","");c.status(10,"thinking","new");c.status(9,"error","obsolete");
 yes(c.listener.phase.equals("thinking"),"old cancelled error replaced new request");""")

    def test_cancel_without_owned_work_cannot_admit_an_unsolicited_error(self):
        self.compile_and_run("""c.cancel(9,"","");c.status(9,"error","unsolicited");
 yes(!c.listener.phase.equals("error"),"unowned error accepted");
 c.text_begin(10,"","");c.status(10,"thinking","new");c.cancel(8,"","");
 yes(c.textSession&&c.listener.playing,"stale cancel stopped current task");""")

    def test_previous_completion_cannot_clear_newer_text_or_announcement_controls(self):
        self.compile_and_run("""c.announcement_begin(8,"","");c.status(8,"speaking","old");
 c.text_begin(9,"","");c.status(9,"thinking","new");c.announcement_done(8,"","completed");
 yes(c.listener.phase.equals("thinking")&&c.listener.playing,"old announcement completed over new text");
 c.announcement_begin(10,"","");c.status(10,"thinking","new announcement");c.text_done(9,"","");
 yes(c.listener.phase.equals("thinking")&&c.listener.playing,"old text completed over new announcement");""")

    def test_vision_cleanup_is_current_and_cannot_close_a_newer_owner(self):
        self.compile_and_run("""c.text_begin(8,"","");c.textSession=false;
 c.visual=true;c.visualGeneration=8;c.status(8,"error","capture failed");
 c.vision_done(8,"","failed");yes(!c.visual&&c.listener.visionCalls==1,"current vision was not released");
 yes(c.listener.preserveVisionError,"current error was not preserved by completion");
 c.text_begin(9,"","");c.status(9,"thinking","new");
 c.visual=true;c.visualGeneration=8;c.vision_done(8,"","failed");
 yes(c.listener.visionCalls==1&&c.listener.playing,"old vision cleanup reached newer owner");""")


if __name__ == '__main__':
    unittest.main()
