package com.happyhome.cc8assistant;

/** An accepted status callback remains bound while it waits for the UI thread. */
final class StatusPresentation {
  final String phase, message, listeningState;
  final int generation, captureEpoch, lifetime;
  final long route;
  final Object captureOwner;

  StatusPresentation(
      String phase,
      String message,
      String listeningState,
      int generation,
      int captureEpoch,
      int lifetime,
      long route,
      Object captureOwner) {
    this.phase = phase;
    this.message = message;
    this.listeningState = listeningState;
    this.generation = generation;
    this.captureEpoch = captureEpoch;
    this.lifetime = lifetime;
    this.route = route;
    this.captureOwner = captureOwner;
  }

  boolean current(
      boolean foreground,
      boolean recording,
      Object owner,
      int epoch,
      int lifetime,
      long route,
      int minimumGeneration,
      boolean statusGenerationCurrent) {
    return foreground
        && this.lifetime == lifetime
        && this.route == route
        && generation >= minimumGeneration
        && statusGenerationCurrent
        && (captureOwner == null || captureOwner == owner)
        && (!"listening".equals(phase)
            || (recording
                && captureOwner != null
                && captureOwner == owner
                && captureEpoch == epoch));
  }
}
