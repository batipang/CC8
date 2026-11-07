package com.happyhome.cc8diagnostics;

/** Bounded, RAM-only one-millisecond averages for comparison with known playback PCM. */
public final class ReferenceMatch {
  public static final int ANALYSIS_RATE = 1000, MAX_POINTS = 6000, MAX_OFFSET = 500;
  public static final int MIN_OVERLAP = 1000, PEAK_EXCLUSION = 20;
  private final int rate, channels;
  private final double[][] values;
  private final long[] sums;
  private int channel, groupFrames, points;
  private long frames;

  public ReferenceMatch(int rate, int channels) {
    if (rate < ANALYSIS_RATE || rate > 192000 || channels < 1 || channels > 4)
      throw new IllegalArgumentException("Unsupported comparison layout");
    this.rate = rate;
    this.channels = channels;
    values = new double[channels][MAX_POINTS];
    sums = new long[channels];
  }

  public void add(short[] pcm, int offset, int count) {
    if (offset < 0 || count < 0 || offset > pcm.length - count)
      throw new IllegalArgumentException("Invalid PCM range");
    for (int i = offset; i < offset + count && points < MAX_POINTS; i++) {
      sums[channel++] += pcm[i];
      if (channel != channels) continue;
      channel = 0;
      frames++;
      groupFrames++;
      // Rational boundaries also support 44.1 kHz without accumulating clock drift.
      if (frames * ANALYSIS_RATE < (long) (points + 1) * rate) continue;
      for (int c = 0; c < channels; c++) {
        values[c][points] = sums[c] / (double) groupFrames;
        sums[c] = 0;
      }
      points++;
      groupFrames = 0;
    }
  }

  public int points() {
    return points;
  }

  public static final class Match {
    public final double correlation;
    public final int offset, overlap;
    public Double competingCorrelation;
    public Integer competingOffset;

    private Match(double correlation, int offset, int overlap) {
      this.correlation = correlation;
      this.offset = offset;
      this.overlap = overlap;
    }
  }

  private double[] centered(int c) {
    double mean = 0;
    for (int i = 0; i < points; i++) mean += values[c][i];
    mean /= Math.max(1, points);
    double[] result = new double[points];
    for (int i = 0; i < points; i++) result[i] = values[c][i] - mean;
    return result;
  }

  /** Positive offsets mean later input array indices, not measured acoustic latency. */
  public Match compare(int c, ReferenceMatch reference) {
    if (c < 0 || c >= channels || reference.channels != 1)
      throw new IllegalArgumentException("Expected an input channel and mono reference");
    if (Math.min(points, reference.points) < MIN_OVERLAP) return null;
    double[] input = centered(c), expected = reference.centered(0);
    Double[] scores = new Double[MAX_OFFSET * 2 + 1];
    Match best = null;
    for (int lag = -MAX_OFFSET; lag <= MAX_OFFSET; lag++) {
      if (Thread.currentThread().isInterrupted()) return null;
      int a = Math.max(0, lag), b = Math.max(0, -lag);
      int n = Math.min(input.length - a, expected.length - b);
      if (n < MIN_OVERLAP) continue;
      double sx = 0, sy = 0, xx = 0, yy = 0, xy = 0;
      for (int i = 0; i < n; i++) {
        double x = input[a + i], y = expected[b + i];
        sx += x;
        sy += y;
        xx += x * x;
        yy += y * y;
        xy += x * y;
      }
      double vx = xx - sx * sx / n, vy = yy - sy * sy / n;
      if (vx <= 0 || vy <= 0) continue;
      double score = Math.max(-1, Math.min(1, (xy - sx * sy / n) / Math.sqrt(vx * vy)));
      scores[lag + MAX_OFFSET] = score;
      if (best == null || Math.abs(score) > Math.abs(best.correlation))
        best = new Match(score, lag, n);
    }
    if (best == null) return null;
    for (int lag = -MAX_OFFSET; lag <= MAX_OFFSET; lag++) {
      Double score = scores[lag + MAX_OFFSET];
      if (score == null || Math.abs(lag - best.offset) < PEAK_EXCLUSION) continue;
      if (best.competingCorrelation == null
          || Math.abs(score) > Math.abs(best.competingCorrelation)) {
        best.competingCorrelation = score;
        best.competingOffset = lag;
      }
    }
    return best;
  }
}
