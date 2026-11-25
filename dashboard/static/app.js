"use strict";

// One script for every page (home, caregiver, patient, robot). Each feature
// only runs when its elements exist on the current page.

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const PAGE = document.body.dataset.page;

const LEVEL_LABEL = { info: "Info", warning: "Warning", critical: "Critical" };
const REASONS = {
  path_blocked: "Path blocked",
  lost_line: "Robot lost its line",
  low_battery: "Robot battery too low",
  delivery_timeout: "Delivery took too long",
  load_failed: "Dispenser could not load the dose",
  robot_unavailable: "Robot unavailable",
  not_confirmed: "Patient did not confirm in time",
  past_grace_window: "Not delivered before the grace window ended",
  slot_empty: "Dispenser slot is empty",
  dispenser_offline: "Dispenser offline",
  robot_offline: "Robot offline (no heartbeat)",
  emergency_stop: "Emergency stop pressed",
};
const DAY_LABEL = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };
const ROBOT_LABEL = {
  IDLE: "Idle",
  DELIVERING: "Delivering",
  WAITING_CONFIRM: "With patient",
  RETURNING: "Returning",
  CHARGING: "Charging",
  EMERGENCY_STOP: "Emergency stop",
  OFFLINE: "Offline",
};
const ROBOT_TEXT = {
  IDLE: "Waiting at the dock",
  DELIVERING: "On the way to the patient",
  WAITING_CONFIRM: "With the patient, waiting for confirmation",
  RETURNING: "Heading back to the dock",
  CHARGING: "Charging at the dock",
  EMERGENCY_STOP: "Stopped. Release the emergency stop to continue",
  OFFLINE: "No heartbeat from the robot",
};

// Delivery cycle stages reported by the backend -> stepper position.
// Steps: 0 Dose due, 1 Deliver, 2 Alert, 3 Detect, 4 Return, 5 = all done.
const STAGES = {
  due: { label: "Dose due", step: 0 },
  loading: { label: "Loading in dispenser", step: 0 },
  delivering: { label: "Delivering", step: 1 },
  arrived: { label: "Arrived at patient", step: 1 },
  waiting: { label: "Waiting for confirmation", step: 2 },
  taken: { label: "Dose taken", step: 3 },
  missed: { label: "Not confirmed, caregiver alerted", step: 3 },
  failed: { label: "Delivery failed, caregiver alerted", step: null },
  returning: { label: "Returning to dock", step: 4 },
  charging: { label: "Charging", step: 4 },
  complete: { label: "Cycle complete", step: 5 },
};
const FAILED_AT_DUE = new Set(["load_failed", "robot_unavailable", "slot_empty", "dispenser_offline"]);

const known = new Map(); // notification id -> notification
let notificationsReady = false;
let simNow = null; // simulated clock (it runs faster than real time)
let lastSnapshot = null;
let patientConfirmedAt = null;
const prefs = loadPrefs();

// ------------------------------------------------------------------ helpers

function loadPrefs() {
  const defaults = { alerts: false, lastRead: 0 };
  try {
    return { ...defaults, ...JSON.parse(localStorage.getItem("mediagent.prefs") || "{}") };
  } catch {
    return defaults;
  }
}

function savePrefs() {
  try {
    localStorage.setItem("mediagent.prefs", JSON.stringify(prefs));
  } catch {
    /* storage unavailable: preferences last for this page only */
  }
}

function setText(selector, text) {
  const node = typeof selector === "string" ? $(selector) : selector;
  if (node && node.textContent !== text) node.textContent = text;
}

function formatTime(ts, withSeconds = true) {
  if (ts == null) return "";
  const date = new Date(ts * 1000);
  const options = withSeconds ? { hour: "2-digit", minute: "2-digit", second: "2-digit" } : { hour: "2-digit", minute: "2-digit" };
  const time = date.toLocaleTimeString([], options);
  const reference = simNow ? new Date(simNow * 1000) : new Date();
  const sameDay = reference.toDateString() === date.toDateString();
  return sameDay ? time : `${date.toLocaleDateString([], { month: "short", day: "numeric" })} ${time}`;
}

function reasonText(reason) {
  return reason ? REASONS[reason] ?? reason.replaceAll("_", " ") : "";
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children) if (child != null) node.append(child);
  return node;
}

async function api(path, options = {}) {
  const init = { ...options, headers: { ...(options.headers || {}) } };
  if (options.json !== undefined) {
    init.body = JSON.stringify(options.json);
    init.headers["Content-Type"] = "application/json";
    delete init.json;
  }
  const response = await fetch(path, init);
  let data = null;
  try {
    data = await response.json();
  } catch {
    /* empty body */
  }
  if (!response.ok) {
    const error = new Error(`${init.method || "GET"} ${path} failed (${response.status})`);
    error.status = response.status;
    error.errors = data?.errors ?? { server: error.message };
    throw error;
  }
  return data;
}

