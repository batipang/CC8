package com.happyhome.cc8assistant;

import android.Manifest;
import android.annotation.SuppressLint;
import android.content.Context;
import android.content.BroadcastReceiver;
import android.content.Intent;
import android.content.IntentFilter;
import android.content.pm.PackageManager;
import android.media.*;
import android.os.SystemClock;
import android.os.Build;
import java.io.*;
import java.net.*;
import java.util.concurrent.*;
import org.json.JSONObject;
import org.json.JSONArray;

final class VoiceSession {
  // 设备端资源所有者：前台期间维护双通道、AudioRecord 与 AudioTrack，退出即释放。
  // lifecycle 隔离前后台连接任务，epoch 隔离收音线程，generation 隔离回答/字幕。
  interface Listener {
    void link(boolean connected, long route);

    void state(String phase, String message);

    void caption(String text, String mood);

    void microphone(boolean active);

    void playback(boolean active);

    void heard(String text);

    void sources(JSONArray items, String retrievedAt);

    void brain(String provider);

    void recognition(String engine);

    void dotResult(String text, boolean truncated, String sourceTitle);

    void dotChats(JSONObject data);

    void savedTasks(JSONObject data);

    void screen(boolean sleeping);

    void vision(boolean active, String message, boolean preserveError);

    void cameraCapture(JSONObject request);

    void announcement();

    void workspace(JSONObject data, long route);

    void workspaceResult(boolean ok, String message, long route);
  }

  private final Context context;
  private final Listener listener;
  private final Object routeLock = new Object();
  private final Object captureResourceLock = new Object();
  private final LanConnector lan;
  private volatile Link activeLink, usbLink, wifiLink;
  private volatile String wifiError = "";
  private boolean receiverRegistered;
  private volatile boolean usbPhysical, wakeMode = true;
  private final DesktopCameraAuth desktopCameraAuth = new DesktopCameraAuth();
  private volatile boolean visual;
  private volatile int visualGeneration;
  private volatile String pendingVisionNonce = "", visionRequestId = "", desktopVisionNonce = "";
  private volatile boolean foreground, recording, announcement;
  private volatile int announcementGeneration;
  private volatile ServerSocket server;
  private volatile Socket socket;
  private volatile DataOutputStream output;
  private volatile AudioRecord recorder;
  private volatile CaptureWindow captureWindow;
  private volatile Player player;
  private volatile long lastPlaybackSpeechFrames;
  private volatile int lastPlaybackPaddingFrames;
  private volatile float inputLevel = 0;
  private volatile CaptionProgress lastCaption = CaptionProgress.empty();
  private volatile boolean textSession;
  private volatile int textGeneration;
  private volatile int epoch = 0, minimumGeneration = 0, lifecycle = 0;
  // UI ownership follows each activation, including reselecting the same Link.
  private volatile long routeEpoch;
  private volatile long micFrames = 0, renderFrames = 0;
  private volatile String phase = "idle";
  private final StatusFeedback statusFeedback = new StatusFeedback();
  private volatile boolean dotChatSupported, savedTasksSupported;

  boolean supportsDotChatHandoff() {
    return dotChatSupported;
  }

  boolean supportsSavedTaskResults() {
    return savedTasksSupported;
  }

  VoiceSession(Context context, Listener listener) {
    this.context = context.getApplicationContext();
    this.listener = listener;
    this.lan = new LanConnector(this.context);
  }

  boolean isRecording() {
    return recording;
  }

  boolean isConnected() {
    Link link = activeLink;
    return link != null && !link.closed;
  }

  boolean currentRoute(long route) {
    return route == routeEpoch;
  }

  long connectedRoute() {
    synchronized (routeLock) {
      return foreground && activeLink != null && activeLink.ready && !activeLink.closed
          ? routeEpoch
          : -1;
    }
  }

  private long routeFor(Link source) {
    synchronized (routeLock) {
      return foreground && source == activeLink ? routeEpoch : -1;
    }
  }

  String transportName() {
    Link link = activeLink;
    return link == null ? "none" : link.kind;
  }

  boolean isVisual() {
    return visual || !pendingVisionNonce.isEmpty();
  }

  boolean wifiPaired() {
    return lan.paired();
  }

  private final BroadcastReceiver usbState =
      new BroadcastReceiver() {
        public void onReceive(Context ignored, Intent event) {
          if ("android.hardware.usb.action.USB_STATE".equals(event.getAction())) {
            usbPhysical = event.getBooleanExtra("connected", false);
            Link link = usbLink;
            if (!usbPhysical && link != null) link.close();
          } else if (!lan.networkAvailable()) {
            Link link = wifiLink;
            if (link != null) link.close();
          }
        }
      };

  void resume() {
    final int ticket;
    synchronized (routeLock) {
      if (foreground) return;
      foreground = true;
      ticket = ++lifecycle;
    }
    if (!receiverRegistered) {
      IntentFilter filter = new IntentFilter("android.hardware.usb.action.USB_STATE");
      filter.addAction("android.net.conn.CONNECTIVITY_CHANGE");
      context.registerReceiver(usbState, filter);
      receiverRegistered = true;
    }
    if (lan.paired()) lan.acquire();
    new Thread(() -> accept(ticket), "cc8-usb-listener").start();
    new Thread(() -> wifiLoop(ticket), "cc8-wifi-link").start();
  }

  void pause() {
    // 先使生命周期票据失效，再停音频和关闭连接；旧异步任务不能在后台重建资源。
    cancelVision();
    textSession = false;
    ServerSocket listener;
    synchronized (routeLock) {
      foreground = false;
      lifecycle++;
      routeEpoch++;
      listener = server;
      server = null;
    }
    lan.cancelPending();
    stopMicrophone();
    cancelPlayback();
    closeSocket();
    if (listener != null)
      try {
        listener.close();
      } catch (IOException ignored) {
      }
    if (receiverRegistered) {
      context.unregisterReceiver(usbState);
      receiverRegistered = false;
    }
    lan.release();
  }

  private void closeSocket() {
    final Link usb, wifi;
    final long route;
    synchronized (routeLock) {
      usb = usbLink;
      wifi = wifiLink;
      activeLink = null;
      route = routeEpoch;
      dotChatSupported = savedTasksSupported = false;
      output = null;
      socket = null;
    }
    if (usb != null) usb.close();
    if (wifi != null) wifi.close();
    listener.link(false, route);
  }

