package com.happyhome.cc8assistant;

import java.io.IOException;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicReference;

/** Real worker/writer threads around the production component, without Android or audio devices. */
public final class SpeakerSessionCheck {
  private static final class Alive implements SpeakerSession.Alive {
    volatile boolean value = true;

    public boolean allows() {
      return value;
    }
  }

  private static final class Track implements SpeakerSession.Device {
    final AtomicInteger plays = new AtomicInteger(), pauses = new AtomicInteger();
    final AtomicInteger flushes = new AtomicInteger(), releases = new AtomicInteger();
    final AtomicInteger writes = new AtomicInteger(), concurrent = new AtomicInteger();
    final CountDownLatch entered = new CountDownLatch(1), unblock = new CountDownLatch(1);
    volatile boolean blockSpeech, blockIdle, failPlay, failPause, failFlush;
    volatile int badWrite, limit = 320;
    volatile boolean failIdle;
    volatile AssertionError fatalWrite;
    volatile CountDownLatch nextIdle;
    long frames, lateFrames;

    public void play() {
      if (failPlay) throw new IllegalStateException("play");
      plays.incrementAndGet();
    }

    public void pause() {
      if (failPause) throw new IllegalStateException("pause");
      pauses.incrementAndGet();
    }

    public synchronized void flush() {
      if (failFlush) throw new IllegalStateException("flush");
      flushes.incrementAndGet();
      frames = lateFrames = 0;
    }

    public int write(short[] data, int offset, int count) {
      yes(concurrent.incrementAndGet() == 1, "two native writers");
      try {
        boolean speech = data[offset] != 0;
        CountDownLatch observeIdle = nextIdle;
        if (!speech && observeIdle != null) observeIdle.countDown();
        if (!speech && failIdle) throw new IllegalStateException("idle write");
        if ((speech && blockSpeech) || (!speech && blockIdle)) {
          entered.countDown();
          await(unblock);
        }
        if (fatalWrite != null) throw fatalWrite;
        if (speech && badWrite != 0) {
          if (badWrite == 1) return -6;
          if (badWrite == 2) return count + 1;
          if (badWrite == 3) throw new IllegalStateException("write");
          if (badWrite == 4) return 0;
        }
        // Pace idle output so the test is bounded, without simulating any device clock claims.
        try {
          Thread.sleep(1);
        } catch (InterruptedException error) {
          throw new AssertionError(error);
        }
        int n = Math.min(count, limit);
        synchronized (this) {
          frames += n;
          if (speech && flushes.get() > 0) lateFrames += n;
        }
        writes.incrementAndGet();
        return n;
      } finally {
        concurrent.decrementAndGet();
      }
    }

    public void release() {
      yes(concurrent.get() == 0, "released during native write");
      yes(releases.incrementAndGet() == 1, "double release");
    }

    synchronized long head() {
      return frames;
    }
  }

  private static final class Fixture implements AutoCloseable {
    final Track track = new Track();
    final Alive alive = new Alive();
    final AtomicInteger opens = new AtomicInteger();
    final SpeakerSession session;

    Fixture() {
      session =
          new SpeakerSession(
              () -> {
                opens.incrementAndGet();
                return track;
              },
              alive);
    }

    SpeakerSession.Lease lease(int id) throws Exception {
      return session.acquire(id, id + 10, alive);
    }

    public void close() throws InterruptedException {
      track.unblock.countDown();
      session.close();
      yes(session.awaitReleased(2000), "worker leaked");
      yes(track.releases.get() == 1, "native track leaked");
    }
  }

  private static final class Job extends Thread {
    final CountDownLatch done = new CountDownLatch(1);
    final AtomicReference<Throwable> error = new AtomicReference<>();
    final Runnable task;

    Job(Runnable task) {
      this.task = task;
      start();
    }

    public void run() {
      try {
        task.run();
      } catch (Throwable failure) {
        error.set(failure);
      } finally {
        done.countDown();
      }
    }

    void check() {
      await(done);
      if (error.get() != null) throw new AssertionError("thread failure", error.get());
    }
  }

  private static void yes(boolean value, String why) {
    if (!value) throw new AssertionError(why);
  }

  private static void await(CountDownLatch latch) {
    try {
      yes(latch.await(3, TimeUnit.SECONDS), "latch timed out");
    } catch (InterruptedException error) {
      throw new AssertionError(error);
    }
  }

