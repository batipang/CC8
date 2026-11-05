package com.happyhome.cc8assistant;

import java.io.IOException;

/** One foreground audio owner; clauses borrow the running track instead of restarting its clock. */
final class SpeakerSession implements AutoCloseable {
  interface Alive {
    // Only read volatile ownership fields here; never take the route/session locks.
    boolean allows();
  }

  interface Device {
    void play();

    void pause();

    void flush();

    int write(short[] samples, int offset, int count);

    void release();
  }

  interface Factory {
    Device open() throws IOException;
  }

  static final class Lease {
    final int generation, playbackId;
    final long base, epoch;
    final Device device;
    private final Alive alive;
    private boolean valid = true, paused;

    private Lease(
        int generation, int playbackId, long base, long epoch, Device device, Alive alive) {
      this.generation = generation;
      this.playbackId = playbackId;
      this.base = base;
      this.epoch = epoch;
      this.device = device;
      this.alive = alive;
    }
  }

  private static final int BLOCK = 320, WARM_FRAMES = 9600;
  private final Object guard = new Object();
  private final Alive alive;
  private final Thread worker;
  private Device device;
  private Lease owner;
  private IOException failure;
  private boolean closed, writing, resetting;
  private int waiting, warmFrames;
  private long epoch, accepted;

  SpeakerSession(Factory factory, Alive alive) {
    this.alive = alive;
    worker = new Thread(() -> run(factory), "cc8-speaker-clock");
    worker.start();
  }

  Lease acquire(int generation, int playbackId, Alive caller)
      throws IOException, InterruptedException {
    long deadline = System.nanoTime() + 5000000000L;
    synchronized (guard) {
      waiting++;
      try {
        while (true) {
          if (failure != null) throw new IOException("Speaker unavailable", failure);
          if (closed || !alive.allows() || !caller.allows())
            throw new IOException("Speaker owner ended");
          if (device != null
              && owner == null
              && !writing
              && !resetting
              && warmFrames >= WARM_FRAMES) {
            owner = new Lease(generation, playbackId, accepted, epoch, device, caller);
            return owner;
          }
          if (System.nanoTime() >= deadline) throw new IOException("Speaker acquisition timed out");
          guard.wait(20);
        }
      } finally {
        waiting--;
        guard.notifyAll();
      }
    }
  }

  int write(Lease lease, short[] samples, int offset, int count) throws IOException {
    if (offset < 0 || count < 0 || offset > samples.length - count)
      throw new IndexOutOfBoundsException("PCM range");
    if (count == 0) return 0;
    synchronized (guard) {
      if (!current(lease) || lease.paused) return 0;
      if (writing) throw new IOException("Concurrent speaker writers");
      writing = true;
    }
    return writeBlock(lease.device, lease.epoch, samples, offset, count);
  }

  private int writeBlock(Device target, long ticket, short[] samples, int offset, int count)
      throws IOException {
    int result = 0;
    IOException problem = null;
    try {
      // A native write can block until pause. Controls must remain able to take guard.
      result = target.write(samples, offset, count);
      if (result < 0 || result > count) throw new IOException("Invalid speaker write count");
    } catch (RuntimeException error) {
      problem = new IOException("Speaker write failed", error);
    } catch (IOException error) {
      problem = error;
    }
    synchronized (guard) {
      writing = false;
      if (problem != null) throw failed(problem);
      if (closed || epoch != ticket) result = 0;
      else accepted += result;
      guard.notifyAll();
      return result;
    }
  }

  private IOException failed(IOException error) {
    synchronized (guard) {
      if (failure == null) failure = error;
      closeLocked();
      guard.notifyAll();
    }
    return error;
  }

  long pause(Lease lease) {
    synchronized (guard) {
      if (!current(lease) || lease.paused) return 0;
      try {
        lease.device.pause();
      } catch (RuntimeException error) {
        failed(new IOException("Speaker pause failed", error));
        throw error;
      }
      lease.paused = true;
      return System.nanoTime();
    }
  }

  long resume(Lease lease) {
    synchronized (guard) {
      if (!current(lease) || !lease.paused) return 0;
      try {
        lease.device.play();
      } catch (RuntimeException error) {
        failed(new IOException("Speaker resume failed", error));
        throw error;
      }
      lease.paused = false;
      guard.notifyAll();
      return System.nanoTime();
    }
  }

