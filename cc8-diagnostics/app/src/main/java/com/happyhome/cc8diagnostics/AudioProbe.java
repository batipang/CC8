package com.happyhome.cc8diagnostics;

import android.Manifest;
import android.annotation.SuppressLint;
import android.content.Context;
import android.content.pm.PackageManager;
import android.media.*;
import android.media.audiofx.AcousticEchoCanceler;
import android.os.Build;
import android.os.SystemClock;
import org.json.*;
import java.io.*;
import java.util.Locale;
import java.util.concurrent.*;
import java.util.concurrent.atomic.AtomicBoolean;

public final class AudioProbe {
  public interface Listener {
    void progress(String message, double dbfs, int elapsed, int total);

    void finished(JSONObject result, File capture);
  }

  private final Context context;
  private final Listener listener;
  private final ExecutorService executor = Executors.newSingleThreadExecutor();
  private final AtomicBoolean running = new AtomicBoolean(false);
  private final Object lock = new Object();
  private volatile boolean cancelled;
  private AudioRecord currentRecorder;
  private AudioTrack currentPlayer;
  private volatile File lastCapture;

  public AudioProbe(Context context, Listener listener) {
    this.context = context.getApplicationContext();
    this.listener = listener;
    File[] cached =
        this.context
            .getCacheDir()
            .listFiles((dir, name) -> name.matches("cc8-capture-[0-9]+\\.wav"));
    if (cached != null)
      for (File file : cached)
        if (file.isFile()
            && file.length() > 44
            && (lastCapture == null || file.lastModified() > lastCapture.lastModified()))
          lastCapture = file;
  }

  public boolean isRunning() {
    return running.get();
  }

  public File getLastCapture() {
    File file = lastCapture;
    return file != null && file.exists() ? file : null;
  }

  public boolean start(
      String mode, int source, int sampleRate, boolean aec, boolean communicationMode) {
    if (!running.compareAndSet(false, true)) return false;
    cancelled = false;
    executor.execute(() -> run(mode, source, sampleRate, aec, communicationMode));
    return true;
  }

  public boolean startChannels(int source, int sampleRate, int channels) {
    return startChannels(source, sampleRate, channels, false);
  }

  public boolean startChannels(int source, int sampleRate, int channels, boolean playReference) {
    if (channels != 2 && channels != 4)
      throw new IllegalArgumentException("Expected two or four channels");
    if (!running.compareAndSet(false, true)) return false;
    cancelled = false;
    executor.execute(() -> runChannels(source, sampleRate, channels, playReference));
    return true;
  }

  public boolean replay() {
    File file = lastCapture;
    if (file == null || !file.exists() || !running.compareAndSet(false, true)) return false;
    cancelled = false;
    executor.execute(() -> playCapture(file));
    return true;
  }

  public void cancel() {
    cancelled = true;
    synchronized (lock) {
      try {
        if (currentRecorder != null
            && currentRecorder.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING)
          currentRecorder.stop();
      } catch (RuntimeException ignored) {
      }
      try {
        if (currentPlayer != null && currentPlayer.getPlayState() == AudioTrack.PLAYSTATE_PLAYING)
          currentPlayer.stop();
      } catch (RuntimeException ignored) {
      }
    }
  }

  public void close() {
    cancel();
    executor.shutdownNow();
  }

  private void checkCancelled() throws InterruptedException {
    if (cancelled || Thread.currentThread().isInterrupted())
      throw new InterruptedException("User stopped test");
  }

  private void publish(String text, double level, int elapsed, int total) {
    listener.progress(text, level, elapsed, total);
  }

  private AudioTrack player(short[] pcm, int rate) throws IOException {
    if (pcm.length == 0) throw new IOException("Empty reference audio");
    AudioTrack track =
        new AudioTrack(
            AudioManager.STREAM_MUSIC,
            rate,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            pcm.length * 2,
            AudioTrack.MODE_STATIC);
    synchronized (lock) {
      currentPlayer = track;
    }
    try {
      // MODE_STATIC legitimately starts in STATE_NO_STATIC_DATA. It is
      // ready for playback only after a successful first write.
      if (track.getState() == AudioTrack.STATE_UNINITIALIZED)
        throw new IOException("AudioTrack creation failed (state=" + track.getState() + ")");
      int written = track.write(pcm, 0, pcm.length);
      if (written != pcm.length) throw new IOException("AudioTrack write failed: " + written);
      if (track.getState() != AudioTrack.STATE_INITIALIZED)
        throw new IOException(
            "AudioTrack not ready after PCM write (state=" + track.getState() + ")");
      track.setVolume(.35f);
      return track;
    } catch (IOException | RuntimeException e) {
      synchronized (lock) {
        if (currentPlayer == track) currentPlayer = null;
      }
      try {
        track.release();
      } catch (RuntimeException ignored) {
      }
      throw e;
    }
  }

