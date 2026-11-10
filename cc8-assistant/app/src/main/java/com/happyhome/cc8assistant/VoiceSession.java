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

    default void identityChanged(boolean cancelAutoVoice) {}

    void state(String phase, String message);

    default void status(StatusPresentation update) {
      state(update.phase, update.message);
    }

    void caption(String text, String mood);

    void playbackPresentation(PlaybackPresentation update);

    void microphone(boolean active);

    void captureRejected(int stoppedEpoch);

    void playback(boolean active);

    void heard(String text);

    void sources(SourcesPresentation update);

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

    default void speechCorrection(JSONObject state, long route) {}

    default void preferenceResult(JSONObject data, long route) {}

    default void vocabularyResult(JSONObject data, long route) {}

    default void workflowResult(JSONObject data, long route) {}

    default void meetingResult(JSONObject data, long route) {}

    default void releaseInfo(JSONObject data, long route) {}
  }

  private final Context context;
  private final IdentityState identity;
  private volatile boolean identityHello;
  private volatile String identityRequestId = "";
  private final Listener listener;
  private final Object routeLock = new Object();
  private final Object captureResourceLock = new Object();
  private final LanConnector lan;
  private final SpeakerOwner speakerOwner;
  private volatile Link activeLink, usbLink, wifiLink;
  private volatile String wifiError = "";
  private boolean receiverRegistered;
  private volatile boolean usbPhysical, wakeMode = true;
  private final DesktopCameraAuth desktopCameraAuth = new DesktopCameraAuth();
  private volatile boolean visual;
  private volatile int visualGeneration;
  private volatile String pendingVisionNonce = "", visionRequestId = "", desktopVisionNonce = "";
  private volatile String pendingPhotoId = "", pendingPhotoForgetId = "";
  private final PhotoFollowupWindow photoFollowup = new PhotoFollowupWindow();
  private volatile boolean foreground, recording, announcement;
  private volatile SourcesPresentation sourcesPresentation;
  private volatile int announcementGeneration;
  private volatile ServerSocket server;
  private volatile Socket socket;
  private volatile DataOutputStream output;
  private volatile AudioRecord recorder;
  private volatile CaptureWindow captureWindow;
  private volatile Player player;
  private volatile PlaybackPresentation playbackPresentation;
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
  private final HostActivation hostActivation = new HostActivation();
  private final ConnectionAvailability availability = new ConnectionAvailability();
  private volatile boolean dotChatSupported, savedTasksSupported, preferenceMemorySupported;
  private volatile boolean resultProvenanceSupported;
  private volatile boolean resultReadingSupported;
  private volatile boolean visionModesSupported, photoFollowupSupported, macWindowSupported;
  private volatile boolean vocabularySupported,
      workflowSupported,
      reportSourcesSupported,
      reportArchiveSupported,
      threadQueueSupported,
      reportProgressSupported;
  private volatile JSONObject voicePreferences;
  private final WorkflowConsent workflowConsent = new WorkflowConsent();
  private final MeetingConsent meetingConsent = new MeetingConsent();
  private volatile boolean meetingSupported, meetingHostReserved;
  private volatile boolean meetingReleaseFailed;
  private volatile MeetingRecorder meetingRecorder;
  private String meetingId = "", meetingNonce = "";
  private long meetingRoute = -1, meetingRevision;
  private int meetingGeneration;
  private volatile JSONObject speechCorrection;
  private int speechCorrectionRevision = -1;

  boolean supportsDotChatHandoff() {
    return dotChatSupported;
  }

  boolean supportsPreferenceMemory() {
    return preferenceMemorySupported;
  }

  boolean supportsVisionModes() {
    return visionModesSupported;
  }

  boolean supportsMacWindow() {
    return macWindowSupported;
  }

  boolean supportsVocabulary() {
    return vocabularySupported;
  }

  boolean supportsWorkflows() {
    return workflowSupported;
  }

  boolean supportsMeetings() {
    return meetingSupported;
  }

  boolean isMeetingCapturing() {
    MeetingRecorder current = meetingRecorder;
    return current != null && current.capturing();
  }

  boolean meetingMediaBlocked() {
    return meetingReleaseFailed;
  }

  private boolean meetingReserved() {
    synchronized (routeLock) {
      return meetingHostReserved
          || meetingRecorder != null
          || meetingReleaseFailed
          || meetingConsent.pending();
    }
  }

  boolean supportsReportSources() {
    return reportSourcesSupported;
  }

  boolean supportsReportArchive() {
    return reportArchiveSupported;
  }

  boolean supportsReportProgress() {
    return reportProgressSupported;
  }

  boolean supportsThreadQueue() {
    return threadQueueSupported;
  }

  PhotoFollowupWindow.Picture photoForFollowup() {
    synchronized (routeLock) {
      return foreground
              && photoFollowupSupported
              && isConnected()
              && !visual
              && !announcement
              && !textSession
          ? photoFollowup.current(routeEpoch, minimumGeneration, SystemClock.elapsedRealtime())
          : null;
    }
  }

  boolean supportsSavedTaskResults() {
    return savedTasksSupported;
  }

  boolean supportsResultProvenance() {
    return resultProvenanceSupported;
  }

  boolean supportsResultReading() {
    return resultReadingSupported;
  }

  VoiceSession(Context context, Listener listener) {
    this.context = context.getApplicationContext();
    this.listener = listener;
    this.identity = new IdentityState(new IdentityPreferences(this.context));
    this.lan = new LanConnector(this.context);
    this.speakerOwner = new SpeakerOwner(NativeSpeaker::new, () -> foreground);
  }

  boolean isRecording() {
    return recording;
  }

  boolean isConnected() {
    Link link = activeLink;
    return link != null && !link.closed && identityConnected();
  }

  long identityEpoch() {
    return identity.epoch();
  }

  boolean currentIdentity(long epoch) {
    return identity.current(epoch);
  }

  boolean ownerMode() {
    return identity.owner();
  }

  String identityLabel() {
    return !identity.ready() ? "身份待确认" : identity.owner() ? "主人 · Dot" : "访客 · MiniMax";
  }

  boolean enterGuest() {
    if (!foreground
        || !isConnected()
        || !identity.owner()
        || !identity.supported()
        || visual
        || textSession
        || announcement
        || meetingReserved()
        || "thinking".equals(phase)
        || "speaking".equals(phase)
        || "calibrating".equals(phase)) return false;
    final long revision;
    synchronized (routeLock) {
      if (!foreground || !isConnected() || !identity.owner()) return false;
      try {
        revision = identity.beginGuest();
      } catch (IOException error) {
        return false;
      }
      identityHello = false;
      identityRequestId = java.util.UUID.randomUUID().toString().replace("-", "");
    }
    final String requestId = identityRequestId;
    // Invalidate queued owner callbacks and stop output before asynchronous disk work.
    stopMicrophone();
    cancelVision();
    cancelPlayback();
    listener.identityChanged(true);
    new Thread(
            () -> {
              try {
                identity.persistGuest(revision);
                if (identity.ready() || identity.revision() != revision)
                  return; // A newer handshake has already reconciled this intent.
                JSONObject request = Wire.event("identity_request");
                Wire.put(request, "request_id", requestId);
                JSONObject arguments = new JSONObject();
                Wire.put(arguments, "action", "guest");
                Wire.put(arguments, "revision", revision);
                Wire.put(request, "arguments", arguments);
                send(request);
              } catch (IOException error) {
                if (identity.revision() != revision || identity.ready()) return;
                listener.identityChanged(true);
                listener.state("error", "访客切换未确认，访问已锁定，请在 Mac 检查");
              }
            },
            "cc8-identity-choice")
        .start();
    return true;
  }

  private boolean handleIdentityEvent(JSONObject j, long route, Link source) throws IOException {
    String event = j.optString("event");
    if ("identity_state".equals(event)) {
      boolean duplicate =
          identityConnected()
              && identity.token().equals(j.optString("token"))
              && identity.mode().equals(j.optString("identity_mode"))
              && j.opt("identity_revision") instanceof Number
              && ((Number) j.opt("identity_revision")).longValue() == identity.revision();
      boolean restore = identity.guestRequested() && "owner".equals(j.optString("identity_mode"));
      if (!identity.accept(j, route)) return true;
      identityRequestId = "";
      if (restore) {
        stopMicrophone();
        cancelVision();
        cancelPlayback();
      }
      if (!duplicate) {
        identityHello = false;
        sourcesPresentation = null;
        photoFollowup.clear();
        speechCorrection = null;
        listener.identityChanged(restore);
      }
      JSONObject ack = Wire.event("identity_applied");
      Wire.put(ack, "token", identity.token());
      Wire.put(ack, "mode", identity.mode());
      Wire.put(ack, "revision", identity.revision());
      source.event(ack);
      return true;
    }
    if ("identity_result".equals(event) || "identity_unavailable".equals(event)) {
      if ("identity_result".equals(event)) {
        if (identityRequestId.isEmpty() || !identityRequestId.equals(j.optString("request_id")))
          return true;
        identityRequestId = "";
      } else if (identityConnected()) return true;
      if (!Boolean.TRUE.equals(j.opt("ok"))) {
        stopMicrophone();
        cancelPlayback();
        listener.identityChanged(true);
        listener.state("idle", j.optString("message", "身份切换未确认，请在 Mac 检查"));
      }
      return true;
    }
    return false;
  }

  private boolean guestEvent(String event, JSONObject data) {
    if ("device_control".equals(event)) return "status".equals(data.optString("action"));
    return "hello".equals(event)
        || "brain_provider".equals(event)
        || "status".equals(event)
        || "transcript".equals(event)
        || "dot_result".equals(event)
        || "text_begin".equals(event)
        || "text_done".equals(event)
        || "play_begin".equals(event)
        || "play_end".equals(event)
        || "caption_timing".equals(event)
        || "playback_pause".equals(event)
        || "playback_resume".equals(event)
        || "capture_started".equals(event)
        || "capture_rejected".equals(event)
        || "screen_sleep".equals(event)
        || "screen_wake".equals(event)
        || "workspace".equals(event)
        || "workspace_result".equals(event)
        || "web_sources".equals(event)
        || "cancel".equals(event)
        || "ping".equals(event);
  }

  private boolean identityConnected() {
    return identity.ready() && identityHello;
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
    synchronized (routeLock) {
      if (foreground) return;
    }
    try {
      identity.load();
      listener.identityChanged(false);
    } catch (IOException error) {
      listener.identityChanged(true);
      listener.state("error", "身份记录无法读取，访问已锁定，请在 Mac 检查");
      return;
    }
    final int ticket;
    synchronized (routeLock) {
      if (foreground) return;
      foreground = true;
      availability.begin(SystemClock.elapsedRealtime());
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
    ServerSocket listener;
    synchronized (routeLock) {
      revokeMeeting();
      availability.end(SystemClock.elapsedRealtime());
      cancelVision();
      textSession = false;
      foreground = false;
      lifecycle++;
      routeEpoch++;
      identity.route(routeEpoch);
      identityHello = false;
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

  boolean currentSourcesPresentation(SourcesPresentation update) {
    synchronized (routeLock) {
      return identityConnected()
          && foreground
          && update != null
          && sourcesPresentation == update
          && currentRoute(update.route)
          && update.generation >= minimumGeneration;
    }
  }

  private void closeSocket() {
    final Link usb, wifi;
    final long route;
    synchronized (routeLock) {
      revokeMeeting();
      meetingSupported = meetingHostReserved = false;
      usb = usbLink;
      wifi = wifiLink;
      activeLink = null;
      route = routeEpoch;
      sourcesPresentation = null;
      photoFollowup.clear();
      workflowConsent.clear();
      pendingPhotoId = pendingPhotoForgetId = "";
      photoFollowupSupported =
          visionModesSupported =
              macWindowSupported =
                  vocabularySupported =
                      workflowSupported =
                          reportSourcesSupported =
                              reportArchiveSupported =
                                  reportProgressSupported =
                                      threadQueueSupported =
                                          dotChatSupported =
                                              savedTasksSupported =
                                                  preferenceMemorySupported =
                                                      resultProvenanceSupported =
                                                          resultReadingSupported = false;
      voicePreferences = null;
      speechCorrection = null;
      speechCorrectionRevision = -1;
      output = null;
      socket = null;
      availability.offline(SystemClock.elapsedRealtime());
    }
    if (usb != null) usb.close();
    if (wifi != null) wifi.close();
    listener.link(false, route);
  }

  private void accept(int ticket) {
    boolean reportedUnavailable = false;
    while (foreground && lifecycle == ticket) {
      try (ServerSocket ss = new ServerSocket()) {
        synchronized (routeLock) {
          if (!foreground || lifecycle != ticket) return;
          ss.setReuseAddress(true);
          ss.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), 18765), 1);
          server = ss;
        }
        reportedUnavailable = false;
        while (foreground && lifecycle == ticket) {
          Socket connection = ss.accept();
          Link next;
          synchronized (routeLock) {
            if (!foreground || lifecycle != ticket) {
              connection.close();
              return;
            }
            Link old = usbLink;
            if (old != null && !old.closed) {
              connection.close();
              continue;
            }
            next = new Link(connection, "usb", ticket);
            usbLink = next;
          }
          new Thread(next, "cc8-usb-link").start();
        }
      } catch (IOException error) {
        synchronized (routeLock) {
          if (!foreground || lifecycle != ticket) return;
          if (!reportedUnavailable && activeLink == null)
            listener.state("idle", "USB 通道等待重连，Wi-Fi 仍可连接");
          reportedUnavailable = true;
        }
      } finally {
        synchronized (routeLock) {
          if (lifecycle == ticket) server = null;
        }
      }
      // A bind or accept error must not permanently disable USB in this foreground session.
      if (!foreground || lifecycle != ticket) return;
      try {
        Thread.sleep(1000);
      } catch (InterruptedException error) {
        Thread.currentThread().interrupt();
        return;
      }
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
    // Local fallback may preserve an already open recorder, but cannot invent
    // the host's activation token. Its provisional Ready is ignored by a new host.
    activateRoute(next, null, next.outputBound);
  }

  private void activateRoute(Link next, String routeToken, boolean outputBound) throws IOException {
    // routeLock 保证只选一路；切换可以保留已开启的收音，但必须丢弃旧回答和看图。
    synchronized (routeLock) {
      if (!foreground || next.closed || next.ticket != lifecycle) return;
      next.ready = true;
      boolean changed =
          activeLink != next
              || !java.util.Objects.equals(next.routeToken, routeToken)
              || next.outputBound != outputBound;
      final CaptureWindow currentCapture;
      synchronized (this) {
        currentCapture = recording ? captureWindow : null;
      }
      boolean captureWasActive = currentCapture != null && currentCapture.nativeReady;
      if (changed) {
        revokeMeeting();
        meetingSupported = meetingHostReserved = false;
        sourcesPresentation = null;
        // A reused USB Link after Wi-Fi is still a new activation. Invalidate
        // queued workspace callbacks before publishing this route's ready state.
        routeEpoch++;
        identity.route(routeEpoch);
        identityHello = false;
        desktopCameraAuth.reset();
        if (visual) {
          visual = false;
          listener.vision(false, "连接切换，请重新看图", false);
        }
        cancelPlayback();
        speakerOwner.endTasksThrough(Integer.MAX_VALUE);
        announcement = false;
        textSession = false;
        listener.playback(false);
        listener.caption("", "attentive");
        minimumGeneration = 0;
        statusFeedback.reset();
        activeLink = next;
        next.routeToken = routeToken;
        next.outputBound = outputBound;
        next.captureStartToken = null;
        next.pairedCaptureRequested = false;
        photoFollowup.clear();
        workflowConsent.clear();
        pendingPhotoId = pendingPhotoForgetId = "";
        photoFollowupSupported =
            visionModesSupported =
                macWindowSupported =
                    vocabularySupported =
                        workflowSupported =
                            reportSourcesSupported =
                                reportArchiveSupported =
                                    reportProgressSupported =
                                        threadQueueSupported =
                                            dotChatSupported =
                                                savedTasksSupported =
                                                    preferenceMemorySupported =
                                                        resultProvenanceSupported =
                                                            resultReadingSupported = false;
        voicePreferences = null;
        speechCorrection = null;
        speechCorrectionRevision = -1;
        socket = next.connection;
        output = next.out;
        phase = recordingPhase();
      }
      JSONObject hello = Wire.event("device_ready");
      Wire.put(hello, "identity_version", 1);
      Wire.put(hello, "identity_guest_requested", identity.guestRequested());
      Wire.put(hello, "supports_playback_pause", true);
      Wire.put(hello, "speech_correction_version", 1);
      Wire.put(hello, "meeting_version", 1);
      Wire.put(hello, "playback_pause_applied_version", 1);
      Wire.put(hello, "playout_receipt_version", 1);
      Wire.put(hello, "render_clock_report_version", 1);
      Wire.put(hello, "playback_done_id_version", 1);
      Wire.put(hello, "desktop_camera_version", 1);
      Wire.put(hello, "vision_capture_version", 2);
      Wire.put(hello, "vision_review_version", 1);
      Wire.put(hello, "vision_followup_version", 1);
      Wire.put(hello, "desktop_camera_challenge", desktopCameraAuth.challenge());
      Wire.put(hello, "protocol", BuildConfig.CC8_PROTOCOL);
      Wire.put(hello, "sample_rate", 16000);
      Wire.put(hello, "capture_active", captureWasActive);
      Wire.put(hello, "capture_reject_version", 1);
      Wire.put(hello, "capture_start_ack_version", 1);
      Wire.put(hello, "paired_capture_version", 1);
      Wire.put(hello, "wake_mode", wakeMode);
      next.event(hello);
      availability.online(SystemClock.elapsedRealtime());
      // The hello receipt registers any current capture on the new route.
      if (changed) listener.link(true, routeEpoch);
    }
  }

  private String recordingPhase() {
    CaptureWindow window = captureWindow;
    if (!recording || window == null) return "idle";
    return window.startConfirmed ? "listening" : "calibrating";
  }

  private void registerCapture(CaptureWindow window, int captureEpoch) throws IOException {
    Link destination = activeLink;
    if (!foreground
        || !recording
        || !identity.ready()
        || !identityHello
        || window == null
        || captureWindow != window
        || epoch != captureEpoch
        || !window.nativeReady
        || window.registeredRoute == routeEpoch
        || window.watchdogRemaining(SystemClock.elapsedRealtime()) == 0
        || destination == null
        || destination.captureStartToken == null) return;
    if (window.paired && !destination.pairedCaptureRequested) {
      if (stopMicrophone(window)) listener.captureRejected(captureEpoch + 1);
      return;
    }
    String token = destination.captureStartToken;
    if (window.startToken == null) window.startToken = token;
    if (!window.startToken.equals(token)) {
      if (stopMicrophone(window)) listener.captureRejected(captureEpoch + 1);
      return;
    }
    boolean rebind = window.startConfirmed;
    JSONObject event = Wire.event(rebind ? "session_rebind" : "session_start");
    if (rebind) Wire.put(event, "capture_active", true);
    Wire.put(event, "epoch", captureEpoch);
    Wire.put(event, "capture_id", window.id);
    if (!token.isEmpty()) Wire.put(event, "capture_start_token", token);
    Wire.put(event, "source", 7);
    Wire.put(event, "sample_rate", 16000);
    Wire.put(event, "capture_frontend", window.paired ? "paired" : "mono");
    Wire.put(event, "capture_sample_rate", window.paired ? 48000 : 16000);
    Wire.put(event, "capture_channels", window.paired ? 4 : 1);
    if (window.captureFallbackReason != null)
      Wire.put(event, "capture_fallback_reason", window.captureFallbackReason);
    Wire.put(event, "render_gain", .35);
    destination.event(event);
    window.registeredRoute = routeEpoch;
    // Older Mac backends have no acceptance receipt.
    if (token.isEmpty()) window.startConfirmed = true;
  }

  private boolean captureHello(Link source, JSONObject hello) throws IOException {
    Object token = hello.opt("capture_start_token");
    if (token != null && (!(token instanceof String) || !((String) token).matches("[a-f0-9]{32}")))
      throw new IOException("Invalid capture start capability");
    Object frontend = hello.opt("capture_frontend");
    if (frontend != null && !"mono".equals(frontend) && !"paired".equals(frontend))
      throw new IOException("Invalid capture frontend capability");
    source.pairedCaptureRequested = "paired".equals(frontend);
    source.captureStartToken = token == null ? "" : (String) token;
    identityHello = true;
    CaptureWindow window = captureWindow;
    registerCapture(window, epoch);
    listener.link(true, routeEpoch);
    return window == null || captureWindow == window;
  }

  private void confirmCaptureStart(JSONObject receipt, long route) {
    CaptureWindow window = captureWindow;
    if (recording
        && currentRoute(route)
        && window != null
        && window.registeredRoute == route
        && window.watchdogRemaining(SystemClock.elapsedRealtime()) > 0
        && window.id.equals(receipt.opt("capture_id"))
        && Integer.valueOf(epoch).equals(receipt.opt("epoch"))) window.startConfirmed = true;
  }

  private void lost(Link link) {
    synchronized (routeLock) {
      if (usbLink == link) usbLink = null;
      if (wifiLink == link) wifiLink = null;
      if (activeLink != link) return;
      revokeMeeting();
      meetingSupported = meetingHostReserved = false;
      activeLink = null;
      routeEpoch++;
      identity.route(routeEpoch);
      identityHello = false;
      sourcesPresentation = null;
      photoFollowup.clear();
      workflowConsent.clear();
      pendingPhotoId = pendingPhotoForgetId = "";
      photoFollowupSupported =
          visionModesSupported =
              macWindowSupported =
                  vocabularySupported =
                      workflowSupported =
                          reportSourcesSupported =
                              reportArchiveSupported =
                                  reportProgressSupported =
                                      threadQueueSupported =
                                          dotChatSupported =
                                              savedTasksSupported =
                                                  preferenceMemorySupported =
                                                      resultProvenanceSupported =
                                                          resultReadingSupported = false;
      voicePreferences = null;
      speechCorrection = null;
      speechCorrectionRevision = -1;
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
      // Timestamp the observed loss before bounded audio/camera cleanup.
      availability.offline(SystemClock.elapsedRealtime());
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
    String captureStartToken;
    String routeToken;
    boolean outputBound;
    volatile boolean pairedCaptureRequested;
    DataOutputStream out;

    Link(Socket connection, String kind, int ticket) {
      this.connection = connection;
      this.kind = kind;
      this.ticket = ticket;
    }

    void event(JSONObject value) throws IOException {
      synchronized (routeLock) {
        Wire.Packet packet = eventPacket(value);
        if (!value.optString("event").startsWith("transport_")) ownPacket(packet, this);
        offer(packet);
      }
    }

    void cameraEvent(JSONObject value, int gen, String ident, long route) throws IOException {
      Wire.Packet packet = eventPacket(value);
      packet.cameraGeneration = gen;
      packet.cameraRequestId = ident;
      packet.cameraRoute = route;
      ownPacket(packet, this);
      offer(packet);
    }

    private Wire.Packet eventPacket(JSONObject value) {
      Wire.Packet packet = new Wire.Packet();
      packet.type = Wire.EVENT;
      packet.generation = epoch;
      packet.nanos = System.nanoTime();
      packet.data = Wire.jsonBytes(value);
      return packet;
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
                      if (packet != null) writeQueuedPacket(packet, this);
                    }
                  } catch (Exception error) {
                    close();
                  }
                },
                "cc8-" + kind + "-writer")
            .start();
        JSONObject hello = Wire.event("transport_hello");
        Wire.put(hello, "protocol", BuildConfig.CC8_PROTOCOL);
        Wire.put(hello, "route_envelope_version", 1);
        Wire.put(hello, "route_output_version", 1);
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

  private void handle(Wire.Packet received, Link source) throws Exception {
    final long route;
    final String receiveToken;
    final boolean outputBound;
    synchronized (routeLock) {
      route = routeFor(source);
      receiveToken = source.routeToken;
      outputBound = source.outputBound;
    }
    final boolean wrapped = received.type == Wire.ROUTE_BOUND;
    final Wire.Packet p;
    if (wrapped) {
      if (!outputBound || route < 0) return;
      p = Wire.unbindRoute(received, receiveToken);
      if (p == null) return;
    } else p = received;
    if (p.type == Wire.EVENT) {
      JSONObject control = Wire.json(p);
      String name = control.optString("event");
      if (!wrapped && "transport_ping".equals(name)) {
        source.event(Wire.event("transport_pong"));
        return;
      }
      if (!wrapped && "transport_activate".equals(name)) {
        Object token = control.opt("route_token");
        if (token != null
            && (!(token instanceof String) || !((String) token).matches("[a-f0-9]{32}")))
          throw new IOException("Invalid route activation");
        Object outputVersion = control.opt("route_output_version");
        boolean duplex = Integer.valueOf(1).equals(outputVersion);
        if (outputVersion != null && !duplex) throw new IOException("Invalid output capability");
        synchronized (routeLock) {
          if (!foreground || source.closed || source.ticket != lifecycle) return;
          // A live socket cannot silently downgrade after negotiating duplex.
          if (source.outputBound && !duplex) return;
          if (duplex) {
            Object owner = control.opt("route_owner"), serial = control.opt("route_serial");
            if (!(owner instanceof String)
                || !(serial instanceof Integer || serial instanceof Long)
                || token == null) throw new IOException("Invalid host activation identity");
            if (!hostActivation.accept((String) owner, ((Number) serial).longValue())) return;
          }
          activateRoute(source, (String) token, duplex);
        }
        return;
      }
      if (!wrapped && "transport_standby".equals(name)) {
        source.ready = true;
        return;
      }
      if ("lan_config".equals(name)) {
        if (outputBound && !wrapped) return;
        if (source == activeLink && "usb".equals(source.kind)) {
          try {
            synchronized (routeLock) {
              if (foreground && source == activeLink && currentRoute(route)) {
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
    if (outputBound && !wrapped) return;
    if (route < 0 || !currentRoute(route)) return;
    if (p.type == Wire.EVENT) {
      JSONObject j = Wire.json(p);
      synchronized (routeLock) {
        // Parsing may outlive a transport activation. Publish controls only on their original
        // route.
        if (!foreground || !currentRoute(route)) return;
        String event = j.optString("event");
        if (handleIdentityEvent(j, route, source)) return;
        if ("hello".equals(event) && j.opt("identity_mode") == null) {
          if (!identity.acceptLegacy(route)) return;
          listener.identityChanged(false);
        }
        boolean control =
            "cancel".equals(event)
                || "ping".equals(event)
                || "capture_rejected".equals(event)
                || "screen_wake".equals(event);
        if (!control && !identity.accepts(j, route)) return;
        if (!identity.owner() && !guestEvent(event, j)) return;
        j.remove("identity_mode");
        j.remove("identity_revision");
        if ("hello".equals(event)) {
          boolean keepVoiceStatus = captureHello(source, j);
          dotChatSupported = Boolean.TRUE.equals(j.opt("supports_dot_chat_handoff"));
          savedTasksSupported = Boolean.TRUE.equals(j.opt("supports_saved_task_results"));
          listener.recognition(j.optString("asr_engine", "sensevoice"));
          synchronized (routeLock) {
            if (!foreground || !currentRoute(route) || p.generation < minimumGeneration) return;
            listener.releaseInfo(j.optJSONObject("release_info"), route);
            preferenceMemorySupported = Boolean.TRUE.equals(j.opt("supports_preference_memory"));
            vocabularySupported = Boolean.TRUE.equals(j.opt("supports_vocabulary"));
            workflowSupported = Boolean.TRUE.equals(j.opt("supports_workflows"));
            meetingHello(j);
            reportSourcesSupported = Boolean.TRUE.equals(j.opt("supports_report_sources"));
            reportArchiveSupported = Boolean.TRUE.equals(j.opt("supports_report_archive"));
            threadQueueSupported = Boolean.TRUE.equals(j.opt("supports_thread_queue"));
            reportProgressSupported = Boolean.TRUE.equals(j.opt("supports_report_progress"));
            visionModesSupported = Boolean.TRUE.equals(j.opt("supports_vision_modes"));
            macWindowSupported = Boolean.TRUE.equals(j.opt("supports_mac_window"));
            photoFollowupSupported = Boolean.TRUE.equals(j.opt("supports_photo_followup"));
            if (!photoFollowupSupported) photoFollowup.clear();
            resultProvenanceSupported = Boolean.TRUE.equals(j.opt("supports_result_provenance"));
            resultReadingSupported = Boolean.TRUE.equals(j.opt("supports_result_reading"));
            acceptVoicePreferences(j.optJSONObject("voice_preferences"));
            if (Boolean.TRUE.equals(j.opt("playback_interrupted_by_transport"))) {
              if (!statusFeedback.transportInterruption(p.generation)) return;
              phase = "error";
              listener.state(phase, j.optString("message", "播报因连接切换中断，可在任务页查看已完成结果。"));
            } else if (keepVoiceStatus && !statusFeedback.transportInterrupted(p.generation)) {
              String voicePhase = recordingPhase();
              listener.state(
                  voicePhase,
                  "calibrating".equals(voicePhase)
                      ? "正在准备音频，请保持安静"
                      : j.optString("message", "Mac 已连接，点击开始语音"));
            }
          }
        } else if ("speech_correction".equals(event)) {
          if (p.generation < minimumGeneration) return;
          if (acceptSpeechCorrection(j)) listener.speechCorrection(j, route);
        } else if ("voice_preferences".equals(event)) {
          if (p.generation < minimumGeneration) return;
          acceptVoicePreferences(j.optJSONObject("voice_preferences"));
          listener.workspaceResult(Boolean.TRUE.equals(j.opt("ok")), j.optString("message"), route);
        } else if ("capture_started".equals(event)) {
          confirmCaptureStart(j, route);
        } else if ("capture_rejected".equals(event)) {
          rejectCapture(j.opt("capture_id"), route);
        } else if ("screen_sleep".equals(event)) {
          if (recording) listener.screen(true);
        } else if ("screen_wake".equals(event)) {
          listener.screen(false);
        } else if ("device_control".equals(event)) {
          deviceControl(j);
        } else if ("text_begin".equals(event)) {
          if (foreground
              && p.generation >= minimumGeneration
              && statusFeedback.begin(p.generation)) {
            textSession = true;
            textGeneration = p.generation;
            if (!("workflow".equals(j.optString("input_origin"))
                && workflowConsent.bind(
                    j.optString("workflow_camera_nonce"),
                    route,
                    p.generation,
                    SystemClock.elapsedRealtime()))) {
              String revoked = workflowConsent.clear();
              if (!revoked.isEmpty() && revoked.equals(pendingVisionNonce)) pendingVisionNonce = "";
            }
            beginTaskOutput("text", p.generation);
            boolean meetingDocument = "meeting".equals(j.optString("input_origin"));
            listener.playback(!meetingDocument);
            if (meetingDocument) listener.state("thinking", "Dot 正在处理会议文字 · 麦克风关闭");
            boolean corrected = "correction".equals(j.optString("input_origin"));
            listener.heard((corrected ? "已确认更正：" : "") + j.optString("text"));
            listener.caption(
                meetingDocument
                    ? "正在处理完整会议文字。\n结果可在会议页查看和导出。"
                    : corrected ? "已确认更正，正在处理新问题。\n原来已执行的操作不会撤销。" : "已收到文字指令。\n正在处理…",
                "attentive");
          }
        } else if ("text_done".equals(event)) {
          if (textSession && p.generation == textGeneration) {
            textSession = false;
            speakerOwner.endTask(p.generation);
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
          if (foreground)
            listener.workspaceResult(j.optBoolean("ok"), j.optString("message"), route);
        } else if ("photo_followup_ready".equals(event)) {
          synchronized (routeLock) {
            if (!foreground
                || !photoFollowupSupported
                || !currentRoute(route)
                || p.generation != minimumGeneration) return;
            Object width = j.opt("width"), height = j.opt("height"), ttl = j.opt("ttl_ms");
            if (!(width instanceof Integer)
                || !(height instanceof Integer)
                || !(ttl instanceof Integer)) return;
            photoFollowup.offer(
                j.optString("photo_id"),
                p.generation,
                (Integer) width,
                (Integer) height,
                (Integer) ttl,
                route,
                SystemClock.elapsedRealtime());
          }
        } else if ("photo_followup_expired".equals(event)) {
          synchronized (routeLock) {
            if (foreground && currentRoute(route)) {
              photoFollowup.expire(j.optString("photo_id"), p.generation, route);
              if (p.generation == minimumGeneration
                  && !pendingPhotoForgetId.isEmpty()
                  && pendingPhotoForgetId.equals(j.optString("photo_id"))) {
                pendingPhotoForgetId = "";
                listener.workspaceResult(true, "照片已清除", route);
              }
            }
          }
        } else if ("photo_followup_forget_rejected".equals(event)) {
          synchronized (routeLock) {
            if (foreground
                && currentRoute(route)
                && p.generation == minimumGeneration
                && !pendingPhotoForgetId.isEmpty()
                && pendingPhotoForgetId.equals(j.optString("photo_id"))) {
              pendingPhotoForgetId = "";
              listener.workspaceResult(false, "照片已变化，请重新打开看图入口", route);
            }
          }
        } else if ("photo_followup_rejected".equals(event)) {
          synchronized (routeLock) {
            if (!foreground
                || !currentRoute(route)
                || p.generation < minimumGeneration
                || pendingPhotoId.isEmpty()
                || !pendingPhotoId.equals(j.optString("photo_id"))
                || !pendingVisionNonce.equals(j.optString("nonce"))) return;
            pendingPhotoId = "";
            pendingVisionNonce = "";
            if (!visual && !textSession && !announcement)
              listener.vision(false, j.optString("message", "照片已不可追问，请重新拍摄"), false);
          }
        } else if ("vision_begin".equals(event)) {
          String nonce = j.optString("nonce");
          if (!foreground
              || p.generation < minimumGeneration
              || (visual && p.generation == visualGeneration)) return;
          boolean desktop = "desktop".equals(j.optString("origin"));
          boolean workflow = "workflow".equals(j.optString("origin"));
          if (workflow
              && !workflowConsent.accepts(
                  nonce, route, p.generation, SystemClock.elapsedRealtime())) return;
          boolean macWindow = "mac_window".equals(j.optString("origin"));
          if (macWindow
              && (!macWindowSupported
                  || !nonce.matches("[a-f0-9]{32}")
                  || !nonce.equals(pendingVisionNonce))) return;
          boolean userFollowup = "followup".equals(j.optString("origin"));
          boolean voiceFollowup = "followup_voice".equals(j.optString("origin"));
          boolean followup = userFollowup || voiceFollowup;
          if (voiceFollowup
              && (!photoFollowupSupported
                  || !recording
                  || !nonce.matches("[a-f0-9]{32}")
                  || !j.optString("photo_id").matches("[a-f0-9]{32}"))) return;
          if (userFollowup
              && (!photoFollowupSupported
                  || pendingPhotoId.isEmpty()
                  || !pendingPhotoId.equals(j.optString("photo_id"))
                  || !nonce.equals(pendingVisionNonce))) return;
          if (desktop
              && !lan.authorizeDesktopCamera(
                  desktopCameraAuth, nonce, p.generation, j.optString("camera_auth"))) return;
          if (!desktop
              && ((!recording && !nonce.equals(pendingVisionNonce))
                  || (nonce.isEmpty() && !recording))) return;
          if (workflow) {
            // This is a camera stage inside the already admitted text request,
            // not a second begin that can reopen an unrelated/failed task.
            if (!textSession
                || textGeneration != p.generation
                || !statusFeedback.current(p.generation)
                || statusFeedback.failed(p.generation)
                || statusFeedback.cancelled(p.generation)) return;
          } else if (!statusFeedback.begin(p.generation)) return;
          desktopVisionNonce = desktop ? nonce : "";
          visual = true;
          visualGeneration = p.generation;
          if (!desktop) beginTaskOutput("vision", p.generation);
          visionRequestId =
              macWindow
                  ? nonce
                  : userFollowup ? pendingPhotoId : voiceFollowup ? j.optString("photo_id") : "";
          if (followup || macWindow) {
            photoFollowup.clear();
            pendingPhotoId = "";
            pendingVisionNonce = "";
          }
          listener.dotResult("", false, "");
          listener.caption(
              macWindow
                  ? "请在 Mac 系统面板选择一个窗口，核对冻结画面后点击发送。CC8 摄像头关闭。"
                  : followup
                      ? "正在分析你指定的同一张照片，不重新拍摄。"
                      : desktop
                          ? "Dot 请求拍照，请调整画面；预览 5 秒后拍摄并返回当前聊天。"
                          : "请调整画面，预览 5 秒后拍摄；拍后核对按你的看图设置执行。",
              "attentive");
          listener.screen(false);
          listener.vision(
              true,
              macWindow
                  ? "等待 Mac 选窗确认 · CC8 摄像头关闭"
                  : followup ? "正在追问本张照片 · 摄像头关闭" : "请调整画面 · 预览 5 秒后拍摄",
              false);
          final int waitingGeneration = visualGeneration;
          final long waitingRoute = routeEpoch;
          new android.os.Handler(android.os.Looper.getMainLooper())
              .postDelayed(
                  () -> {
                    synchronized (routeLock) {
                      if (currentRoute(waitingRoute)
                          && visual
                          && visualGeneration == waitingGeneration
                          && visionRequestId.isEmpty()) {
                        cancelVision();
                        listener.state(recording ? "listening" : "idle", "拍摄请求超时，请重试");
                      }
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
          Wire.put(j, "vision_route", route);
          boolean allowed =
              "workflow".equals(j.optString("origin"))
                  ? nonce.equals(pendingVisionNonce)
                      && workflowConsent.consume(
                          nonce, route, p.generation, SystemClock.elapsedRealtime())
                  : "desktop".equals(j.optString("origin"))
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
          listener.cameraCapture(j);
        } else if ("vision_done".equals(event)) {
          if (visual && p.generation == visualGeneration) {
            visual = false;
            speakerOwner.endTask(p.generation);
            if ("workflow".equals(j.optString("origin"))
                && textSession
                && p.generation == textGeneration
                && foreground
                && p.generation >= minimumGeneration) beginTaskOutput("text", p.generation);
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
          speakerOwner.endTask(p.generation);
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
          String listeningState = "unknown";
          if ("listening".equals(nextPhase)) {
            CaptureWindow window = captureWindow;
            listeningState =
                ListeningState.hostValue(
                    j.opt("listening_state"),
                    j.opt("capture_id"),
                    j.opt("epoch"),
                    window == null ? null : window.id,
                    epoch,
                    window != null && window.startConfirmed);
            if (listeningState == null) return;
          }
          boolean permitted =
              recording
                  || (announcement && p.generation == announcementGeneration)
                  || (visual && p.generation == visualGeneration)
                  || (textSession && p.generation == textGeneration)
                  // Playback failure first cancels output, then reports why.
                  // Admit that one terminal error only for the request we owned.
                  // Before begin, require a typed startup marker on the exact
                  // unstarted cancellation successor instead.
                  || ("error".equals(nextPhase)
                      && (statusFeedback.cancelled(p.generation)
                          || (Boolean.TRUE.equals(j.opt("request_start_failed"))
                              && statusFeedback.unstarted(p.generation))));
          if (foreground
              && permitted
              && p.generation >= minimumGeneration
              && statusFeedback.accept(p.generation, nextPhase)) {
            phase = nextPhase;
            listener.status(
                new StatusPresentation(
                    phase,
                    j.optString("message"),
                    listeningState,
                    p.generation,
                    epoch,
                    lifecycle,
                    route,
                    captureWindow));
          }
        } else if ("transcript".equals(event)) {
          if (recording && p.generation >= minimumGeneration && currentRoute(route)) {
            String text = j.optString("text");
            CaptureWindow window = captureWindow;
            if (window != null)
              window.observeTranscript(route, p.generation, text, SystemClock.elapsedRealtime());
            listener.heard(text);
          }
        } else if ("announcement_begin".equals(event)) {
          if (foreground
              && p.generation >= minimumGeneration
              && statusFeedback.begin(p.generation)) {
            announcement = true;
            announcementGeneration = p.generation;
            beginTaskOutput("announcement", p.generation);
            listener.announcement();
            listener.playback(true);
          }
        } else if ("announcement_done".equals(event)) {
          if (announcement && p.generation == announcementGeneration) {
            announcement = false;
            speakerOwner.endTask(p.generation);
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
                j.optString("text"),
                j.optBoolean("truncated"),
                j.optString("handoff_source_title"));
        } else if ("dot_chat_state".equals(event)) {
          dotChatSupported = true;
          if (foreground) listener.dotChats(j);
        } else if ("preference_result".equals(event)) {
          if (foreground) listener.preferenceResult(j, route);
        } else if ("vocabulary_result".equals(event)) {
          if (foreground) listener.vocabularyResult(j, route);
        } else if ("workflow_result".equals(event)) {
          if (foreground && workflowSupported) listener.workflowResult(j, route);
        } else if ("meeting_result".equals(event) || "meeting_update".equals(event)) {
          if (foreground && meetingSupported) acceptMeeting(j, route, p.generation);
        } else if ("saved_task_result".equals(event)) {
          if (foreground) listener.savedTasks(j);
        } else if ("web_sources".equals(event)) {
          if (foreground
              && (recording || visual || textSession)
              && p.generation >= minimumGeneration) {
            sourcesPresentation =
                new SourcesPresentation(
                    j.optJSONArray("items"),
                    j.optString("retrieved_at"),
                    j.optString("time_basis"),
                    route,
                    p.generation);
            listener.sources(sourcesPresentation);
          }
        } else if ("cancel".equals(event)) {
          if (p.generation < minimumGeneration
              || (visual && p.generation < visualGeneration)
              || !statusFeedback.cancel(
                  p.generation, recording || announcement || visual || textSession)) return;
          minimumGeneration = Math.max(minimumGeneration, p.generation);
          cancelMeetingForGeneration(p.generation);
          String revoked = workflowConsent.cancel(p.generation);
          if (!revoked.isEmpty() && revoked.equals(pendingVisionNonce)) pendingVisionNonce = "";
          photoFollowup.clear();
          pendingPhotoForgetId = "";
          sourcesPresentation = new SourcesPresentation(null, "", "", route, p.generation);
          listener.sources(sourcesPresentation);
          if (Boolean.TRUE.equals(j.opt("speech_correction_cleared"))) clearSpeechCorrection(route);
          if (visual) {
            visual = false;
            listener.vision(false, "已停止看图 · 摄像头关闭", false);
          }
          announcement = false;
          textSession = false;
          listener.playback(false);
          cancelPlaybackForCommand(p.generation, route);
          speakerOwner.endTasksThrough(p.generation);
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
      }
    } else if (p.type == Wire.SPEAKER) {
      if (!identity.ready() || !identityHello) return;
      Player current = player;
      if ((recording || announcement || visual || textSession)
          && current != null
          && current.playbackRoute == route
          && currentRoute(route)
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
    dotChatRequest(requestId, action, sourceId, cursor, null);
  }

  void dotChatRequest(
      String requestId, String action, String sourceId, String cursor, String period) {
    if (!foreground || !isConnected()) return;
    JSONObject j = Wire.event("dot_chat_request");
    Wire.put(j, "request_id", requestId);
    Wire.put(j, "action", action);
    if (sourceId != null) Wire.put(j, "source_id", sourceId);
    if (cursor != null) Wire.put(j, "cursor", cursor);
    if (period != null) Wire.put(j, "period", period);
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

  boolean revealResultFile(String requestId, String id, int index, boolean saved, long route) {
    if (!resultProvenanceSupported) return false;
    JSONObject j = Wire.event("saved_task_request");
    Wire.put(j, "request_id", requestId);
    Wire.put(j, "action", saved ? "reveal_saved" : "reveal_live");
    Wire.put(j, "id", id);
    Wire.put(j, "file_index", index);
    return sendWorkspace(j, route);
  }

  boolean readResult(String id, boolean restart, long route) {
    if (!resultReadingSupported) return false;
    JSONObject j = Wire.event("result_read_request");
    Wire.put(j, "request_id", java.util.UUID.randomUUID().toString().replace("-", ""));
    Wire.put(j, "id", id);
    Wire.put(j, "restart", restart);
    return sendWorkspace(j, route);
  }

  private void acceptVoicePreferences(JSONObject data) {
    voicePreferences = null;
    if (data == null
        || !Integer.valueOf(1).equals(data.opt("version"))
        || !(data.opt("revision") instanceof Integer)
        || data.optInt("revision", -1) < 0
        || !(data.opt("natural_barge") instanceof Boolean)
        || !Boolean.TRUE.equals(data.opt("supported"))) return;
    String rate = data.optString("speech_rate");
    if (data.opt("recognition_language_supported") != null
        && (!(data.opt("recognition_language_supported") instanceof Boolean)
            || !(data.opt("recognition_language") instanceof String)
            || !("auto".equals(data.optString("recognition_language"))
                || "zh".equals(data.optString("recognition_language"))
                || "en".equals(data.optString("recognition_language"))))) return;
    if (data.opt("environment_profiles_supported") != null
        && (!(data.opt("environment_profiles_supported") instanceof Boolean)
            || !(data.opt("environment_profile") instanceof String)
            || !("normal".equals(data.optString("environment_profile"))
                || "quiet".equals(data.optString("environment_profile"))
                || "television".equals(data.optString("environment_profile"))))) return;
    if ("slow".equals(rate) || "normal".equals(rate) || "fast".equals(rate))
      voicePreferences = data;
  }

  private boolean acceptSpeechCorrection(JSONObject state) {
    Object revision = state.opt("revision");
    if (!(revision instanceof Integer)
        || (Integer) revision < 0
        || (Integer) revision < speechCorrectionRevision
        || !(state.opt("pending") instanceof Boolean)) return false;
    if (Boolean.TRUE.equals(state.opt("pending"))) {
      if (!state.optString("draft_id").matches("[a-f0-9]{32}")
          || !(state.opt("original") instanceof String)
          || !(state.opt("proposed") instanceof String)
          || state.optString("original").isEmpty()
          || state.optString("original").length() > 1024
          || state.optString("proposed").isEmpty()
          || state.optString("proposed").length() > 1024) return false;
    }
    speechCorrectionRevision = (Integer) revision;
    speechCorrection = state;
    return true;
  }

  private void clearSpeechCorrection(long route) {
    JSONObject cleared = Wire.event("speech_correction");
    Wire.put(cleared, "pending", false);
    speechCorrection = cleared;
    listener.speechCorrection(cleared, route);
  }

  boolean currentSpeechCorrection(JSONObject state, long route) {
    synchronized (routeLock) {
      return foreground && currentRoute(route) && speechCorrection == state;
    }
  }

  boolean speechCorrectionAction(String id, String action, long route) {
    synchronized (routeLock) {
      JSONObject current = speechCorrection;
      if (current == null
          || !currentSpeechCorrection(current, route)
          || !Boolean.TRUE.equals(current.opt("pending"))
          || !id.equals(current.optString("draft_id"))) return false;
      JSONObject request = Wire.event("speech_correction_action");
      Wire.put(request, "request_id", java.util.UUID.randomUUID().toString().replace("-", ""));
      Wire.put(request, "draft_id", id);
      Wire.put(request, "action", action);
      return sendWorkspace(request, route);
    }
  }

  JSONObject voicePreferences(long route) {
    synchronized (routeLock) {
      return foreground && currentRoute(route) ? voicePreferences : null;
    }
  }

  boolean setVoicePreferences(String rate, boolean naturalBarge, int revision, long route) {
    synchronized (routeLock) {
      if (voicePreferences(route) == null) return false;
      JSONObject j = Wire.event("voice_preferences_set");
      Wire.put(j, "speech_rate", rate);
      Wire.put(j, "natural_barge", naturalBarge);
      Wire.put(j, "revision", revision);
      return sendWorkspace(j, route);
    }
  }

  boolean setRecognitionLanguage(String language, int revision, long route) {
    synchronized (routeLock) {
      JSONObject current = voicePreferences(route);
      if (current == null
          || !Boolean.TRUE.equals(current.opt("recognition_language_supported"))
          || !("auto".equals(language) || "zh".equals(language) || "en".equals(language)))
        return false;
      JSONObject j = Wire.event("recognition_language_set");
      Wire.put(j, "language", language);
      Wire.put(j, "revision", revision);
      return sendWorkspace(j, route);
    }
  }

  boolean setEnvironmentProfile(String profile, int revision, long route) {
    synchronized (routeLock) {
      JSONObject current = voicePreferences(route);
      if (current == null
          || !Boolean.TRUE.equals(current.opt("environment_profiles_supported"))
          || !("normal".equals(profile) || "quiet".equals(profile) || "television".equals(profile)))
        return false;
      JSONObject request = Wire.event("environment_profile_set");
      Wire.put(request, "profile", profile);
      Wire.put(request, "revision", revision);
      return sendWorkspace(request, route);
    }
  }

  boolean preferenceRequest(String requestId, JSONObject args, long route) {
    synchronized (routeLock) {
      if (!preferenceMemorySupported || !foreground || !currentRoute(route)) return false;
      JSONObject j = Wire.event("preference_request");
      Wire.put(j, "request_id", requestId);
      java.util.Iterator<String> keys = args.keys();
      while (keys.hasNext()) {
        String key = keys.next();
        if (!"action".equals(key)
            && !"revision".equals(key)
            && !"key".equals(key)
            && !"value".equals(key)
            && !"enabled".equals(key)) return false;
        Wire.put(j, key, args.opt(key));
      }
      return sendWorkspace(j, route);
    }
  }

  boolean vocabularyRequest(String requestId, JSONObject args, long route) {
    synchronized (routeLock) {
      if (!vocabularySupported
          || !foreground
          || !currentRoute(route)
          || !requestId.matches("[a-f0-9]{32}")) return false;
      JSONObject request = Wire.event("vocabulary_request");
      Wire.put(request, "request_id", requestId);
      java.util.Iterator<String> keys = args.keys();
      while (keys.hasNext()) {
        String key = keys.next();
        if (!("action".equals(key)
            || "revision".equals(key)
            || "term".equals(key)
            || "aliases".equals(key)
            || "alias".equals(key)
            || "source_id".equals(key))) return false;
        Wire.put(request, key, args.opt(key));
      }
      return sendWorkspace(request, route);
    }
  }

  private void cancelMeetingForGeneration(int generation) {
    if (!meetingId.isEmpty() && meetingRecorder != null && generation >= meetingGeneration)
      revokeMeeting();
  }

  private void meetingHello(JSONObject hello) {
    meetingSupported = Boolean.TRUE.equals(hello.opt("supports_meetings"));
    JSONObject state = hello.optJSONObject("meeting_state");
    meetingHostReserved =
        state != null
            && ("capturing".equals(state.optString("state"))
                || "paused".equals(state.optString("state"))
                || state.optInt("pending_count") > 0);
  }

  boolean meetingRequest(String requestId, JSONObject args, long route) {
    synchronized (routeLock) {
      if (!foreground
          || !meetingSupported
          || !currentRoute(route)
          || !requestId.matches("[a-f0-9]{32}")) return false;
      String action = args.optString("action");
      boolean starting = "start".equals(action) || "resume".equals(action);
      if (starting) {
        if (!Boolean.TRUE.equals(args.opt("confirmed"))
            || recording
            || visual
            || announcement
            || textSession
            || meetingRecorder != null
            || meetingReleaseFailed
            || context.checkCallingOrSelfPermission(Manifest.permission.RECORD_AUDIO)
                != PackageManager.PERMISSION_GRANTED
            || !meetingConsent.arm(requestId, route, SystemClock.elapsedRealtime())) return false;
      } else if ("pause".equals(action) || "end".equals(action)) {
        String pendingStart = revokeMeeting();
        if (!pendingStart.isEmpty()) abortMeetingStart(pendingStart, route);
      }
      JSONObject request = Wire.event("meeting_request");
      Wire.put(request, "request_id", requestId);
      java.util.Iterator<String> keys = args.keys();
      while (keys.hasNext()) {
        String key = keys.next();
        if (!"event".equals(key) && !"request_id".equals(key))
          Wire.put(request, key, args.opt(key));
      }
      if (sendWorkspace(request, route)) return true;
      if (starting) meetingConsent.reject(requestId, route);
      return false;
    }
  }

  private void abortMeetingStart(String requestId, long route) {
    if (!requestId.matches("[a-f0-9]{32}")) return;
    JSONObject args = new JSONObject();
    Wire.put(args, "action", "abort_start");
    Wire.put(args, "start_request_id", requestId);
    meetingRequest(WorkflowModel.id(), args, route);
  }

  void leaveMeeting(long route) {
    synchronized (routeLock) {
      if (!currentRoute(route)) return;
      String id = meetingId;
      String pendingStart = revokeMeeting();
      if (!pendingStart.isEmpty()) abortMeetingStart(pendingStart, route);
      if (!id.isEmpty()) {
        JSONObject args = new JSONObject();
        Wire.put(args, "action", "pause");
        Wire.put(args, "id", id);
        meetingRequest(WorkflowModel.id(), args, route);
      }
    }
  }

  /** Caller holds routeLock. Keep the recorder reference until its physical release receipt. */
  private String revokeMeeting() {
    String pendingStart = meetingConsent.clear();
    meetingId = meetingNonce = "";
    meetingRoute = -1;
    MeetingRecorder old = meetingRecorder;
    if (old != null) old.stop();
    return pendingStart;
  }

  private void acceptMeeting(JSONObject data, long route, int generation) {
    if (generation < minimumGeneration) return;
    String requestId = data.optString("request_id");
    JSONObject state = data.optJSONObject("state");
    JSONObject capture = data.optJSONObject("capture");
    if (capture != null && Boolean.TRUE.equals(data.opt("ok"))) {
      boolean consent = meetingConsent.take(requestId, route, SystemClock.elapsedRealtime());
      String id = capture.optString("id"), nonce = capture.optString("nonce");
      if (!consent
          || !id.matches("[a-f0-9]{32}")
          || !nonce.matches("[a-f0-9]{32}")
          || capture.optInt("generation", -1) != generation
          || generation < minimumGeneration
          || capture.optInt("sample_rate") != 16000
          || capture.optInt("packet_type") != Wire.MEETING
          || state == null
          || !id.equals(state.optString("id"))
          || !"capturing".equals(state.optString("state"))
          || state.optLong("revision", -1) < 0
          || recording
          || visual
          || announcement
          || textSession
          || meetingRecorder != null) {
        abortMeetingStart(requestId, route);
      } else {
        meetingId = id;
        meetingNonce = nonce;
        meetingRoute = route;
        meetingGeneration = generation;
        meetingRevision = state.optLong("revision");
        minimumGeneration = Math.max(minimumGeneration, generation);
        startMeetingRecorder(id, nonce, route, generation, requestId);
      }
    } else if ("meeting_result".equals(data.optString("event"))) {
      meetingConsent.reject(requestId, route);
    }
    if (state != null) {
      String mode = state.optString("state");
      meetingHostReserved =
          "capturing".equals(mode) || "paused".equals(mode) || state.optInt("pending_count") > 0;
      if (!meetingId.isEmpty()
          && meetingId.equals(state.optString("id"))
          && state.optLong("revision", -1) >= meetingRevision
          && !"capturing".equals(mode)) revokeMeeting();
    }
    listener.meetingResult(data, route);
  }

  private void startMeetingRecorder(
      String id, String nonce, long route, int generation, String requestId) {
    final MeetingRecorder[] owner = new MeetingRecorder[1];
    final long confirmedAt = SystemClock.elapsedRealtime();
    MeetingRecorder candidate =
        new MeetingRecorder(
            captureResourceLock,
            new MeetingRecorder.Port() {
              public boolean current() {
                synchronized (routeLock) {
                  return foreground
                      && currentRoute(route)
                      && meetingRecorder == owner[0]
                      && id.equals(meetingId)
                      && nonce.equals(meetingNonce)
                      && meetingRoute == route
                      && meetingGeneration == generation
                      && isConnected()
                      && !recording;
                }
              }

              public boolean start(AudioRecord record) {
                synchronized (routeLock) {
                  if (!current()
                      || SystemClock.elapsedRealtime() - confirmedAt >= 5000
                      || context.checkCallingOrSelfPermission(Manifest.permission.RECORD_AUDIO)
                          != PackageManager.PERMISSION_GRANTED) return false;
                  record.startRecording();
                  return record.getRecordingState() == AudioRecord.RECORDSTATE_RECORDING;
                }
              }

              public boolean send(byte[] pcm, long sequence, long frame) {
                synchronized (routeLock) {
                  Link destination = activeLink;
                  if (!current() || destination == null || destination.closed || !destination.ready)
                    return false;
                  byte[] data = new byte[16 + pcm.length];
                  for (int i = 0; i < 16; i++)
                    data[i] = (byte) Integer.parseInt(nonce.substring(i * 2, i * 2 + 2), 16);
                  System.arraycopy(pcm, 0, data, 16, pcm.length);
                  Wire.Packet packet = new Wire.Packet();
                  packet.type = Wire.MEETING;
                  packet.generation = generation;
                  packet.sequence = sequence;
                  packet.frame = frame;
                  packet.nanos = System.nanoTime();
                  packet.data = data;
                  try {
                    ownPacket(packet, destination);
                    destination.offer(packet);
                    return true;
                  } catch (IOException failure) {
                    destination.close();
                    return false;
                  }
                }
              }

              public void finished(boolean failed, boolean released) {
                synchronized (routeLock) {
                  if (meetingRecorder != owner[0]) return;
                  boolean stillOwned = current();
                  if (!released) meetingReleaseFailed = true;
                  meetingRecorder = null;
                  if (failed && stillOwned) {
                    JSONObject args = new JSONObject();
                    Wire.put(args, "action", "capture_failed");
                    Wire.put(args, "id", id);
                    Wire.put(args, "nonce", nonce);
                    Wire.put(args, "reason", "capture_failed");
                    meetingRequest(WorkflowModel.id(), args, route);
                  }
                  if (stillOwned) revokeMeeting();
                }
              }
            });
    owner[0] = candidate;
    meetingRecorder = candidate;
    try {
      candidate.start();
    } catch (RuntimeException failure) {
      meetingRecorder = null;
      revokeMeeting();
      abortMeetingStart(requestId, route);
    }
  }

  boolean workflowRequest(String requestId, JSONObject args, long route) {
    synchronized (routeLock) {
      if (!workflowSupported
          || !foreground
          || !currentRoute(route)
          || requestId == null
          || !requestId.matches("[a-f0-9]{32}")
          || args == null) return false;
      JSONObject request = Wire.event("workflow_request");
      Wire.put(request, "request_id", requestId);
      java.util.Iterator<String> keys = args.keys();
      while (keys.hasNext()) {
        String key = keys.next();
        if (!("action".equals(key)
            || "revision".equals(key)
            || "id".equals(key)
            || "value".equals(key)
            || "confirmed".equals(key)
            || "offer_id".equals(key)
            || "run_id".equals(key)
            || "step_id".equals(key)
            || "camera_nonce".equals(key))) return false;
        Wire.put(request, key, args.opt(key));
      }
      String nonce = args.optString("camera_nonce");
      if (!nonce.isEmpty()) {
        if (!"run".equals(args.optString("action"))
            || !Boolean.TRUE.equals(args.opt("confirmed"))
            || recording
            || visual
            || !pendingVisionNonce.isEmpty()
            || !workflowConsent.offer(nonce, route, SystemClock.elapsedRealtime())) return false;
        pendingVisionNonce = nonce;
        new android.os.Handler(android.os.Looper.getMainLooper())
            .postDelayed(
                () -> {
                  synchronized (routeLock) {
                    if (foreground
                        && currentRoute(route)
                        && nonce.equals(pendingVisionNonce)
                        && !visual) {
                      workflowConsent.clear();
                      pendingVisionNonce = "";
                      listener.workspaceResult(false, "本次取景确认已过期，请重新核对流程后确认。", route);
                    }
                  }
                },
                15000);
      }
      if (sendWorkspace(request, route)) return true;
      if (!nonce.isEmpty()) {
        workflowConsent.clear();
        if (nonce.equals(pendingVisionNonce)) pendingVisionNonce = "";
      }
      return false;
    }
  }

  boolean reportSourcesRequest(String requestId, JSONObject args, long route) {
    synchronized (routeLock) {
      if (!reportSourcesSupported
          || !foreground
          || !currentRoute(route)
          || !requestId.matches("[a-f0-9]{32}")) return false;
      String reportAction = args.optString("action");
      if (("progress_checkpoints".equals(reportAction)
              || "progress_checkpoint".equals(reportAction)
              || "save_progress".equals(reportAction)
              || "restore_progress".equals(reportAction)
              || "delete_progress".equals(reportAction)
              || "clear_progress".equals(reportAction))
          && !reportProgressSupported) return false;
      if ("source_queue".equals(reportAction) && !threadQueueSupported) return false;
      if (("saved_reports".equals(reportAction)
              || "saved_report".equals(reportAction)
              || "save_report".equals(reportAction)
              || "delete_report".equals(reportAction)
              || "clear_reports".equals(reportAction))
          && !reportArchiveSupported) return false;
      JSONObject request = Wire.event("dot_chat_request");
      Wire.put(request, "request_id", requestId);
      java.util.Iterator<String> keys = args.keys();
      while (keys.hasNext()) {
        String key = keys.next();
        if (!("action".equals(key)
            || "revision".equals(key)
            || "confirmed".equals(key)
            || "source_id".equals(key)
            || "registration_id".equals(key)
            || "cursor".equals(key)
            || "period".equals(key)
            || "period_seconds".equals(key)
            || "channel".equals(key)
            || "mode".equals(key)
            || "page".equals(key)
            || "snapshot_id".equals(key)
            || "saved_id".equals(key)
            || "offer_id".equals(key)
            || "checkpoint_id".equals(key))) return false;
        Wire.put(request, key, args.opt(key));
      }
      return sendWorkspace(request, route);
    }
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
      boolean microphonePacket = type == Wire.MIC || type == Wire.MIC_PAIRED;
      if (microphonePacket && !captureRegistered(generation)) return;
      Wire.Packet packet = new Wire.Packet();
      packet.type = type;
      packet.generation = generation;
      packet.sequence = sequence;
      packet.frame = frame;
      packet.nanos = nanos;
      packet.data = bytes;
      ownPacket(packet, destination);
      try {
        destination.offer(packet);
      } catch (IOException error) {
        Link fallback = activeLink;
        if (fallback == null || fallback == destination || fallback.closed) throw error;
        // A route-owned packet cannot be forwarded to a different activation.
        // The next native read/control must acquire that activation explicitly.
        if (packet.outgoingRoute == routeEpoch
            && (!microphonePacket || captureRegistered(generation))) fallback.offer(packet);
      }
    }
  }

  private boolean captureRegistered(int captureEpoch) {
    CaptureWindow window = captureWindow;
    return recording
        && epoch == captureEpoch
        && window != null
        && window.registeredRoute == routeEpoch;
  }

  boolean isVisionRequestCurrent(JSONObject request) {
    synchronized (routeLock) {
      return ownsVisionRequest(
          request.optInt("vision_generation", -1),
          request.optString("request_id"),
          request.optLong("vision_route", -1));
    }
  }

  private boolean ownsVisionRequest(int gen, String ident, long route) {
    return foreground
        && visual
        && currentRoute(route)
        && gen == visualGeneration
        && gen >= minimumGeneration
        && !ident.isEmpty()
        && visionRequestId.equals(ident);
  }

  void requestVision() {
    requestVision("observe");
  }

  boolean requestVision(String mode) {
    synchronized (routeLock) {
      if (!("observe".equals(mode) || "ocr".equals(mode) || "mac_window".equals(mode))
          || ("ocr".equals(mode) && !visionModesSupported)
          || ("mac_window".equals(mode) && !macWindowSupported)
          || !foreground
          || !isConnected()
          || visual
          || !pendingVisionNonce.isEmpty()) return false;
      pendingVisionNonce = java.util.UUID.randomUUID().toString().replace("-", "");
      JSONObject e = Wire.event("vision_request");
      Wire.put(e, "nonce", pendingVisionNonce);
      if (!"observe".equals(mode)) Wire.put(e, "mode", mode);
      if (!sendWorkspace(e, routeEpoch)) {
        pendingVisionNonce = "";
        return false;
      }
      listener.vision(
          true,
          "mac_window".equals(mode) ? "请在 Mac 选择并确认一个窗口 · CC8 摄像头关闭" : "正在请求拍摄 · 图片将交给 Dot",
          false);
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
      return true;
    }
  }

  boolean requestPhotoFollowup(PhotoFollowupWindow.Picture expected, String text) {
    synchronized (routeLock) {
      if (expected == null
          || photoForFollowup() != expected
          || !pendingVisionNonce.isEmpty()
          || !PhotoFollowupWindow.validQuestion(text)) return false;
      pendingVisionNonce = java.util.UUID.randomUUID().toString().replace("-", "");
      pendingPhotoId = expected.id;
      JSONObject e = Wire.event("photo_followup_request");
      Wire.put(e, "nonce", pendingVisionNonce);
      Wire.put(e, "photo_id", expected.id);
      Wire.put(e, "text", text.trim());
      if (!sendWorkspace(e, expected.route)) {
        pendingVisionNonce = "";
        pendingPhotoId = "";
        return false;
      }
      photoFollowup.clear();
      listener.vision(true, "正在提交本张照片的追问 · 摄像头关闭", false);
      final String nonce = pendingVisionNonce;
      new android.os.Handler(android.os.Looper.getMainLooper())
          .postDelayed(
              () -> {
                synchronized (routeLock) {
                  if (foreground
                      && currentRoute(expected.route)
                      && nonce.equals(pendingVisionNonce)
                      && expected.id.equals(pendingPhotoId)) {
                    cancelVision();
                    listener.vision(false, "追问请求未确认，请重新打开看图入口", false);
                  }
                }
              },
              15000);
      return true;
    }
  }

  boolean forgetPhotoForFollowup() {
    synchronized (routeLock) {
      PhotoFollowupWindow.Picture picture = photoForFollowup();
      if (picture == null) return true;
      photoFollowup.clear();
      JSONObject event = Wire.event("photo_followup_forget");
      Wire.put(event, "photo_id", picture.id);
      pendingPhotoForgetId = picture.id;
      if (sendWorkspace(event, picture.route)) return true;
      pendingPhotoForgetId = "";
      return false;
    }
  }

  void cancelVision() {
    synchronized (routeLock) {
      workflowConsent.clear();
      photoFollowup.clear();
      pendingPhotoId = "";
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
        speakerOwner.endTask(stoppedGeneration);
        JSONObject stopped = Wire.event("vision_cancel");
        Wire.put(stopped, "vision_generation", stoppedGeneration);
        Wire.put(stopped, "nonce", stoppedNonce);
        send(stopped);
        listener.vision(false, "已取消看图 · 摄像头关闭", false);
      }
    }
  }

  void cameraFailed(JSONObject request, String reason) {
    JSONObject e = Wire.event("camera_error");
    Wire.put(e, "error", reason);
    try {
      sendCameraEvent(
          e,
          request.optInt("vision_generation", -1),
          request.optString("request_id"),
          request.optLong("vision_route", -1));
    } catch (IOException ignored) {
      // The originating request no longer owns a usable route.
    }
  }

  void imageReady(JSONObject request, byte[] jpeg, int width, int height) {
    imageReady(request, jpeg, width, height, false);
  }

  void imageReady(JSONObject request, byte[] jpeg, int width, int height, boolean keepPhoto) {
    final int gen = request.optInt("vision_generation", -1);
    final String ident = request.optString("request_id");
    final long route = request.optLong("vision_route", -1);
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
                if (keepPhoto && Boolean.TRUE.equals(request.opt("followup_allowed")))
                  Wire.put(head, "retain_for_followup", true);
                sendCameraEvent(head, gen, ident, route);
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
                  sendCameraEvent(chunk, gen, ident, route);
                }
                sendCameraEvent(Wire.event("camera_image_end"), gen, ident, route);
              } catch (Exception error) {
                cameraFailed(request, "image_transfer_failed");
              }
            },
            "cc8-camera-transfer")
        .start();
  }

  private void sendCameraEvent(JSONObject e, int gen, String ident, long route) throws IOException {
    synchronized (routeLock) {
      Link destination = activeLink;
      if (!ownsVisionRequest(gen, ident, route)
          || destination == null
          || !destination.ready
          || destination.closed) throw new IOException("Stale camera request");
      Wire.put(e, "request_id", ident);
      Wire.put(e, "vision_generation", gen);
      // A route switch cancels this capture. Never requeue its bytes on a backup.
      destination.cameraEvent(e, gen, ident, route);
    }
  }

  private boolean queuedCameraCurrent(Wire.Packet packet, Link link) {
    if (packet.cameraRequestId == null) return true;
    synchronized (routeLock) {
      return activeLink == link
          && link.ready
          && !link.closed
          && ownsVisionRequest(packet.cameraGeneration, packet.cameraRequestId, packet.cameraRoute);
    }
  }

  private void ownPacket(Wire.Packet packet, Link link) throws IOException {
    if (!foreground || activeLink != link || link.closed) throw new IOException("Stale route");
    packet.outgoingRoute = routeEpoch;
    if (link.routeToken != null) Wire.bindRoute(packet, link.routeToken);
  }

  private boolean queuedPacketCurrent(Wire.Packet packet, Link link) {
    synchronized (routeLock) {
      return (packet.outgoingRoute < 0
              || activeLink == link && currentRoute(packet.outgoingRoute) && !link.closed)
          && queuedCameraCurrent(packet, link);
    }
  }

  private void writeQueuedPacket(Wire.Packet packet, Link link) throws IOException {
    if (!queuedPacketCurrent(packet, link)) return;
    // Do not hold routeLock over a socket write. A packet already admitted here
    // remains stamped with its old token and is fenced again by the host.
    Wire.write(
        link.out,
        packet.type,
        packet.generation,
        packet.sequence,
        packet.frame,
        packet.nanos,
        packet.data);
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
    speakerOwner.endTasksThrough(Integer.MAX_VALUE);
    listener.caption("", "attentive");
    send(Wire.event("interrupt"));
    if (recording) listener.state("listening", "我在听，你继续说");
    else {
      phase = "idle";
      listener.state("idle", "播报已停止 · 麦克风关闭");
    }
  }

  @SuppressLint("MissingPermission") // Only MainActivity's authorized foreground opening or Start
  // action may
  // call; checked again here.
  void startMicrophone() {
    startMicrophone(0);
  }

  @SuppressLint("MissingPermission")
  void startMicrophone(long testLimitMs) {
    final CaptureWindow window = new CaptureWindow(testLimitMs, SystemClock.elapsedRealtime());
    // mine 固定本次录音 epoch，旧线程 finally 只能清理自己，不能关闭后来新开的录音。
    if (!foreground || recording || !isConnected()) return;
    if (meetingReserved()) {
      listener.state("idle", "会议占用音频，请先在会议页结束或删除会议。");
      return;
    }
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
    synchronized (routeLock) {
      synchronized (this) {
        if (!foreground || recording || !isConnected() || meetingReserved()) return;
        recording = true;
        captureWindow = window;
        mine = ++epoch;
        micFrames = 0;
        renderFrames = 0;
        minimumGeneration = 0;
        statusFeedback.reset();
        speakerOwner.beginVoice(mine, () -> recording && epoch == mine);
      }
    }
    listener.microphone(true);
    listener.state("calibrating", "正在准备音频，请保持安静");
    new Thread(
            () -> {
              synchronized (captureResourceLock) {
                if (!recording || !foreground || epoch != mine) return;
                AudioRecord record = null;
                CaptureAudioInput input = null;
                Thread timeout = null;
                long sequence = 0, origin = 0, last = System.nanoTime();
                try {
                  // The bound lives in this AudioRecord worker, independent of
                  // the Mac runner and the UI event queue. Expiry follows the
                  // same epoch-owned finally path as any other capture end.
                  if (window.watchdogRemaining(SystemClock.elapsedRealtime()) == 0) return;
                  boolean preferPaired;
                  // The authorized opening can precede the peer hello. Wait for this
                  // route's explicit format choice before acquiring any recorder.
                  while (true) {
                    if (!recording
                        || !foreground
                        || epoch != mine
                        || !window.allows(SystemClock.elapsedRealtime())) return;
                    if (window.watchdogRemaining(SystemClock.elapsedRealtime()) == 0)
                      throw new IOException("麦克风连接确认超时，请重试");
                    synchronized (routeLock) {
                      if (activeLink != null && activeLink.captureStartToken != null) {
                        preferPaired = activeLink.pairedCaptureRequested;
                        break;
                      }
                    }
                    Thread.sleep(20);
                  }
                  input = CaptureAudioInput.open(preferPaired);
                  record = input.recorder;
                  window.paired = input.paired;
                  window.captureFallbackReason = input.fallbackReason;
                  synchronized (VoiceSession.this) {
                    if (!recording
                        || !foreground
                        || epoch != mine
                        || window.watchdogRemaining(SystemClock.elapsedRealtime()) == 0) return;
                    recorder = record;
                    record.startRecording();
                    if (record.getRecordingState() != AudioRecord.RECORDSTATE_RECORDING)
                      throw new IOException("麦克风无法开始");
                  }
                  synchronized (routeLock) {
                    if (!recording || !foreground || epoch != mine) return;
                    window.nativeReady = true;
                    try {
                      registerCapture(window, mine);
                    } catch (IOException routeLost) {
                      // The link closes on failed enqueue. A surviving route can
                      // register the same, still pending recording after its hello.
                    }
                  }
                  if (window.limitMs > 0 || !window.startConfirmed) {
                    final AudioRecord ownedRecord = record;
                    timeout =
                        new Thread(
                            () -> watchCapture(window, mine, ownedRecord), "cc8-capture-timeout");
                    timeout.setDaemon(true);
                    timeout.start();
                  }
                  CaptureClockStats clockStats = window.clockStats;
                  AudioTimestamp captureStamp =
                      clockStats != null && !window.paired && Build.VERSION.SDK_INT >= 24
                          ? new AudioTimestamp()
                          : null;
                  long inputRoute = -1;
                  while (recording
                      && foreground
                      && epoch == mine
                      && window.allows(SystemClock.elapsedRealtime())) {
                    final long readRoute;
                    synchronized (routeLock) {
                      readRoute = routeEpoch;
                    }
                    if (inputRoute != readRoute) {
                      // Erase paired converter/partial-packet history on the worker,
                      // never concurrently with AudioRecord.read from the UI thread.
                      input.clear();
                      inputRoute = readRoute;
                    }
                    CaptureAudioInput.Chunk chunk = input.read();
                    if (chunk == null) continue;
                    try {
                      if (!recording
                          || !foreground
                          || epoch != mine
                          || !window.allows(SystemClock.elapsedRealtime())) break;
                      int count = chunk.frames;
                      long now = System.nanoTime();
                      if (origin == 0) origin = now - count * 1000000000L / 16000;
                      synchronized (routeLock) {
                        // A blocking native read may straddle activation (including
                        // USB -> Wi-Fi -> the same USB object). Drop that whole block.
                        if (!currentRoute(readRoute) || !identity.ready() || !identityHello) {
                          micFrames += count;
                          continue;
                        }
                        inputLevel = chunk.level();
                        if (now - last > 400000000L && micFrames > 0) {
                          JSONObject gap = Wire.event("capture_gap");
                          Wire.put(gap, "milliseconds", (now - last) / 1000000);
                          Wire.put(gap, "epoch", mine);
                          Wire.put(gap, "sequence", sequence);
                          send(gap);
                        }
                        last = now;
                        sendPacket(
                            chunk.paired ? Wire.MIC_PAIRED : Wire.MIC,
                            mine,
                            sequence++,
                            micFrames,
                            origin + micFrames * 1000000000L / 16000,
                            Wire.pcm(chunk.samples, chunk.samples.length));
                      }
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
                    } finally {
                      chunk.clear();
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
                  if (input != null) input.clear();
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
                      speakerOwner.endVoice(mine);
                    }
                  }
                  if (owned) {
                    listener.microphone(false);
                    JSONObject stopped = Wire.event("session_stop");
                    Wire.put(stopped, "epoch", mine);
                    Wire.put(stopped, "capture_id", window.id);
                    send(stopped);
                  }
                }
              }
            },
            "cc8-microphone")
        .start();
  }

  private void watchCapture(CaptureWindow window, int mine, AudioRecord ownedRecord) {
    try {
      while (recording && epoch == mine) {
        long remaining = window.watchdogRemaining(SystemClock.elapsedRealtime());
        if (remaining == Long.MAX_VALUE) return;
        if (remaining > 0) {
          Thread.sleep(Math.min(1000, remaining));
          continue;
        }
        synchronized (routeLock) {
          if (!recording || epoch != mine) return;
          long now = SystemClock.elapsedRealtime();
          if (window.watchdogRemaining(now) > 0) continue;
          if (window.allows(now)) {
            if (stopMicrophone(window)) listener.captureRejected(mine + 1);
            return;
          }
        }
        // The test cap owns this AudioRecord. Do not hold the route lock while
        // unblocking a native read on the expired hardware.
        ownedRecord.stop();
        return;
      }
    } catch (InterruptedException interrupted) {
      Thread.currentThread().interrupt();
    } catch (RuntimeException alreadyReleased) {
      // The original recording may have already released its AudioRecord.
    }
  }

  private void rejectCapture(Object captureId, long route) {
    synchronized (routeLock) {
      CaptureWindow window = captureWindow;
      if (!foreground || !currentRoute(route) || window == null || !window.id.equals(captureId))
        return;
      int rejectedEpoch = epoch;
      if (stopMicrophone(window)) listener.captureRejected(rejectedEpoch + 1);
    }
  }

  boolean captureRejectionCurrent(int stoppedEpoch) {
    return foreground && !recording && epoch == stoppedEpoch;
  }

  void stopMicrophone() {
    stopMicrophone(null);
  }

  private boolean stopMicrophone(CaptureWindow expected) {
    // 先推进 epoch 再停止硬件，使阻塞 read 返回后的旧帧无法进入新会话。
    final int stoppedEpoch;
    final AudioRecord r;
    final CaptureWindow stoppedWindow;
    synchronized (this) {
      if (expected != null && captureWindow != expected) return false;
      stoppedEpoch = epoch++;
      recording = false;
      stoppedWindow = captureWindow;
      captureWindow = null;
      r = recorder;
      recorder = null;
      phase = expected == null ? "idle" : "error";
      announcement = false;
      cancelPlayback();
      speakerOwner.close();
    }
    listener.playback(false);
    inputLevel = 0;
    if (r != null)
      try {
        r.stop();
      } catch (RuntimeException ignored) {
      }
    listener.microphone(false);
    JSONObject stopped = Wire.event("session_stop");
    Wire.put(stopped, "epoch", stoppedEpoch);
    if (stoppedWindow != null) Wire.put(stopped, "capture_id", stoppedWindow.id);
    send(stopped);
    return true;
  }

  private synchronized void cancelPlayback() {
    Player p = player;
    player = null;
    playbackPresentation = null;
    lastCaption = CaptionProgress.empty();
    if (p != null) p.cancel();
  }

  private synchronized void beginTaskOutput(String role, int generation) {
    if (recording) return;
    cancelPlayback();
    speakerOwner.beginTask(
        generation,
        () ->
            "text".equals(role)
                ? textSession && textGeneration == generation
                : "vision".equals(role)
                    ? visual && visualGeneration == generation
                    : announcement && announcementGeneration == generation);
  }

  private void cancelPlaybackForCommand(int cancelGeneration, long route) {
    synchronized (routeLock) {
      if (!foreground || !currentRoute(route)) return;
      synchronized (this) {
        Player stopped = player;
        CaptureWindow window = captureWindow;
        cancelPlayback();
        if (recording && window != null && stopped != null)
          window.observeStop(
              route,
              stopped.generation,
              stopped.playbackId,
              cancelGeneration,
              stopped.cancelAppliedNanos,
              SystemClock.elapsedRealtime());
      }
    }
  }

  void noteTestStopVisuals(CaptionProgress frame) {
    CaptureWindow window = captureWindow;
    if (window == null
        || window.limitMs == 0
        || frame.id != 0
        || frame.level != 0
        || frame.playing
        || frame.paused) return;
    long appliedNanos = System.nanoTime();
    synchronized (routeLock) {
      if (foreground && recording && captureWindow == window && player == null)
        window.observeStopVisuals(
            routeEpoch, minimumGeneration, appliedNanos, SystemClock.elapsedRealtime());
    }
  }

  boolean currentStatusPresentation(StatusPresentation update) {
    synchronized (routeLock) {
      return identityConnected()
          && update != null
          && update.current(
              foreground,
              recording,
              captureWindow,
              epoch,
              lifecycle,
              routeEpoch,
              minimumGeneration,
              statusFeedback.current(update.generation));
    }
  }

  static final class PlaybackPresentation {
    final String text, mood, phase, message;

    PlaybackPresentation(String text, String mood, String phase, String message) {
      this.text = text;
      this.mood = mood;
      this.phase = phase;
      this.message = message;
    }
  }

  boolean currentPlaybackPresentation(PlaybackPresentation update) {
    return identityConnected() && foreground && playbackPresentation == update;
  }

  private void publishPlayback(Player owner, String text, String nextPhase, String message) {
    PlaybackPresentation update;
    synchronized (this) {
      if (!foreground || player != owner) return;
      phase = nextPhase;
      update = new PlaybackPresentation(text, owner.mood, nextPhase, message);
      playbackPresentation = update;
    }
    // Do not hold the session lock while handing off to the main thread.
    listener.playbackPresentation(update);
  }

  float microphoneLevel() {
    return recording ? inputLevel : 0;
  }

  long playbackHead() {
    Player p = player;
    AudioTrack t = p == null ? null : p.track;
    try {
      return t == null ? 0 : p.localHead(t);
    } catch (IllegalStateException ignored) {
      return 0;
    }
  }

  JSONObject snapshot() {
    JSONObject j = Wire.event("snapshot");
    synchronized (routeLock) {
      Wire.put(j, "foreground", foreground);
      Wire.put(j, "connected", isConnected());
      Wire.put(j, "transport", transportName());
      Wire.put(
          j,
          "connection_availability",
          new JSONObject(availability.snapshot(SystemClock.elapsedRealtime()).values()));
      Link current = activeLink;
      boolean bound =
          foreground
              && current != null
              && current.ready
              && !current.closed
              && current.routeToken != null;
      Wire.put(j, "route_envelope_version", bound ? 1 : 0);
      Wire.put(j, "route_output_version", bound && current.outputBound ? 1 : 0);
    }
    Wire.put(j, "usb_connected", usbLink != null && !usbLink.closed);
    Wire.put(j, "usb_physical", usbPhysical);
    Wire.put(j, "wifi_ready", wifiLink != null && wifiLink.ready && !wifiLink.closed);
    Wire.put(j, "wifi_paired", lan.paired());
    Wire.put(j, "wifi_error", wifiError);
    Wire.put(j, "microphone_active", recording || isMeetingCapturing());
    Wire.put(j, "meeting_capture_active", isMeetingCapturing());
    CaptureWindow window = captureWindow;
    Wire.put(j, "test_capture_limit_supported", true);
    Wire.put(j, "test_capture_epoch_supported", true);
    if (window != null && window.limitMs > 0 && window.allows(SystemClock.elapsedRealtime())) {
      synchronized (this) {
        if (recording && captureWindow == window) Wire.put(j, "test_capture_epoch", epoch);
      }
    }
    Wire.put(j, "test_interruption_transcript_supported", true);
    Wire.put(j, "test_playback_stop_supported", true);
    Wire.put(j, "test_stop_visual_timing_supported", true);
    if (recording && window != null) {
      CaptureWindow.StopEvidence stop =
          window.stopEvidence(routeEpoch, SystemClock.elapsedRealtime());
      if (stop != null && recording && captureWindow == window && currentRoute(stop.route)) {
        JSONObject receipt = new JSONObject();
        Wire.put(receipt, "generation", stop.generation);
        Wire.put(receipt, "playback_id", stop.playbackId);
        Wire.put(receipt, "cancel_generation", stop.cancelGeneration);
        Wire.put(receipt, "nanos", stop.nanos);
        Wire.put(j, "test_playback_stop", receipt);
        long visualNanos = stop.visualNanos;
        if (visualNanos > 0) {
          JSONObject visuals = new JSONObject();
          Wire.put(visuals, "cancel_generation", stop.cancelGeneration);
          Wire.put(visuals, "nanos", visualNanos);
          Wire.put(j, "test_stop_visual_timing", visuals);
        }
      }
      CaptureWindow.TranscriptEvidence evidence =
          window.transcriptEvidence(routeEpoch, SystemClock.elapsedRealtime());
      if (evidence != null) {
        JSONObject receipt = new JSONObject();
        Wire.put(receipt, "generation", evidence.generation);
        Wire.put(receipt, "matched", evidence.matched);
        Wire.put(j, "test_interruption_transcript", receipt);
      }
    }
    Wire.put(j, "test_capture_limit_ms", recording && window != null ? window.limitMs : 0);
    Wire.put(
        j,
        "test_capture_remaining_ms",
        recording && window != null ? window.remaining(SystemClock.elapsedRealtime()) : 0);
    boolean pairedCapture =
        window != null ? window.paired : activeLink != null && activeLink.pairedCaptureRequested;
    Wire.put(
        j,
        "capture_frontend",
        recording && window != null && window.nativeReady
            ? (window.paired ? "paired" : "mono")
            : "none");
    Wire.put(j, "paired_capture_version", 1);
    Wire.put(
        j,
        "capture_sample_rate",
        recording && window != null && window.nativeReady ? (window.paired ? 48000 : 16000) : 0);
    Wire.put(
        j,
        "capture_channels",
        recording && window != null && window.nativeReady ? (window.paired ? 4 : 1) : 0);
    if (recording && window != null && window.captureFallbackReason != null)
      Wire.put(j, "capture_fallback_reason", window.captureFallbackReason);
    boolean captureClockSupported = Build.VERSION.SDK_INT >= 24 && !pairedCapture;
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
    Wire.put(
        j,
        "playback_paused",
        playingNow != null && !playingNow.stopped && playingNow.frozenCaption != null);
    Wire.put(j, "phase", phase);
    Wire.put(j, "raw_audio_saved", false);
    return j;
  }

  static final class CaptionProgress {
    final int id;
    final String text;
    final String mood;
    final Object speechOwner;
    final float position, level, speechDetail;
    final boolean timed, playing, paused;
    final long elapsedMs, totalMs;

    CaptionProgress(
        int id,
        String text,
        float position,
        boolean timed,
        boolean playing,
        boolean paused,
        long elapsed,
        long total,
        float level,
        String mood) {
      this(id, text, position, timed, playing, paused, elapsed, total, level, 0, null, mood);
    }

    CaptionProgress(
        int id,
        String text,
        float position,
        boolean timed,
        boolean playing,
        boolean paused,
        long elapsed,
        long total,
        float level,
        float speechDetail,
        Object speechOwner,
        String mood) {
      this.id = id;
      this.text = text;
      this.position = position;
      this.timed = timed;
      this.playing = playing;
      this.paused = paused;
      this.elapsedMs = elapsed;
      this.totalMs = total;
      this.level = level;
      this.speechDetail = speechDetail;
      this.speechOwner = speechOwner;
      this.mood = mood;
    }

    static CaptionProgress empty() {
      return new CaptionProgress(0, "", 0, false, false, false, 0, 0, 0, "attentive");
    }
  }

  CaptionProgress playbackFrame() {
    Player p = player;
    if (p == null) return lastCaption;
    CaptionProgress frame = p.visualFrame();
    return player == p ? frame : lastCaption;
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
        speakerOwner.endTasksThrough(Integer.MAX_VALUE);
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
      Wire.put(result, "microphone_active", recording || isMeetingCapturing());
      Wire.put(result, "foreground", foreground);
      Wire.put(result, "transport", transportName());
      Wire.put(result, "wifi_ready", wifiLink != null && wifiLink.ready && !wifiLink.closed);
      if ("status".equals(action)) {
        JSONObject release = new JSONObject();
        Wire.put(release, "schema_version", 1);
        Wire.put(release, "assistant_version", BuildConfig.VERSION_NAME);
        Wire.put(release, "assistant_code", BuildConfig.VERSION_CODE);
        Wire.put(release, "protocol_version", BuildConfig.CC8_PROTOCOL);
        Wire.put(result, "device_release", release);
        synchronized (routeLock) {
          Wire.put(
              result,
              "connection_availability",
              new JSONObject(availability.snapshot(SystemClock.elapsedRealtime()).values()));
        }
      }
    } catch (RuntimeException e) {
      Wire.put(result, "ok", false);
      Wire.put(result, "error", e.getClass().getSimpleName());
    }
    send(result);
  }

  private final class Player extends Thread implements PlaybackDrain.Sink {
    // 播放头决定字幕、嘴型和完成回执；队列中已有数据不能被当成已经发声。
    final int generation, playbackId;
    final long playbackRoute;
    final boolean reportPlayout;
    final RenderClockStats clockStats;
    final PlayoutProbe playout = new PlayoutProbe();
    final String text, mood, role;
    // A content-free local token separates even reused server IDs after reconnect.
    // Do not retain the Player (and its PCM queue) in the view's pose state.
    final Object speechOwner = new Object();
    final SpeakerOwner.Ticket outputOwner;
    volatile CaptionTimeline timings;
    long renderSequence;
    long writeNanos;
    final ArrayBlockingQueue<byte[]> queue = new ArrayBlockingQueue<>(64);
    final ConcurrentLinkedQueue<Envelope> envelopes = new ConcurrentLinkedQueue<>();
    volatile boolean ended, stopped, paused;
    volatile CaptionProgress frozenCaption;
    volatile int pauseToken;
    volatile AudioTrack track;
    volatile SpeakerSession.Lease lease;
    volatile long written = 0;
    volatile long resumeCount = 0;
    volatile long cancelAppliedNanos;
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
      playbackRoute = routeEpoch;
      text = t;
      mood = m;
      this.role = role;
      outputOwner = speakerOwner.forPlayback(g);
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
      cancelAppliedNanos = 0;
      frozenCaption = null;
      queue.clear();
      try {
        if (outputOwner != null && lease != null)
          cancelAppliedNanos = outputOwner.output().cancel(lease);
      } catch (RuntimeException ignored) {
      } finally {
        // Cancel the lease before publishing stopped: its worker also observes allowed().
        // Otherwise that worker can win the flush and consume our applied-stop witness.
        stopped = true;
        interrupt();
      }
    }

    void pauseForVoice(int token) {
      synchronized (routeLock) {
        if (!allowed() || (paused && pauseToken == token)) return;
        pauseToken = token;
        paused = true;
        AudioTrack t = track;
        if (t == null) return; // The player will apply the pending pause before writing PCM.
        try {
          pauseTrack(t, token);
        } catch (IllegalStateException error) {
          failPlayback(error);
        }
      }
    }

    private void pauseTrack(AudioTrack t, int token) {
      long nanos = outputOwner.output().pause(lease);
      if (nanos == 0) return;
      if (!allowed()) return;
      frozenCaption = sampleFrame(t, true);
      publishPlayback(this, text, "listening", "听到你说话，正在听…");
      pauseReceipt("playback_paused", token, nanos);
    }

    private void pauseReceipt(String name, int token, long nanos) {
      JSONObject ack = Wire.event(name);
      Wire.put(ack, "generation", generation);
      Wire.put(ack, "token", token);
      Wire.put(ack, "nanos", nanos);
      send(ack);
    }

    void resumeAfterNoise(int token) {
      synchronized (routeLock) {
        if (!allowed() || !paused || pauseToken != token) return;
        AudioTrack t = track;
        if (t == null) {
          paused = false; // A withdrawn pending pause never changed AudioTrack.
          return;
        }
        final long nanos;
        try {
          nanos = outputOwner.output().resume(lease);
        } catch (IllegalStateException error) {
          failPlayback(error);
          return;
        }
        if (nanos == 0 || !allowed()) return;
        paused = false;
        frozenCaption = null;
        resumeCount++;
        pauseReceipt("playback_resumed", token, nanos);
        publishPlayback(this, text, "speaking", "继续播报 · 你可以开口打断");
        synchronized (this) {
          notifyAll();
        }
      }
    }

    private boolean startTrack(SpeakerSession.Lease acquired) {
      // Serialize startup and control messages without holding a lock during PCM writes.
      synchronized (routeLock) {
        if (!allowed()) return false;
        lease = acquired;
        AudioTrack t = ((NativeSpeaker) acquired.device).track;
        track = t;
        if (paused) pauseTrack(t, pauseToken);
        return allowed();
      }
    }

    private void failPlayback(Exception error) {
      synchronized (routeLock) {
        if (!allowed()) return;
        cancel();
        publishPlayback(this, null, "error", "播放已停止：" + error.getMessage());
        JSONObject failure = Wire.event("playback_error");
        Wire.put(failure, "error_type", error.getClass().getSimpleName());
        Wire.put(failure, "generation", generation);
        Wire.put(failure, "playback_id", playbackId);
        send(failure);
      }
    }

    void waitIfPaused() throws InterruptedException {
      synchronized (this) {
        while (paused && allowed()) wait(30);
      }
    }

    CaptionProgress visualFrame() {
      CaptionProgress frozen = frozenCaption;
      if (stopped) return CaptionProgress.empty();
      if (frozen != null) return frozen;
      CaptionProgress live = sampleFrame(track, false);
      // A pause can land during the hardware read. Keep one coherent frame
      // without waiting for the transport lock on the UI thread.
      frozen = frozenCaption;
      return stopped ? CaptionProgress.empty() : frozen != null ? frozen : live;
    }

    private CaptionProgress sampleFrame(AudioTrack t, boolean freeze) {
      long head = 0;
      Envelope speech = null;
      try {
        if (t != null) {
          head = localHead(t);
          // A running track may still be playing zeros before this clause's physical origin.
          if (!freeze && head > 0 && t.getPlayState() == AudioTrack.PLAYSTATE_PLAYING)
            speech = envelopeAt(head);
        }
      } catch (IllegalStateException ignored) {
      }
      CaptionTimeline line = timings;
      return new CaptionProgress(
          playbackId,
          text,
          line.position(head),
          line.rows.length > 0,
          true,
          freeze,
          head / 16,
          ended && queue.isEmpty() ? written / 16 : 0,
          speech == null ? 0 : speech.value,
          speech == null ? 0 : speech.detail,
          speechOwner,
          mood);
    }

    private Envelope envelopeAt(long head) {
      Envelope e = envelopes.peek();
      while (e != null && head >= e.end) {
        envelopes.poll();
        e = envelopes.peek();
      }
      return e != null && head >= e.start ? e : null;
    }

    public boolean allowed() {
      return !stopped
          && foreground
          && currentRoute(playbackRoute)
          && outputOwner != null
          && outputOwner.allows()
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

    public int write(short[] samples, int offset, int count) throws IOException {
      writeNanos = System.nanoTime();
      return outputOwner.output().write(lease, samples, offset, count);
    }

    public void accepted(short[] samples, int offset, int count) throws IOException {
      AudioTrack t = track;
      SpeakerSession speaker = outputOwner.output();
      long now = System.nanoTime();
      long head = speaker.absolutePosition(t.getPlaybackHeadPosition() & 0xffffffffL);
      long target = lease.base + written;
      AudioTimestamp stamp = new AudioTimestamp();
      boolean valid = t.getTimestamp(stamp);
      long position = speaker.absolutePosition(stamp.framePosition);
      long speakerTime = PlaybackClock.estimate(now, target, head, valid, position, stamp.nanoTime);
      if (clockStats != null)
        clockStats.observe(
            SystemClock.elapsedRealtime(),
            written,
            count,
            speakerTime,
            PlaybackClock.source(now, target, valid, position, stamp.nanoTime));
      short[] accepted = new short[count];
      System.arraycopy(samples, offset, accepted, 0, count);
      envelopes.add(new Envelope(written, accepted));
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
              localHead(t),
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
      return localHead(track);
    }

    private long localHead(AudioTrack t) {
      return t == null || outputOwner == null
          ? 0
          : outputOwner.output().localHead(lease, t.getPlaybackHeadPosition() & 0xffffffffL);
    }

    public int bufferFrames() {
      return ((NativeSpeaker) lease.device).bufferFrames();
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
      SpeakerSession.Lease acquired = null;
      try {
        if (!allowed()) return;
        acquired = outputOwner.output().acquire(generation, playbackId, this::allowed);
        if (!startTrack(acquired)) return;
        AudioTrack t = track;
        waitIfPaused();
        if (!allowed()) return;
        synchronized (routeLock) {
          // Pause/resume may already have published the newest state.
          if (!paused && resumeCount == 0)
            publishPlayback(
                this,
                text,
                "calibration".equals(role) ? "calibrating" : "speaking",
                "calibration".equals(role)
                    ? "首次校准：请安静听完这段声音"
                    : "announcement".equals(role) && !recording
                        ? "正在播报 · 点击打断可停止"
                        : visual && !recording ? "正在看图回答 · 点击停止看图可打断" : "正在回答 · 你可以开口打断");
        }
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
        long finalHead = 0;
        boolean finished = false;
        while (allowed() && !finished) {
          if (!drain.drain(written, this)) return;
          synchronized (routeLock) {
            if (!allowed()) return;
            if (paused) continue;
            finalHead = localHead(t);
            if (!outputOwner.output().finish(acquired)) throw new IOException("播放会话已结束");
            finished = true;
          }
        }
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
                      written / 16,
                      0,
                      mood);
          }
          JSONObject done = Wire.event("play_done");
          Wire.put(done, "generation", generation);
          Wire.put(done, "role", role);
          Wire.put(done, "playback_id", playbackId);
          Wire.put(done, "played_frames", finalHead);
          if (clockStats != null) {
            Wire.put(done, "render_clock", clockReport());
          }
          send(done);
          if (recording) publishPlayback(this, text, "listening", "我在听");
        }
      } catch (InterruptedException error) {
        Thread.currentThread().interrupt();
      } catch (Exception error) {
        failPlayback(error);
      } finally {
        synchronized (routeLock) {
          track = null;
          lease = null;
          frozenCaption = null;
        }
        if (acquired != null) {
          try {
            outputOwner.output().cancel(acquired);
          } catch (RuntimeException ignored) {
          }
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
    final float value, detail;

    Envelope(long s, short[] samples) {
      start = s;
      end = s + samples.length;
      double power = 0, changes = 0;
      for (int i = 0; i < samples.length; i++) {
        double v = samples[i];
        power += v * v;
        if (i > 0) {
          double difference = v - samples[i - 1];
          changes += difference * difference;
        }
      }
      value = (float) Math.sqrt(power / Math.max(1, samples.length)) / 32768;
      // Cheap bounded signal variation changes shape, not the playback timing.
      // It is deliberately not a vowel, consonant or phoneme classifier.
      detail = power == 0 ? 0 : (float) Math.min(1, Math.sqrt(changes / (4 * power)));
    }
  }
}
