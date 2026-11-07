package com.happyhome.cc8assistant;

import android.annotation.SuppressLint;
import android.media.AudioFormat;
import android.media.AudioRecord;
import android.media.MediaRecorder;
import android.os.Build;
import java.io.IOException;
import java.util.Arrays;

/** One initialized recorder and its bounded conversion history. Starting/stopping
 * remains with VoiceSession's existing capture owner and watchdog.
 */
final class CaptureAudioInput {
  final AudioRecord recorder;
  final boolean paired;
  final String fallbackReason;
  private final short[] input;
  private final short[] pending;
  private final PairedCapturePcm converter;
  private int pendingCount;

  private CaptureAudioInput(AudioRecord recorder, boolean paired, String fallback) {
    this.recorder = recorder;
    this.paired = paired;
    fallbackReason = fallback;
    input = new short[paired ? 3840 : 320];
    pending = paired ? new short[1280] : null;
    converter = paired ? new PairedCapturePcm() : null;
  }

  /** An unsupported paired format falls back before the native recorder starts. */
  @SuppressLint("MissingPermission") // Authorized foreground worker checks RECORD_AUDIO first.
  static CaptureAudioInput open(boolean preferPaired) throws IOException {
    String fallback = preferPaired ? "platform_api" : null;
    if (preferPaired && Build.VERSION.SDK_INT >= 23) {
      AudioRecord candidate = null;
      try {
        int minimum = minimum(48000);
        AudioFormat format =
            new AudioFormat.Builder()
                .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                .setSampleRate(48000)
                .setChannelIndexMask(15)
                .build();
        candidate =
            new AudioRecord.Builder()
                .setAudioSource(MediaRecorder.AudioSource.VOICE_COMMUNICATION)
                .setAudioFormat(format)
                .setBufferSizeInBytes(Math.max(minimum * 8, 76800))
                .build();
        verify(candidate, 48000, 4);
        CaptureAudioInput result = new CaptureAudioInput(candidate, true, null);
        candidate = null;
        return result;
      } catch (IOException | RuntimeException | LinkageError unavailable) {
        fallback = "paired_format_unavailable";
      } finally {
        releaseFailed(candidate);
      }
    }
    AudioRecord candidate = null;
    try {
      int minimum = minimum(16000);
      candidate =
          new AudioRecord(
              MediaRecorder.AudioSource.VOICE_COMMUNICATION,
              16000,
              AudioFormat.CHANNEL_IN_MONO,
              AudioFormat.ENCODING_PCM_16BIT,
              Math.max(minimum * 2, 6400));
      verify(candidate, 16000, 1);
      CaptureAudioInput result = new CaptureAudioInput(candidate, false, fallback);
      candidate = null;
      return result;
    } finally {
      releaseFailed(candidate);
    }
  }

  private static int minimum(int rate) throws IOException {
    int bytes =
        AudioRecord.getMinBufferSize(
            rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
    if (bytes <= 0 || bytes > 1048576) throw new IOException("麦克风缓冲暂不可用");
    return bytes;
  }

  private static void verify(AudioRecord record, int rate, int channels) throws IOException {
    if (record == null
        || record.getState() != AudioRecord.STATE_INITIALIZED
        || record.getSampleRate() != rate
        || record.getChannelCount() != channels
        || record.getAudioFormat() != AudioFormat.ENCODING_PCM_16BIT)
      throw new IOException("麦克风格式暂不可用");
  }

  private static void releaseFailed(AudioRecord record) throws IOException {
    if (record != null)
      try {
        record.release();
      } catch (RuntimeException failure) {
        throw new IOException("麦克风释放未确认，请重新打开助手");
      }
  }

  static final class Chunk {
    final short[] samples;
    final int frames;
    final boolean paired;

    Chunk(short[] samples, boolean paired) {
      this.samples = samples;
      this.paired = paired;
      frames = samples.length / (paired ? 2 : 1);
    }

    float level() {
      double power = 0;
      int stride = paired ? 2 : 1;
      for (int index = 0; index < samples.length; index += stride)
        power += (double) samples[index] * samples[index];
      return frames == 0 ? 0 : (float) Math.sqrt(power / frames) / 32768;
    }

    void clear() {
      Arrays.fill(samples, (short) 0);
    }
  }

  /** Emit complete paired 20 ms packets even when AudioRecord splits raw frames. */
  Chunk read() throws IOException {
    short[] converted = null;
    try {
      int count = recorder.read(input, 0, input.length);
      if (count < 0 || count > input.length) throw new IOException("麦克风数据读取失败");
      if (count == 0) return null;
      if (!paired) return new Chunk(Arrays.copyOf(input, count), false);
      converted = converter.push(input, 0, count);
      if (pendingCount + converted.length > pending.length) throw new IOException("麦克风数据处理失败，请重试");
      System.arraycopy(converted, 0, pending, pendingCount, converted.length);
      pendingCount += converted.length;
      if (pendingCount < 640) return null;
      short[] packet = Arrays.copyOf(pending, 640);
      pendingCount -= 640;
      System.arraycopy(pending, 640, pending, 0, pendingCount);
      Arrays.fill(pending, pendingCount, pending.length, (short) 0);
      return new Chunk(packet, true);
    } finally {
      Arrays.fill(input, (short) 0);
      if (converted != null) Arrays.fill(converted, (short) 0);
    }
  }

  /** The capture worker releases recorder itself; this only erases owned samples. */
  void clear() {
    Arrays.fill(input, (short) 0);
    if (pending != null) Arrays.fill(pending, (short) 0);
    if (converter != null) converter.clear();
    pendingCount = 0;
  }
}
