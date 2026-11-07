package com.happyhome.cc8diagnostics;

/** Streaming interleaved channel statistics. Retains one incomplete frame and numeric totals. */
public final class ChannelMath {
  public final AudioMath[] channels;
  private final short[] frame;
  private final double[] sums;
  private final double[][] products;
  private final long[][] equal;
  private int pending;
  public long frames;

  public ChannelMath(int count) {
    if (count != 2 && count != 4)
      throw new IllegalArgumentException("Expected two or four channels");
    channels = new AudioMath[count];
    for (int i = 0; i < count; i++) channels[i] = new AudioMath();
    frame = new short[count];
    sums = new double[count];
    products = new double[count][count];
    equal = new long[count][count];
  }

  public void add(short[] pcm, int offset, int count) {
    if (offset < 0 || count < 0 || offset > pcm.length - count)
      throw new IllegalArgumentException("Invalid PCM range");
    for (int n = offset; n < offset + count; n++) {
      frame[pending++] = pcm[n];
      if (pending != channels.length) continue;
      for (int i = 0; i < channels.length; i++) {
        channels[i].add(frame, i, 1);
        sums[i] += frame[i];
        for (int j = i + 1; j < channels.length; j++) {
          products[i][j] += (double) frame[i] * frame[j];
          if (frame[i] == frame[j]) equal[i][j]++;
        }
      }
      frames++;
      pending = 0;
    }
  }

  private void requirePair(int first, int second) {
    if (first < 0 || first >= second || second >= channels.length)
      throw new IllegalArgumentException("Expected an ordered pair of distinct input channels");
  }

  public int pendingSamples() {
    return pending;
  }

  public Double meanPcm(int channel) {
    requireChannel(channel);
    return frames == 0 ? null : sums[channel] / frames;
  }

  public Double acDbfs(int channel) {
    requireChannel(channel);
    if (frames == 0) return null;
    double variance =
        Math.max(0, channels[channel].squares / frames - Math.pow(sums[channel] / frames, 2));
    return variance == 0 ? -120 : Math.max(-120, 10 * Math.log10(variance / (32768.0 * 32768.0)));
  }

  private void requireChannel(int channel) {
    if (channel < 0 || channel >= channels.length)
      throw new IllegalArgumentException("Invalid input channel");
  }

  public Double correlation(int first, int second) {
    requirePair(first, second);
    if (frames == 0) return null;
    double a = channels[first].squares - sums[first] * sums[first] / frames;
    double b = channels[second].squares - sums[second] * sums[second] / frames;
    if (a <= 0 || b <= 0) return null;
    double covariance = products[first][second] - sums[first] * sums[second] / frames;
    return Math.max(-1, Math.min(1, covariance / Math.sqrt(a * b)));
  }

  public double equalRatio(int first, int second) {
    requirePair(first, second);
    return frames == 0 ? 0 : (double) equal[first][second] / frames;
  }
}