function throttle(fn, ms) {
  let last = 0;
  let timer = null;
  return () => {
    const wait = last + ms - Date.now();
    if (wait <= 0) {
      last = Date.now();
      fn();
    } else if (!timer) {
      timer = setTimeout(() => {
        timer = null;
        last = Date.now();
        fn();
      }, wait);
    }
  };
}

// -------------------------------------------------------------------- state

function renderState(snapshot) {
  if (snapshot.now) simNow = snapshot.now;
  lastSnapshot = { ...(lastSnapshot || {}), ...snapshot };
  const state = snapshot.robot_state;
  const battery = Math.round(snapshot.battery);
  const cycle = snapshot.cycle;
  const cycleActive = cycle && STAGES[cycle.stage] && cycle.stage !== "complete";

  // Robot page
  const stateLabel = ROBOT_LABEL[state] ?? state;
  setText("#robot-state", stateLabel);
  setText("#robot-state-text", ROBOT_TEXT[state] ?? "");
  setText("#battery", `${battery}%`);
  const bar = $("#battery-bar");
  if (bar) {
    bar.style.width = `${Math.max(0, Math.min(100, snapshot.battery))}%`;
    bar.dataset.level = battery < 15 ? "bad" : battery < 30 ? "warn" : "good";
  }
  setText("#battery-text", battery < 15 ? "Critical: too low to deliver" : battery < 30 ? "Low: will charge before delivering" : "Good");
  setText("#robot-task", snapshot.active_dose?.medication ?? "No task");
  setText("#robot-task-stage", cycleActive ? STAGES[cycle.stage].label : "");
  const screen = $("#robot-screen");
  if (screen) {
    screen.dataset.on = String(Boolean(snapshot.alert_on));
    setText(screen, snapshot.alert_on ? snapshot.alert_text || "Alert on" : "Screen off");
  }
  $$("#state-sequence li").forEach((step) => {
    const current = step.dataset.state === state;
    step.classList.toggle("current", current);
    if (current) step.setAttribute("aria-current", "step");
    else step.removeAttribute("aria-current");
  });

  // Caregiver summary cards
  setText("#caregiver-robot-state", stateLabel);
  setText("#summary-battery", `Battery ${battery}%`);
  setText("#summary-dose", cycleActive ? cycle.medication : "None");
  setText(
    "#summary-next",
    cycleActive
      ? STAGES[cycle.stage].label
      : snapshot.next_dose
        ? `Next: ${snapshot.next_dose.medication} at ${formatTime(snapshot.next_dose.due_ts, false)}`
        : "Nothing scheduled",
  );

  // Home page
  setText("[data-home-robot]", `Now: ${(ROBOT_TEXT[state] ?? state).toLowerCase()} · battery ${battery}%`);
  setText("[data-home-patient]", snapshot.active_dose ? `Now: ${snapshot.active_dose.medication} in progress` : "Now: no medicine due");

  renderPatient(snapshot);
  renderCycle(snapshot);
  if (snapshot.notifications) ingestNotifications(snapshot.notifications);
}

function renderPatient(snapshot) {
  const card = $("#patient-card");
  if (!card) return;
  const cycle = snapshot.cycle;
  const waiting = snapshot.robot_state === "WAITING_CONFIRM";
  // Outcome messages ("Thank you", "delayed"...) stay up for 2 simulated minutes.
  const recent = cycle && simNow != null && simNow - cycle.since < 120;
  const fresh = (c) => c.stage !== "complete" || recent;
  const medication = cycle?.medication ?? snapshot.active_dose?.medication ?? "";
  let mode = "idle";
  if (waiting) mode = "waiting";
  else if (cycle && ["due", "loading"].includes(cycle.stage)) mode = "preparing";
  else if (cycle && ["delivering", "arrived"].includes(cycle.stage)) mode = "coming";
  else if (cycle?.result === "taken" && fresh(cycle)) mode = "done";
  else if (cycle?.result === "missed" && fresh(cycle)) mode = "missed";
  else if (cycle?.result === "failed" && (cycle.stage !== "failed" || recent) && fresh(cycle)) mode = "delayed";
  if (mode === "waiting" && patientConfirmedAt && Date.now() - patientConfirmedAt < 5000) mode = "done";

  const copy = {
    idle: ["No medicine right now", "You can relax. We will tell you when it is time."],
    preparing: ["Getting your medicine ready", "The dispenser is preparing your dose."],
    coming: ["Your medicine is on its way", "The robot is bringing it to you now."],
    waiting: ["Time to take your medicine", "Take the medicine from the robot, then press the big button."],
    done: ["Thank you!", "Your dose has been recorded."],
    missed: ["We let your caregiver know", "Your caregiver will check in with you soon."],
    delayed: ["Your medicine is delayed", "Your caregiver has been told and will help."],
  }[mode];
  card.dataset.mode = mode;
  setText("#patient-title", copy[0]);
  setText("#patient-message", copy[1]);
  const med = $("#medicine");
  med.hidden = !["waiting", "coming", "preparing"].includes(mode) || !medication;
  setText(med, medication);
  $("#confirm-button").disabled = !waiting || mode === "done";
  setText(
    "#patient-next",
    snapshot.next_dose ? `Next medicine: ${snapshot.next_dose.medication} at ${formatTime(snapshot.next_dose.due_ts, false)}` : "",
  );
}