  private static void rejects(SpeakerSession session, Alive caller) throws Exception {
    try {
      session.acquire(99, 99, caller);
      throw new AssertionError("obsolete acquire succeeded");
    } catch (IOException expected) {
      // Native failure, closed lifetime or revoked caller must not acquire output.
    }
  }

  private static void handover() throws Exception {
    try (Fixture f = new Fixture()) {
      SpeakerSession.Lease a = f.lease(1);
      yes(a.base >= 9600 && a.generation == 1 && a.playbackId == 11, "warm lease identity");
      yes(f.session.localHead(a, f.track.head()) == 0, "warm zeros counted as speech");
      yes(f.session.write(a, new short[] {7, 8, 9}, 0, 3) == 3, "speech write");
      yes(f.session.localHead(a, f.track.head()) == 3, "local head");
      yes(f.session.absolutePosition(f.track.head()) == a.base + 3, "physical head");
      yes(f.session.finish(a), "normal end");
      SpeakerSession.Lease b = f.lease(2);
      yes(
          b.base >= a.base + 3 && f.opens.get() == 1 && f.track.plays.get() == 1,
          "track restarted");
      yes(f.session.localHead(b, f.track.head()) == 0, "second clause inherited progress");
      yes(f.session.cancel(a) == 0 && !f.session.finish(a), "late old cleanup");
      yes(f.session.write(a, new short[] {1}, 0, 1) == 0, "old writer entered new lease");
      yes(f.track.pauses.get() == 0 && f.track.flushes.get() == 0, "normal end flushed output");
      yes(f.session.finish(b), "second end");
    }
  }

  private static void lateWrite() throws Exception {
    try (Fixture f = new Fixture()) {
      SpeakerSession.Lease a = f.lease(1);
      f.track.blockSpeech = true;
      AtomicInteger written = new AtomicInteger(-1);
      Job writer =
          new Job(
              () -> {
                try {
                  written.set(f.session.write(a, new short[] {9, 9, 9}, 0, 3));
                } catch (IOException error) {
                  throw new AssertionError(error);
                }
              });
      await(f.track.entered);
      yes(f.session.cancel(a) > 0 && f.track.flushes.get() == 1, "no immediate stop witness");
      AtomicReference<SpeakerSession.Lease> acquired = new AtomicReference<>();
      CountDownLatch claiming = new CountDownLatch(1);
      Job claimant =
          new Job(
              () -> {
                try {
                  acquired.set(
                      f.session.acquire(
                          2,
                          12,
                          () -> {
                            claiming.countDown();
                            return true;
                          }));
                } catch (Exception error) {
                  throw new AssertionError(error);
                }
              });
      await(claiming);
      yes(!claimant.done.await(50, TimeUnit.MILLISECONDS), "new owner overtook blocked write");
      f.track.unblock.countDown();
      writer.check();
      claimant.check();
      SpeakerSession.Lease b = acquired.get();
      yes(written.get() == 0 && f.track.flushes.get() == 2, "late write survived reset");
      yes(f.track.lateFrames == 0 && b.base >= 9600, "old speech returned after warmup");
      yes(f.session.localHead(b, f.track.head()) == 0, "old frames entered new head");
      yes(f.session.cancel(a) == 0 && f.track.flushes.get() == 2, "late cancel reset new output");
      yes(f.session.finish(b), "new lease unusable");
    }
  }

  private static void pause() throws Exception {
    try (Fixture f = new Fixture()) {
      SpeakerSession.Lease lease = f.lease(1);
      yes(f.session.pause(lease) > 0 && f.session.pause(lease) == 0, "duplicate pause");
      int writes = f.track.writes.get();
      yes(f.session.write(lease, new short[] {1}, 0, 1) == 0, "paused speech accepted");
      yes(!f.session.finish(lease), "paused lease released");
      Thread.sleep(50);
      yes(f.track.writes.get() == writes, "idle zeros written during pause");
      yes(f.session.resume(lease) > 0 && f.session.resume(lease) == 0, "duplicate resume");
      yes(f.track.flushes.get() == 0 && f.opens.get() == 1, "pause reset track");
      yes(f.session.write(lease, new short[] {2}, 0, 1) == 1, "resume lost lease");
      yes(f.session.finish(lease), "resumed lease end");
      yes(f.session.pause(lease) == 0 && f.session.resume(lease) == 0, "old control applied");
    }
  }