  private void release(AudioRecord record, AudioTrack track, AcousticEchoCanceler effect) {
    synchronized (lock) {
      currentRecorder = null;
      currentPlayer = null;
    }
    if (effect != null)
      try {
        effect.release();
      } catch (RuntimeException ignored) {
      }
    if (record != null) {
      try {
        record.stop();
      } catch (RuntimeException ignored) {
      }
      try {
        record.release();
      } catch (RuntimeException ignored) {
      }
    }
    if (track != null) {
      try {
        track.stop();
      } catch (RuntimeException ignored) {
      }
      try {
        track.release();
      } catch (RuntimeException ignored) {
      }
    }
  }

  private JSONObject metrics(AudioMath math, int rate) {
    return DeviceReport.object(
        "frames",
        math.samples,
        "sample_duration_ms",
        Math.round(math.samples * 1000.0 / rate),
        "rms_dbfs",
        math.dbfs(),
        "peak_dbfs",
        math.peakDbfs(),
        "zero_ratio",
        math.zeroRatio(),
        "clipped_ratio",
        math.clipRatio());
  }

  @SuppressLint(
      "MissingPermission") // Rechecked immediately below; the Activity also requests the foreground
  // permission.
  private void run(
      String mode, int source, int rate, boolean requestAec, boolean requestCommunicationMode) {
    boolean capture = !"playback".equals(mode), duplex = "duplex".equals(mode);
    int duration = duplex ? 10000 : 6000;
    JSONObject result =
        DeviceReport.object(
            "type",
            mode,
            "started_at",
            DeviceReport.timestamp(),
            "requested_sample_rate",
            rate,
            "requested_source",
            source,
            "aec_requested",
            requestAec,
            "communication_mode_requested",
            requestCommunicationMode,
            "reference_stream",
            "STREAM_MUSIC",
            "reference_volume",
            .35);
    AudioRecord record = null;
    AudioTrack track = null;
    AcousticEchoCanceler effect = null;
    AudioManager manager = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
    int previousMode = -1;
    boolean modeChanged = false;
    AudioMath all = new AudioMath(), quiet = new AudioMath(), doubleTalk = new AudioMath();
    short[] saved = new short[rate * (duration / 1000 + 1)];
    int savedCount = 0;
    long started = SystemClock.elapsedRealtime(), maxReadGap = 0, lastRead = started, lastUi = 0;
    File completed = null;
    String failure = null;
    boolean aborted = false;
    try {
      checkCancelled();
      previousMode = manager.getMode();
      if (capture
          && context.checkCallingOrSelfPermission(Manifest.permission.RECORD_AUDIO)
              != PackageManager.PERMISSION_GRANTED)
        throw new SecurityException("Microphone permission not granted");
      if (requestCommunicationMode) {
        manager.setMode(AudioManager.MODE_IN_COMMUNICATION);
        modeChanged = true;
      }
      DeviceReport.put(result, "actual_audio_mode", manager.getMode());
      DeviceReport.put(result, "microphone_mute_flag", manager.isMicrophoneMute());
      DeviceReport.put(
          result, "media_stream_volume", manager.getStreamVolume(AudioManager.STREAM_MUSIC));
      DeviceReport.put(result, "battery_before", DeviceReport.battery(context));
      if (!capture || duplex) {
        WavCodec.Pcm reference;
        try (InputStream input = context.getAssets().open("reference.wav")) {
          reference = WavCodec.read(input);
        }
        short[] repeated =
            AudioMath.repeat(reference.samples, reference.sampleRate * (duration / 1000));
        track = player(repeated, reference.sampleRate);
        DeviceReport.put(result, "playback_state_after_write", track.getState());
        DeviceReport.put(result, "playback_mode", "MODE_STATIC");
        DeviceReport.put(result, "playback_sample_rate", reference.sampleRate);
        DeviceReport.put(result, "playback_frames_written", repeated.length);
      }
      if (capture) {
        int minimum =
            AudioRecord.getMinBufferSize(
                rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
        DeviceReport.put(result, "minimum_buffer_bytes", minimum);
        if (minimum <= 0) throw new IOException("Unsupported recording buffer: " + minimum);
        record =
            new AudioRecord(
                source,
                rate,
                AudioFormat.CHANNEL_IN_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                Math.max(minimum * 2, rate / 5 * 2));
        synchronized (lock) {
          currentRecorder = record;
        }
        if (record.getState() != AudioRecord.STATE_INITIALIZED)
          throw new IOException("AudioRecord initialization failed");
        DeviceReport.put(result, "actual_sample_rate", record.getSampleRate());
        DeviceReport.put(result, "channel_count", record.getChannelCount());
        DeviceReport.put(result, "audio_session_id", record.getAudioSessionId());
        boolean available = AcousticEchoCanceler.isAvailable();
        JSONObject aec =
            DeviceReport.object(
                "api_available", available, "created", false, "requested_enabled", requestAec);
        if (available) {
          try {
            effect = AcousticEchoCanceler.create(record.getAudioSessionId());
            DeviceReport.put(aec, "created", effect != null);
            if (effect != null) {
              DeviceReport.put(aec, "has_control", effect.hasControl());
              DeviceReport.put(aec, "initial_enabled", effect.getEnabled());
              DeviceReport.put(aec, "set_enabled_result", effect.setEnabled(requestAec));
              DeviceReport.put(aec, "actual_enabled", effect.getEnabled());
            }
          } catch (RuntimeException e) {
            DeviceReport.put(aec, "error", e.getClass().getSimpleName() + ": " + e.getMessage());
          }
        }
        DeviceReport.put(aec, "interpretation", "创建/启用成功不代表回声已有效消除；原厂 DSP 也可能已预处理输入。");
        DeviceReport.put(result, "aec", aec);
        checkCancelled();
        record.startRecording();
        if (record.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING)
          throw new IOException("Recording did not start");
      }
      checkCancelled();
      if (track != null) track.play();
      started = SystemClock.elapsedRealtime();
      lastRead = started;
      short[] buffer = new short[Math.max(160, rate / 50)];
      while (SystemClock.elapsedRealtime() - started < duration) {
        checkCancelled();
        if (record != null) {
          int count = record.read(buffer, 0, buffer.length);
          if (count < 0) {
            checkCancelled();
            throw new IOException("AudioRecord read error: " + count);
          }
          if (count == 0) {
            Thread.sleep(10);
            continue;
          }
          long now = SystemClock.elapsedRealtime();
          maxReadGap = Math.max(maxReadGap, now - lastRead);
          lastRead = now;
          long before = all.samples;
          all.add(buffer, 0, count);
          int quietCount = (int) Math.min(count, Math.max(0, (long) rate * 3 - before));
          if (quietCount > 0) quiet.add(buffer, 0, quietCount);
          if (count > quietCount) doubleTalk.add(buffer, quietCount, count - quietCount);
          int keep = Math.min(count, saved.length - savedCount);
          if (keep > 0) {
            System.arraycopy(buffer, 0, saved, savedCount, keep);
            savedCount += keep;
          }
          if (now - lastUi >= 180) {
            AudioMath current = new AudioMath();
            current.add(buffer, 0, count);
            int elapsed = (int) (now - started);
            publish(
                duplex ? (elapsed < 3000 ? "安静阶段：先不要说话" : "双讲阶段：边听边说“一二三，测试”") : "录音中：请说“一二三，测试”",
                current.dbfs(),
                elapsed,
                duration);
            lastUi = now;
          }
        } else {
          int elapsed = (int) (SystemClock.elapsedRealtime() - started);
          publish("播放中：请确认是否听到中文参考音", -120, elapsed, duration);
          Thread.sleep(100);
        }
      }
      if (record != null) {
        if (Build.VERSION.SDK_INT >= 24) {
          final AudioRecord recorded = record;
          DeviceReport.put(
              result,
              "capture_timestamp",
              DeviceReport.safe(
                  () -> {
                    AudioTimestamp timestamp = new AudioTimestamp();
                    int code = recorded.getTimestamp(timestamp, AudioTimestamp.TIMEBASE_MONOTONIC);
                    return DeviceReport.object(
                        "result",
                        code,
                        "frames",
                        timestamp.framePosition,
                        "nano_time",
                        timestamp.nanoTime);
                  }));
        }
        DeviceReport.put(result, "capture", metrics(all, rate));
        DeviceReport.put(result, "first_3_seconds", metrics(quiet, rate));
        DeviceReport.put(result, "after_3_seconds", metrics(doubleTalk, rate));
        DeviceReport.put(result, "max_read_gap_ms", maxReadGap);
      }
      if (track != null)
        DeviceReport.put(
            result, "playback_head_frames", track.getPlaybackHeadPosition() & 0xffffffffL);
    } catch (InterruptedException e) {
      cancelled = true;
      aborted = true;
    } catch (Exception | LinkageError e) {
      failure = e.getClass().getSimpleName() + ": " + e.getMessage();
    } finally {
      aborted = aborted || cancelled;
      long elapsed = SystemClock.elapsedRealtime() - started;
      release(record, track, effect);
      if (modeChanged)
        try {
          if (manager.getMode() == AudioManager.MODE_IN_COMMUNICATION)
            manager.setMode(previousMode);
        } catch (RuntimeException ignored) {
        }
      if (capture) {
        DeviceReport.put(result, "capture", metrics(all, rate));
        DeviceReport.put(result, "first_3_seconds", metrics(quiet, rate));
        DeviceReport.put(result, "after_3_seconds", metrics(doubleTalk, rate));
        DeviceReport.put(result, "max_read_gap_ms", maxReadGap);
      }
      if (!aborted && failure == null && capture && savedCount > rate / 2) {
        try {
          File file =
              new File(context.getCacheDir(), "cc8-capture-" + System.currentTimeMillis() + ".wav");
          WavCodec.write(file, saved, savedCount, rate);
          File previous = lastCapture;
          lastCapture = file;
          completed = file;
          if (previous != null && !previous.equals(file)) previous.delete();
        } catch (IOException e) {
          DeviceReport.put(result, "capture_save_error", e.getMessage());
        }
      }
      DeviceReport.put(result, "elapsed_ms", elapsed);
      DeviceReport.put(result, "cancelled", aborted);
      DeviceReport.put(
          result, "status", aborted ? "cancelled" : failure == null ? "completed" : "error");
      if (failure != null) DeviceReport.put(result, "error", failure);
      if (capture && all.samples == 0)
        DeviceReport.put(result, "signal_note", "没有有效采样，不能判定麦克风或全双工可用。");
      else if (capture && all.zeroRatio() > .99)
        DeviceReport.put(result, "signal_note", "超过 99% 采样为零：请检查硬件静音、权限与原厂助手占用；不自动归因。");
      else if (capture)
        DeviceReport.put(result, "signal_note", "收到非零采样不等于识别人声成功；请回放并对比 AEC 开关、音量与原厂服务状态。");
      DeviceReport.put(result, "battery_after", DeviceReport.battery(context));
      running.set(false);
      listener.finished(result, completed);
    }
  }

  @SuppressLint("MissingPermission") // Rechecked before constructing the recorder.
  private void runChannels(int source, int rate, int channels, boolean playReference) {
    JSONObject result =
        DeviceReport.object(
            "type",
            "channel_capture",
            "started_at",
            DeviceReport.timestamp(),
            "requested_sample_rate",
            rate,
            "requested_source",
            source,
            "requested_channels",
            channels,
            "reference_playback_requested",
            playReference,
            "raw_audio_saved",
            false);
    AudioRecord record = null;
    AudioTrack track = null;
    ChannelMath math = new ChannelMath(channels);
    long started = SystemClock.elapsedRealtime(), lastUi = started;
    boolean aborted = false;
    String error = null;
    try {
      checkCancelled();
      if (Build.VERSION.SDK_INT < 23)
        throw new UnsupportedOperationException("Channel statistics require Android 6 or newer");
      if (context.checkCallingOrSelfPermission(Manifest.permission.RECORD_AUDIO)
          != PackageManager.PERMISSION_GRANTED)
        throw new SecurityException("Microphone permission not granted");
      int minimum =
          AudioRecord.getMinBufferSize(
              rate, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
      if (minimum <= 0) throw new IOException("Unsupported recording buffer");
      AudioFormat.Builder format =
          new AudioFormat.Builder().setEncoding(AudioFormat.ENCODING_PCM_16BIT).setSampleRate(rate);
      if (channels == 2) format.setChannelMask(AudioFormat.CHANNEL_IN_STEREO);
      else format.setChannelIndexMask((1 << channels) - 1);
      record =
          new AudioRecord.Builder()
              .setAudioSource(source)
              .setAudioFormat(format.build())
              .setBufferSizeInBytes(Math.max(minimum * channels * 2, rate * channels * 2 / 5))
              .build();
      synchronized (lock) {
        currentRecorder = record;
      }
      if (record.getState() != AudioRecord.STATE_INITIALIZED
          || record.getChannelCount() != channels)
        throw new IOException("Requested channel layout unavailable");
      DeviceReport.put(result, "actual_sample_rate", record.getSampleRate());
      DeviceReport.put(result, "actual_channels", record.getChannelCount());
      DeviceReport.put(result, "channel_index_mask", record.getFormat().getChannelIndexMask());
      DeviceReport.put(result, "channel_mask", record.getFormat().getChannelMask());
      if (playReference) {
        WavCodec.Pcm reference;
        try (InputStream input = context.getAssets().open("reference.wav")) {
          reference = WavCodec.read(input);
        }
        track =
            player(
                AudioMath.repeat(reference.samples, reference.sampleRate * 6),
                reference.sampleRate);
        DeviceReport.put(result, "reference_sample_rate", reference.sampleRate);
        DeviceReport.put(result, "reference_frames_written", reference.sampleRate * 6);
      }
      checkCancelled();
      record.startRecording();
      if (record.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING)
        throw new IOException("Recording did not start");
      if (track != null) track.play();
      started = SystemClock.elapsedRealtime();
      short[] buffer = new short[Math.max(160, rate / 100) * channels];
      while (SystemClock.elapsedRealtime() - started < 6000) {
        checkCancelled();
        int count = record.read(buffer, 0, buffer.length, AudioRecord.READ_NON_BLOCKING);
        if (count < 0) throw new IOException("Channel read failed: " + count);
        math.add(buffer, 0, count);
        if (count == 0) Thread.sleep(5);
        long now = SystemClock.elapsedRealtime();
        if (now - lastUi >= 180) {
          publish("多声道检测中，仅保存统计数字", math.channels[0].dbfs(), (int) (now - started), 6000);
          lastUi = now;
        }
      }
      if (math.frames < rate / 2) throw new IOException("Insufficient channel frames");
    } catch (InterruptedException e) {
      aborted = true;
    } catch (Exception | LinkageError e) {
      error = e.getClass().getSimpleName() + ": " + e.getMessage();
    } finally {
      aborted |= cancelled;
      if (track != null) {
        final AudioTrack reference = track;
        DeviceReport.put(
            result,
            "reference_playback_head_frames",
            DeviceReport.safe(() -> reference.getPlaybackHeadPosition() & 0xffffffffL));
      }
      release(record, track, null);
      JSONArray levels = new JSONArray(), pairs = new JSONArray();
      for (AudioMath channel : math.channels) levels.put(metrics(channel, rate));
      for (int i = 0; i < channels; i++)
        for (int j = i + 1; j < channels; j++)
          pairs.put(
              DeviceReport.object(
                  "first",
                  i,
                  "second",
                  j,
                  "correlation",
                  math.correlation(i, j),
                  "identical_sample_ratio",
                  math.equalRatio(i, j)));
      DeviceReport.put(result, "channel_metrics", levels);
      DeviceReport.put(result, "channel_pairs", pairs);
      DeviceReport.put(result, "frames", math.frames);
      DeviceReport.put(result, "trailing_samples", math.pendingSamples());
      DeviceReport.put(result, "elapsed_ms", SystemClock.elapsedRealtime() - started);
      DeviceReport.put(result, "cancelled", aborted);
      DeviceReport.put(
          result, "status", aborted ? "cancelled" : error == null ? "completed" : "error");
      if (error != null) DeviceReport.put(result, "error", error);
      DeviceReport.put(result, "signal_note", "通道差异需结合声源对照，声道数量不能证明麦克风阵列可用于定位。");
      running.set(false);
      listener.finished(result, null);
    }
  }

  private void playCapture(File file) {
    JSONObject result =
        DeviceReport.object("type", "replay_capture", "started_at", DeviceReport.timestamp());
    AudioTrack track = null;
    String error = null;
    try (InputStream input = new FileInputStream(file)) {
      WavCodec.Pcm pcm = WavCodec.read(input);
      checkCancelled();
      track = player(pcm.samples, pcm.sampleRate);
      checkCancelled();
      track.play();
      long started = SystemClock.elapsedRealtime();
      int duration = (int) (pcm.samples.length * 1000L / pcm.sampleRate);
      while (SystemClock.elapsedRealtime() - started < duration) {
        checkCancelled();
        publish(
            "回放最近录音：请用耳朵比较人声与扬声器回声",
            -120,
            (int) (SystemClock.elapsedRealtime() - started),
            duration);
        Thread.sleep(100);
      }
      DeviceReport.put(
          result, "playback_head_frames", track.getPlaybackHeadPosition() & 0xffffffffL);
    } catch (InterruptedException e) {
      cancelled = true;
    } catch (Exception e) {
      error = e.getClass().getSimpleName() + ": " + e.getMessage();
    } finally {
      release(null, track, null);
      DeviceReport.put(result, "cancelled", cancelled);
      DeviceReport.put(
          result, "status", cancelled ? "cancelled" : error == null ? "completed" : "error");
      if (error != null) DeviceReport.put(result, "error", error);
      running.set(false);
      listener.finished(result, null);
    }
  }
}
