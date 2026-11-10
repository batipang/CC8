package com.happyhome.cc8assistant;

import android.media.AudioRecord;
import android.os.SystemClock;
import java.io.IOException;
import java.util.Arrays;

/** One confirmed leg. Shares the native recorder lock with ordinary voice, never stores audio. */
final class MeetingRecorder {
  interface Port {
    boolean current();

    boolean start(AudioRecord record);

    boolean send(byte[] pcm, long sequence, long frame);

    void finished(boolean failed, boolean released);
  }

  private final Object resource;
  private final Port port;
  private volatile boolean cancelled;
  private volatile boolean started;
  private volatile AudioRecord record;
  private boolean stopRequested;
  private boolean consumed;
  private Thread worker;

  MeetingRecorder(Object resource, Port port) {
    this.resource = resource;
    this.port = port;
  }

  synchronized void start() {
    if (consumed || worker != null || cancelled)
      throw new IllegalStateException("Recorder already consumed");
    consumed = true;
    Thread candidate = new Thread(this::run, "cc8-meeting-recorder");
    candidate.start();
    worker = candidate;
  }

  synchronized boolean busy() {
    return worker != null;
  }

  boolean capturing() {
    return started && !cancelled;
  }

  void stop() {
    final AudioRecord old;
    synchronized (this) {
      cancelled = true;
      if (stopRequested) return;
      stopRequested = true;
      old = record;
    }
    // Logical revocation precedes physical stop; a blocked OEM read must not
    // hold the UI or transport lock. Only one stop helper exists for this leg.
    if (old != null)
      new Thread(
              () -> {
                try {
                  old.stop();
                } catch (RuntimeException ignored) {
                }
              },
              "cc8-meeting-stop")
          .start();
  }

  private boolean current() {
    return !cancelled && port.current();
  }

  private void run() {
    boolean failed = false;
    boolean released = true;
    synchronized (resource) {
      CaptureAudioInput input = null;
      try {
        if (!current()) return;
        input = CaptureAudioInput.open(false);
        record = input.recorder;
        // The port rechecks foreground, permission and the exact route/nonce
        // while holding the route guard immediately around physical Start.
        if (!current()) return;
        if (!port.start(record)) {
          failed = current();
          return;
        }
        started = true;
        long sequence = 0, frames = 0, startedAt = SystemClock.elapsedRealtime();
        int empty = 0;
        while (current()
            && frames < 16000L * 3600
            && SystemClock.elapsedRealtime() - startedAt < 3600000L) {
          CaptureAudioInput.Chunk chunk = input.read();
          if (chunk == null) {
            if (++empty > 50) throw new IOException("No microphone data");
            Thread.sleep(5);
            continue;
          }
          empty = 0;
          byte[] bytes = null;
          try {
            bytes = Wire.pcm(chunk.samples, chunk.frames);
            if (!current() || !port.send(bytes, sequence, frames)) return;
            sequence++;
            frames += chunk.frames;
          } finally {
            chunk.clear();
            if (bytes != null) Arrays.fill(bytes, (byte) 0);
          }
        }
        // A local one-hour watchdog stops even if host/UI timers stall.
        failed = current();
      } catch (CaptureAudioInput.ReleaseUnconfirmed failure) {
        failed = current();
        released = false;
      } catch (IOException | RuntimeException | InterruptedException failure) {
        failed = current();
      } finally {
        started = false;
        AudioRecord owned = record;
        record = null;
        if (owned != null) {
          try {
            owned.stop();
          } catch (RuntimeException ignored) {
          }
          try {
            owned.release();
          } catch (RuntimeException ignored) {
            failed = true;
            released = false;
          }
        }
        if (input != null) input.clear();
        synchronized (this) {
          worker = null;
        }
        port.finished(failed, released);
      }
    }
  }
}
