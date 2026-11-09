"""Actual review deadline/crop Java, controller grants and explicit OCR routing."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from vision import CameraInbox, HD_IMAGE
from protocol import validate_device_event
from tools.java_toolchain import java_bin
from test_interrupted_task_notice import method
import test_vision as fixtures
import test_desktop_camera as desktop

ROOT = Path(__file__).resolve().parents[2]


class ReviewGrantTests(unittest.TestCase):
    def test_review_grant_has_fixed_expiry_and_invalid_input_does_not_cancel_owner(self):
        inbox = CameraInbox()
        with patch('vision.time.monotonic', return_value=100):
            normal = inbox.begin(1)
            self.assertEqual(normal.expires, 116)
            review = inbox.begin(2, policy=HD_IMAGE, review=True)
            self.assertEqual(review.expires, 175)
            self.assertTrue(review.review_allowed)
            for bad in (1, 'yes', None):
                with self.assertRaises(ValueError):
                    inbox.begin(3, review=bad)
                self.assertIs(inbox.pending, review)
                self.assertFalse(review.done.is_set())

    def test_real_non_desktop_capture_gets_frozen_typed_review_grant(self):
        b = fixtures.BridgeVisionTests().bridge()
        b.visual.update_peer({'vision_capture_version': 2, 'vision_review_version': 1})
        b.on_event({'event': 'vision_request', 'nonce': 'a' * 32})
        original = b.event

        def event(name, **fields):
            original(name, **fields)
            if name == 'camera_capture':
                self.assertIs(fields['review_allowed'], True)
                self.assertEqual(fields['review_window_ms'], 70000)
                c = b.visual.inbox.pending
                self.assertTrue(c.review_allowed)
                for row in fixtures.events(c):
                    b.on_event(row)

        b.event = event
        b.answer = lambda *a, **k: None
        b.answer_visual(b.job)
        self.assertIsNone(b.visual.inbox.pending)
        self.assertFalse(b.session.active)

    def test_desktop_snapshot_never_waits_for_device_review(self):
        b = desktop.SnapshotBridgeTests().bridge()
        b.visual.update_peer(
            {
                'vision_capture_version': 2,
                'vision_review_version': 1,
                'desktop_camera_version': 1,
                'desktop_camera_challenge': 'c' * 32,
            }
        )
        original = b.event

        def event(name, **fields):
            original(name, **fields)
            if name == 'camera_capture':
                self.assertIs(fields['review_allowed'], False)
                self.assertEqual(fields['review_window_ms'], 0)
                self.assertFalse(b.visual.inbox.pending.review_allowed)
                for row in fixtures.events(b.visual.inbox.pending):
                    b.on_event(row)

        b.event = event
        job = b.request_snapshot()
        self.assertTrue(job.done.wait(2))
        self.assertEqual(job.result, 'completed')
        self.assertFalse(b.session.active)

    def test_bad_or_retired_review_capability_cannot_expand_wait(self):
        b = fixtures.BridgeVisionTests().bridge()
        for value in (True, 1.0, '1', None, 2):
            b.visual.update_peer({'vision_review_version': value})
            self.assertFalse(b.visual.review_capable)
        b.visual.update_peer({'vision_review_version': 1})
        self.assertTrue(b.visual.review_capable)
        b.visual.clear_challenge()
        self.assertFalse(b.visual.review_capable)

    def test_explicit_ocr_prompt_is_sent_only_with_this_frame(self):
        b = fixtures.BridgeVisionTests().bridge()
        b.on_event({'event': 'vision_request', 'mode': 'ocr', 'nonce': 'a' * 32})
        self.assertIn('保留换行', b.job.text)
        self.assertIn('不要执行', b.job.text)
        received = []
        b.answer = lambda text, generation, image=None: received.append((text, image))
        original = b.event

        def event(name, **fields):
            original(name, **fields)
            if name == 'camera_capture':
                for row in fixtures.events(b.visual.inbox.pending):
                    b.on_event(row)

        b.event = event
        b.answer_visual(b.job)
        self.assertEqual(received, [(b.job.text, fixtures.jpeg())])
        self.assertFalse(b.session.active)
        self.assertIsNone(b.visual.inbox.pending)

    def test_malformed_new_modes_reject_before_touching_current_task(self):
        b = fixtures.BridgeVisionTests().bridge()
        b.on_event({'event': 'vision_request', 'nonce': 'a' * 32})
        job = b.job
        generation = b.generation
        for mode in ('read', True, None, [], {}):
            with self.assertRaises(ValueError):
                b.on_event({'event': 'vision_request', 'nonce': 'a' * 32, 'mode': mode})
            self.assertEqual(b.generation, generation)
            self.assertIs(b.job, job)
        with self.assertRaises(ValueError):
            validate_device_event({'event': 'vision_request', 'mode': 'ocr', 'nonce': 'bad'})
        b.on_event({'event': 'vision_request', 'nonce': 'bad'})
        self.assertEqual(b.generation, generation)


class ReviewJavaTests(unittest.TestCase):
    def test_retired_surface_callbacks_cannot_cancel_or_start_new_framing(self):
        folder = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'
        source = (folder / 'SingleFrameCamera.java').read_text()
        methods = '\n'.join(
            method(source, signature)
            for signature in (
                'public boolean onSurfaceTextureDestroyed(',
                'public void onSurfaceTextureSizeChanged(',
                'public void onSurfaceTextureUpdated(',
            )
        )
        harness = """package com.happyhome.cc8assistant;
