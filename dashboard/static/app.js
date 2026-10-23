const stateElement = document.querySelector("#robot-state");
const batteryElement = document.querySelector("#battery");
const batteryBar = document.querySelector("#battery-bar");
const medicineElement = document.querySelector("#medicine");
const reminderElement = document.querySelector("#reminder");
const confirmButton = document.querySelector("#confirm-button");
const alertBox = document.querySelector("#alert-box");
const caregiverState = document.querySelector("#caregiver-robot-state");
const eventLog = document.querySelector("#event-log");
const scheduleList = document.querySelector("#schedule-list");

async function updateState() {
  const response = await fetch("/state");
  if (!response.ok) throw new Error("Could not load robot state");
  const snapshot = await response.json();
  const state = snapshot.robot_state;
  stateElement.textContent = state;
  caregiverState.textContent = state;
  batteryElement.textContent = `${Math.round(snapshot.battery)}%`;
  batteryBar.style.width = `${Math.max(0, Math.min(100, snapshot.battery))}%`;
  document.querySelectorAll("#state-sequence li").forEach((step) => {
    step.classList.toggle("current", step.dataset.state === state);
  });

  medicineElement.textContent = snapshot.active_dose?.medication ?? "No active dose";
  const waiting = state === "WAITING_CONFIRM";
  reminderElement.hidden = !waiting;
  reminderElement.classList.toggle("flash", waiting);
  confirmButton.disabled = !waiting;

  const latestAlert = snapshot.alerts.at(-1);
  alertBox.hidden = !latestAlert;
  if (latestAlert) {
    document.querySelector("#alert-level").textContent = latestAlert.level.toUpperCase();
    document.querySelector("#alert-message").textContent = latestAlert.message;
  }
}

async function updateEvents() {
  const response = await fetch("/events");
  if (!response.ok) throw new Error("Could not load event log");
  const events = await response.json();
  eventLog.replaceChildren();
  for (const event of events) {
    const item = document.createElement("li");
    const detail = JSON.parse(event.detail);
    item.textContent = `${event.kind} · ${detail.message ?? detail.event ?? detail.result ?? detail.reason ?? detail.action ?? detail.medication ?? ""}`;
    eventLog.append(item);
  }
}

async function updateSchedules() {
  const response = await fetch("/schedules");
  if (!response.ok) throw new Error("Could not load medication schedules");
  const schedules = await response.json();
  scheduleList.replaceChildren();
  if (schedules.length === 0) {
    const empty = document.createElement("li");
    empty.textContent = "No medication schedules configured";
    scheduleList.append(empty);
    return;
  }
  for (const schedule of schedules) {
    const item = document.createElement("li");
    item.textContent = `${schedule.time_hhmm} · ${schedule.medication} · Slot ${schedule.slot}`;
    scheduleList.append(item);
  }
}

async function poll() {
  try {
    await Promise.all([updateState(), updateEvents(), updateSchedules()]);
  } catch (error) {
    console.error(error);
  }
}

document.querySelector("#trigger-button").addEventListener("click", async () => {
  const response = await fetch("/trigger", { method: "POST" });
  if (!response.ok) console.error("Could not trigger a dose");
});
confirmButton.addEventListener("click", async () => {
  const response = await fetch("/confirm", { method: "POST" });
  if (!response.ok) console.error("Could not confirm the dose");
});

poll();
setInterval(poll, 1000);