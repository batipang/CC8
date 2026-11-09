"""Negotiated image bounds and original local IPC/Java selectors; no real camera or model."""

import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from vision import (
    CameraInbox,
    VisionError,
    LEGACY_IMAGE,
    HD_IMAGE,
    jpeg_dimensions,
    image_policy,
    DesktopSnapshot,
)
from camera import receive_photo, save_photo
from announce import AnnouncementServer
from dot_client import DotBrain
from tools.java_toolchain import java_bin
import test_vision as fixtures
import test_desktop_camera as desktop

ROOT = Path(__file__).resolve().parents[2]


def hd_events(capture):
    image = fixtures.jpeg(1280, 960)
    rows = list(fixtures.events(capture, image))
    rows[0].update(width=1280, height=960)
    return image, rows


class HDImageTests(unittest.TestCase):
    def test_only_negotiated_capture_accepts_hd_and_consumes_ram(self):
        inbox = CameraInbox()
        legacy = inbox.begin(1)
        _, rows = hd_events(legacy)
        self.assertTrue(inbox.feed(rows[0]))
        self.assertEqual(legacy.error, 'image_dimensions_exceeded')
        hd = inbox.begin(2, policy=HD_IMAGE)
        image, rows = hd_events(hd)
        for row in rows:
            self.assertTrue(inbox.feed(row))
        self.assertEqual(inbox.wait(hd, lambda: True), image)
        self.assertIsNone(inbox.pending)
        self.assertIsNone(hd.image)
        self.assertEqual(hd.data, bytearray())

    def test_exact_profile_bytes_dimensions_and_typed_version(self):
        image = fixtures.jpeg(1280, 960)
        with self.assertRaises(VisionError):
            jpeg_dimensions(image)
        self.assertEqual(jpeg_dimensions(image, policy=HD_IMAGE), (1280, 960))
        for width, height in [(1282, 960), (1280, 962), (0, 480), (640, 1920)]:
            with self.assertRaises(VisionError):
                jpeg_dimensions(fixtures.jpeg(width, height), policy=HD_IMAGE)
        image = fixtures.jpeg()[:-2] + b'0' * (LEGACY_IMAGE.max_bytes + 1) + b'\xff\xd9'
        with self.assertRaises(VisionError):
            jpeg_dimensions(image)
        self.assertEqual(jpeg_dimensions(image, policy=HD_IMAGE), (640, 480))
        for bad in [True, 2.0, '2', None, 3, 0]:
            with self.assertRaises(ValueError):
                image_policy(bad)

    def test_capture_policy_is_frozen_before_peer_downgrade_and_cancel(self):
        b = fixtures.BridgeVisionTests().bridge()
        b.visual.update_peer({'vision_capture_version': 2})
        old = b.visual.inbox.begin(1, policy=b.visual.image_policy)
        b.visual.update_peer({})
        self.assertIs(old.policy, HD_IMAGE)
        self.assertIs(b.visual.image_policy, LEGACY_IMAGE)
        fresh = b.visual.inbox.begin(2, policy=b.visual.image_policy)
        self.assertIs(fresh.policy, LEGACY_IMAGE)
        image, old_rows = hd_events(old)
        self.assertTrue(all(not b.visual.feed(row) for row in old_rows))
        self.assertFalse(fresh.done.is_set())
        b.visual.clear_challenge()
        self.assertIs(b.visual.image_policy, LEGACY_IMAGE)

    def test_malformed_capability_never_widens_limits(self):
        b = fixtures.BridgeVisionTests().bridge()
        for value in [True, 2.0, '2', None, 3]:
            b.visual.update_peer({'vision_capture_version': value})
            self.assertIs(b.visual.image_policy, LEGACY_IMAGE)

    def test_real_controller_announces_grant_and_forwards_only_current_frame(self):
        b = fixtures.BridgeVisionTests().bridge()
        b.visual.update_peer({'vision_capture_version': 2})
        b.on_event({'event': 'vision_request', 'nonce': 'a' * 32})
        received = []
        b.answer = lambda text, generation, image=None: received.append(image)
        original = b.event

        def event(name, **fields):
            original(name, **fields)
            if name == 'camera_capture':
                self.assertEqual({key: fields[key] for key in HD_IMAGE.fields()}, HD_IMAGE.fields())
                image, rows = hd_events(b.visual.inbox.pending)
                for row in rows:
                    b.on_event(row)

        b.event = event
        b.answer_visual(b.job)
        self.assertEqual(received, [fixtures.jpeg(1280, 960)])
        self.assertFalse(b.session.active)
        self.assertIsNone(b.visual.inbox.pending)

    def test_actual_dot_input_accepts_bounded_hd_with_no_file(self):
        rpc = fixtures.FakeRpc()
        brain = DotBrain(rpc)
        list(brain.clauses('看这张图片', [], image=fixtures.jpeg(1280, 960)))
        inputs = next(params['input'] for method, params in rpc.calls if method == 'turn/start')
        self.assertTrue(
            any(
                i.get('type') == 'image' and i['url'].startswith('data:image/jpeg;base64,')
                for i in inputs
            )
        )
        self.assertFalse(any('path' in i for i in inputs))

    def test_actual_private_socket_and_explicit_save_keep_hd_policy(self):
        class HDDesktop(desktop.IpcBridge):
            def request_snapshot(self):
                self.job = DesktopSnapshot(self.generation, 'f' * 32, policy=HD_IMAGE)
                self.job.image = fixtures.jpeg(1280, 960)
                self.job.result = 'completed'
                self.job.done.set()
                return self.job

        with tempfile.TemporaryDirectory() as folder:
            bridge = HDDesktop()
            server = AnnouncementServer(bridge, Path(folder) / 'camera.sock')
            server.start()
            try:
                result, image = receive_photo(server.path)
                self.assertEqual(
                    (result['width'], result['height'], result['capture_version']), (1280, 960, 2)
                )
                self.assertIsNone(bridge.job.image)
                with self.assertRaises(VisionError):
                    save_photo(image, Path(folder) / 'legacy')
                path = save_photo(image, Path(folder) / 'photos', capture_version=2)
                self.assertEqual(path.read_bytes(), image)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)
            finally:
                server.close()