  private static void construction(boolean revoke) throws Exception {
    Track track = new Track();
    Alive alive = new Alive();
    CountDownLatch entered = new CountDownLatch(1), resume = new CountDownLatch(1);
    SpeakerSession session =
        new SpeakerSession(
            () -> {
              entered.countDown();
              await(resume);
              return track;
            },
            alive);
    try {
      await(entered);
      if (revoke) alive.value = false;
      else session.close();
      resume.countDown();
      yes(session.awaitReleased(2000), "constructor worker ignored lifetime");
    } finally {
      resume.countDown();
      session.close();
      yes(session.awaitReleased(2000), "constructor worker leaked");
    }
    yes(track.plays.get() == 0 && track.writes.get() == 0, "obsolete constructor started audio");
    yes(track.releases.get() == 1, "obsolete constructor not released");
    rejects(session, alive);
  }

  private static void closeWrite() throws Exception {
    try (Fixture f = new Fixture()) {
      SpeakerSession.Lease lease = f.lease(1);
      f.track.blockSpeech = true;
      AtomicInteger accepted = new AtomicInteger(-1);
      Job writer =
          new Job(
              () -> {
                try {
                  accepted.set(f.session.write(lease, new short[] {1}, 0, 1));
                } catch (IOException error) {
                  throw new AssertionError(error);
                }
              });
      await(f.track.entered);
      f.session.close();
      f.session.close();
      yes(f.track.pauses.get() == 1 && f.track.releases.get() == 0, "unsafe close order");
      yes(!f.session.awaitReleased(30), "released outstanding writer");
      f.track.unblock.countDown();
      writer.check();
      yes(accepted.get() == 0, "post-close write counted");
      yes(f.session.awaitReleased(2000), "close did not finish");
      rejects(f.session, f.alive);
    }
  }

  private static void writeErrors(int kind) throws Exception {
    try (Fixture f = new Fixture()) {
      SpeakerSession.Lease lease = f.lease(1);
      f.track.badWrite = kind;
      if (kind == 4) {
        yes(f.session.write(lease, new short[] {1}, 0, 1) == 0, "zero write altered");
        f.track.badWrite = 0;
        f.track.limit = 1;
        yes(f.session.write(lease, new short[] {1, 2}, 0, 2) == 1, "short write altered");
        yes(f.session.localHead(lease, f.track.head()) == 1, "short write accounting");
        yes(f.session.finish(lease), "short write lease unusable");
      } else {
        try {
          f.session.write(lease, new short[] {1}, 0, 1);
          throw new AssertionError("native write failure hidden");
        } catch (IOException expected) {
          yes(f.session.awaitReleased(2000), "failed writer leaked");
          rejects(f.session, f.alive);
        }
      }
    }
  }

  private static void controlErrors(String kind) throws Exception {
    try (Fixture f = new Fixture()) {
      SpeakerSession.Lease lease = f.lease(1);
      if (kind.equals("resume")) {
        yes(f.session.pause(lease) > 0, "initial pause");
        f.track.failPlay = true;
      } else if (kind.equals("cancel")) f.track.failFlush = true;
      else f.track.failPause = true;
      try {
        if (kind.equals("resume")) f.session.resume(lease);
        else if (kind.equals("cancel")) f.session.cancel(lease);
        else f.session.pause(lease);
        throw new AssertionError("control error hidden");
      } catch (IllegalStateException expected) {
        yes(f.session.awaitReleased(2000), "failed control leaked track");
        rejects(f.session, f.alive);
      }
    }
  }

  private static void idleRevocation() throws Exception {
    try (Fixture f = new Fixture()) {
      SpeakerSession.Lease lease = f.lease(1);
      yes(f.session.finish(lease), "release");
      f.alive.value = false;
      yes(f.session.awaitReleased(2000), "idle audio survived lifetime");
      rejects(f.session, f.alive);
    }
  }

  private static void idleFailure() throws Exception {
    try (Fixture f = new Fixture()) {
      SpeakerSession.Lease lease = f.lease(1);
      f.track.failIdle = true;
      yes(f.session.finish(lease), "release");
      yes(f.session.awaitReleased(2000), "failed idle writer leaked");
      rejects(f.session, f.alive);
    }
  }

