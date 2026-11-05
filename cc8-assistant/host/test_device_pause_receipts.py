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
        # Keep the original pending/stale/failing controls and visual race assertions,
        # now running the complete production Player with actual output ownership.
        from speaker_harness import SpeakerHarness

        fixture = SpeakerHarness()
        try:
            for scenario in ('controls', 'visuals'):
                with self.subTest(scenario=scenario):
                    result = fixture.run(scenario)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertEqual(result.stdout.strip(), scenario + ' passed')
        finally:
            fixture.close()

    def test_queued_playback_updates_recheck_their_owner_on_the_main_thread(self):
        java = require_java()
        source = SOURCE.read_text()
        activity = SOURCE.with_name('MainActivity.java').read_text()
        session_methods = '\n'.join(
            method(source, signature)
            for signature in (
                'static final class PlaybackPresentation',
                'boolean currentPlaybackPresentation(PlaybackPresentation update)',
                'private void publishPlayback(Player owner, String text, String nextPhase, String message)',
                'private synchronized void cancelPlayback()',
            )
        )
        program = (
            """
import java.util.*;
public class UiFixture {
  static class CaptionProgress {
    static CaptionProgress empty() { return new CaptionProgress(); }
  }
  static class Player {
    final String mood;
    boolean cancelled;
    Player(String mood) { this.mood = mood; }
    void cancel() { cancelled = true; }
  }
  static class VoiceSession {
    boolean foreground = true;
    Player player;
    MainActivity listener;
    CaptionProgress lastCaption;
    PlaybackPresentation playbackPresentation;
    String phase;
"""
            + session_methods
            + """
  }
  static class MainActivity {
    boolean foreground = true;
    final VoiceSession session = new VoiceSession();
    final Deque<Runnable> pending = new ArrayDeque<>();
    String caption, mood, phase, message;
    MainActivity() { session.listener = this; }
    void runOnUiThread(Runnable work) { pending.addLast(work); }
    void applyCaption(String text, String emotion) { caption = text; mood = emotion; }
    void applyState(String next, String detail) { phase = next; message = detail; }
"""
            + method(
                activity,
                'public void playbackPresentation(VoiceSession.PlaybackPresentation update)',
            )
            + """
  }
  static void yes(boolean condition, String reason) {
    if (!condition) throw new AssertionError(reason);
  }
  public static void main(String[] args) {
    MainActivity ui = new MainActivity();
    VoiceSession session = ui.session;
    Player previous = new Player("old mood");
    session.player = previous;
    session.publishPlayback(previous, "old caption", "speaking", "old");
    session.publishPlayback(previous, "old caption", "listening", "pause");
    session.cancelPlayback();
    Player next = new Player("new mood");
    session.player = next;
    session.publishPlayback(next, "new caption", "speaking", "new");
    session.publishPlayback(previous, "late caption", "error", "late");
    yes(previous.cancelled && ui.pending.size() == 3, "old producer published after replacement");
    ui.pending.removeLast().run();
    ui.pending.removeFirst().run();
    ui.pending.removeFirst().run();
    yes(ui.caption.equals("new caption") && ui.phase.equals("speaking")
        && ui.mood.equals("new mood") && ui.message.equals("new"), "stale frame overwrote new UI");

    session.publishPlayback(next, "new caption", "listening", "pause");
    session.publishPlayback(next, "new caption", "speaking", "resume");
    ui.pending.removeLast().run();
    ui.pending.removeFirst().run();
    yes(ui.message.equals("resume"), "old pause overwrote resume");

    session.publishPlayback(next, "finished", "listening", "done");
    session.player = null; // Natural finish can precede UI delivery.
    ui.pending.removeFirst().run();
    yes(ui.caption.equals("finished") && ui.message.equals("done"), "dropped natural completion");
    session.player = next;
    session.publishPlayback(next, "private stale caption", "speaking", "stale");
    ui.foreground = false;
    ui.pending.removeFirst().run();
    yes(ui.caption.equals("finished"), "background screen applied a caption");
  }
}
"""
        )
        with tempfile.TemporaryDirectory(prefix='cc8-playback-ui-') as folder:
            path = Path(folder) / 'UiFixture.java'
            path.write_text(program)
            for command in (
                [str(java / 'javac'), '-encoding', 'UTF-8', '-d', folder, str(path)],
                [str(java / 'java'), '-cp', folder, 'UiFixture'],
            ):
                result = subprocess.run(command, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