class NativeImageBoundsTests(unittest.TestCase):
    def test_actual_java_selection_and_grant_parser(self):
        source = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant'
        java = java_bin()
        stub = """package org.json;import java.util.*;public class JSONObject {final Map<String,Object> m=new HashMap<>();public JSONObject(Object... p){for(int i=0;i<p.length;i+=2)m.put((String)p[i],p[i+1]);}public Object opt(String k){return m.get(k);}}"""
        harness = """package com.happyhome.cc8assistant;import org.json.JSONObject;import java.util.Arrays;
public class ImageCheck {
 static void yes(boolean b){if(!b)throw new AssertionError();}
 public static void main(String[] args){
 int[][] candidates={{1920,1080},{1280,960},{1280,720},{640,480},{320,240},{-2,480},{641,480}};
 yes(Arrays.equals(PreviewSizes.select(candidates,false,1280,1280*960),new int[]{640,480}));
 yes(Arrays.equals(PreviewSizes.select(candidates,true,1280,1280*960),new int[]{1280,960}));
 yes(Arrays.equals(PreviewSizes.select(candidates,true,640,640*480),new int[]{640,480}));
 yes(Arrays.equals(PreviewSizes.select(new int[][]{{320,240},{641,480}},true,1280,1280*960),new int[]{320,240}));
 yes(PreviewSizes.select(new int[][]{{Integer.MAX_VALUE,Integer.MAX_VALUE},{641,480},{1280,962}},true,1280,1280*960)==null);
 yes(PreviewSizes.select(new int[257][],true,1280,1280*960)==null);
 yes(CaptureProfile.fromRequest(new JSONObject()).version==1);
 CaptureProfile hd=CaptureProfile.fromRequest(new JSONObject("capture_version",2,"max_image_edge",1280,"max_image_pixels",1280*960,"max_image_bytes",768*1024));
 yes(hd.version==2&&hd.maxBytes==768*1024&&!hd.reviewAllowed);
 yes(hd.acceptsFrame(1280,960,1280*960*3/2));yes(hd.acceptsFrame(960,1280,1280*960*3/2));
 yes(!CaptureProfile.legacy().acceptsFrame(1280,960,1280*960*3/2));
 yes(!hd.acceptsFrame(1280,960,1280*960*3/2-1));yes(!hd.acceptsFrame(1280,960,1280*960*3/2+1));
 yes(!hd.acceptsFrame(641,480,641*480*3/2));yes(!hd.acceptsFrame(640,-480,-1));
 yes(!hd.acceptsFrame(Integer.MAX_VALUE,Integer.MAX_VALUE,1));
 CaptureProfile review=CaptureProfile.fromRequest(new JSONObject("capture_version",2,"max_image_edge",1280,"max_image_pixels",1280*960,"max_image_bytes",768*1024,"review_allowed",true,"review_window_ms",70000));
 yes(review.reviewAllowed&&review.version==2);
 for(Object[] grant:new Object[][]{{true,70001},{true,0},{false,70000},{"true",70000},{true,70000.0},{true,null},{null,70000}}){
  try{CaptureProfile.fromRequest(new JSONObject("capture_version",2,"max_image_edge",1280,"max_image_pixels",1280*960,"max_image_bytes",768*1024,"review_allowed",grant[0],"review_window_ms",grant[1]));throw new AssertionError();}catch(IllegalArgumentException expected){}
 }
 yes(!CaptureProfile.fromRequest(new JSONObject("review_allowed",true,"review_window_ms",70000)).reviewAllowed);
 for(Object version:new Object[]{true,2.0,"2",3}){
  try{CaptureProfile.fromRequest(new JSONObject("capture_version",version));throw new AssertionError();}catch(IllegalArgumentException expected){}
 }
 for(Object bytes:new Object[]{true,768*1024+1,"786432",null}){
  try{CaptureProfile.fromRequest(new JSONObject("capture_version",2,"max_image_edge",1280,"max_image_pixels",1280*960,"max_image_bytes",bytes));throw new AssertionError();}catch(IllegalArgumentException expected){}
 }
 }
}"""
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            (folder / 'JSONObject.java').write_text(stub)
            (folder / 'ImageCheck.java').write_text(harness)
            result = subprocess.run(
                [
                    str(java / 'javac'),
                    '-d',
                    str(folder),
                    str(source / 'PreviewSizes.java'),
                    str(source / 'CaptureProfile.java'),
                    str(folder / 'JSONObject.java'),
                    str(folder / 'ImageCheck.java'),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(
                [str(java / 'java'), '-cp', str(folder), 'com.happyhome.cc8assistant.ImageCheck'],
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