  private void accept(int ticket) {
    try (ServerSocket ss = new ServerSocket()) {
      synchronized (routeLock) {
        if (!foreground || lifecycle != ticket) return;
        ss.setReuseAddress(true);
        ss.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), 18765), 1);
        server = ss;
      }
      while (foreground && lifecycle == ticket) {
        Socket connection = ss.accept();
        if (!foreground || lifecycle != ticket) {
          connection.close();
          break;
        }
        Link old = usbLink;
        if (old != null && !old.closed) {
          connection.close();
          continue;
        }
        Link next = new Link(connection, "usb", ticket);
        usbLink = next;
        new Thread(next, "cc8-usb-link").start();
      }
    } catch (IOException error) {
      if (foreground && lifecycle == ticket) listener.state("idle", "USB 通道等待重连，Wi-Fi 仍可连接");
    } finally {
      if (lifecycle == ticket) server = null;
    }
  }

  private void wifiLoop(int ticket) {
    while (foreground && lifecycle == ticket) {
      if (lan.paired() && lan.networkAvailable())
        try {
          Socket connection = lan.connect(() -> foreground && lifecycle == ticket);
          Link next;
          synchronized (routeLock) {
            if (!foreground || lifecycle != ticket) {
              connection.close();
              break;
            }
            next = new Link(connection, "wifi", ticket);
            wifiLink = next;
            wifiError = "";
          }
          next.run();
        } catch (Exception error) {
          if (foreground && lifecycle == ticket) wifiError = error.getClass().getSimpleName();
        }
      try {
        Thread.sleep(1000);
      } catch (InterruptedException error) {
        Thread.currentThread().interrupt();
        break;
      }
    }
  }

  private void select(Link next) throws IOException {
    // routeLock 保证只选一路；切换可以保留已开启的收音，但必须丢弃旧回答和看图。
    synchronized (routeLock) {
      if (!foreground || next.closed || next.ticket != lifecycle) return;
      next.ready = true;
      boolean changed = activeLink != next;
      boolean captureWasActive = recording;
      int captureEpoch = epoch;
      if (changed) {
        // A reused USB Link after Wi-Fi is still a new activation. Invalidate
        // queued workspace callbacks before publishing this route's ready state.
        routeEpoch++;
        desktopCameraAuth.reset();
        if (visual) {
          visual = false;
          listener.vision(false, "连接切换，请重新看图", false);
        }
        cancelPlayback();
        announcement = false;
        textSession = false;
        listener.playback(false);
        listener.caption("", "attentive");
        minimumGeneration = 0;
        statusFeedback.reset();
        activeLink = next;
        dotChatSupported = savedTasksSupported = false;
        socket = next.connection;
        output = next.out;
        phase = recording ? "listening" : "idle";
      }
      JSONObject hello = Wire.event("device_ready");
      Wire.put(hello, "supports_playback_pause", true);
      Wire.put(hello, "playout_receipt_version", 1);
      Wire.put(hello, "render_clock_report_version", 1);
      Wire.put(hello, "playback_done_id_version", 1);
      Wire.put(hello, "desktop_camera_version", 1);
      Wire.put(hello, "desktop_camera_challenge", desktopCameraAuth.challenge());
      Wire.put(hello, "protocol", BuildConfig.CC8_PROTOCOL);
      Wire.put(hello, "sample_rate", 16000);
      Wire.put(hello, "capture_active", captureWasActive);
      Wire.put(hello, "wake_mode", wakeMode);
      next.event(hello);
      if (captureWasActive) {
        JSONObject rebound = Wire.event("session_rebind");
        Wire.put(rebound, "capture_active", true);
        Wire.put(rebound, "epoch", captureEpoch);
        Wire.put(rebound, "source", 7);
        Wire.put(rebound, "sample_rate", 16000);
        Wire.put(rebound, "render_gain", .35);
        next.event(rebound);
      }
      // Ready/rebind must enter the wire before a UI callback can start capture.
      if (changed) listener.link(true, routeEpoch);
    }
  }

  private void lost(Link link) {
    synchronized (routeLock) {
      if (usbLink == link) usbLink = null;
      if (wifiLink == link) wifiLink = null;
      if (activeLink != link) return;
      activeLink = null;
      routeEpoch++;
      dotChatSupported = savedTasksSupported = false;
      socket = null;
      output = null;
      Link backup =
          usbLink != null && usbLink.ready && !usbLink.closed
              ? usbLink
              : wifiLink != null && wifiLink.ready && !wifiLink.closed ? wifiLink : null;
      if (foreground && backup != null)
        try {
          select(backup);
          return;
        } catch (IOException ignored) {
        }
      cancelVision();
      stopMicrophone();
      cancelPlayback();
      listener.link(false, routeEpoch);
      if (foreground) listener.state("idle", "连接已断开，正在自动重连 · 麦克风已关闭");
    }
  }

  private final class Link implements Runnable {
    final Socket connection;
    final String kind;
    final int ticket;
    final ArrayBlockingQueue<Wire.Packet> queue = new ArrayBlockingQueue<>(128);
    volatile boolean closed, ready;
    DataOutputStream out;

    Link(Socket connection, String kind, int ticket) {
      this.connection = connection;
      this.kind = kind;
      this.ticket = ticket;
    }

    void event(JSONObject value) throws IOException {
      Wire.Packet packet = new Wire.Packet();
      packet.type = Wire.EVENT;
      packet.generation = epoch;
      packet.nanos = System.nanoTime();
      packet.data = Wire.jsonBytes(value);
      offer(packet);
    }

    void offer(Wire.Packet packet) throws IOException {
      if (closed || !queue.offer(packet)) {
        close();
        throw new IOException("Transport queue unavailable");
      }
    }

    public void run() {
      try {
        connection.setTcpNoDelay(true);
        connection.setKeepAlive(true);
        connection.setSoTimeout(10000);
        out = new DataOutputStream(connection.getOutputStream());
        new Thread(
                () -> {
                  try {
                    while (!closed && foreground && lifecycle == ticket) {
                      Wire.Packet packet = queue.poll(1, TimeUnit.SECONDS);
                      if (packet != null)
                        Wire.write(
                            out,
                            packet.type,
                            packet.generation,
                            packet.sequence,
                            packet.frame,
                            packet.nanos,
                            packet.data);
                    }
                  } catch (Exception error) {
                    close();
                  }
                },
                "cc8-" + kind + "-writer")
            .start();
        JSONObject hello = Wire.event("transport_hello");
        Wire.put(hello, "protocol", BuildConfig.CC8_PROTOCOL);
        event(hello);
        DataInputStream in = new DataInputStream(connection.getInputStream());
        while (!closed && foreground && lifecycle == ticket) handle(Wire.read(in), this);
      } catch (Exception ignored) {
      } finally {
        close();
      }
    }

    void close() {
      synchronized (this) {
        if (closed) return;
        closed = true;
        queue.clear();
      }
      TransportClose.asynchronously(connection);
      lost(this);
    }
  }

  private void handle(Wire.Packet p, Link source) throws Exception {
    final long route = routeFor(source);
    if (p.type == Wire.EVENT) {
      JSONObject control = Wire.json(p);
      String name = control.optString("event");
      if ("transport_ping".equals(name)) {
        source.event(Wire.event("transport_pong"));
        return;
      }
      if ("transport_activate".equals(name)) {
        select(source);
        return;
      }
      if ("transport_standby".equals(name)) {
        source.ready = true;
        return;
      }
      if ("lan_config".equals(name)) {
        if (source == activeLink && "usb".equals(source.kind)) {
          try {
            synchronized (routeLock) {
              if (foreground && source == activeLink) {
                lan.configure(control.getJSONObject("config"));
                lan.acquire();
              }
            }
          } catch (Exception error) {
            wifiError = error.getClass().getSimpleName();
          }
        }
        return;
      }
    }
    if (route < 0 || !currentRoute(route)) return;
    if (p.type == Wire.EVENT) {
      JSONObject j = Wire.json(p);
      String event = j.optString("event");
      if ("hello".equals(event)) {
        dotChatSupported = Boolean.TRUE.equals(j.opt("supports_dot_chat_handoff"));
        savedTasksSupported = Boolean.TRUE.equals(j.opt("supports_saved_task_results"));
        listener.recognition(j.optString("asr_engine", "sensevoice"));
        synchronized (routeLock) {
          if (!foreground || !currentRoute(route) || p.generation < minimumGeneration) return;
          if (Boolean.TRUE.equals(j.opt("playback_interrupted_by_transport"))) {
            if (!statusFeedback.transportInterruption(p.generation)) return;
            phase = "error";
            listener.state(phase, j.optString("message", "播报因连接切换中断，可在任务页查看已完成结果。"));
          } else if (!statusFeedback.transportInterrupted(p.generation)) {
            listener.state(
                recording ? "listening" : "idle", j.optString("message", "Mac 已连接，点击开始语音"));
          }
        }
      } else if ("screen_sleep".equals(event)) {
        if (recording) listener.screen(true);
      } else if ("screen_wake".equals(event)) {
        listener.screen(false);
      } else if ("device_control".equals(event)) {
        deviceControl(j);
      } else if ("text_begin".equals(event)) {
        if (foreground && p.generation >= minimumGeneration && statusFeedback.begin(p.generation)) {
          textSession = true;
          textGeneration = p.generation;
          listener.playback(true);
          listener.heard(j.optString("text"));
          listener.caption("已收到文字指令。\n正在处理…", "attentive");
        }
      } else if ("text_done".equals(event)) {
        if (textSession && p.generation == textGeneration) {
          textSession = false;
          if (!foreground
              || p.generation < minimumGeneration
              || !statusFeedback.current(p.generation)) return;
          listener.playback(false);
          // Finishing a failed task still releases controls, but must leave
          // the actionable error visible until the next explicit interaction.
          if (!statusFeedback.failed(p.generation)) {
            phase = recording ? "listening" : "idle";
            listener.state(phase, recording ? "我在听" : "任务已结束 · 麦克风关闭");
          }
        }
      } else if ("workspace".equals(event)) {
        if (foreground) listener.workspace(j, route);
      } else if ("workspace_result".equals(event)) {
        if (foreground) listener.workspaceResult(j.optBoolean("ok"), j.optString("message"), route);
      } else if ("vision_begin".equals(event)) {
        String nonce = j.optString("nonce");
        if (!foreground
            || p.generation < minimumGeneration
            || (visual && p.generation == visualGeneration)) return;
        boolean desktop = "desktop".equals(j.optString("origin"));
        if (desktop
            && !lan.authorizeDesktopCamera(
                desktopCameraAuth, nonce, p.generation, j.optString("camera_auth"))) return;
        if (!desktop
            && ((!recording && !nonce.equals(pendingVisionNonce))
                || (nonce.isEmpty() && !recording))) return;
        if (!statusFeedback.begin(p.generation)) return;
        desktopVisionNonce = desktop ? nonce : "";
        visual = true;
        visualGeneration = p.generation;
        visionRequestId = "";
        listener.dotResult("", false, "");
        listener.caption(
            desktop ? "Dot 请求拍照，请调整画面；预览 5 秒后拍摄并返回当前聊天。" : "请调整画面，预览 5 秒后自动拍摄并交给 Dot 分析。",
            "attentive");
        listener.screen(false);
        listener.vision(true, "请调整画面 · 预览 5 秒后拍摄", false);
        final int waitingGeneration = visualGeneration;
        new android.os.Handler(android.os.Looper.getMainLooper())
            .postDelayed(
                () -> {
                  if (visual
                      && visualGeneration == waitingGeneration
                      && visionRequestId.isEmpty()) {
                    cancelVision();
                    listener.state(recording ? "listening" : "idle", "拍摄请求超时，请重试");
                  }
                },
                15000);
      } else if ("camera_capture".equals(event)) {
        if (!foreground
            || !visual
            || p.generation != visualGeneration
            || p.generation < minimumGeneration) return;
        String nonce = j.optString("nonce");
        String ident = j.optString("request_id");
        if (!visionRequestId.isEmpty() || !ident.matches("[a-f0-9]{32}")) return;
        visionRequestId = ident;
        Wire.put(j, "vision_generation", p.generation);
        boolean allowed =
            "desktop".equals(j.optString("origin"))
                ? !nonce.isEmpty() && nonce.equals(desktopVisionNonce) && ident.equals(nonce)
                : "button".equals(j.optString("origin"))
                    ? !nonce.isEmpty() && nonce.equals(pendingVisionNonce)
                    : "voice".equals(j.optString("origin")) && recording;
        pendingVisionNonce = "";
        desktopVisionNonce = "";
        if (!allowed) {
          cameraFailed(j, "request_not_authorized");
          return;
        }
        Wire.put(j, "vision_generation", p.generation);
        listener.cameraCapture(j);
      } else if ("vision_done".equals(event)) {
        if (visual && p.generation == visualGeneration) {
          visual = false;
          pendingVisionNonce = "";
          desktopVisionNonce = "";
          if (!foreground
              || p.generation < minimumGeneration
              || !statusFeedback.current(p.generation)) return;
          boolean desktop = "desktop".equals(j.optString("origin"));
          if (desktop && "completed".equals(j.optString("result")))
            listener.caption("照片已交给 Mac，请在 Dot 查看本次拍照结果。", "friendly");
          listener.vision(
              false,
              "completed".equals(j.optString("result"))
                  ? desktop ? "拍照完成 · 摄像头关闭" : "看图已结束 · 摄像头关闭"
                  : "看图未完成 · 摄像头关闭",
              statusFeedback.failed(p.generation));
        }
      } else if ("vision_unavailable".equals(event)) {
        visual = false;
        pendingVisionNonce = "";
        listener.vision(false, j.optString("message", "看图暂不可用"), false);
      } else if ("caption_timing".equals(event)) {
        Player current = player;
        if (current != null
            && current.generation == p.generation
            && current.playbackId == j.optInt("playback_id", -1))
          current.timings = timings(j.optJSONArray("timings"), current.text.length());
      } else if ("ping".equals(event)) {
        send(Wire.event("pong"));
      } else if ("status".equals(event)) {
        String nextPhase = j.optString("phase", "listening");
        boolean permitted =
            recording
                || (announcement && p.generation == announcementGeneration)
                || (visual && p.generation == visualGeneration)
                || (textSession && p.generation == textGeneration)
                // Playback failure first cancels output, then reports why.
                // Admit that one terminal error only for the request we owned.
                || ("error".equals(nextPhase) && statusFeedback.cancelled(p.generation));
        if (foreground
            && permitted
            && p.generation >= minimumGeneration
            && statusFeedback.accept(p.generation, nextPhase)) {
          phase = nextPhase;
          listener.state(phase, j.optString("message"));
        }
      } else if ("transcript".equals(event)) {
        if (recording) listener.heard(j.optString("text"));
      } else if ("announcement_begin".equals(event)) {
        if (foreground && p.generation >= minimumGeneration && statusFeedback.begin(p.generation)) {
          announcement = true;
          announcementGeneration = p.generation;
          listener.announcement();
          listener.playback(true);
        }
      } else if ("announcement_done".equals(event)) {
        if (announcement && p.generation == announcementGeneration) {
          announcement = false;
          if (!foreground
              || p.generation < minimumGeneration
              || !statusFeedback.current(p.generation)) return;
          listener.playback(false);
          if (!statusFeedback.failed(p.generation)) {
            phase = recording ? "listening" : "idle";
            listener.state(
                phase,
                "completed".equals(j.optString("result"))
                    ? (recording ? "播报已结束，我在听" : "播报已结束 · 麦克风关闭")
                    : (recording ? "播报未完成，我在听" : "播报未完成 · 麦克风关闭"));
          }
        }
      } else if ("brain_provider".equals(event)) {
        listener.brain(j.optString("provider", "minimax"));
      } else if ("dot_result".equals(event)) {
        if ((recording || visual || textSession) && p.generation >= minimumGeneration)
          listener.dotResult(
              j.optString("text"), j.optBoolean("truncated"), j.optString("handoff_source_title"));
      } else if ("dot_chat_state".equals(event)) {
        dotChatSupported = true;
        if (foreground) listener.dotChats(j);
      } else if ("saved_task_result".equals(event)) {
        if (foreground) listener.savedTasks(j);
      } else if ("web_sources".equals(event)) {
        if ((recording || visual || textSession) && p.generation >= minimumGeneration)
          listener.sources(j.optJSONArray("items"), j.optString("retrieved_at"));
      } else if ("cancel".equals(event)) {
        if (p.generation < minimumGeneration
            || (visual && p.generation < visualGeneration)
            || !statusFeedback.cancel(
                p.generation, recording || announcement || visual || textSession)) return;
        minimumGeneration = Math.max(minimumGeneration, p.generation);
        if (visual) {
          visual = false;
          listener.vision(false, "已停止看图 · 摄像头关闭", false);
        }
        announcement = false;
        textSession = false;
        listener.playback(false);
        cancelPlayback();
        listener.caption("", "attentive");
        if (recording) listener.state("listening", "我在听，你继续说");
        else {
          phase = "idle";
          listener.state("idle", "播报已停止 · 麦克风关闭");
        }
      } else if ("playback_pause".equals(event)) {
        Player current = player;
        if (recording
            && current != null
            && current.generation == p.generation
            && p.generation >= minimumGeneration) current.pauseForVoice(j.optInt("token"));
      } else if ("playback_resume".equals(event)) {
        Player current = player;
        if (recording
            && current != null
            && current.generation == p.generation
            && p.generation >= minimumGeneration) current.resumeAfterNoise(j.optInt("token"));
      } else if ("play_begin".equals(event)) {
        boolean permitted =
            recording
                || (textSession && p.generation == textGeneration)
                || (visual && p.generation == visualGeneration)
                || (announcement
                    && p.generation == announcementGeneration
                    && "announcement".equals(j.optString("role")));
        if (!foreground || !permitted || p.generation < minimumGeneration) return;
        cancelPlayback();
        Player next =
            new Player(
                p.generation,
                j.optString("text"),
                j.optString("mood", "friendly"),
                j.optString("role", "assistant"),
                j.optInt("playback_id"),
                timings(j.optJSONArray("timings"), j.optString("text").length()),
                PlayoutProbe.requested(j.opt("playout_receipt_version")),
                Boolean.TRUE.equals(j.opt("report_audio_clock")));
        player = next;
        next.start();
      } else if ("play_end".equals(event)) {
        Player current = player;
        if (current != null && current.generation == p.generation) current.ended = true;
      }
    } else if (p.type == Wire.SPEAKER) {
      Player current = player;
      if ((recording || announcement || visual || textSession)
          && current != null
          && current.generation == p.generation
          && p.generation >= minimumGeneration) current.offer(p.data);
    }
  }

  private void send(JSONObject j) {
    try {
      sendPacket(Wire.EVENT, epoch, 0, 0, System.nanoTime(), Wire.jsonBytes(j));
    } catch (IOException ignored) {
    }
  }

  void requestText(String text) {
    if (!foreground || !isConnected()) return;
    JSONObject j = Wire.event("text_request");
    Wire.put(j, "text", text);
    send(j);
  }

  void workspaceGet() {
    send(Wire.event("workspace_get"));
  }

  void dotChatRequest(String requestId, String action, String sourceId, String cursor) {
    if (!foreground || !isConnected()) return;
    JSONObject j = Wire.event("dot_chat_request");
    Wire.put(j, "request_id", requestId);
    Wire.put(j, "action", action);
    if (sourceId != null) Wire.put(j, "source_id", sourceId);
    if (cursor != null) Wire.put(j, "cursor", cursor);
    send(j);
  }

  void dotChatCancel(String requestId) {
    JSONObject j = Wire.event("dot_chat_cancel");
    Wire.put(j, "request_id", requestId);
    send(j);
  }

  boolean savedTaskRequest(String requestId, String action, String id, long route) {
    JSONObject j = Wire.event("saved_task_request");
    Wire.put(j, "request_id", requestId);
    Wire.put(j, "action", action);
    if (id != null) Wire.put(j, "id", id);
    return sendWorkspace(j, route);
  }

  boolean reminderAction(JSONObject args, long route) {
    JSONObject j = Wire.event("reminder_action");
    Wire.put(j, "arguments", args);
    return sendWorkspace(j, route);
  }

  boolean cancelTask(String id, long route) {
    JSONObject j = Wire.event("task_cancel");
    Wire.put(j, "id", id);
    return sendWorkspace(j, route);
  }

  private boolean sendWorkspace(JSONObject j, long route) {
    synchronized (routeLock) {
      Link destination = activeLink;
      if (!foreground
          || route < 0
          || route != routeEpoch
          || destination == null
          || destination.closed
          || !destination.ready) return false;
      try {
        // A dialog authorizes one enqueue on its own activation. Do not use
        // sendPacket's audio fallback to forward this write to another route.
        destination.event(j);
        return true;
      } catch (IOException error) {
        destination.close();
        return false;
      }
    }
  }

  private void sendPacket(
      int type, int generation, long sequence, long frame, long nanos, byte[] bytes)
      throws IOException {
    synchronized (routeLock) {
      Link destination = activeLink;
      if (destination != null && destination.closed) {
        Link backup =
            usbLink != null && usbLink.ready && !usbLink.closed
                ? usbLink
                : wifiLink != null && wifiLink.ready && !wifiLink.closed ? wifiLink : null;
        if (backup != null) select(backup);
        destination = activeLink;
      }
      if (destination == null || destination.closed) throw new IOException("Mac disconnected");
      Wire.Packet packet = new Wire.Packet();
      packet.type = type;
      packet.generation = generation;
      packet.sequence = sequence;
      packet.frame = frame;
      packet.nanos = nanos;
      packet.data = bytes;
      try {
        destination.offer(packet);
      } catch (IOException error) {
        Link fallback = activeLink;
        if (fallback != null && fallback != destination && !fallback.closed) fallback.offer(packet);
        else throw error;
      }
    }
  }

  boolean isVisionRequestCurrent(JSONObject request) {
    return foreground
        && visual
        && visualGeneration == request.optInt("vision_generation", -1)
        && visionRequestId.equals(request.optString("request_id"));
  }

  void requestVision() {
    if (!foreground || !isConnected() || visual || !pendingVisionNonce.isEmpty()) return;
    pendingVisionNonce = java.util.UUID.randomUUID().toString().replace("-", "");
    JSONObject e = Wire.event("vision_request");
    Wire.put(e, "nonce", pendingVisionNonce);
    send(e);
    listener.vision(true, "正在请求拍摄 · 图片将交给 Dot", false);
    final String nonce = pendingVisionNonce;
    new android.os.Handler(android.os.Looper.getMainLooper())
        .postDelayed(
            () -> {
              if (!nonce.isEmpty() && nonce.equals(pendingVisionNonce)) {
                cancelVision();
                listener.state(recording ? "listening" : "idle", "拍摄请求超时，请重试");
              }
            },
            15000);
  }

  void cancelVision() {
    boolean had = visual || !pendingVisionNonce.isEmpty();
    int stoppedGeneration = visual ? visualGeneration : -1;
    String stoppedNonce = pendingVisionNonce;
    if (visual) minimumGeneration = Math.max(minimumGeneration, visualGeneration + 1);
    visual = false;
    pendingVisionNonce = "";
    visionRequestId = "";
    desktopVisionNonce = "";
    if (had) {
      cancelPlayback();
      JSONObject stopped = Wire.event("vision_cancel");
      Wire.put(stopped, "vision_generation", stoppedGeneration);
      Wire.put(stopped, "nonce", stoppedNonce);
      send(stopped);
      listener.vision(false, "已取消看图 · 摄像头关闭", false);
    }
  }

  void cameraFailed(JSONObject request, String reason) {
    JSONObject e = Wire.event("camera_error");
    Wire.put(e, "request_id", request.optString("request_id"));
    Wire.put(e, "vision_generation", request.optInt("vision_generation", visualGeneration));
    Wire.put(e, "error", reason);
    send(e);
  }

  void imageReady(JSONObject request, byte[] jpeg, int width, int height) {
    final int gen = request.optInt("vision_generation", -1);
    final String ident = request.optString("request_id");
    new Thread(
            () -> {
              try {
                byte[] hash = java.security.MessageDigest.getInstance("SHA-256").digest(jpeg);
                StringBuilder hex = new StringBuilder();
                for (byte b : hash)
                  hex.append(String.format(java.util.Locale.ROOT, "%02x", b & 255));
                JSONObject head = Wire.event("camera_image_begin");
                Wire.put(head, "bytes", jpeg.length);
                Wire.put(head, "width", width);
                Wire.put(head, "height", height);
                Wire.put(head, "sha256", hex.toString());
                sendImageEvent(head, gen, ident);
                int index = 0;
                for (int at = 0; at < jpeg.length; at += 12288) {
                  JSONObject chunk = Wire.event("camera_image_chunk");
                  Wire.put(chunk, "index", index++);
                  Wire.put(
                      chunk,
                      "data",
                      android.util.Base64.encodeToString(
                          java.util.Arrays.copyOfRange(jpeg, at, Math.min(jpeg.length, at + 12288)),
                          android.util.Base64.NO_WRAP));
                  sendImageEvent(chunk, gen, ident);
                }
                sendImageEvent(Wire.event("camera_image_end"), gen, ident);
              } catch (Exception error) {
                if (visual && visualGeneration == gen)
                  cameraFailed(request, "image_transfer_failed");
              }
            },
            "cc8-camera-transfer")
        .start();
  }

  private void sendImageEvent(JSONObject e, int gen, String ident) throws IOException {
    synchronized (routeLock) {
      if (!foreground || !visual || gen != visualGeneration || gen < minimumGeneration)
        throw new IOException("Stale camera request");
      Wire.put(e, "request_id", ident);
      Wire.put(e, "vision_generation", gen);
      sendPacket(Wire.EVENT, epoch, 0, 0, System.nanoTime(), Wire.jsonBytes(e));
    }
  }

  void settings(boolean wake) {
    wakeMode = wake;
    JSONObject e = Wire.event("settings");
    Wire.put(e, "wake_mode", wake);
    send(e);
  }

  void brain(String provider) {
    JSONObject e = Wire.event("settings");
    Wire.put(e, "brain_provider", provider);
    send(e);
  }

  void interrupt() {
    cancelVision();
    announcement = false;
    textSession = false;
    listener.playback(false);
    cancelPlayback();
    listener.caption("", "attentive");
    send(Wire.event("interrupt"));
    if (recording) listener.state("listening", "我在听，你继续说");
    else {
      phase = "idle";
      listener.state("idle", "播报已停止 · 麦克风关闭");
    }
  }

  @SuppressLint(
      "MissingPermission") // Only MainActivity's authorized foreground opening or Start action may
  // call; checked again here.
  void startMicrophone() {
    startMicrophone(0);
  }

  @SuppressLint("MissingPermission")
  void startMicrophone(long testLimitMs) {
    final CaptureWindow window = new CaptureWindow(testLimitMs, SystemClock.elapsedRealtime());
    // mine 固定本次录音 epoch，旧线程 finally 只能清理自己，不能关闭后来新开的录音。
    if (!foreground || recording || !isConnected()) return;
    cancelVision();
    if (context.checkCallingOrSelfPermission(Manifest.permission.RECORD_AUDIO)
        != PackageManager.PERMISSION_GRANTED) {
      listener.state("error", "请先允许麦克风权限");
      return;
    }
    announcement = false;
    textSession = false;
    listener.playback(false);
    cancelPlayback();
    final int mine;
    synchronized (this) {
      if (!foreground || recording || !isConnected()) return;
      recording = true;
      captureWindow = window;
      mine = ++epoch;
      micFrames = 0;
      renderFrames = 0;
      minimumGeneration = 0;
      statusFeedback.reset();
    }
    listener.microphone(true);
    listener.state("calibrating", "正在准备音频，请保持安静");
    new Thread(
            () -> {
              synchronized (captureResourceLock) {
                if (!recording || !foreground || epoch != mine) return;
                AudioRecord record = null;
                Thread timeout = null;
                long sequence = 0, origin = 0, last = System.nanoTime();
                try {
                  // The bound lives in this AudioRecord worker, independent of
                  // the Mac runner and the UI event queue. Expiry follows the
                  // same epoch-owned finally path as any other capture end.
                  if (!window.allows(SystemClock.elapsedRealtime())) return;
                  int min =
                      AudioRecord.getMinBufferSize(
                          16000, AudioFormat.CHANNEL_IN_MONO, AudioFormat.ENCODING_PCM_16BIT);
                  if (min <= 0) throw new IOException("不支持 16 kHz 录音");
                  record =
                      new AudioRecord(
                          MediaRecorder.AudioSource.VOICE_COMMUNICATION,
                          16000,
                          AudioFormat.CHANNEL_IN_MONO,
                          AudioFormat.ENCODING_PCM_16BIT,
                          Math.max(min * 2, 6400));
                  if (record.getState() != AudioRecord.STATE_INITIALIZED)
                    throw new IOException("麦克风初始化失败");
                  synchronized (VoiceSession.this) {
                    if (!recording
                        || !foreground
                        || epoch != mine
                        || !window.allows(SystemClock.elapsedRealtime())) return;
                    recorder = record;
                    record.startRecording();
                    if (record.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING)
                      throw new IOException("麦克风无法开始");
                  }
                  if (window.limitMs > 0) {
                    final AudioRecord ownedRecord = record;
                    timeout =
                        new Thread(
                            () -> {
                              try {
                                while (recording
                                    && epoch == mine
                                    && window.allows(SystemClock.elapsedRealtime())) {
                                  Thread.sleep(
                                      Math.min(
                                          1000, window.remaining(SystemClock.elapsedRealtime())));
                                }
                                // Stop only this worker's hardware, never a replacement
                                // recording. This also unblocks a stalled native read.
                                if (recording
                                    && epoch == mine
                                    && !window.allows(SystemClock.elapsedRealtime()))
                                  ownedRecord.stop();
                              } catch (InterruptedException interrupted) {
                                Thread.currentThread().interrupt();
                              } catch (RuntimeException alreadyReleased) {
                                // The epoch owner may already have closed this record.
                              }
                            },
                            "cc8-test-capture-timeout");
                    timeout.setDaemon(true);
                    timeout.start();
                  }
                  JSONObject began = Wire.event("session_start");
                  Wire.put(began, "epoch", mine);
                  Wire.put(began, "source", 7);
                  Wire.put(began, "sample_rate", 16000);
                  Wire.put(began, "render_gain", .35);
                  synchronized (routeLock) {
                    if (!recording || !foreground || epoch != mine) return;
                    send(began);
                  }
                  CaptureClockStats clockStats = window.clockStats;
                  AudioTimestamp captureStamp =
                      clockStats != null && Build.VERSION.SDK_INT >= 24
                          ? new AudioTimestamp()
                          : null;
                  short[] chunk = new short[320];
                  while (recording
                      && foreground
                      && epoch == mine
                      && window.allows(SystemClock.elapsedRealtime())) {
                    int count = record.read(chunk, 0, chunk.length);
                    if (count < 0) throw new IOException("录音读取错误 " + count);
                    if (count == 0) continue;
                    if (!recording
                        || !foreground
                        || epoch != mine
                        || !window.allows(SystemClock.elapsedRealtime())) break;
                    long now = System.nanoTime();
                    double power = 0;
                    for (int i = 0; i < count; i++) power += (double) chunk[i] * chunk[i];
                    inputLevel = (float) Math.sqrt(power / count) / 32768;
                    if (origin == 0) origin = now - count * 1000000000L / 16000;
                    if (now - last > 400000000L && micFrames > 0) {
                      JSONObject gap = Wire.event("capture_gap");
                      Wire.put(gap, "milliseconds", (now - last) / 1000000);
                      send(gap);
                    }
                    last = now;
                    sendPacket(
                        Wire.MIC,
                        mine,
                        sequence++,
                        micFrames,
                        origin + micFrames * 1000000000L / 16000,
                        Wire.pcm(chunk, count));
                    micFrames += count;
                    if (captureStamp != null
                        && Build.VERSION.SDK_INT >= 24
                        && recording
                        && epoch == mine
                        && window.allows(SystemClock.elapsedRealtime())
                        && clockStats.due(System.nanoTime())) {
                      boolean success;
                      try {
                        success =
                            record.getTimestamp(captureStamp, AudioTimestamp.TIMEBASE_MONOTONIC)
                                == AudioRecord.SUCCESS;
                      } catch (RuntimeException unavailable) {
                        success = false;
                      }
                      clockStats.observe(
                          success,
                          captureStamp.framePosition,
                          captureStamp.nanoTime,
                          System.nanoTime(),
                          micFrames,
                          origin);
                    }
                  }
                } catch (Exception error) {
                  if (recording
                      && foreground
                      && epoch == mine
                      && window.allows(SystemClock.elapsedRealtime()))
                    listener.state("error", "录音已停止：" + error.getMessage());
                } finally {
                  if (timeout != null) timeout.interrupt();
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
                  boolean owned;
                  synchronized (VoiceSession.this) {
                    if (recorder == record) recorder = null;
                    owned = epoch == mine;
                    if (owned) {
                      recording = false;
                      captureWindow = null;
                    }
                  }
                  if (owned) {
                    listener.microphone(false);
                    JSONObject stopped = Wire.event("session_stop");
                    Wire.put(stopped, "epoch", mine);
                    send(stopped);
                  }
                }
              }
            },
            "cc8-microphone")
        .start();
  }

  void stopMicrophone() {
    // 先推进 epoch 再停止硬件，使阻塞 read 返回后的旧帧无法进入新会话。
    final int stoppedEpoch;
    final AudioRecord r;
    synchronized (this) {
      stoppedEpoch = epoch++;
      recording = false;
      captureWindow = null;
      r = recorder;
      recorder = null;
    }
    announcement = false;
    listener.playback(false);
    inputLevel = 0;
    phase = "idle";
    if (r != null)
      try {
        r.stop();
      } catch (RuntimeException ignored) {
      }
    cancelPlayback();
    listener.microphone(false);
    JSONObject stopped = Wire.event("session_stop");
    Wire.put(stopped, "epoch", stoppedEpoch);
    send(stopped);
  }

  private synchronized void cancelPlayback() {
    Player p = player;
    player = null;
    lastCaption = CaptionProgress.empty();
    if (p != null) p.cancel();
  }

  float microphoneLevel() {
    return recording ? inputLevel : 0;
  }

  float mouthLevel() {
    Player p = player;
    return p == null ? 0 : p.level();
  }

  String mood() {
    Player p = player;
    return p == null ? "attentive" : p.mood;
  }

  long playbackHead() {
    Player p = player;
    AudioTrack t = p == null ? null : p.track;
    try {
      return t == null ? 0 : t.getPlaybackHeadPosition() & 0xffffffffL;
    } catch (IllegalStateException ignored) {
      return 0;
    }
  }

  JSONObject snapshot() {
    JSONObject j = Wire.event("snapshot");
    Wire.put(j, "foreground", foreground);
    Wire.put(j, "connected", isConnected());
    Wire.put(j, "transport", transportName());
    Wire.put(j, "usb_connected", usbLink != null && !usbLink.closed);
    Wire.put(j, "usb_physical", usbPhysical);
    Wire.put(j, "wifi_ready", wifiLink != null && wifiLink.ready && !wifiLink.closed);
    Wire.put(j, "wifi_paired", lan.paired());
    Wire.put(j, "wifi_error", wifiError);
    Wire.put(j, "microphone_active", recording);
    CaptureWindow window = captureWindow;
    Wire.put(j, "test_capture_limit_supported", true);
    Wire.put(j, "test_capture_limit_ms", recording && window != null ? window.limitMs : 0);
    Wire.put(
        j,
        "test_capture_remaining_ms",
        recording && window != null ? window.remaining(SystemClock.elapsedRealtime()) : 0);
    boolean captureClockSupported = Build.VERSION.SDK_INT >= 24;
    Wire.put(j, "capture_clock_evidence_supported", captureClockSupported);
    if (captureClockSupported
        && recording
        && window != null
        && window.clockStats != null
        && window.allows(SystemClock.elapsedRealtime())) {
      Wire.put(j, "capture_clock_evidence", new JSONObject(window.clockStats.snapshot()));
    }
    Wire.put(j, "announcement_active", announcement);
    Wire.put(j, "vision_active", isVisual());
    Wire.put(j, "mic_frames", micFrames);
    Wire.put(j, "render_frames", renderFrames);
    Wire.put(j, "playback_head", playbackHead());
    Player playingNow = player;
    Wire.put(j, "playback_speech_frames", playingNow == null ? 0 : playingNow.written);
    Wire.put(
        j, "playback_padding_frames", playingNow == null ? 0 : playingNow.drain.paddingFrames());
    Wire.put(j, "last_playback_speech_frames", lastPlaybackSpeechFrames);
    Wire.put(j, "last_playback_padding_frames", lastPlaybackPaddingFrames);
    Wire.put(j, "playback_paused", player != null && player.paused);
    Wire.put(j, "phase", phase);
    Wire.put(j, "raw_audio_saved", false);
    return j;
  }

  static final class CaptionProgress {
    final int id;
    final String text;
    final float position;
    final boolean timed, playing, paused;
    final long elapsedMs, totalMs;

    CaptionProgress(
        int id, String text, float position, boolean timed, boolean playing, boolean paused) {
      this(id, text, position, timed, playing, paused, 0, 0);
    }

    CaptionProgress(
        int id,
        String text,
        float position,
        boolean timed,
        boolean playing,
        boolean paused,
        long elapsed,
        long total) {
      this.id = id;
      this.text = text;
      this.position = position;
      this.timed = timed;
      this.playing = playing;
      this.paused = paused;
      this.elapsedMs = elapsed;
      this.totalMs = total;
    }

    static CaptionProgress empty() {
      return new CaptionProgress(0, "", 0, false, false, false);
    }
  }

  CaptionProgress captionProgress() {
    Player p = player;
    if (p == null) return lastCaption;
    AudioTrack t = p.track;
    long head = 0;
    try {
      if (t != null) head = t.getPlaybackHeadPosition() & 0xffffffffL;
    } catch (IllegalStateException ignored) {
    }
    CaptionTimeline line = p.timings;
    return new CaptionProgress(
        p.playbackId,
        p.text,
        line.position(head),
        line.rows.length > 0,
        true,
        p.paused,
        head / 16,
        p.ended && p.queue.isEmpty() ? p.written / 16 : 0);
  }

  private static CaptionTimeline timings(JSONArray values, int length) {
    if (values == null || values.length() > 2048) return new CaptionTimeline(new int[0][], length);
    int[][] rows = new int[values.length()][];
    for (int i = 0; i < rows.length; i++) {
      JSONArray r = values.optJSONArray(i);
      if (r == null || r.length() != 4) return new CaptionTimeline(new int[0][], length);
      rows[i] = new int[] {r.optInt(0, -1), r.optInt(1, -1), r.optInt(2, -1), r.optInt(3, -1)};
    }
    return new CaptionTimeline(rows, length);
  }

  void screenTouched() {
    listener.screen(false);
    send(Wire.event("screen_touch"));
  }

  private void deviceControl(JSONObject j) {
    JSONObject result = Wire.event("device_control_result");
    Wire.put(result, "request_id", j.optString("request_id"));
    try {
      String action = j.optString("action");
      AudioManager audio = (AudioManager) context.getSystemService(Context.AUDIO_SERVICE);
      if ("volume".equals(action)) {
        int percent = j.optInt("percent", -1);
        if (percent < 0 || percent > 100) throw new IllegalArgumentException("volume range");
        audio.setStreamVolume(
            AudioManager.STREAM_MUSIC,
            Math.round(audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC) * percent / 100f),
            0);
      } else if ("stop".equals(action)) {
        cancelPlayback();
        listener.playback(false);
      } else if ("wake_screen".equals(action)) listener.screen(false);
      else if (!"status".equals(action)) throw new IllegalArgumentException("unknown action");
      Wire.put(result, "ok", true);
      Wire.put(
          result,
          "volume_percent",
          Math.round(
              100f
                  * audio.getStreamVolume(AudioManager.STREAM_MUSIC)
                  / Math.max(1, audio.getStreamMaxVolume(AudioManager.STREAM_MUSIC))));
      Wire.put(result, "microphone_active", recording);
      Wire.put(result, "foreground", foreground);
      Wire.put(result, "transport", transportName());
      Wire.put(result, "wifi_ready", wifiLink != null && wifiLink.ready && !wifiLink.closed);
    } catch (RuntimeException e) {
      Wire.put(result, "ok", false);
      Wire.put(result, "error", e.getClass().getSimpleName());
    }
    send(result);
  }

  private final class Player extends Thread implements PlaybackDrain.Sink {
    // 播放头决定字幕、嘴型和完成回执；队列中已有数据不能被当成已经发声。
    final int generation, playbackId;
    final boolean reportPlayout;
    final RenderClockStats clockStats;
    final PlayoutProbe playout = new PlayoutProbe();
    final String text, mood, role;
    volatile CaptionTimeline timings;
    long renderSequence;
    long writeNanos;
    final ArrayBlockingQueue<byte[]> queue = new ArrayBlockingQueue<>(64);
    final ConcurrentLinkedQueue<Envelope> envelopes = new ConcurrentLinkedQueue<>();
    volatile boolean ended, stopped, paused;
    volatile int pauseToken;
    volatile AudioTrack track;
    volatile long written = 0;
    volatile long resumeCount = 0;
    int configuredBufferFrames;
    final PlaybackDrain drain = new PlaybackDrain();

    Player(
        int g,
        String t,
        String m,
        String role,
        int id,
        CaptionTimeline line,
        boolean report,
        boolean reportClock) {
      super("cc8-speaker");
      generation = g;
      text = t;
      mood = m;
      this.role = role;
      playbackId = id;
      timings = line;
      reportPlayout = report;
      clockStats =
          report && reportClock && recording
              ? new RenderClockStats(SystemClock.elapsedRealtime())
              : null;
    }

    void offer(byte[] pcm) throws IOException {
      try {
        if (!stopped && !queue.offer(pcm, 2, TimeUnit.SECONDS)) throw new IOException("播放队列积压");
      } catch (InterruptedException e) {
        Thread.currentThread().interrupt();
        throw new IOException(e);
      }
    }

    void cancel() {
      stopped = true;
      queue.clear();
      AudioTrack t = track;
      if (t != null)
        try {
          t.pause();
          t.flush();
        } catch (RuntimeException ignored) {
        }
      interrupt();
    }

    void pauseForVoice(int token) {
      if (!allowed()) return;
      pauseToken = token;
      paused = true;
      AudioTrack t = track;
      if (t != null)
        try {
          t.pause();
        } catch (IllegalStateException ignored) {
        }
      phase = "listening";
      listener.state("listening", "听到你说话，正在听…");
      JSONObject ack = Wire.event("playback_paused");
      Wire.put(ack, "generation", generation);
      Wire.put(ack, "token", token);
      Wire.put(ack, "nanos", System.nanoTime());
      send(ack);
    }

    void resumeAfterNoise(int token) {
      if (!allowed() || !paused || pauseToken != token) return;
      paused = false;
      AudioTrack t = track;
      if (t != null)
        try {
          t.play();
        } catch (IllegalStateException ignored) {
        }
      resumeCount++;
      JSONObject ack = Wire.event("playback_resumed");
      Wire.put(ack, "generation", generation);
      Wire.put(ack, "token", token);
      Wire.put(ack, "nanos", System.nanoTime());
      send(ack);
      phase = "speaking";
      listener.caption(text, mood);
      listener.state("speaking", "继续播报 · 你可以开口打断");
      synchronized (this) {
        notifyAll();
      }
    }

    void waitIfPaused() throws InterruptedException {
      synchronized (this) {
        while (paused && allowed()) wait(30);
      }
    }

    float level() {
      try {
        if (paused) return 0;
        AudioTrack t = track;
        if (t == null || stopped || t.getPlayState() != AudioTrack.PLAYSTATE_PLAYING) return 0;
        long head = t.getPlaybackHeadPosition() & 0xffffffffL;
        Envelope e = envelopes.peek();
        while (e != null && head >= e.end) {
          envelopes.poll();
          e = envelopes.peek();
        }
        return e != null && head >= e.start ? e.value : 0;
      } catch (IllegalStateException ignored) {
        return 0;
      }
    }

    public boolean allowed() {
      return !stopped
          && foreground
          && (recording
              || (textSession && generation == textGeneration)
              || (visual && generation == visualGeneration)
              || ("announcement".equals(role)
                  && announcement
                  && generation == announcementGeneration));
    }

    public boolean paused() {
      return paused;
    }

    public void awaitReady() throws InterruptedException {
      waitIfPaused();
    }

    public int write(short[] samples, int offset, int count) {
      writeNanos = System.nanoTime();
      return track.write(samples, offset, count);
    }

    public void accepted(short[] samples, int offset, int count) throws IOException {
      AudioTrack t = track;
      long now = System.nanoTime(), head = t.getPlaybackHeadPosition() & 0xffffffffL;
      AudioTimestamp stamp = new AudioTimestamp();
      boolean valid = t.getTimestamp(stamp);
      long speakerTime =
          PlaybackClock.estimate(now, written, head, valid, stamp.framePosition, stamp.nanoTime);
      if (clockStats != null)
        clockStats.observe(
            SystemClock.elapsedRealtime(),
            written,
            count,
            speakerTime,
            PlaybackClock.source(now, written, valid, stamp.framePosition, stamp.nanoTime));
      double power = 0;
      short[] accepted = new short[count];
      System.arraycopy(samples, offset, accepted, 0, count);
      for (short v : accepted) power += (double) v * v;
      float value = (float) Math.sqrt(power / Math.max(1, count)) / 32768;
      envelopes.add(new Envelope(written, written + count, value));
      sendPacket(
          Wire.RENDER,
          generation,
          renderSequence++,
          written,
          speakerTime,
          Wire.pcm(accepted, count));
      written += count;
      renderFrames += count;
      if (reportPlayout) {
        playout.accepted(samples, offset, count);
        reportPlayout(t);
      }
    }

    void reportPlayout(AudioTrack t) {
      if (!reportPlayout || !playout.pending()) return;
      PlayoutProbe.Observation seen =
          playout.observe(
              t.getPlaybackHeadPosition() & 0xffffffffL,
              System.nanoTime(),
              t.getPlayState() == AudioTrack.PLAYSTATE_PLAYING,
              paused,
              allowed());
      if (seen == null) return;
      JSONObject ack = Wire.event("playout_progress");
      Wire.put(ack, "generation", generation);
      Wire.put(ack, "playback_id", playbackId);
      Wire.put(ack, "first_signal_frame", seen.firstSignal);
      Wire.put(ack, "played_frames", seen.played);
      Wire.put(ack, "accepted_frames", seen.accepted);
      Wire.put(ack, "nanos", seen.nanos);
      send(ack);
    }

    private JSONObject clockReport() {
      JSONObject result = new JSONObject(), sources = new JSONObject();
      Wire.put(result, "version", 1);
      Wire.put(result, "complete", clockStats.complete());
      Wire.put(result, "expired", clockStats.expired);
      Wire.put(result, "invalid", clockStats.invalid);
      Wire.put(result, "observations", clockStats.observations);
      Wire.put(result, "frames", clockStats.frames);
      Wire.put(result, "source_changes", clockStats.sourceChanges);
      Wire.put(result, "clock_steps", clockStats.clockSteps);
      Wire.put(result, "max_step_nanos", clockStats.maxStepNanos);
      for (int i = 0; i < RenderClockStats.SOURCES.length; i++)
        Wire.put(sources, RenderClockStats.SOURCES[i], clockStats.counts[i]);
      Wire.put(result, "source_counts", sources);
      return result;
    }

    public long now() {
      return SystemClock.elapsedRealtime();
    }

    public long head() {
      return track.getPlaybackHeadPosition() & 0xffffffffL;
    }

    public int bufferFrames() {
      return Build.VERSION.SDK_INT >= 23 ? track.getBufferSizeInFrames() : configuredBufferFrames;
    }

    public long resumeSerial() {
      return resumeCount;
    }

    public void probe() {
      reportPlayout(track);
    }

    public void waitTick(long milliseconds) throws InterruptedException {
      Thread.sleep(milliseconds);
    }

    @Override
    public void run() {
      AudioTrack t = null;
      try {
        int min =
            AudioTrack.getMinBufferSize(
                16000, AudioFormat.CHANNEL_OUT_MONO, AudioFormat.ENCODING_PCM_16BIT);
        configuredBufferFrames = Math.max(min * 2, 3200) / 2;
        t =
            new AudioTrack(
                AudioManager.STREAM_MUSIC,
                16000,
                AudioFormat.CHANNEL_OUT_MONO,
                AudioFormat.ENCODING_PCM_16BIT,
                Math.max(min * 2, 3200),
                AudioTrack.MODE_STREAM);
        track = t;
        if (t.getState() != AudioTrack.STATE_INITIALIZED) throw new IOException("播放初始化失败");
        t.setVolume(.35f);
        t.play();
        if (!allowed()) return;
        if (paused) {
          t.pause();
          waitIfPaused();
        }
        if (!allowed()) return;
        listener.caption(text, mood);
        listener.state(
            "calibration".equals(role) ? "calibrating" : "speaking",
            "calibration".equals(role)
                ? "首次校准：请安静听完这段声音"
                : "announcement".equals(role) && !recording
                    ? "正在播报 · 点击打断可停止"
                    : visual && !recording ? "正在看图回答 · 点击停止看图可打断" : "正在回答 · 你可以开口打断");
        long sequence = 0;
        while (allowed()) {
          waitIfPaused();
          if (!allowed()) break;
          byte[] bytes = queue.poll(50, TimeUnit.MILLISECONDS);
          if (bytes == null) {
            reportPlayout(t);
            if (ended && queue.isEmpty()) break;
            continue;
          }
          if (!PcmPump.writeAll(Wire.samples(bytes), this)) break;
        }
        if (!drain.drain(written, this)) return;
        if (allowed()) {
          synchronized (VoiceSession.this) {
            if (player == this)
              lastCaption =
                  new CaptionProgress(
                      playbackId,
                      text,
                      text.length(),
                      timings.rows.length > 0,
                      false,
                      false,
                      written / 16,
                      written / 16);
          }
          JSONObject done = Wire.event("play_done");
          Wire.put(done, "generation", generation);
          Wire.put(done, "role", role);
          Wire.put(done, "playback_id", playbackId);
          Wire.put(done, "played_frames", t.getPlaybackHeadPosition() & 0xffffffffL);
          if (clockStats != null) {
            Wire.put(done, "render_clock", clockReport());
          }
          send(done);
          if (recording) listener.state("listening", "我在听");
        }
      } catch (InterruptedException error) {
        Thread.currentThread().interrupt();
      } catch (Exception error) {
        if (!stopped && foreground) {
          listener.state("error", "播放已停止：" + error.getMessage());
          JSONObject failure = Wire.event("playback_error");
          Wire.put(failure, "error_type", error.getClass().getSimpleName());
          Wire.put(failure, "generation", generation);
          Wire.put(failure, "playback_id", playbackId);
          send(failure);
        }
      } finally {
        track = null;
        if (t != null) {
          try {
            t.stop();
          } catch (RuntimeException ignored) {
          }
          t.release();
        }
        synchronized (VoiceSession.this) {
          if (player == this) {
            lastPlaybackSpeechFrames = written;
            lastPlaybackPaddingFrames = drain.paddingFrames();
            player = null;
          }
        }
      }
    }
  }

  private static final class Envelope {
    final long start, end;
    final float value;

    Envelope(long s, long e, float v) {
      start = s;
      end = e;
      value = v;
    }
  }
}
