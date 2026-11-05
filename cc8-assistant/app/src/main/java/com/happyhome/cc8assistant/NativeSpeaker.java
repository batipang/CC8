package com.happyhome.cc8assistant;

import android.media.AudioFormat;
import android.media.AudioManager;
import android.media.AudioTrack;
import android.os.Build;
import java.io.IOException;

/** Sole native-track adapter. Players may read its clock, but only SpeakerSession controls it. */
final class NativeSpeaker implements SpeakerSession.Device {
  final AudioTrack track;
  private final int configuredBufferFrames;

  NativeSpeaker() throws IOException {
    int min =
        AudioTrack.getMinBufferSize(
            16000, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT);
    if (min <= 0 || min > 32000) throw new IOException("不支持 16 kHz 播放");
    int bytes = Math.max(min * 2, 3200);
    configuredBufferFrames = bytes / 2;
    AudioTrack opened =
        new AudioTrack(
            AudioManager.STREAM_MUSIC,
            16000,
            AudioFormat.CHANNEL_OUT_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            bytes,
            AudioTrack.MODE_STREAM);
    try {
      if (opened.getState() != AudioTrack.STATE_INITIALIZED) throw new IOException("播放初始化失败");
      if (opened.setVolume(.35f) != 0) throw new IOException("无法设置播放音量");
      track = opened;
    } catch (IOException | RuntimeException error) {
      opened.release();
      throw error;
    }
  }

  public void play() {
    track.play();
  }

  public void pause() {
    track.pause();
  }

  public void flush() {
    track.flush();
  }

  public int write(short[] samples, int offset, int count) {
    // A bounded block also preserves short-write handling on the legacy API fallback.
    int amount = Math.min(320, count);
    return Build.VERSION.SDK_INT >= 23
        ? track.write(samples, offset, amount, AudioTrack.WRITE_NON_BLOCKING)
        : track.write(samples, offset, amount);
  }

  int bufferFrames() {
    return Build.VERSION.SDK_INT >= 23 ? track.getBufferSizeInFrames() : configuredBufferFrames;
  }

  public void release() {
    track.release();
  }
}