  private static void fatalWriteCleanup() throws Exception {
    try (Fixture f = new Fixture()) {
      SpeakerSession.Lease lease = f.lease(1);
      AssertionError failure = new AssertionError("fixed native write failure");
      f.track.fatalWrite = failure;
      try {
        f.session.write(lease, new short[] {1}, 0, 1);
        throw new AssertionError("write failure was swallowed");
      } catch (AssertionError error) {
        yes(error == failure, "write changed the fatal error");
      }
      yes(f.session.awaitReleased(1000), "failed write left the native track held");
      rejects(f.session, f.alive);
    }
  }

  private static void fatalIdleWriteCleanup() throws Exception {
    try (Fixture f = new Fixture()) {
      SpeakerSession.Lease lease = f.lease(1);
      f.track.fatalWrite = new AssertionError("fixed idle write failure");
      yes(f.session.finish(lease), "release speech lease before idle write");
      yes(f.session.awaitReleased(1000), "idle writer could not release its own track");
      rejects(f.session, f.alive);
    }
  }

  private static void shortWarm() throws Exception {
    Track track = new Track();
    track.limit = 37;
    Alive alive = new Alive();
    SpeakerSession session = new SpeakerSession(() -> track, alive);
    try {
      SpeakerSession.Lease lease = session.acquire(1, 1, alive);
      yes(lease.base >= 9600 && track.writes.get() >= 260, "short warm writes counted as blocks");
      yes(session.localHead(lease, track.head()) == 0, "warmup entered clause");
      yes(session.finish(lease), "short warm end");
    } finally {
      session.close();
      yes(session.awaitReleased(2000), "short warm worker leak");
    }
  }

  private static void revokedClaimant() throws Exception {
    try (Fixture f = new Fixture()) {
      SpeakerSession.Lease lease = f.lease(1);
      Alive caller = new Alive();
      CountDownLatch entered = new CountDownLatch(1);
      Job claim =
          new Job(
              () -> {
                try {
                  try {
                    f.session.acquire(
                        2,
                        12,
                        () -> {
                          entered.countDown();
                          return caller.value;
                        });
                    throw new AssertionError("revoked claimant acquired output");
                  } catch (IOException expected) {
                    // An active lease owns output until this claimant is revoked.
                  }
                } catch (Exception error) {
                  throw new AssertionError(error);
                }
              });
      await(entered);
      yes(!claim.done.await(40, TimeUnit.MILLISECONDS), "active lease stolen");
      caller.value = false;
      claim.check();
      yes(f.session.finish(lease), "release after revoked claimant");
      SpeakerSession.Lease next = f.lease(2);
      yes(f.session.finish(next), "revoked waiter starved fresh claimant");
    }
  }

  private static void initialFailure() throws Exception {
    Alive alive = new Alive();
    SpeakerSession session =
        new SpeakerSession(
            () -> {
              throw new IOException("constructor failure");
            },
            alive);
    try {
      rejects(session, alive);
      yes(session.awaitReleased(2000), "failed constructor worker leak");
    } finally {
      session.close();
    }
  }

  private static void idleClose() throws Exception {
    Track track = new Track();
    track.blockIdle = true;
    Alive alive = new Alive();
    SpeakerSession session = new SpeakerSession(() -> track, alive);
    try {
      await(track.entered);
      session.close();
      yes(
          !session.awaitReleased(30) && track.releases.get() == 0,
          "idle write released underneath");
      rejects(session, alive);
    } finally {
      track.unblock.countDown();
      session.close();
      yes(session.awaitReleased(2000), "idle writer did not finish");
      yes(track.releases.get() == 1, "idle close release count");
    }
  }

  private static void callerLoss() throws Exception {
    try (Fixture f = new Fixture()) {
      Alive caller = new Alive();
      SpeakerSession.Lease old = f.session.acquire(1, 11, caller);
      caller.value = false;
      SpeakerSession.Lease next = f.lease(2);
      yes(f.track.flushes.get() == 2 && next.epoch > old.epoch, "revoked clause not cleared");
      yes(f.session.cancel(old) == 0 && f.session.finish(next), "old clause affected new owner");
    }
  }