function renderCycle(snapshot) {
  const status = $("#cycle-status");
  if (!status) return;
  const cycle = snapshot.cycle;
  const stage = cycle ? STAGES[cycle.stage] : null;
  let failedAt = null;
  if (cycle?.result === "failed") failedAt = FAILED_AT_DUE.has(cycle.reason) ? 0 : 1;
  if (cycle?.result === "missed") failedAt = 3;
  let current = stage ? stage.step : -1;
  if (cycle?.stage === "failed") current = failedAt ?? 1;

  if (!cycle || !stage) {
    setText(status, "No active dose");
  } else {
    const reason = cycle.result === "failed" || cycle.result === "missed" ? ` (${reasonText(cycle.reason)})` : "";
    setText(status, `${stage.label}${reason} · ${cycle.medication} · since ${formatTime(cycle.since)}`);
  }
  status.dataset.tone = cycle?.result === "failed" || cycle?.result === "missed" ? "bad" : cycle?.result === "taken" ? "good" : "";

  $$(".stepper li").forEach((item) => {
    const index = Number(item.dataset.step);
    let label = "Waiting";
    let kind = "upcoming";
    if (failedAt !== null && index === failedAt) {
      kind = "failed";
      label = "Failed";
    } else if (failedAt !== null && index > failedAt && index < 4) {
      kind = "skipped";
      label = "Skipped";
    } else if (index < current) {
      kind = "done";
      label = "Done";
    } else if (index === current) {
      kind = "current";
      label = "In progress";
    }
    item.className = kind;
    setText(item.querySelector(".step-state"), label);
    if (index === current) item.setAttribute("aria-current", "step");
    else item.removeAttribute("aria-current");
  });

  const failurePath = $("#failure-path");
  const alerted = failedAt !== null;
  failurePath.classList.toggle("active", alerted);
  setText(failurePath.querySelector(".step-state"), alerted ? "Triggered" : "Not triggered");

  const parts = [];
  if (snapshot.queued > 1 || (snapshot.queued === 1 && cycle?.stage !== "due")) {
    parts.push(`${snapshot.queued} dose${snapshot.queued === 1 ? "" : "s"} queued, waiting for the robot`);
  }
  if (snapshot.next_dose) parts.push(`Next: ${snapshot.next_dose.medication} at ${formatTime(snapshot.next_dose.due_ts, false)}`);
  setText("#queue-info", parts.join(" · "));
}

// ------------------------------------------------------------ notifications

function ingestNotifications(list) {
  let changed = false;
  for (const notification of list) {
    const previous = known.get(notification.id);
    if (!previous) {
      known.set(notification.id, notification);
      changed = true;
      if (notificationsReady) announce(notification);
    } else if ((notification.repeat ?? 0) > (previous.repeat ?? 0) && !previous.acked) {
      // An unacknowledged critical alert repeats until someone acknowledges it.
      known.set(notification.id, { ...previous, ...notification });
      changed = true;
      document.getElementById(`toast-${notification.id}`)?.remove();
      if (notificationsReady) announce(notification);
    } else if (previous.acked !== notification.acked) {
      known.set(notification.id, { ...previous, ...notification });
      changed = true;
      if (notification.acked) removeToast(notification.id);
    }
  }
  if (changed) renderNotifications();
}

function needsAck(notification) {
  return notification.level !== "info" && !notification.acked;
}

function isUnread(notification) {
  return notification.ts > prefs.lastRead || needsAck(notification);
}