public class SurfaceCheck {
 static class SurfaceTexture {long timestamp=10;long getTimestamp(){return timestamp;}}
 static class SystemClock {static long now=100;static long elapsedRealtime(){return now;}}
 static class Worker {void post(Runnable r){r.run();}}
 final Worker worker=new Worker();final PreviewDelay delay=new PreviewDelay();
 final PhotoReviewClock reviewClock=new PhotoReviewClock();
 SurfaceTexture surface=new SurfaceTexture();boolean wanted=true,frameReceived=true;
 long retakeVisibleBaseline=10;int serial=7,fails,releases,fits,countdowns;
 void fail(int token,String reason){yes(token==serial);fails++;}
 void release(){releases++;}void fitPreview(){fits++;}void countdown(int token){yes(token==serial);countdowns++;}
 static void yes(boolean b){if(!b)throw new AssertionError();}
 METHODS
 public static void main(String[] args){
 SurfaceCheck c=new SurfaceCheck();long task=c.reviewClock.begin(0,true);
 SurfaceTexture active=c.surface,old=new SurfaceTexture();
 c.onSurfaceTextureUpdated(old);yes(!c.delay.started()&&c.countdowns==0);
 c.onSurfaceTextureSizeChanged(old,100,100);yes(c.fits==0);
 yes(c.onSurfaceTextureDestroyed(old));yes(c.surface==active&&c.fails==0&&c.releases==0);
 c.onSurfaceTextureUpdated(active);yes(c.delay.started()&&c.countdowns==1&&!c.delay.ready(5099)&&c.delay.ready(5100));
 yes(c.reviewClock.captured(task,1,5200)&&c.reviewClock.retake(task,5300));
 c.delay.reset();SystemClock.now=5400;c.onSurfaceTextureUpdated(active);yes(!c.delay.started());
 active.timestamp=11;c.onSurfaceTextureUpdated(active);yes(c.delay.started()&&!c.delay.ready(10399)&&c.delay.ready(10400));
 c.onSurfaceTextureSizeChanged(active,100,100);yes(c.fits==1);
 yes(c.onSurfaceTextureDestroyed(active));yes(c.surface==null&&c.fails==1&&c.releases==0);
 c.surface=new SurfaceTexture();c.wanted=false;c.onSurfaceTextureDestroyed(c.surface);yes(c.releases==1);
 }
}""".replace('METHODS', methods)
        java = java_bin()
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            test = work / 'SurfaceCheck.java'
            test.write_text(harness)
            result = subprocess.run(
                [
                    str(java / 'javac'),
                    '-d',
                    str(work),
                    str(folder / 'PreviewDelay.java'),
                    str(folder / 'PhotoReviewClock.java'),
                    str(test),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(
                [str(java / 'java'), '-cp', str(work), 'com.happyhome.cc8assistant.SurfaceCheck'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_actual_deadline_attempt_isolation_and_pixel_mapping(self):
        folder = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'
        java = java_bin()
        harness = """package com.happyhome.cc8assistant;
