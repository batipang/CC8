"""Compile the production Player controls against a deterministic AudioTrack."""

from pathlib import Path
import subprocess
import tempfile
import unittest

from tools.java_toolchain import require_java
from test_interrupted_task_notice import method

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'cc8-assistant/app/src/main/java/com/happyhome/cc8assistant/VoiceSession.java'


class DevicePauseReceiptTests(unittest.TestCase):
    def test_only_applied_audio_track_transitions_send_receipts(self):
        java = require_java()
        source = SOURCE.read_text()
        player = source.index('private final class Player')
        methods = source[
            source.index('    void cancel() {', player) : source.index(
                '    void waitIfPaused()', player
            )
        ]
        frame = method(source, 'static final class CaptionProgress')
        visual = '\n'.join(
            method(source, signature)
            for signature in (
                'CaptionProgress visualFrame()',
                'private CaptionProgress sampleFrame(AudioTrack t, boolean freeze)',
                'private float levelAt(long head)',
                'private static final class Envelope',
            )
        )
        timeline = SOURCE.with_name('CaptionTimeline.java').read_text().split('\n', 1)[1]
        program = (
            """
import java.util.*;
public class PauseFixture {
  static class JSONObject extends HashMap<String,Object> {}
  static class Wire {
    static JSONObject event(String name) {
      JSONObject value = new JSONObject();
      value.put("event", name);
      return value;
    }
    static void put(JSONObject value, String key, Object item) {
      value.put(key, item);
    }
  }
  static class AudioTrack {
    static final int PLAYSTATE_PLAYING = 3;
    boolean failPause, failPlay, playing;
    int pauses, plays, headReads, head;
    Runnable duringRead;
    int getPlayState() { return playing ? PLAYSTATE_PLAYING : 2; }
    int getPlaybackHeadPosition() {
      headReads++;
      int result = head;
      Runnable callback = duringRead;
      duringRead = null;
      if (callback != null) callback.run();
      return result;
    }
    void pause() {
      if (failPause) throw new IllegalStateException("fixture pause");
      playing = false;
      pauses++;
    }
    void play() {
      if (failPlay) throw new IllegalStateException("fixture resume");
      playing = true;
      plays++;
    }
    void flush() {}
  }
  static class Listener {
    long listeningNanos;
    String lastState;
    void state(String phase, String detail) {
      lastState = phase;
      if (phase.equals("listening")) listeningNanos = System.nanoTime();
    }
    void caption(String text, String mood) {}
  }
  static class Player {
    final Object routeLock = new Object();
    final Queue<Object> queue = new ArrayDeque<>();
    final List<JSONObject> events = new ArrayList<>();
    final Listener listener = new Listener();
    final String text = "fixture", mood = "neutral";
    final int generation = 2, playbackId = 3;
    final Queue<Envelope> envelopes = new ArrayDeque<>();
    boolean foreground = true, ended, stopped, paused;
    CaptionProgress frozenCaption;
    CaptionTimeline timings = new CaptionTimeline(new int[][]{{0, 7, 0, 700}}, 7);
    long written = 700;
    int pauseToken;
    long resumeCount;
    String phase;
    AudioTrack track;
    boolean allowed() { return foreground && !stopped; }
    void send(JSONObject event) { events.add(event); }
    void interrupt() {}
    boolean event(int index, String name, int token) {
      JSONObject value = events.get(index);
      return value.get("event").equals(name) && value.get("token").equals(token)
          && value.get("generation").equals(generation);
    }
"""
            + methods
            + visual
            + """
  }
"""
            + frame
            + """
  static void yes(boolean condition, String reason) {
    if (!condition) throw new AssertionError(reason);
  }
  public static void main(String[] args) {
    Player pending = new Player();
    pending.pauseForVoice(10);
    yes(pending.paused && pending.frozenCaption == null && pending.events.isEmpty(), "early receipt");
    AudioTrack ready = new AudioTrack();
    yes(pending.startTrack(ready), "track startup failed");
    yes(!ready.playing && pending.frozenCaption != null && pending.events.size() == 1, "pending pause");
    yes(pending.event(0, "playback_paused", 10), "wrong pause identity");
    yes((long)pending.events.get(0).get("nanos") <= pending.listener.listeningNanos, "late timestamp");
    pending.pauseForVoice(10);
    pending.resumeAfterNoise(9);
    yes(pending.events.size() == 1 && ready.pauses == 1, "duplicate or stale control");
    pending.resumeAfterNoise(10);
    yes(ready.playing && !pending.paused && pending.frozenCaption == null, "resume state");
    yes(pending.event(1, "playback_resumed", 10) && pending.resumeCount == 1, "resume receipt");

    Player withdrawn = new Player();
    withdrawn.pauseForVoice(11);
    withdrawn.resumeAfterNoise(11);
    AudioTrack unaffected = new AudioTrack();
    yes(withdrawn.startTrack(unaffected) && unaffected.playing && unaffected.pauses == 0,
        "withdrawn pause applied");
    yes(withdrawn.events.isEmpty() && withdrawn.resumeCount == 0, "withdrawn pause reported");

    Player failedPause = new Player();
    AudioTrack brokenPause = new AudioTrack();
    failedPause.startTrack(brokenPause);
    brokenPause.failPause = true;
    failedPause.pauseForVoice(12);
    yes(failedPause.stopped && failedPause.frozenCaption == null, "failed pause did not stop");
    yes(failedPause.events.size() == 1
        && failedPause.events.get(0).get("event").equals("playback_error"), "false pause receipt");
    failedPause.failPlayback(new IllegalStateException());
    yes(failedPause.events.size() == 1, "duplicate failure");

    Player failedResume = new Player();
    AudioTrack brokenResume = new AudioTrack();
    failedResume.startTrack(brokenResume);
    failedResume.pauseForVoice(13);
    brokenResume.failPlay = true;
    failedResume.resumeAfterNoise(13);
    yes(failedResume.stopped && failedResume.resumeCount == 0, "failed resume advanced");
    yes(failedResume.events.size() == 2
        && failedResume.events.get(1).get("event").equals("playback_error"), "false resume receipt");

    Player cancelled = new Player();
    cancelled.pauseForVoice(14);
    cancelled.cancel();
    AudioTrack neverStarted = new AudioTrack();
    yes(!cancelled.startTrack(neverStarted) && neverStarted.plays == 0
        && cancelled.events.isEmpty(), "cancelled startup");

    Player visual = new Player();
    AudioTrack displayTrack = new AudioTrack();
    visual.startTrack(displayTrack);
    visual.envelopes.add(new Player.Envelope(0, 700, .25f));
    displayTrack.head = 100;
    CaptionProgress live = visual.visualFrame();
    yes(displayTrack.headReads == 1 && live.level == .25f && live.position == 1
        && !live.paused && live.mood.equals("neutral"), "visuals did not share one live frame");
    displayTrack.head = 200;
    displayTrack.duringRead = () -> visual.pauseForVoice(15);
    CaptionProgress frozen = visual.visualFrame();
    yes(frozen.paused && frozen.level == 0 && frozen.position == 2 && frozen.elapsedMs == 12,
        "pause racing the visual sample did not close the mouth and freeze at the applied head");
    int pausedReads = displayTrack.headReads;
    displayTrack.head = 300;
    visual.timings = new CaptionTimeline(new int[][]{{0, 7, 0, 350}}, 7);
    yes(visual.visualFrame() == frozen && displayTrack.headReads == pausedReads,
        "late timings or hardware head movement changed paused visuals");
    visual.resumeAfterNoise(15);
    CaptionProgress resumed = visual.visualFrame();
    yes(!resumed.paused && resumed.position == 6 && resumed.level == .25f,
        "resume did not return to the current hardware frame and timings");
    visual.pauseForVoice(16);
    visual.cancel();
    yes(visual.visualFrame().id == 0 && visual.visualFrame().level == 0, "cancel retained a visual");
  }
}
"""
            + timeline
        )
        with tempfile.TemporaryDirectory(prefix='cc8-device-pause-') as folder:
            path = Path(folder) / 'PauseFixture.java'
            path.write_text(program)
            for command in (
                [str(java / 'javac'), '-encoding', 'UTF-8', '-d', folder, str(path)],
                [str(java / 'java'), '-cp', folder, 'PauseFixture'],
            ):
                result = subprocess.run(command, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