function renderNotifications() {
  const all = [...known.values()].sort((a, b) => b.ts - a.ts);
  const unread = all.filter(isUnread).length;
  const pending = all.filter(needsAck);
  const critical = pending.filter((n) => n.level === "critical").length;
  const plural = pending.length === 1 ? "" : "s";

  // Badges on every page (nav link and the caregiver Live tab).
  $$("[data-ack-badge]").forEach((badge) => {
    badge.hidden = pending.length === 0;
    badge.textContent = String(pending.length);
    badge.setAttribute("aria-label", `${pending.length} alert${plural} to acknowledge`);
    badge.dataset.level = critical ? "critical" : "warning";
  });
  setText("[data-home-alerts]", pending.length ? `${pending.length} alert${plural} need attention` : "No alerts need attention");

  setText("#unread-count", `${unread} unread`);
  updateBacklogToast();
  const bell = $("#notif-button");
  if (bell) {
    const count = $("#notif-count");
    count.hidden = unread === 0;
    count.textContent = unread > 99 ? "99+" : String(unread);
    count.dataset.level = critical ? "critical" : pending.length ? "warning" : "info";
    bell.setAttribute(
      "aria-label",
      `Notifications: ${unread} unread${pending.length ? `, ${pending.length} need${pending.length === 1 ? "s" : ""} acknowledgement` : ""}`,
    );
  }
  setText("#summary-alerts", pending.length ? `${pending.length} to acknowledge` : "All clear");
  const alertsCard = $("#summary-alerts-card");
  if (alertsCard) alertsCard.dataset.tone = critical ? "bad" : pending.length ? "warn" : "";
  document.title = `${pending.length ? `(${pending.length}) ` : ""}${document.title.replace(/^\(\d+\) /, "")}`;

  const list = $("#notification-list");
  if (!list) return;
  list.replaceChildren();
  if (all.length === 0) list.append(el("li", { class: "empty" }, "No notifications yet"));
  for (const notification of all.slice(0, 50)) {
    const item = el(
      "li",
      { class: `notification level-${notification.level}${isUnread(notification) ? " unread" : ""}` },
      el("span", { class: "level-badge" }, LEVEL_LABEL[notification.level] ?? notification.level),
      el("span", { class: "notification-time" }, formatTime(notification.ts)),
      el("p", { class: "notification-message" }, notification.message),
    );
    if (needsAck(notification)) {
      item.append(el("button", { class: "btn btn-secondary btn-small", type: "button", onclick: () => acknowledge(notification.id) }, "Acknowledge"));
    } else if (notification.level !== "info") {
      item.append(el("span", { class: "acked" }, "Acknowledged"));
    }
    list.append(item);
  }
  const needs = $("#needs-ack");
  needs.hidden = pending.length === 0;
  needs.textContent = `${pending.length} alert${plural} need${pending.length === 1 ? "s" : ""} acknowledgement`;
}

async function acknowledge(id) {
  try {
    await api(`/notifications/${encodeURIComponent(id)}/ack`, { method: "POST" });
    const notification = known.get(id);
    if (notification) known.set(id, { ...notification, acked: true });
    const panel = $("#notif-panel");
    const focusWasInPanel = panel?.contains(document.activeElement);
    removeToast(id);
    renderNotifications();
    if (focusWasInPanel) ($("#notification-list .btn") ?? panel).focus();
  } catch (error) {
    console.error(error);
    $$(".js-trigger-status").forEach((node) => setText(node, `Could not acknowledge: ${Object.values(error.errors).join("; ")}`));
  }
}

function announce(notification) {
  if (PAGE !== "caregiver") return; // toasts, sound and desktop alerts are for the caregiver
  showToast(notification);
  if (!prefs.alerts || notification.level === "info") return;
  beep(notification.level === "critical" ? 2 : 1);
  if ("Notification" in window && Notification.permission === "granted") {
    try {
      const desktop = new Notification(`MediAgent ${LEVEL_LABEL[notification.level]}`, { body: notification.message, tag: notification.id });
      desktop.onclick = () => {
        window.focus();
        openNotifications();
      };
    } catch (error) {
      console.error(error);
    }
  }
}

function showToast(notification) {
  const container = $(notification.level === "info" ? "#toast-polite" : "#toast-assertive");
  if (!container || document.getElementById(`toast-${notification.id}`)) return;
  const urgent = notification.level !== "info";
  const toast = el(
    "div",
    { class: `toast level-${notification.level}`, id: `toast-${notification.id}`, role: urgent ? "alert" : "status" },
    el(
      "div",
      { class: "toast-head" },
      el("strong", { class: "level-badge" }, LEVEL_LABEL[notification.level] ?? notification.level),
      el("span", { class: "notification-time" }, formatTime(notification.ts)),
    ),
    el("p", {}, notification.message),
  );
  const actions = el("div", { class: "toast-actions" });
  if (urgent) actions.append(el("button", { class: "btn btn-primary btn-small", type: "button", onclick: () => acknowledge(notification.id) }, "Acknowledge"));
  if (notification.level !== "critical") actions.append(el("button", { class: "btn btn-ghost btn-small", type: "button", onclick: () => removeToast(notification.id) }, "Dismiss"));
  toast.append(actions);
  container.append(toast);
  // Critical toasts stay until acknowledged; the others fade out but remain in the list.
  if (notification.level === "info") setTimeout(() => removeToast(notification.id), 6000);
  if (notification.level === "warning") setTimeout(() => removeToast(notification.id), 20000);
}

