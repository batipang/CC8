package com.happyhome.cc8assistant;

/** Explicit foreground audio lifetimes; receiving late PCM can never create a new device. */
final class SpeakerOwner implements AutoCloseable {
  final class Ticket {
    final boolean voice;
    final int id;
    private final SpeakerSession.Alive caller;
    private SpeakerSession session;

    private Ticket(boolean voice, int id, SpeakerSession.Alive caller) {
      this.voice = voice;
      this.id = id;
      this.caller = caller;
    }

    boolean allows() {
      // This callback is also used under SpeakerSession's guard: no owner/route locks here.
      return current == this && foreground.allows() && caller.allows();
    }

    SpeakerSession output() {
      return session;
    }
  }

  private final SpeakerSession.Factory factory;
  private final SpeakerSession.Alive foreground;
  private volatile Ticket current;

  SpeakerOwner(SpeakerSession.Factory factory, SpeakerSession.Alive foreground) {
    this.factory = factory;
    this.foreground = foreground;
  }

  synchronized void beginVoice(int captureEpoch, SpeakerSession.Alive caller) {
    begin(true, captureEpoch, caller);
  }

  synchronized void beginTask(int generation, SpeakerSession.Alive caller) {
    if (current != null && current.voice && current.allows()) return;
    begin(false, generation, caller);
  }

  private void begin(boolean voice, int id, SpeakerSession.Alive caller) {
    if (!foreground.allows() || !caller.allows()) return;
    close();
    Ticket next = new Ticket(voice, id, caller);
    current = next;
    next.session = new SpeakerSession(factory, next::allows);
  }

  synchronized Ticket forPlayback(int generation) {
    Ticket ticket = current;
    return ticket != null && ticket.allows() && (ticket.voice || ticket.id == generation)
        ? ticket
        : null;
  }

  synchronized void endVoice(int captureEpoch) {
    if (current != null && current.voice && current.id == captureEpoch) close();
  }

  synchronized void endTask(int generation) {
    if (current != null && !current.voice && current.id == generation) close();
  }

  synchronized void endTasksThrough(int generation) {
    if (current != null && !current.voice && current.id <= generation) close();
  }

  @Override
  public synchronized void close() {
    Ticket previous = current;
    current = null;
    if (previous != null) previous.session.close();
  }
}
