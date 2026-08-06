(function () {
  "use strict";

  const POLL = Object.freeze({ status: 2000, errors: 10000, measurements: 30000 });
  const ENDPOINTS = Object.freeze({
    status: "/api/status",
    measurements: "/api/measurements?limit=5000",
    errors: "/api/errors?limit=50",
    snapshot: "/latest.jpg",
    stream: "/stream/index.m3u8",
    actions: {
      start: "/api/experiment/start",
      stop: "/api/experiment/stop",
      restart: "/api/experiment/restart",
      continue: "/api/experiment/continue",
    },
  });

  const ACTION_COPY = Object.freeze({
    start: {
      title: "Start the experiment?",
      consequence: "Test 1 will begin and the doorbell will start generating camera activity.",
      confirm: "Start experiment",
      variant: "brand",
    },
    stop: {
      title: "Stop the experiment?",
      consequence: "Current camera activity will stop immediately. You can resume the interrupted test later.",
      confirm: "Stop experiment",
      variant: "danger",
    },
    restart: {
      title: "Restart from test 1?",
      consequence: "The current run will stop and a new historical run will begin from the first test.",
      confirm: "Restart from beginning",
      variant: "danger",
    },
    continue: {
      title: "Continue with the next test?",
      consequence: "The recovery wait will be shortened. Blink must first confirm that the battery is no longer low.",
      confirm: "Check battery and continue",
      variant: "warning",
    },
  });

  const STATES = Object.freeze({
    not_started: { label: "Not started", variant: "neutral", activity: "Waiting to start" },
    running_snapshot: { label: "Snapshot test", variant: "brand", activity: "Scheduled snapshots active" },
    running_stream: { label: "Live stream test", variant: "brand", activity: "Continuous live stream active" },
    recovery: { label: "Recovery", variant: "warning", activity: "No camera activity" },
    completed: { label: "Completed", variant: "success", activity: "Experiment complete" },
    stopped_low_battery: { label: "Low battery", variant: "danger", activity: "Camera activity stopped" },
    stopped_manual: { label: "Stopped", variant: "neutral", activity: "Stopped manually" },
    stopped_error: { label: "Error", variant: "danger", activity: "Camera activity stopped" },
  });

  const dom = {};
  const runtime = {
    status: null,
    statusReceivedAt: 0,
    pendingAction: null,
    actionBusy: false,
    polling: { status: false, errors: false, measurements: false },
    chart: null,
    hls: null,
    hlsActive: false,
    snapshotVersion: null,
    snapshotFailedVersion: null,
    mediaTimestamp: null,
  };

  document.addEventListener("DOMContentLoaded", init);

  async function init() {
    [
      "connection-status", "phase-badge", "start-button", "stop-button", "restart-button", "continue-button",
      "state-callout", "callout-title", "callout-message", "test-name", "test-step", "start-time", "elapsed-time",
      "remaining-time", "activity-label", "progress-label", "overall-progress", "camera-name", "media-badge",
      "media-placeholder", "snapshot-image", "stream-video", "media-mode", "media-age", "battery-badge",
      "battery-voltage", "battery-level", "battery-check", "snapshot-successes", "snapshot-failures", "stream-bytes",
      "snapshot-timeouts", "stream-reconnects", "chart-status", "battery-chart", "errors-empty", "errors-list", "last-updated",
      "history-summary", "phase-summary", "measurements-table-body",
      "confirmation-dialog", "confirmation-message", "confirmation-consequence", "cancel-action", "confirm-action",
      "toast-region",
    ].forEach((id) => { dom[toCamel(id)] = document.getElementById(id); });

    document.querySelectorAll("[data-action]").forEach((button) => {
      button.addEventListener("click", () => openConfirmation(button.dataset.action));
    });
    dom.confirmAction.addEventListener("click", performPendingAction);

    await Promise.all([
      "wa-badge", "wa-button", "wa-callout", "wa-dialog", "wa-progress-bar",
    ].map((name) => window.customElements.whenDefined(name)));

    pollStatus();
    pollErrors();
    pollMeasurements();
    window.setInterval(pollStatus, POLL.status);
    window.setInterval(pollErrors, POLL.errors);
    window.setInterval(pollMeasurements, POLL.measurements);
    window.setInterval(updateClocks, 1000);
    window.addEventListener("beforeunload", destroyStream);
  }

  function toCamel(value) {
    return value.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase());
  }

  async function fetchJson(url, options = {}) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(url, {
        cache: "no-store",
        credentials: "same-origin",
        ...options,
        signal: controller.signal,
        headers: { Accept: "application/json", ...(options.headers || {}) },
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) {
        const message = body.detail || body.error || body.message || `Request failed (${response.status})`;
        const error = new Error(typeof message === "string" ? message : "Request failed");
        error.status = response.status;
        throw error;
      }
      return body;
    } finally {
      window.clearTimeout(timeout);
    }
  }

  async function pollStatus() {
    if (runtime.polling.status) return;
    runtime.polling.status = true;
    try {
      const raw = await fetchJson(ENDPOINTS.status);
      runtime.status = normalizeStatus(raw);
      runtime.statusReceivedAt = Date.now();
      renderStatus(runtime.status);
      setConnection(true);
    } catch (error) {
      setConnection(false, error.name === "AbortError" ? "Status request timed out" : error.message);
    } finally {
      runtime.polling.status = false;
    }
  }

  async function pollErrors() {
    if (runtime.polling.errors) return;
    runtime.polling.errors = true;
    try {
      const raw = await fetchJson(ENDPOINTS.errors);
      renderErrors(Array.isArray(raw) ? raw : raw.errors || raw.items || []);
    } catch (_) {
      // The connection indicator is driven by the authoritative status request.
    } finally {
      runtime.polling.errors = false;
    }
  }

  async function pollMeasurements() {
    if (runtime.polling.measurements) return;
    runtime.polling.measurements = true;
    try {
      const raw = await fetchJson(ENDPOINTS.measurements);
      renderChart(raw);
    } catch (error) {
      dom.chartStatus.textContent = error.name === "AbortError" ? "Chart request timed out" : "Measurements unavailable";
    } finally {
      runtime.polling.measurements = false;
    }
  }

  function normalizeStatus(raw) {
    const test = raw.test || {};
    const phase = raw.phase || {};
    const battery = raw.battery || raw.latest_battery || raw.last_battery || {};
    const counters = raw.counters || {};
    const media = raw.media || {};
    const controls = raw.controls || raw.allowed_actions || {};
    const adapter = raw.adapter || {};
    const state = raw.state || raw.experiment_state || "not_started";
    const testIndexValue = firstDefined(test.index, raw.test_index, raw.current_test_index, raw.current_test, 0);
    const testIndex = Math.max(0, Number(testIndexValue) || 0);
    const progressValue = firstDefined(raw.overall_progress_percent, raw.progress_percent, raw.progress, 0);
    const camera = firstDefined(adapter.camera, raw.camera, {});

    return {
      state,
      testIndex,
      testName: firstDefined(test.name, raw.test_name, raw.current_test_name, state === "not_started" ? "Ready for test 1" : `Test ${testIndex}`),
      testCount: Math.max(1, Number(firstDefined(raw.test_count, raw.total_tests, 4)) || 4),
      startedAt: firstDefined(phase.started_at_utc, raw.phase_started_at_utc, raw.phase_start_utc, raw.started_at_utc, raw.start_time),
      elapsedSeconds: finiteOrNull(firstDefined(phase.elapsed_seconds, raw.elapsed_seconds, raw.phase_elapsed_seconds, 0)),
      remainingSeconds: finiteOrNull(firstDefined(phase.remaining_seconds, raw.remaining_seconds, raw.phase_remaining_seconds)),
      progress: clamp(Number(progressValue) <= 1 && Number(progressValue) > 0 ? Number(progressValue) * 100 : Number(progressValue) || 0, 0, 100),
      cameraName: typeof camera === "string" ? camera : firstDefined(camera.name, camera.camera_name, raw.camera_name, "Selected doorbell"),
      battery: {
        state: firstDefined(battery.battery_state, battery.state, raw.battery_state),
        voltage: finiteOrNull(firstDefined(battery.battery_voltage_volts, battery.voltage_volts, battery.voltage, raw.battery_voltage_volts)),
        level: firstDefined(battery.battery_level_raw, battery.level_raw, battery.level, raw.battery_level_raw),
        checkedAt: firstDefined(battery.blink_battery_check_time, battery.checked_at_utc, battery.observed_at_utc, raw.blink_battery_check_time),
      },
      counters: {
        snapshotSuccesses: integer(firstDefined(counters.successful_snapshot_count, counters.snapshot_successes, raw.successful_snapshot_count, raw.snapshot_success_count)),
        snapshotFailures: integer(firstDefined(counters.failed_snapshot_count, counters.snapshot_failures, raw.failed_snapshot_count, raw.snapshot_failure_count)),
        snapshotTimeouts: integer(firstDefined(counters.snapshot_timeouts, counters.snapshot_timeout_count, raw.snapshot_timeouts, raw.snapshot_timeout_count)),
        streamBytes: integer(firstDefined(counters.received_stream_bytes, counters.stream_bytes, raw.received_stream_bytes, raw.stream_bytes)),
        streamReconnects: integer(firstDefined(counters.stream_reconnect_count, counters.stream_reconnects, raw.stream_reconnect_count, raw.stream_reconnects)),
      },
      media: {
        mode: firstDefined(media.mode, raw.media_mode, state === "running_stream" ? "stream" : "snapshot"),
        ready: Boolean(firstDefined(media.ready, media.stream_ready, raw.media_ready, raw.stream_ready, false)),
        updatedAt: firstDefined(media.latest_at_utc, media.last_received_at_utc, media.updated_at_utc, media.timestamp, raw.latest_media_at_utc, raw.latest_snapshot_at_utc),
        snapshotAvailable: Boolean(firstDefined(media.snapshot_available, raw.snapshot_available, media.latest_at_utc, raw.latest_snapshot_at_utc)),
      },
      controls: {
        start: Boolean(firstDefined(controls.start, controls.can_start, raw.can_start, state === "not_started" || state === "stopped_manual")),
        stop: Boolean(firstDefined(controls.stop, controls.can_stop, raw.can_stop, state.startsWith("running_") || state === "recovery")),
        restart: Boolean(firstDefined(controls.restart, controls.can_restart, raw.can_restart, state !== "not_started")),
        continue: Boolean(firstDefined(controls.continue, controls.can_continue, raw.can_continue, state === "stopped_low_battery")),
      },
      stopReason: firstDefined(raw.stop_reason, raw.latest_error),
      authReady: firstDefined(raw.auth_ready, adapter.auth_ready, true) !== false,
    };
  }

  function renderStatus(status) {
    const stateInfo = STATES[status.state] || { label: humanize(status.state), variant: "neutral", activity: humanize(status.state) };
    dom.phaseBadge.textContent = stateInfo.label;
    dom.phaseBadge.variant = stateInfo.variant;
    dom.testName.textContent = status.testName;
    const displayIndex = status.state === "not_started" ? 0 : Math.min(status.testIndex + 1, status.testCount);
    dom.testStep.textContent = `Test ${displayIndex} of ${status.testCount}`;
    dom.startTime.textContent = formatDateTime(status.startedAt, "Not started");
    dom.activityLabel.textContent = stateInfo.activity;
    dom.cameraName.textContent = status.cameraName;
    dom.progressLabel.textContent = `${Math.round(status.progress)}%`;
    dom.overallProgress.value = status.progress;
    dom.startButton.disabled = !status.controls.start || runtime.actionBusy;
    dom.stopButton.disabled = !status.controls.stop || runtime.actionBusy;
    dom.restartButton.disabled = !status.controls.restart || runtime.actionBusy;
    dom.continueButton.hidden = !status.controls.continue;
    dom.continueButton.disabled = !status.controls.continue || runtime.actionBusy;

    renderCallout(status);
    renderBattery(status.battery);
    renderCounters(status.counters);
    renderMedia(status.media, status.state);
    updateClocks();
    dom.lastUpdated.textContent = `Updated ${new Intl.DateTimeFormat(undefined, { timeStyle: "medium" }).format(new Date())}`;
  }

  function renderCallout(status) {
    let title = "";
    let message = "";
    let variant = "warning";
    if (!status.authReady) {
      title = "Blink authentication required";
      message = "Complete the credential or MFA prompt in the launcher window, then this dashboard will reconnect automatically.";
      variant = "danger";
    } else if (status.state === "stopped_low_battery") {
      title = "Experiment paused for low battery";
      message = status.stopReason || "All camera activity stopped immediately. Recovery monitoring continues without snapshots or streaming.";
      variant = "danger";
    } else if (status.state === "stopped_error") {
      title = "Experiment stopped after a fatal error";
      message = status.stopReason || "Review recent errors before restarting the experiment.";
      variant = "danger";
    } else if (status.state === "recovery") {
      title = "Doorbell recovery period";
      message = "No snapshots or live stream are being requested. Battery checks and measurements continue.";
      variant = "warning";
    }
    dom.stateCallout.hidden = !title;
    if (title) {
      dom.stateCallout.variant = variant;
      dom.calloutTitle.textContent = title;
      dom.calloutMessage.textContent = message;
    }
  }

  function renderBattery(battery) {
    const state = battery.state ? String(battery.state) : "Unknown";
    const normalized = state.toLowerCase().replace(/[\s-]+/g, "_");
    dom.batteryBadge.textContent = humanize(state);
    dom.batteryBadge.variant = ["low", "replace", "replace_battery", "needs_replacement"].includes(normalized)
      ? "danger"
      : state === "Unknown" ? "neutral" : "success";
    dom.batteryVoltage.textContent = battery.voltage === null ? "Not available" : `${battery.voltage.toFixed(2)} V`;
    dom.batteryLevel.textContent = battery.level === null || battery.level === undefined ? "Not available" : String(battery.level);
    dom.batteryCheck.textContent = formatDateTime(battery.checkedAt, "Not available");
  }

  function renderCounters(counters) {
    dom.snapshotSuccesses.textContent = formatInteger(counters.snapshotSuccesses);
    dom.snapshotFailures.textContent = formatInteger(counters.snapshotFailures);
    dom.snapshotTimeouts.textContent = formatInteger(counters.snapshotTimeouts);
    dom.streamBytes.textContent = formatBytes(counters.streamBytes);
    dom.streamReconnects.textContent = formatInteger(counters.streamReconnects);
  }

  function renderMedia(media, state) {
    runtime.mediaTimestamp = media.updatedAt || runtime.mediaTimestamp;
    const wantsStream = state === "running_stream" && media.mode === "stream";
    if (wantsStream && media.ready) {
      showStream();
      dom.mediaBadge.textContent = "Live";
      dom.mediaBadge.variant = "success";
      dom.mediaMode.textContent = "Local HLS live stream";
    } else {
      destroyStream();
      const snapshotAvailable = media.snapshotAvailable || Boolean(media.updatedAt);
      const snapshotShown = showSnapshot(snapshotAvailable, media.updatedAt);
      dom.mediaBadge.textContent = snapshotShown ? (wantsStream ? "Fallback" : "Snapshot") : snapshotAvailable ? "Unavailable" : "Waiting";
      dom.mediaBadge.variant = snapshotShown ? "brand" : snapshotAvailable ? "warning" : "neutral";
      dom.mediaMode.textContent = wantsStream ? "Latest snapshot while live stream starts" : "Latest successful snapshot";
    }
    updateMediaAge();
  }

  function showSnapshot(available, version) {
    dom.streamVideo.hidden = true;
    const cacheVersion = version || Math.floor(Date.now() / POLL.status);
    if (available && cacheVersion === runtime.snapshotFailedVersion) {
      dom.snapshotImage.hidden = true;
      dom.mediaPlaceholder.hidden = false;
      dom.mediaBadge.textContent = "Unavailable";
      dom.mediaBadge.variant = "warning";
      return false;
    }
    dom.snapshotImage.hidden = !available;
    dom.mediaPlaceholder.hidden = available;
    if (!available) return false;
    dom.snapshotImage.onerror = () => {
      runtime.snapshotFailedVersion = cacheVersion;
      dom.snapshotImage.hidden = true;
      dom.mediaPlaceholder.hidden = false;
      dom.mediaBadge.textContent = "Unavailable";
      dom.mediaBadge.variant = "warning";
    };
    if (cacheVersion !== runtime.snapshotVersion) {
      runtime.snapshotVersion = cacheVersion;
      runtime.snapshotFailedVersion = null;
      dom.snapshotImage.src = `${ENDPOINTS.snapshot}?v=${encodeURIComponent(cacheVersion)}`;
    }
    return true;
  }

  function showStream() {
    dom.mediaPlaceholder.hidden = true;
    dom.snapshotImage.hidden = true;
    dom.streamVideo.hidden = false;
    if (runtime.hlsActive) return;
    const video = dom.streamVideo;
    if (video.canPlayType("application/vnd.apple.mpegurl")) {
      video.src = ENDPOINTS.stream;
      runtime.hlsActive = true;
      video.play().catch(() => {});
      return;
    }
    if (window.Hls && window.Hls.isSupported()) {
      runtime.hls = new window.Hls({ liveSyncDurationCount: 2, maxLiveSyncPlaybackRate: 1.25 });
      runtime.hls.loadSource(ENDPOINTS.stream);
      runtime.hls.attachMedia(video);
      runtime.hls.on(window.Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
      runtime.hls.on(window.Hls.Events.ERROR, (_, data) => {
        if (data.fatal) {
          destroyStream();
          showSnapshot(true, runtime.mediaTimestamp);
          dom.mediaBadge.textContent = "Fallback";
          dom.mediaBadge.variant = "warning";
        }
      });
      runtime.hlsActive = true;
      return;
    }
    showSnapshot(true, runtime.mediaTimestamp);
    dom.mediaBadge.textContent = "HLS unsupported";
    dom.mediaBadge.variant = "warning";
  }

  function destroyStream() {
    if (runtime.hls) {
      runtime.hls.destroy();
      runtime.hls = null;
    }
    if (dom.streamVideo) {
      dom.streamVideo.pause();
      dom.streamVideo.removeAttribute("src");
      dom.streamVideo.load();
    }
    runtime.hlsActive = false;
  }

  function updateClocks() {
    if (!runtime.status) return;
    const delta = Math.max(0, (Date.now() - runtime.statusReceivedAt) / 1000);
    const advances = runtime.status.state.startsWith("running_") || runtime.status.state === "recovery";
    const elapsed = (runtime.status.elapsedSeconds || 0) + (advances ? delta : 0);
    const remaining = runtime.status.remainingSeconds === null
      ? null
      : Math.max(0, runtime.status.remainingSeconds - (advances ? delta : 0));
    dom.elapsedTime.textContent = formatDuration(elapsed);
    dom.remainingTime.textContent = remaining === null ? "Not available" : formatDuration(remaining);
    updateMediaAge();
  }

  function updateMediaAge() {
    if (!runtime.mediaTimestamp) {
      dom.mediaAge.textContent = "No media received";
      return;
    }
    const timestamp = new Date(runtime.mediaTimestamp).getTime();
    if (!Number.isFinite(timestamp)) {
      dom.mediaAge.textContent = "Media time unavailable";
      return;
    }
    const age = Math.max(0, (Date.now() - timestamp) / 1000);
    dom.mediaAge.textContent = `Last received ${formatDuration(age)} ago`;
  }

  function renderErrors(errors) {
    const normalized = errors.slice(0, 50);
    dom.errorsEmpty.hidden = normalized.length > 0;
    dom.errorsList.hidden = normalized.length === 0;
    dom.errorsList.replaceChildren(...normalized.map((entry) => {
      const item = document.createElement("li");
      item.className = "error-item";
      const time = document.createElement("time");
      time.className = "error-meta";
      const timestamp = firstDefined(entry.timestamp_utc, entry.timestamp, entry.created_at);
      time.dateTime = timestamp || "";
      time.textContent = formatDateTime(timestamp, "Time unavailable");
      const context = document.createElement("span");
      context.className = "error-meta";
      context.textContent = [firstDefined(entry.test_name, entry.test, "Experiment"), firstDefined(entry.category, entry.level, "warning")].join(" · ");
      const message = document.createElement("span");
      message.textContent = firstDefined(entry.message, entry.error, "No details provided");
      item.append(time, context, message);
      return item;
    }));
  }

  function renderChart(raw) {
    const points = Array.isArray(raw) ? raw : raw.points || raw.measurements || [];
    const phases = Array.isArray(raw.phases) ? raw.phases : [];
    const voltage = [];
    const level = [];
    points.forEach((point) => {
      const timestamp = Date.parse(firstDefined(point.timestamp_utc, point.observed_at_utc, point.timestamp));
      if (!Number.isFinite(timestamp)) return;
      const volts = finiteOrNull(firstDefined(point.battery_voltage_volts, point.voltage_volts, point.voltage));
      const rawLevel = finiteOrNull(firstDefined(point.battery_level_raw, point.raw_battery_level, point.battery_level));
      if (volts !== null) voltage.push({ x: timestamp, y: volts });
      if (rawLevel !== null) level.push({ x: timestamp, y: rawLevel });
    });

    const styles = getComputedStyle(document.documentElement);
    const palette = {
      brand: styles.getPropertyValue("--wa-color-brand-fill-loud").trim(),
      text: styles.getPropertyValue("--wa-color-text-normal").trim(),
      quiet: styles.getPropertyValue("--wa-color-text-quiet").trim(),
      border: styles.getPropertyValue("--wa-color-surface-border").trim(),
      purple: styles.getPropertyValue("--wa-color-purple-50").trim(),
      cyan: styles.getPropertyValue("--wa-color-cyan-50").trim(),
      warning: styles.getPropertyValue("--wa-color-warning-fill-loud").trim(),
    };
    const datasets = [{
      label: "Battery voltage (V)",
      data: voltage,
      borderColor: palette.brand,
      backgroundColor: palette.brand,
      borderWidth: 3,
      pointRadius: voltage.length > 500 ? 0 : 2,
      tension: 0.18,
      yAxisID: "voltage",
    }];
    if (level.length) {
      datasets.push({
        label: "Raw battery level",
        data: level,
        borderColor: palette.purple,
        backgroundColor: palette.purple,
        borderWidth: 2,
        borderDash: [6, 5],
        pointRadius: level.length > 500 ? 0 : 2,
        tension: 0.18,
        yAxisID: "raw",
      });
    }
    const normalizedPhases = phases.map((phase, index) => ({
      start: Date.parse(firstDefined(phase.started_at_utc, phase.start_utc, phase.start)),
      end: Date.parse(firstDefined(phase.ended_at_utc, phase.end_utc, phase.end)) || Date.now(),
      label: firstDefined(phase.label, phase.test_name, phase.name, phase.state, `Phase ${index + 1}`),
      recovery: String(firstDefined(phase.state, phase.type, phase.phase_kind, "")).toLowerCase().includes("recovery"),
    })).filter((phase) => Number.isFinite(phase.start) && Number.isFinite(phase.end));
    renderAccessibleHistory(points, normalizedPhases);
    if (!window.Chart) {
      dom.chartStatus.textContent = "Chart unavailable; accessible history remains available below";
      return;
    }

    if (!runtime.chart) {
      const phaseBands = {
        id: "phaseBands",
        beforeDatasetsDraw(chart, _args, options) {
          const { ctx, chartArea, scales } = chart;
          if (!chartArea || !scales.x) return;
          ctx.save();
          options.phases.forEach((phase, index) => {
            const left = Math.max(chartArea.left, scales.x.getPixelForValue(phase.start));
            const right = Math.min(chartArea.right, scales.x.getPixelForValue(phase.end));
            if (right <= left) return;
            ctx.fillStyle = phase.recovery ? options.recoveryColor : options.activeColors[index % options.activeColors.length];
            ctx.globalAlpha = phase.recovery ? 0.07 : 0.05;
            ctx.fillRect(left, chartArea.top, right - left, chartArea.bottom - chartArea.top);
            ctx.globalAlpha = 0.72;
            ctx.fillStyle = options.textColor;
            const rootStyles = getComputedStyle(document.documentElement);
            const weight = rootStyles.getPropertyValue("--wa-font-weight-semibold").trim();
            const size = rootStyles.getPropertyValue("--wa-font-size-xs").trim();
            ctx.font = `${weight} ${size} ${getComputedStyle(document.body).fontFamily}`;
            ctx.save();
            ctx.translate(left + 8, chartArea.top + 10);
            ctx.rotate(-Math.PI / 2);
            ctx.fillText(humanize(phase.label), 0, 0, Math.max(0, chartArea.height - 20));
            ctx.restore();
          });
          ctx.restore();
        },
      };
      runtime.chart = new window.Chart(dom.batteryChart, {
        type: "line",
        data: { datasets },
        plugins: [phaseBands],
        options: chartOptions(palette, normalizedPhases),
      });
    } else {
      runtime.chart.data.datasets = datasets;
      runtime.chart.options.plugins.phaseBands.phases = normalizedPhases;
      runtime.chart.update("none");
    }
    const truncation = raw.truncated ? " · limited to recent measurements" : "";
    dom.chartStatus.textContent = points.length ? `${formatInteger(points.length)} measurements${truncation}` : "No measurements yet";
  }

  function renderAccessibleHistory(points, phases) {
    const rows = points.map((point) => ({
      timestamp: firstDefined(point.timestamp_utc, point.observed_at_utc, point.timestamp),
      phase: firstDefined(point.test_name, point.state, "Experiment"),
      voltage: finiteOrNull(firstDefined(point.battery_voltage_volts, point.voltage_volts, point.voltage)),
      level: firstDefined(point.battery_level_raw, point.raw_battery_level, point.battery_level),
    })).filter((point) => Number.isFinite(Date.parse(point.timestamp)));
    const voltageRows = rows.filter((point) => point.voltage !== null);
    if (voltageRows.length) {
      const first = voltageRows[0];
      const last = voltageRows[voltageRows.length - 1];
      const change = last.voltage - first.voltage;
      const direction = change === 0 ? "unchanged" : change > 0 ? "up" : "down";
      dom.historySummary.textContent = `${formatInteger(rows.length)} measurements. Voltage ${direction} ${Math.abs(change).toFixed(2)} V, from ${first.voltage.toFixed(2)} V to ${last.voltage.toFixed(2)} V.`;
    } else {
      dom.historySummary.textContent = rows.length ? `${formatInteger(rows.length)} measurements; voltage was unavailable.` : "No battery measurements yet.";
    }
    dom.phaseSummary.textContent = phases.length
      ? `Recorded phases: ${phases.map((phase) => humanize(phase.label)).join(", ")}.`
      : "No experiment phases recorded yet.";
    const recent = rows.slice(-200).reverse();
    dom.measurementsTableBody.replaceChildren(...recent.map((point) => {
      const row = document.createElement("tr");
      [
        formatDateTime(point.timestamp, "Time unavailable"),
        humanize(point.phase),
        point.voltage === null ? "Not available" : `${point.voltage.toFixed(2)} V`,
        point.level === null || point.level === undefined ? "Not available" : String(point.level),
      ].forEach((value) => {
        const cell = document.createElement("td");
        cell.textContent = value;
        row.append(cell);
      });
      return row;
    }));
  }

  function chartOptions(palette, phases) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? false : { duration: 180 },
      parsing: false,
      normalized: true,
      interaction: { intersect: false, mode: "index" },
      plugins: {
        legend: { position: "bottom", labels: { color: palette.text, usePointStyle: true } },
        tooltip: {
          callbacks: {
            title(items) { return items.length ? formatDateTime(items[0].parsed.x, "") : ""; },
          },
        },
        phaseBands: {
          phases,
          activeColors: [palette.brand, palette.cyan, palette.purple],
          recoveryColor: palette.warning,
          textColor: palette.quiet,
        },
      },
      scales: {
        x: {
          type: "linear",
          grid: { color: palette.border },
          ticks: { color: palette.quiet, maxTicksLimit: 8, callback(value) { return shortDateTime(value); } },
        },
        voltage: {
          type: "linear",
          position: "left",
          title: { display: true, text: "Voltage (V)", color: palette.quiet },
          grid: { color: palette.border },
          ticks: { color: palette.quiet },
        },
        raw: {
          type: "linear",
          position: "right",
          display: "auto",
          title: { display: true, text: "Raw battery level", color: palette.quiet },
          grid: { drawOnChartArea: false },
          ticks: { color: palette.quiet, precision: 0 },
        },
      },
    };
  }

  function openConfirmation(action) {
    const copy = ACTION_COPY[action];
    if (!copy || runtime.actionBusy) return;
    runtime.pendingAction = action;
    dom.confirmationMessage.textContent = copy.title;
    dom.confirmationConsequence.textContent = copy.consequence;
    dom.confirmAction.textContent = copy.confirm;
    dom.confirmAction.variant = copy.variant;
    dom.confirmationDialog.open = true;
  }

  async function performPendingAction() {
    const action = runtime.pendingAction;
    if (!action || runtime.actionBusy) return;
    runtime.actionBusy = true;
    dom.confirmAction.loading = true;
    setControlsBusy(true);
    try {
      await fetchJson(ENDPOINTS.actions[action], {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
      dom.confirmationDialog.open = false;
      showToast(`${ACTION_COPY[action].confirm} request accepted.`);
      await pollStatus();
      await pollMeasurements();
    } catch (error) {
      showToast(error.status === 409 ? error.message : `Action failed: ${error.message}`, true);
    } finally {
      runtime.actionBusy = false;
      runtime.pendingAction = null;
      dom.confirmAction.loading = false;
      setControlsBusy(false);
      if (runtime.status) renderStatus(runtime.status);
    }
  }

  function setControlsBusy(busy) {
    [dom.startButton, dom.stopButton, dom.restartButton, dom.continueButton].forEach((button) => {
      if (busy) button.disabled = true;
    });
  }

  function setConnection(online, detail) {
    dom.connectionStatus.classList.toggle("is-online", online);
    dom.connectionStatus.classList.toggle("is-offline", !online);
    dom.connectionStatus.textContent = online ? "Connected" : "Disconnected";
    dom.connectionStatus.title = online ? "Dashboard service is responding" : (detail || "Dashboard service is unavailable");
  }

  function showToast(message, error = false) {
    const toast = document.createElement("div");
    toast.className = error ? "toast is-error" : "toast";
    toast.textContent = message;
    dom.toastRegion.append(toast);
    window.setTimeout(() => toast.classList.add("is-leaving"), 4200);
    window.setTimeout(() => toast.remove(), 4500);
  }

  function firstDefined(...values) {
    return values.find((value) => value !== undefined && value !== null);
  }

  function finiteOrNull(value) {
    if (value === undefined || value === null || value === "") return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function integer(value) {
    const number = Number(value);
    return Number.isFinite(number) ? Math.max(0, Math.trunc(number)) : 0;
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function humanize(value) {
    return String(value || "Unknown").replace(/[_-]+/g, " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
  }

  function formatInteger(value) {
    return new Intl.NumberFormat().format(value || 0);
  }

  function formatBytes(value) {
    const bytes = Number(value) || 0;
    if (bytes < 1000) return `${bytes} B`;
    const units = ["kB", "MB", "GB", "TB"];
    let amount = bytes;
    let unit = "B";
    for (const candidate of units) {
      amount /= 1000;
      unit = candidate;
      if (amount < 1000) break;
    }
    return `${amount >= 10 ? amount.toFixed(1) : amount.toFixed(2)} ${unit}`;
  }

  function formatDuration(value) {
    if (value === null || value === undefined || !Number.isFinite(Number(value))) return "Not available";
    let seconds = Math.max(0, Math.floor(Number(value)));
    const days = Math.floor(seconds / 86400);
    seconds %= 86400;
    const hours = Math.floor(seconds / 3600);
    seconds %= 3600;
    const minutes = Math.floor(seconds / 60);
    seconds %= 60;
    if (days) return `${days}d ${hours}h`;
    if (hours) return `${hours}h ${minutes}m`;
    if (minutes) return `${minutes}m ${seconds}s`;
    return `${seconds}s`;
  }

  function formatDateTime(value, fallback) {
    if (value === undefined || value === null || value === "") return fallback;
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return fallback;
    return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "medium" }).format(date);
  }

  function shortDateTime(value) {
    const date = new Date(Number(value));
    if (!Number.isFinite(date.getTime())) return "";
    return new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
  }
})();
