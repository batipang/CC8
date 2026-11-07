package com.happyhome.cc8diagnostics;

import java.io.*;
import java.nio.file.Files;
import java.util.Arrays;
import java.util.Random;

public final class HostTests {
  private static int count;

  private static void check(boolean value, String message) {
    count++;
    if (!value) throw new AssertionError(message);
  }

  private static void failRead(byte[] data, String label) throws Exception {
    boolean failed = false;
    try {
      WavCodec.read(new ByteArrayInputStream(data));
    } catch (IOException e) {
      failed = true;
    }
    check(failed, label);
  }

  public static void main(String[] args) throws Exception {
    AudioMath silence = new AudioMath();
    silence.add(new short[16000], 0, 16000);
    check(silence.dbfs() == -120, "Silence must have a finite floor");
    check(silence.zeroRatio() == 1, "Silence must be identified as zero samples");
    AudioMath loud = new AudioMath();
    loud.add(new short[] {-32768, 32767, 0, 0}, 0, 4);
    check(loud.peak == 32768, "Negative full scale must not overflow");
    check(loud.clipRatio() == .5, "Clipped samples must be counted");
    check(loud.zeroRatio() == .5, "Zero ratio must preserve mixed data");
    AudioMath offset = new AudioMath();
    offset.add(new short[] {100, 0, 0, 100}, 1, 2);
    check(offset.samples == 2 && offset.squares == 0, "PCM offsets must be respected");
    check(
        Arrays.equals(
            AudioMath.repeat(new short[] {1, 2, 3}, 7), new short[] {1, 2, 3, 1, 2, 3, 1}),
        "Reference must repeat to exact duration");
    boolean empty = false;
    try {
      AudioMath.repeat(new short[0], 10);
    } catch (IllegalArgumentException e) {
      empty = true;
    }
    check(empty, "Empty reference must be rejected");
    File file = File.createTempFile("cc8-wav-test-", ".wav");
    try {
      short[] samples = {0, -32768, -1, 1, 32767, 1024, -1024};
      WavCodec.write(file, samples, samples.length, 16000);
      byte[] bytes = Files.readAllBytes(file.toPath());
      WavCodec.Pcm pcm = WavCodec.read(new ByteArrayInputStream(bytes));
      check(pcm.sampleRate == 16000, "Sample rate must round trip");
      check(Arrays.equals(samples, pcm.samples), "Signed PCM must round trip");
      check(bytes.length == 44 + samples.length * 2, "WAV length must match sample count");
      byte[] stereo = bytes.clone();
      stereo[22] = 2;
      failRead(stereo, "Stereo reference must not be misread as mono");
      failRead(Arrays.copyOf(bytes, bytes.length - 1), "Truncated PCM must be rejected");
      byte[] invalid = bytes.clone();
      invalid[0] = 'X';
      failRead(invalid, "Non-RIFF files must be rejected");
      ByteArrayOutputStream withMetadata = new ByteArrayOutputStream();
      withMetadata.write(bytes, 0, 12);
      withMetadata.write(new byte[] {'J', 'U', 'N', 'K', 1, 0, 0, 0, 99, 0});
      withMetadata.write(bytes, 12, bytes.length - 12);
      byte[] metadata = withMetadata.toByteArray();
      int riffSize = metadata.length - 8;
      for (int i = 0; i < 4; i++) metadata[4 + i] = (byte) (riffSize >>> (i * 8));
      check(
          Arrays.equals(samples, WavCodec.read(new ByteArrayInputStream(metadata)).samples),
          "Odd metadata chunks and padding must be skipped correctly");
    } finally {
      file.delete();
    }
    ChannelMath stereo = new ChannelMath(2);
    short[] stereoPcm = {-32768, -32768, 32767, 32767, 0, 0, 100, 100};
    stereo.add(stereoPcm, 0, 3);
    check(stereo.frames == 1 && stereo.pendingSamples() == 1, "Incomplete frames must wait");
    stereo.add(stereoPcm, 3, 5);
    check(
        stereo.frames == 4 && stereo.pendingSamples() == 0,
        "Read boundaries must preserve channels");
    check(
        stereo.channels[0].samples == 4 && stereo.channels[1].peak == 32768,
        "Full-scale channel metrics");
    check(stereo.equalRatio(0, 1) == 1, "Duplicated channels must be reported");
    check(
        Math.abs(stereo.correlation(0, 1) - 1) < 1e-10,
        "Identical nonconstant channel correlation");
    ChannelMath quad = new ChannelMath(4);
    short[] quadPcm = {100, -100, 0, 10, -100, 100, 0, 20, 200, -200, 0, 30, -200, 200, 0, 40};
    for (int i = 0; i < quadPcm.length; i++) quad.add(quadPcm, i, 1);
    check(quad.frames == 4 && quad.channels[3].peak == 40, "Four-channel single-sample reads");
    check(
        Math.abs(quad.correlation(0, 1) + 1) < 1e-10, "Opposite polarity must not look identical");
    check(quad.equalRatio(0, 1) == 0, "Different channels must not look duplicated");
    check(quad.correlation(0, 2) == null, "Silent channel correlation must be unknown");
    check(quad.channels[2].zeroRatio() == 1, "Silent channel metrics");
    boolean invalidRange = false;
    try {
      quad.add(quadPcm, 1, quadPcm.length);
    } catch (IllegalArgumentException e) {
      invalidRange = true;
    }
    check(invalidRange && quad.frames == 4, "Invalid reads cannot partially change statistics");
    ChannelMath emptyChannels = new ChannelMath(2);
    check(emptyChannels.correlation(0, 1) == null, "No-frame correlation must be unknown");
    check(emptyChannels.equalRatio(0, 1) == 0, "No-frame counts cannot imply duplication");
    check(
        emptyChannels.meanPcm(0) == null && emptyChannels.acDbfs(0) == null,
        "No-frame DC metrics stay unknown");
    check(Math.abs(quad.meanPcm(3) - 25) < 1e-10, "Signed DC mean");
    check(
        Math.abs(quad.acDbfs(3) - 10 * Math.log10(125.0 / (32768.0 * 32768.0))) < 1e-10,
        "DC is excluded from AC power");
    referenceChecks();
    System.out.println("Host audio tests passed: " + count + " checks");
  }