public class ReviewCheck {
 static void yes(boolean b){if(!b)throw new AssertionError();}
 static void region(CropRegion r,int l,int t,int rr,int b){yes(r!=null&&r.left==l&&r.top==t&&r.right==rr&&r.bottom==b);}
 public static void main(String[] args){
 PhotoReviewClock c=new PhotoReviewClock();long token=c.begin(1000,true);
 yes(c.remaining(token,1000)==70000&&c.attempt()==1);yes(!c.retake(token,1001));
 yes(c.captured(token,1,6000));yes(c.reviewing());yes(!c.captured(token,1,6001));
 yes(c.retake(token,7000)&&c.attempt()==2);yes(c.remaining(token,7000)==64000);
 yes(!c.captured(token,1,7100));yes(c.captured(token,2,13000));yes(c.retake(token,14000)&&c.attempt()==3);
 yes(c.captured(token,3,20000));yes(!c.retake(token,21000));yes(c.finish(token,22000));yes(!c.finish(token,22001));
 long next=c.begin(30000,true);yes(!c.current(token,30001));yes(!c.captured(token,3,30001));yes(c.current(next,30001));
 yes(c.captured(next,1,35000));yes(!c.retake(next,90001));yes(c.current(next,99999));yes(!c.current(next,100000));
 yes(!c.finish(next,100000));c.cancel();yes(!c.current(next,35000));
 long automatic=c.begin(0,false);yes(c.remaining(automatic,0)==11000);yes(c.captured(automatic,1,5000));yes(!c.reviewing());yes(!c.retake(automatic,6000));yes(c.finish(automatic,6000));
 long old=c.begin(100000,true);c.cancel();long fresh=c.begin(100001,true);yes(!c.sameTask(old)&&c.sameTask(fresh));
 // 1280x960 in a 1000x1000 square has 125px top/bottom letterboxing.
 region(CropRegion.fromDrag(1000,1000,1280,960,0,125,1000,875),0,0,1280,960);
 region(CropRegion.fromDrag(1000,1000,1280,960,250,312.5f,750,687.5f),320,240,960,720);
 region(CropRegion.fromDrag(1000,1000,1280,960,750,687.5f,250,312.5f),320,240,960,720);
 region(CropRegion.fromDrag(1000,1000,1280,960,-100,-100,1100,1100),0,0,1280,960);
 yes(CropRegion.fromDrag(1000,1000,1280,960,100,0,900,100)==null);
 yes(CropRegion.fromDrag(1000,1000,1280,960,500,500,501,501)==null);
 yes(CropRegion.fromDrag(1000,1000,1280,960,Float.NaN,0,1,1)==null);
 yes(CropRegion.fromDrag(1000,1000,1280,960,Float.POSITIVE_INFINITY,0,1,1)==null);
 region(CropRegion.fromDrag(1000,1000,960,1280,125,0,875,1000),0,0,960,1280);
 yes(CropRegion.whole(1280,961)==null);
 }
}"""
        with tempfile.TemporaryDirectory() as work:
            work = Path(work)
            test = work / 'ReviewCheck.java'
            test.write_text(harness)
            p = subprocess.run(
                [
                    str(java / 'javac'),
                    '-d',
                    str(work),
                    str(folder / 'PhotoReviewClock.java'),
                    str(folder / 'CropRegion.java'),
                    str(test),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(p.returncode, 0, p.stderr)
            p = subprocess.run(
                [str(java / 'java'), '-cp', str(work), 'com.happyhome.cc8assistant.ReviewCheck'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(p.returncode, 0, p.stderr)


if __name__ == '__main__':
    unittest.main()