function removeToast(id) {
  document.getElementById(`toast-${id}`)?.remove();
}

function showBacklogToast() {
  const container = $("#toast-assertive");
  if (!container) return;
  const toast = el(
    "div",
    { class: "toast level-critical", id: "toast-backlog", role: "alert" },
    el("div", { class: "toast-head" }, el("strong", { class: "level-badge" }, "Critical")),
    el("p", { id: "backlog-text" }, ""),
    el(
      "div",
      { class: "toast-actions" },
      el("button", { class: "btn btn-primary btn-small", type: "button", onclick: reviewAlerts }, "Review alerts"),
    ),
  );
  container.append(toast);
  updateBacklogToast();
}

function updateBacklogToast() {
  const toast = $("#toast-backlog");
  if (!toast) return;
  const count = [...known.values()].filter((n) => n.level === "critical" && !n.acked).length;
  if (count === 0) toast.remove();
  else setText("#backlog-text", `${count} critical alert${count === 1 ? "" : "s"} still need${count === 1 ? "s" : ""} acknowledgement.`);
}

function reviewAlerts() {
  openNotifications();
  $("#notification-list .btn")?.focus();
}

// ------------------------------------------------------ notifications popup

function openNotifications() {
  const panel = $("#notif-panel");
  if (!panel || !panel.hidden) return;
  panel.hidden = false;
  document.body.classList.add("notif-open");
  $("#notif-button").setAttribute("aria-expanded", "true");
  panel.focus();
}

function closeNotifications(returnFocus = true) {
  const panel = $("#notif-panel");
  if (!panel || panel.hidden) return;
  panel.hidden = true;
  document.body.classList.remove("notif-open");
  $("#notif-button").setAttribute("aria-expanded", "false");
  markAllRead(); // everything shown in the popup now counts as seen
  if (returnFocus) $("#notif-button").focus();
}

function markAllRead() {
  prefs.lastRead = Math.max(prefs.lastRead, ...[...known.values()].map((n) => n.ts));
  savePrefs();
  renderNotifications();
}

function initNotificationPopup() {
  const button = $("#notif-button");
  if (!button) return;
  const panel = $("#notif-panel");
  button.addEventListener("click", () => (panel.hidden ? openNotifications() : closeNotifications()));
  $("#notif-close").addEventListener("click", () => closeNotifications());
  $("#summary-open-notifs")?.addEventListener("click", () => openNotifications());
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !panel.hidden) closeNotifications();
  });
  // A click outside closes the popup without stealing focus from what was clicked.
  document.addEventListener("click", (event) => {
    if (panel.hidden) return;
    if (event.target.closest(".notif, #summary-open-notifs, .toast")) return;
    closeNotifications(false);
  });
}

let audioContext = null;
function beep(times) {
  try {
    audioContext ??= new window.AudioContext();
    for (let i = 0; i < times; i += 1) {
      const start = audioContext.currentTime + i * 0.3;
      const oscillator = audioContext.createOscillator();
      const gain = audioContext.createGain();
      oscillator.frequency.value = 880;
      gain.gain.setValueAtTime(0.2, start);
      gain.gain.exponentialRampToValueAtTime(0.001, start + 0.2);
      oscillator.connect(gain).connect(audioContext.destination);
      oscillator.start(start);
      oscillator.stop(start + 0.2);
    }
  } catch (error) {
    console.error(error);
  }
}

async function loadNotifications() {
  ingestNotifications(await api("/notifications"));
  if (!notificationsReady) {
    notificationsReady = true;
    renderNotifications();
    // Unacknowledged critical alerts from before this page load stay on screen:
    // one toast if there is a single alert, otherwise one summary toast.
    const old = [...known.values()].filter((n) => n.level === "critical" && !n.acked);
    if (old.length === 1) showToast(old[0]);
    else if (old.length > 1) showBacklogToast();
  }
}

// -------------------------------------------------------------------- tabs

