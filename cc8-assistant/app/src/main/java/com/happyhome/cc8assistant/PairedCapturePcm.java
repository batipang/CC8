package com.happyhome.cc8assistant;

import java.util.Arrays;

/** Causal 48 kHz index-4 PCM to aligned 16 kHz microphone/candidate-reference pairs.
 * Only indices 0 and 2 leave this converter. Both use the same anti-alias filter,
 * phase and saturation; no channel can silently advance ahead of the other.
 */
final class PairedCapturePcm {
  static final int INPUT_RATE = 48000, OUTPUT_RATE = 16000, INPUT_CHANNELS = 4;
  // 95-tap unity-DC FIR: 7200 Hz cutoff, Kaiser beta 8.6, at 48000 Hz.
  // Generated from the windowed sinc, not from device recordings. Its causal
  // group delay is 47 input samples in both streams; no future input is used.
  private static final double[] TAPS = {
    2.788764686610072e-06, -1.0534533859291465e-05, -3.0586630043571068e-05,
        -2.8183201972420387e-05,
    2.1962995090482498e-05, 9.6223114616754337e-05, 0.00011295187852317932, -2.7616653045351433e-19,
    -0.00020046419903084578, -0.00030536442048161532, -0.00012665017091686794,
        0.00030349427166955812,
    0.00064297526595924524, 0.00046576604608799702, -0.0002989703635964937, -0.0011139655185445326,
    -0.0011383979594325876, 1.8501167428013929e-18, 0.0016095520618517314, 0.0022293364397347058,
    0.00084879191351692812, -0.0018823746259742952, -0.0037166675836307721, -0.002524680895090484,
    0.0015280442362911478, 0.0053953314654258018, 0.0052490507141392728, -5.4376715543031633e-18,
    -0.0068127441137063359, -0.0090959762330134024, -0.0033516783670661699, 0.0072228273017664471,
    0.01391581417737804, 0.0092647710668594709, -0.0055222633915326703, -0.019304588834998679,
    -0.018707300753901555, 9.720064529565828e-18, 0.024640365605073152, 0.033633229960328026,
    0.012841051765209169, -0.029188221519259313, -0.060808982598837762, -0.045423339219775746,
    0.032252133619277694, 0.15026303511564529, 0.2570505525442045, 0.30000376962266007,
    0.2570505525442045, 0.15026303511564529, 0.032252133619277694, -0.045423339219775746,
    -0.060808982598837762, -0.029188221519259313, 0.012841051765209169, 0.033633229960328026,
    0.024640365605073152, 9.720064529565828e-18, -0.018707300753901555, -0.019304588834998679,
    -0.0055222633915326703, 0.0092647710668594709, 0.01391581417737804, 0.0072228273017664471,
    -0.0033516783670661699, -0.0090959762330134024, -0.0068127441137063359, -5.4376715543031633e-18,
    0.0052490507141392728, 0.0053953314654258018, 0.0015280442362911478, -0.002524680895090484,
    -0.0037166675836307721, -0.0018823746259742952, 0.00084879191351692812, 0.0022293364397347058,
    0.0016095520618517314, 1.8501167428013929e-18, -0.0011383979594325876, -0.0011139655185445326,
    -0.0002989703635964937, 0.00046576604608799702, 0.00064297526595924524, 0.00030349427166955812,
    -0.00012665017091686794, -0.00030536442048161532, -0.00020046419903084578,
        -2.7616653045351433e-19,
    0.00011295187852317932, 9.6223114616754337e-05, 2.1962995090482498e-05, -2.8183201972420387e-05,
    -3.0586630043571068e-05, -1.0534533859291465e-05, 2.788764686610072e-06
  };
  private final double[] microphone = new double[TAPS.length];
  private final double[] reference = new double[TAPS.length];
  private final short[] partial = new short[INPUT_CHANNELS];
  private int head, phase, partialCount;

  /** Returns interleaved mic/reference PCM16; preserves incomplete input frames. */
  short[] push(short[] input, int offset, int count) {
    if (input == null
        || offset < 0
        || count < 0
        || count > 192000
        || offset > input.length
        || count > input.length - offset)
      throw new IllegalArgumentException("Invalid paired input extent");
    int frames = (phase + (partialCount + count) / INPUT_CHANNELS) / 3;
    short[] output = new short[frames * 2];
    int written = 0;
    for (int index = offset; index < offset + count; index++) {
      partial[partialCount++] = input[index];
      if (partialCount != INPUT_CHANNELS) continue;
      microphone[head] = partial[0];
      reference[head] = partial[2];
      head = (head + 1) % TAPS.length;
      partialCount = 0;
      if (++phase != 3) continue;
      phase = 0;
      double mic = 0, ref = 0;
      int sample = head;
      for (double tap : TAPS) {
        sample = sample == 0 ? TAPS.length - 1 : sample - 1;
        mic += tap * microphone[sample];
        ref += tap * reference[sample];
      }
      output[written++] = saturate(mic);
      output[written++] = saturate(ref);
    }
    if (written != output.length) throw new IllegalStateException("Paired frame count changed");
    return output;
  }

  private static short saturate(double value) {
    return (short) Math.max(Short.MIN_VALUE, Math.min(Short.MAX_VALUE, Math.rint(value)));
  }

  /** Called on capture stop/replacement; history cannot cross recording epochs. */
  void clear() {
    Arrays.fill(microphone, 0);
    Arrays.fill(reference, 0);
    Arrays.fill(partial, (short) 0);
    head = phase = partialCount = 0;
  }
}
