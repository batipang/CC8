package com.happyhome.cc8assistant;

import java.io.IOException;

/** Drain the real speech endpoint; post-speech zeros only prime a resumed streaming track. */
final class PlaybackDrain {
  interface Sink extends PcmPump.Sink {
    long now();

    long head();

    int bufferFrames();

    long resumeSerial();

    void probe();

    void waitTick(long milliseconds) throws InterruptedException;
  }

  private volatile int paddingFrames;
  private long lastResume;

  int paddingFrames() {
    return paddingFrames;
  }

  boolean drain(long speechFrames, Sink sink) throws IOException, InterruptedException {
    long deadline = sink.now() + 3000;
    while (sink.allowed()) {
      long before = sink.now();
      sink.awaitReady();
      deadline += sink.now() - before;
      if (!sink.allowed()) return false;
      long head = sink.head();
      sink.probe();
      if (!sink.allowed()) return false;
      if (head >= speechFrames) return true;
      long resumed = sink.resumeSerial();
      if (resumed > lastResume) {
        lastResume = resumed;
        int buffer = sink.bufferFrames();
        if (buffer < 1 || buffer > 32000) throw new IOException("Invalid drain buffer size");
        long queued = Math.max(0, speechFrames + paddingFrames - head);
        int need = (int) Math.min(buffer - paddingFrames, Math.max(0, buffer - queued));
        if (need > 0) {
          before = sink.now();
          boolean accepted =
              PcmPump.writeAll(
                  new short[need],
                  new PcmPump.Sink() {
                    public boolean allowed() {
                      return sink.allowed();
                    }

                    public boolean paused() {
                      return sink.paused();
                    }

                    public void awaitReady() throws InterruptedException {
                      sink.awaitReady();
                    }

                    public int write(short[] samples, int offset, int count) throws IOException {
                      return sink.write(samples, offset, count);
                    }

                    public void accepted(short[] samples, int offset, int count) {
                      // Keep speech/reference/caption accounting unchanged. The
                      // AEC already supplies zeros beyond the speech endpoint.
                      paddingFrames += count;
                    }
                  });
          deadline += sink.now() - before;
          if (!accepted) return false;
        }
      }
      if (sink.now() >= deadline) throw new IOException("播放排空超时");
      sink.waitTick(10);
    }
    return false;
  }
}