function initTabs() {
  const tabs = $$('[role="tab"]');
  if (!tabs.length) return;
  function select(tab, focus) {
    for (const other of tabs) {
      const selected = other === tab;
      other.setAttribute("aria-selected", String(selected));
      other.tabIndex = selected ? 0 : -1;
      document.getElementById(other.getAttribute("aria-controls")).hidden = !selected;
    }
    if (focus) tab.focus();
    history.replaceState(null, "", `#${tab.dataset.tab}`);
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", () => select(tab, false));
    tab.addEventListener("keydown", (event) => {
      const moves = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: tabs.length - 1 };
      if (!(event.key in moves)) return;
      event.preventDefault();
      select(tabs[(moves[event.key] + tabs.length) % tabs.length], true);
    });
  });
  const fromHash = tabs.find((tab) => `#${tab.dataset.tab}` === location.hash);
  if (fromHash) select(fromHash, false);
  // "#notifications" is a deep link that opens the notifications popup.
  if (location.hash === "#notifications") setTimeout(openNotifications, 0);
}

// --------------------------------------------------------------- schedules

let editingId = null;
const form = $("#schedule-form");
const fieldFor = { medication: "#f-medication", slot: "#f-slot", time_hhmm: "#f-time", days: "#f-days" };

async function loadSchedules() {
  const list = $("#schedule-list");
  if (!list) return;
  const schedules = await api("/schedules");
  list.replaceChildren();
  if (schedules.length === 0) {
    list.append(el("li", { class: "empty" }, "No schedules yet. Add the first one with the form."));
    return;
  }
  for (const schedule of schedules) {
    const days = schedule.days.length ? schedule.days.map((d) => DAY_LABEL[d]).join(", ") : "Every day";
    const name = `${schedule.medication} at ${schedule.time_hhmm}`;
    list.append(
      el(
        "li",
        { class: `schedule-card${schedule.enabled ? "" : " is-disabled"}` },
        el("span", { class: "schedule-time" }, schedule.time_hhmm),
        el(
          "div",
          { class: "schedule-info" },
          el("strong", {}, schedule.medication),
          el("span", { class: "muted" }, `Slot ${schedule.slot} · ${days}`),
          el("span", { class: `pill ${schedule.enabled ? "pill-good" : "pill-off"}` }, schedule.enabled ? "Enabled" : "Paused"),
        ),
        el(
          "div",
          { class: "schedule-actions" },
          el("button", { type: "button", class: "btn btn-secondary btn-small", "aria-label": `Edit ${name}`, onclick: () => startEdit(schedule) }, "Edit"),
          el(
            "button",
            { type: "button", class: "btn btn-secondary btn-small", "aria-label": `${schedule.enabled ? "Pause" : "Resume"} ${name}`, onclick: () => toggleSchedule(schedule) },
            schedule.enabled ? "Pause" : "Resume",
          ),
          el("button", { type: "button", class: "btn btn-danger btn-small", "aria-label": `Delete ${name}`, onclick: () => deleteSchedule(schedule) }, "Delete"),
        ),
      ),
    );
  }
}

function clearErrors() {
  const box = $("#form-errors");
  box.hidden = true;
  box.replaceChildren();
  for (const selector of Object.values(fieldFor)) $(selector).removeAttribute("aria-invalid");
}

function showErrors(errors) {
  clearErrors();
  const box = $("#form-errors");
  box.append(el("strong", {}, "Please fix the following:"));
  const list = el("ul");
  for (const message of Object.values(errors)) list.append(el("li", {}, message));
  box.append(list);
  box.hidden = false;
  let focused = false;
  for (const field of Object.keys(errors)) {
    const input = fieldFor[field] && $(fieldFor[field]);
    if (!input) continue;
    input.setAttribute("aria-invalid", "true");
    if (!focused) {
      (input.matches("fieldset") ? input.querySelector("input") : input).focus();
      focused = true;
    }
  }
}

function resetForm() {
  editingId = null;
  form.reset();
  $("#f-enabled").checked = true;
  setText("#form-heading", "Add a schedule");
  setText("#form-submit", "Add schedule");
  $("#form-cancel").hidden = true;
  clearErrors();
}

function startEdit(schedule) {
  resetForm();
  editingId = schedule.id;
  $("#f-medication").value = schedule.medication;
  $("#f-slot").value = schedule.slot;
  $("#f-time").value = schedule.time_hhmm;
  $("#f-enabled").checked = schedule.enabled;
  form.querySelectorAll('input[name="days"]').forEach((box) => {
    box.checked = schedule.days.includes(box.value);
  });
  setText("#form-heading", `Edit: ${schedule.medication} at ${schedule.time_hhmm}`);
  setText("#form-submit", "Save changes");
  $("#form-cancel").hidden = false;
  form.scrollIntoView({ behavior: "smooth", block: "start" });
  $("#f-medication").focus();
}