  private static void controlDuringWrite() throws Exception {
    try (Fixture f = new Fixture()) {
      SpeakerSession.Lease lease = f.lease(1);
      f.track.blockSpeech = true;
      Job writer =
          new Job(
              () -> {
                try {
                  yes(
                      f.session.write(lease, new short[] {1}, 0, 1) == 0,
                      "failed owner counted PCM");
                } catch (IOException error) {
                  throw new AssertionError(error);
                }
              });
      await(f.track.entered);
      f.track.failPause = true;
      try {
        f.session.pause(lease);
        throw new AssertionError("native pause error hidden");
      } catch (IllegalStateException expected) {
        yes(!f.session.awaitReleased(30), "control error forgot outstanding native write");
        yes(f.track.releases.get() == 0, "control error released underneath writer");
      }
      f.track.unblock.countDown();
      writer.check();
      yes(f.session.awaitReleased(2000), "failed control worker leaked");
    }
  }

  private static void interruptedClaimant() throws Exception {
    try (Fixture f = new Fixture()) {
      SpeakerSession.Lease old = f.lease(1);
      CountDownLatch claiming = new CountDownLatch(1);
      Job claim =
          new Job(
              () -> {
                try {
                  f.session.acquire(
                      2,
                      12,
                      () -> {
                        claiming.countDown();
                        return true;
                      });
                  throw new AssertionError("active lease stolen");
                } catch (InterruptedException expected) {
                  Thread.currentThread().interrupt();
                } catch (IOException error) {
                  throw new AssertionError(error);
                }
              });
      await(claiming);
      claim.interrupt();
      claim.check();
      CountDownLatch idle = new CountDownLatch(1);
      f.track.nextIdle = idle;
      yes(f.session.finish(old), "release after interrupted claimant");
      await(idle);
      SpeakerSession.Lease next = f.lease(3);
      yes(f.session.finish(next), "interrupted waiter starved new clause");
    }
  }

  private static void wrap() {
    long turn = 0x100000000L;
    yes(SpeakerSession.unwrap(0xfffffff0L, turn + 20) == turn - 16, "previous wrap");
    yes(SpeakerSession.unwrap(30, turn - 20) == turn + 30, "next wrap");
    yes(SpeakerSession.unwrap(40, 2 * turn + 30) == 2 * turn + 40, "second wrap");
    yes(SpeakerSession.unwrap(0xffffffffL, 0) == -1, "invalid pre-start timestamp");
    yes(SpeakerSession.unwrap(-1, turn) == -1, "negative hardware position accepted");
    yes(SpeakerSession.unwrap(turn, turn) == -1, "unbounded hardware position accepted");
  }

  public static void main(String[] args) throws Exception {
    switch (args[0]) {
      case "handover":
        handover();
        break;
      case "late_write":
        lateWrite();
        break;
      case "pause":
        pause();
        break;
      case "construct_close":
        construction(false);
        break;
      case "construct_revoke":
        construction(true);
        break;
      case "close_write":
        closeWrite();
        break;
      case "negative":
        writeErrors(1);
        break;
      case "oversize":
        writeErrors(2);
        break;
      case "throw_write":
        writeErrors(3);
        break;
      case "short_zero":
        writeErrors(4);
        break;
      case "pause_error":
        controlErrors("pause");
        break;
      case "resume_error":
        controlErrors("resume");
        break;
      case "cancel_error":
        controlErrors("cancel");
        break;
      case "idle_revoke":
        idleRevocation();
        break;
      case "idle_failure":
        idleFailure();
        break;
      case "fatal_write_cleanup":
        fatalWriteCleanup();
        break;
      case "fatal_idle_write_cleanup":
        fatalIdleWriteCleanup();
        break;
      case "short_warm":
        shortWarm();
        break;
      case "revoked_claimant":
        revokedClaimant();
        break;
      case "initial_failure":
        initialFailure();
        break;
      case "idle_close":
        idleClose();
        break;
      case "caller_loss":
        callerLoss();
        break;
      case "control_write_error":
        controlDuringWrite();
        break;
      case "interrupted_claimant":
        interruptedClaimant();
        break;
      case "wrap":
        wrap();
        break;
      default:
        throw new AssertionError("unknown scenario");
    }
    System.out.println(args[0] + " passed");
  }
}