  long cancel(Lease lease) {
    synchronized (guard) {
      if (closed || lease == null || owner != lease || !lease.valid) return 0;
      lease.valid = false;
      owner = null;
      epoch++;
      resetting = true;
      try {
        device.pause();
        device.flush();
        return System.nanoTime();
      } catch (RuntimeException error) {
        failed(new IOException("Speaker cancellation failed", error));
        throw error;
      } finally {
        guard.notifyAll();
      }
    }
  }

  boolean finish(Lease lease) {
    synchronized (guard) {
      if (!current(lease) || lease.paused || writing) return false;
      lease.valid = false;
      owner = null;
      guard.notifyAll();
      return true;
    }
  }

  private boolean current(Lease lease) {
    return !closed
        && lease != null
        && owner == lease
        && lease.valid
        && lease.epoch == epoch
        && alive.allows()
        && lease.alive.allows();
  }

  long absolutePosition(long low32) {
    synchronized (guard) {
      return unwrap(low32, accepted);
    }
  }

  long localHead(Lease lease, long low32) {
    synchronized (guard) {
      if (lease == null || lease.device != device || lease.epoch != epoch || closed) return 0;
      return Math.max(0, Math.min(accepted, unwrap(low32, accepted)) - lease.base);
    }
  }

  // AudioTrack's low 32 bits wrap after about 74.6 hours at 16 kHz. Map near accepted frames.
  static long unwrap(long low32, long near) {
    if (low32 < 0 || low32 > 0xffffffffL) return -1;
    long result = (near & ~0xffffffffL) | low32;
    if (result - near > 0x80000000L) result -= 0x100000000L;
    else if (near - result > 0x80000000L) result += 0x100000000L;
    return result;
  }

  @Override
  public void close() {
    synchronized (guard) {
      closeLocked();
    }
  }

  private void closeLocked() {
    if (closed) return;
    closed = true;
    epoch++;
    if (owner != null) owner.valid = false;
    owner = null;
    if (device != null) {
      try {
        device.pause();
        device.flush();
      } catch (RuntimeException ignored) {
        // Closing still releases the device from the worker after an in-flight write exits.
      }
    }
    guard.notifyAll();
  }

  boolean awaitReleased(long timeoutMs) throws InterruptedException {
    if (timeoutMs <= 0) throw new IllegalArgumentException("Release wait must be bounded");
    worker.join(timeoutMs);
    return !worker.isAlive();
  }

  private void run(Factory factory) {
    Device opened = null;
    try {
      synchronized (guard) {
        if (closed || !alive.allows()) return;
      }
      opened = factory.open();
      synchronized (guard) {
        // close() may have arrived while the native constructor was blocked.
        if (closed || !alive.allows()) return;
        device = opened;
        device.play();
        guard.notifyAll();
      }
      short[] zeros = new short[BLOCK];
      int stalls = 0;
      while (true) {
        final long ticket;
        synchronized (guard) {
          if (!alive.allows()) closeLocked();
          if (closed) break;
          if (owner != null && !owner.alive.allows()) cancel(owner);
          if (writing || owner != null) {
            guard.wait(20);
            continue;
          }
          if (resetting) {
            // A paused write can return after the first flush. Discard its late audio too.
            device.flush();
            accepted = 0;
            warmFrames = 0;
            resetting = false;
            device.play();
          }
          if (waiting > 0 && warmFrames >= WARM_FRAMES) {
            guard.notifyAll();
            guard.wait(20);
            continue;
          }
          writing = true;
          ticket = epoch;
        }
        int count = writeBlock(opened, ticket, zeros, 0, zeros.length);
        synchronized (guard) {
          if (ticket == epoch && !closed) warmFrames = Math.min(WARM_FRAMES, warmFrames + count);
          guard.notifyAll();
        }
        if (count > 0) stalls = 0;
        else {
          if (++stalls > 600) throw new IOException("Speaker idle writer stalled");
          Thread.sleep(5);
        }
      }
    } catch (InterruptedException error) {
      Thread.currentThread().interrupt();
      failed(new IOException("Speaker worker interrupted", error));
    } catch (IOException | RuntimeException error) {
      failed(new IOException("Speaker initialization or idle write failed", error));
    } finally {
      boolean interrupted = Thread.interrupted();
      synchronized (guard) {
        closeLocked();
        // Never release an AudioTrack underneath its outstanding native write.
        while (writing) {
          try {
            guard.wait(20);
          } catch (InterruptedException error) {
            interrupted = true;
          }
        }
        device = null;
        guard.notifyAll();
      }
      if (opened != null) opened.release();
      if (interrupted) Thread.currentThread().interrupt();
    }
  }
}