function initScheduleForm() {
  if (!form) return;
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const body = {
      medication: $("#f-medication").value,
      slot: $("#f-slot").value,
      time_hhmm: $("#f-time").value.slice(0, 5),
      days: [...form.querySelectorAll('input[name="days"]:checked')].map((box) => box.value),
      enabled: $("#f-enabled").checked,
    };
    const submit = $("#form-submit");
    submit.disabled = true;
    try {
      const editing = editingId !== null;
      const result = await api(editing ? `/schedules/${editingId}` : "/schedules", {
        method: editing ? "PUT" : "POST",
        json: body,
      });
      resetForm();
      setText("#form-status", result?.pending ? "Saved; waiting for the system to apply it." : `Schedule ${editing ? "updated" : "added"}.`);
      await Promise.all([loadSchedules(), loadDoses()]);
    } catch (error) {
      showErrors(error.errors);
    } finally {
      submit.disabled = false;
    }
  });
  $("#form-cancel").addEventListener("click", () => {
    resetForm();
    setText("#form-status", "Edit cancelled.");
  });
}

async function toggleSchedule(schedule) {
  try {
    await api(`/schedules/${schedule.id}`, { method: "PUT", json: { enabled: !schedule.enabled } });
    setText("#form-status", `${schedule.medication} at ${schedule.time_hhmm} ${schedule.enabled ? "paused" : "resumed"}.`);
    await Promise.all([loadSchedules(), loadDoses()]);
  } catch (error) {
    setText("#form-status", `Could not update schedule: ${Object.values(error.errors).join("; ")}`);
  }
}

async function deleteSchedule(schedule) {
  if (!window.confirm(`Delete the schedule for ${schedule.medication} at ${schedule.time_hhmm}?`)) return;
  try {
    await api(`/schedules/${schedule.id}`, { method: "DELETE" });
    if (editingId === schedule.id) resetForm();
    setText("#form-status", `Schedule for ${schedule.medication} deleted.`);
    await Promise.all([loadSchedules(), loadDoses()]);
  } catch (error) {
    setText("#form-status", `Could not delete schedule: ${Object.values(error.errors).join("; ")}`);
  }
}

// ------------------------------------------------------------------- doses

async function loadDoses() {
  if (!$("#history-rows")) return;
  const [history, upcoming] = await Promise.all([
    api("/doses?status=taken,missed,failed&limit=100"),
    api("/doses?status=pending,active&order=asc&limit=10"),
  ]);

  const today = new Date((simNow ?? Date.now() / 1000) * 1000).toDateString();
  const counts = { taken: 0, missed: 0, failed: 0 };
  for (const dose of history) {
    if (new Date(dose.due_ts * 1000).toDateString() === today) counts[dose.status] += 1;
  }
  setText("#summary-today", `${counts.taken} taken`);
  setText("#summary-today-sub", `${counts.missed} missed · ${counts.failed} failed`);

  const rows = $("#history-rows");
  rows.replaceChildren();
  if (history.length === 0) rows.append(el("tr", {}, el("td", { colspan: 4 }, "No finished doses yet")));
  for (const dose of history.slice(0, 40)) {
    rows.append(
      el(
        "tr",
        {},
        el("td", {}, formatTime(dose.due_ts)),
        el("td", {}, el("strong", {}, dose.medication), el("span", { class: "muted" }, ` · slot ${dose.slot}`)),
        el("td", {}, el("span", { class: `pill status-${dose.status}` }, dose.status)),
        el("td", {}, reasonText(dose.reason) || "—"),
      ),
    );
  }

  const list = $("#upcoming-list");
  list.replaceChildren();
  if (upcoming.length === 0) list.append(el("li", { class: "empty" }, "Nothing queued"));
  for (const dose of upcoming) {
    const label = dose.status === "active" ? "In progress" : dose.due_ts <= (simNow ?? Date.now() / 1000) ? "Due, queued" : "Scheduled";
    list.append(
      el(
        "li",
        {},
        el("span", { class: "upcoming-time" }, formatTime(dose.due_ts, false)),
        el("strong", {}, dose.medication),
        el("span", { class: `pill ${dose.status === "active" ? "pill-accent" : "pill-off"}` }, label),
      ),
    );
  }
}

async function updateEvents() {
  const log = $("#event-log");
  if (!log) return;
  const events = await api("/events");
  log.replaceChildren();
  for (const event of events) {
    let detail = {};
    try {
      detail = JSON.parse(event.detail);
    } catch {
      /* ignore malformed detail */
    }
    const text = detail.message ?? detail.event ?? detail.result ?? detail.reason ?? detail.action ?? detail.medication ?? "";
    log.append(
      el(
        "li",
        {},
        el("span", { class: "log-time" }, formatTime(event.ts)),
        el("span", { class: "log-kind" }, event.kind.replaceAll("_", " ")),
        el("span", { class: "log-text" }, String(text)),
      ),
    );
  }
}
const refreshEvents = throttle(() => updateEvents().catch(console.error), 1500);
const refreshDoses = throttle(() => loadDoses().catch(console.error), 500);