  private static void referenceChecks() {
    Random random = new Random(20261107);
    short[] pattern = new short[4000], noise = new short[4000];
    for (int i = 0; i < pattern.length; i++) {
      pattern[i] = (short) (random.nextInt(10001) - 5000);
      noise[i] = (short) (random.nextInt(10001) - 5000);
    }
    short[] expected = new short[4000 * 16], input = new short[4000 * 48 * 4];
    for (int i = 0; i < expected.length; i++) expected[i] = pattern[i / 16];
    for (int i = 0; i < input.length / 4; i++) {
      int bin = i / 48;
      input[i * 4] = (short) (1000 + (bin < 23 ? 0 : pattern[bin - 23] * 2));
      input[i * 4 + 1] = (short) (500 - (bin + 31 >= 4000 ? 0 : pattern[bin + 31]));
      input[i * 4 + 2] = 1700;
      input[i * 4 + 3] = noise[bin];
    }
    ReferenceMatch reference = new ReferenceMatch(16000, 1);
    reference.add(expected, 0, expected.length);
    ReferenceMatch whole = new ReferenceMatch(48000, 4), split = new ReferenceMatch(48000, 4);
    whole.add(input, 0, input.length);
    for (int i = 0; i < input.length; i += 997)
      split.add(input, i, Math.min(997, input.length - i));
    check(
        whole.points() == 4000 && split.points() == 4000,
        "Cross-rate and split interleaved sampling");
    ReferenceMatch.Match delayed = whole.compare(0, reference),
        leading = whole.compare(1, reference);
    check(
        delayed != null && delayed.offset == 23 && Math.abs(delayed.correlation - 1) < 1e-10,
        "Delay, gain and DC must preserve a known reference");
    check(delayed.overlap == 3977, "Overlap excludes unmatched prefixes");
    check(
        leading != null && leading.offset == -31 && Math.abs(leading.correlation + 1) < 1e-10,
        "Leading and inverted reference stays signed");
    ReferenceMatch.Match partitioned = split.compare(0, reference);
    check(
        partitioned.offset == delayed.offset && partitioned.correlation == delayed.correlation,
        "Partial reads cannot change comparison");
    check(
        delayed.competingCorrelation != null && Math.abs(delayed.competingCorrelation) < .2,
        "Unrelated competing offsets are separate");
    check(whole.compare(2, reference) == null, "Constant DC is not a reference match");
    check(
        Math.abs(whole.compare(3, reference).correlation) < .2, "Independent noise does not match");
    ChannelMath dc = new ChannelMath(4);
    dc.add(input, 0, input.length);
    check(dc.meanPcm(2) == 1700 && dc.acDbfs(2) == -120, "DC-only channels have zero AC power");
    ReferenceMatch shortInput = new ReferenceMatch(1000, 1);
    shortInput.add(pattern, 0, 999);
    check(shortInput.compare(0, reference) == null, "Insufficient overlap remains unknown");
    ReferenceMatch rational = new ReferenceMatch(44100, 1);
    short[] twoSeconds = new short[88200];
    for (int i = 0; i < twoSeconds.length; i++)
      twoSeconds[i] = pattern[(int) ((long) i * 1000 / 44100)];
    rational.add(twoSeconds, 0, twoSeconds.length);
    check(rational.points() == 2000, "Rational resampling has no accumulated 44.1 kHz drift");
    ReferenceMatch.Match rationalMatch = rational.compare(0, reference);
    check(
        rationalMatch.offset == 0 && Math.abs(rationalMatch.correlation - 1) < 1e-10,
        "44.1 kHz matches the same millisecond sequence");
    short[] repeated = new short[2000];
    for (int i = 0; i < repeated.length; i++) repeated[i] = (short) (i % 20 < 10 ? -2000 : 2000);
    ReferenceMatch periodic = new ReferenceMatch(1000, 1);
    periodic.add(repeated, 0, repeated.length);
    ReferenceMatch.Match ambiguous = periodic.compare(0, periodic);
    check(
        ambiguous.competingCorrelation != null
            && Math.abs(Math.abs(ambiguous.correlation) - Math.abs(ambiguous.competingCorrelation))
                < 1e-10,
        "Periodic peaks remain visibly ambiguous");
    ReferenceMatch bounded = new ReferenceMatch(1000, 1);
    bounded.add(new short[7000], 0, 7000);
    bounded.add(pattern, 0, pattern.length);
    check(
        bounded.points() == ReferenceMatch.MAX_POINTS,
        "Comparison storage remains bounded after overflow");
    boolean rejected = false;
    try {
      split.add(input, 1, input.length);
    } catch (IllegalArgumentException e) {
      rejected = true;
    }
    check(rejected && split.points() == 4000, "Invalid ranges cannot change comparison state");
    rejected = false;
    try {
      new ReferenceMatch(48000, 5);
    } catch (IllegalArgumentException e) {
      rejected = true;
    }
    check(rejected, "Comparison layout has a fixed channel bound");
    Thread.currentThread().interrupt();
    try {
      check(
          whole.compare(0, reference) == null,
          "Interrupted comparison does not finish stale analysis");
    } finally {
      Thread.interrupted();
    }
  }
}