// -------------------------------------------------------- live connection

let pollTimer = null;
let slowTimer = null;

function setConnection(mode, text) {
  const node = $("#conn-status");
  node.dataset.mode = mode;
  setText(node.querySelector(".conn-text"), text);
}

async function pollState() {
  try {
    renderState(await api("/state"));
    refreshEvents();
  } catch (error) {
    setConnection("offline", "Disconnected, retrying");
    console.error(error);
  }
}

async function refreshLists() {
  try {
    await Promise.all([loadNotifications(), loadSchedules(), loadDoses(), updateEvents()]);
  } catch (error) {
    console.error(error);
  }
}

function startPolling() {
  if (pollTimer) return;
  setConnection("polling", "Polling");
  pollState();
  pollTimer = setInterval(pollState, 1000);
  slowTimer = setInterval(refreshLists, 3000);
}

function stopPolling() {
  clearInterval(pollTimer);
  clearInterval(slowTimer);
  pollTimer = null;
  slowTimer = null;
}

function connectStream() {
  if (!("EventSource" in window)) {
    startPolling();
    return;
  }
  const source = new EventSource("/stream");
  source.addEventListener("open", () => {
    stopPolling();
    setConnection("live", "Live");
    refreshLists();
  });
  // EventSource reconnects by itself; poll meanwhile so the page stays current.
  source.addEventListener("error", () => startPolling());
  source.addEventListener("state", (event) => {
    renderState(JSON.parse(event.data));
    refreshEvents();
  });
  source.addEventListener("notification", (event) => ingestNotifications([JSON.parse(event.data)]));
  source.addEventListener("ack", (event) => {
    const { id } = JSON.parse(event.data);
    const notification = known.get(id);
    if (notification) known.set(id, { ...notification, acked: true });
    removeToast(id);
    renderNotifications();
  });
  source.addEventListener("schedules", () => loadSchedules().catch(console.error));
  source.addEventListener("doses", refreshDoses);
}

// ----------------------------------------------------------------- controls

function initControls() {
  $$(".js-trigger").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        await api("/trigger", { method: "POST" });
        $$(".js-trigger-status").forEach((node) => setText(node, "Dose sent. Watch it move through the steps."));
      } catch (error) {
        $$(".js-trigger-status").forEach((node) => setText(node, "Could not trigger a dose."));
        console.error(error);
      } finally {
        setTimeout(() => {
          button.disabled = false;
        }, 1500);
      }
    });
  });

  $$(".js-sim").forEach((button) => {
    button.addEventListener("click", async () => {
      const { device, action, value } = button.dataset;
      const options = { method: "POST" };
      if (value !== undefined) {
        options.headers = { "Content-Type": "application/json" };
        options.body = JSON.stringify({ value: Number(value) });
      }
      try {
        await api(`/sim/${device}/${action}`, options);
        $$(".js-sim-status").forEach((node) => setText(node, `Sent: ${button.textContent.trim()}`));
      } catch (error) {
        $$(".js-sim-status").forEach((node) => setText(node, "Could not reach the simulator."));
        console.error(error);
      }
    });
  });

  $("#confirm-button")?.addEventListener("click", async () => {
    try {
      await api("/confirm", { method: "POST" });
      patientConfirmedAt = Date.now();
      if (lastSnapshot) renderPatient(lastSnapshot);
    } catch (error) {
      console.error("Could not confirm the dose", error);
    }
  });

  $("#mark-read")?.addEventListener("click", markAllRead);

  const alertsToggle = $("#alerts-toggle");
  if (alertsToggle) {
    alertsToggle.checked = prefs.alerts;
    alertsToggle.addEventListener("change", async () => {
      prefs.alerts = alertsToggle.checked;
      savePrefs();
      if (!prefs.alerts) return;
      beep(1); // also unlocks audio after this user gesture
      if ("Notification" in window && Notification.permission === "default") {
        try {
          await Notification.requestPermission();
        } catch (error) {
          console.error(error);
        }
      }
    });
  }
}

(async function start() {
  initTabs();
  initScheduleForm();
  initControls();
  initNotificationPopup();
  try {
    await loadNotifications();
  } catch (error) {
    console.error(error);
    notificationsReady = true;
  }
  await refreshLists();
  await pollState();
  connectStream();
})();
