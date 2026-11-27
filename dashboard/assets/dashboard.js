"use strict";

const get = (id) => document.getElementById(id);
const day = get("day");
const portions = get("portions");
const buffer = get("buffer");
const statusText = get("status");
const refresh = get("refresh");
const table = get("table-container");
const rows = get("reservation-rows");
const empty = get("empty");
const api = new URL(document.querySelector('meta[name="dashboard-api"]').content, window.location.href);
const link = new URLSearchParams(window.location.hash.slice(1));
const managementKey = link.get("manage");
const accessKey = managementKey || link.get("view");
const dailyCaption = new Intl.DateTimeFormat("zh-TW", { dateStyle: "full", timeZone: "UTC" });
const taipeiDate = new Intl.DateTimeFormat("en-CA", {
  timeZone: "Asia/Taipei", year: "numeric", month: "2-digit", day: "2-digit",
});
const periods = { lunch: "午餐", dinner: "晚餐" };
const tableSizes = { 2: "雙人桌", 4: "四人桌" };
const manualForm = get("manual-form");
const manualSplits = get("manual-splits");
const managementStatus = get("management-status");
const moveDialog = get("move-dialog");
const moveStatus = get("move-status");
let counts = null;
let currentRequest = null;
let canManage = false;
let writing = false;
let manualAttempt = null;
let moveAttempt = null;

function todayInTaipei() {
  const parts = Object.fromEntries(taipeiDate.formatToParts().map(({ type, value }) => [type, value]));
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function addDays(value, days) {
  const date = new Date(`${value}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

function summarizeTables(tables) {
  const summary = { 2: { guests: 0, tables: 0 }, 4: { guests: 0, tables: 0 } };
  for (const table of tables) {
    summary[table.size].guests += table.people;
    summary[table.size].tables += 1;
  }
  return summary;
}

function showTableBreakdown(id, tables) {
  const list = get(id);
  list.replaceChildren();
  const summary = tables === null ? null : summarizeTables(tables);
  for (const [size, label] of Object.entries(tableSizes)) {
    const row = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = label;
    const amount = summary?.[size];
    row.append(name, amount ? `${amount.guests} 位 · ${amount.tables} 桌次` : "—");
    list.append(row);
  }
}

function showCounts(reservations) {
  counts = reservations === null ? null : {
    lunch: { guests: 0, bookings: 0, tables: [] }, dinner: { guests: 0, bookings: 0, tables: [] },
  };
  for (const reservation of reservations || []) {
    const group = counts[reservation.period];
    group.guests += reservation.people;
    group.bookings += 1;
    group.tables.push(...reservation.tables);
  }
  for (const period of Object.keys(periods)) {
    get(`${period}-guests`).textContent = counts?.[period].guests ?? "—";
    get(`${period}-bookings`).textContent = counts?.[period].bookings ?? "—";
    showTableBreakdown(`${period}-table-breakdown`, counts?.[period].tables ?? null);
  }
  get("total-guests").textContent = counts ? counts.lunch.guests + counts.dinner.guests : "—";
  get("total-bookings").textContent = counts ? counts.lunch.bookings + counts.dinner.bookings : "—";
  showTableBreakdown("total-table-breakdown", counts ? [...counts.lunch.tables, ...counts.dinner.tables] : null);
  showEstimates();
}

function showEstimates() {
  const valid = [portions, buffer].every((input) => input.value !== "" && input.validity.valid);
  get("estimate-error").hidden = valid;
  const storedPortions = Math.round(portions.valueAsNumber * 10);
  const multiplier = 100 + buffer.valueAsNumber;
  const estimate = (period) => Math.ceil(counts[period].guests * storedPortions * multiplier / 1000);
  const lunch = valid && counts ? estimate("lunch") : null;
  const dinner = valid && counts ? estimate("dinner") : null;
  get("lunch-portions").textContent = lunch ?? "—";
  get("dinner-portions").textContent = dinner ?? "—";
  get("total-portions").textContent = lunch === null ? "—" : lunch + dinner;
}

function showReservations(reservations) {
  rows.replaceChildren();
  for (const booking of reservations) {
    const row = rows.insertRow();
    row.insertCell().textContent = `${booking.start}–${booking.end}`;
    const period = document.createElement("span");
    period.className = `meal-tag meal-${booking.period}`;
    period.textContent = periods[booking.period];
    row.insertCell().append(period);
    row.insertCell().textContent = booking.reference;
    row.insertCell().textContent = `${booking.people} 位`;
    const tables = summarizeTables(booking.tables);
    for (const size of Object.keys(tableSizes)) {
      const { guests, tables: number } = tables[size];
      row.insertCell().textContent = number ? `${guests} 位 · ${number} 桌` : "—";
    }
    if (canManage) {
      const cell = row.insertCell();
      const controls = document.createElement("div");
      controls.className = "row-actions";
      controls.append(actionButton("改期", () => openMove(booking)),
        actionButton("取消", () => cancelBooking(booking.reference), "cancel-action"));
      cell.append(controls);
    }
  }
  table.hidden = reservations.length === 0;
  empty.hidden = reservations.length !== 0;
}

async function loadDay() {
  currentRequest?.abort();
  const controller = new AbortController();
  currentRequest = controller;
  rows.replaceChildren();
  table.hidden = true;
  empty.hidden = true;
  showCounts(null);
  statusText.classList.remove("status-error");
  if (!day.value) {
    statusText.textContent = "請選擇營業日期。";
    refresh.disabled = false;
    return;
  }
  const selectedDay = day.value;
  get("date-caption").textContent = dailyCaption.format(new Date(`${selectedDay}T00:00:00Z`));
  if (!accessKey) {
    get("access-note").hidden = false;
    statusText.textContent = "等待專屬檢視連結";
    refresh.disabled = false;
    return;
  }
  statusText.textContent = "載入訂位中…";
  refresh.disabled = true;
  const url = new URL(api);
  url.searchParams.set("day", selectedDay);
  try {
    const response = await fetch(url, {
      cache: "no-store", credentials: "omit", signal: controller.signal,
      headers: { Authorization: `Bearer ${accessKey}` },
    });
    if (response.status === 401) throw new Error("這個專屬連結已失效，請向管理者索取新連結。");
    if (!response.ok) throw new Error("讀取訂位失敗，請重新整理。");
    const result = await response.json();
    if (currentRequest !== controller) return;
    canManage = Boolean(managementKey && result.can_manage);
    get("management-panel").hidden = !canManage;
    get("manage-column").hidden = !canManage;
    showCounts(result.reservations);
    showReservations(result.reservations);
    statusText.textContent = `已載入 ${result.reservations.length} 筆訂位`;
  } catch (error) {
    if (currentRequest !== controller) return;
    statusText.classList.add("status-error");
    statusText.textContent = error instanceof TypeError ? "無法連上訂位伺服器，請重新整理。" : error.message;
  } finally {
    if (currentRequest === controller) refresh.disabled = false;
  }
}

function actionButton(label, onClick, className = "") {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.className = className;
  button.addEventListener("click", onClick);
  return button;
}

function showManagementStatus(target, message) {
  target.textContent = message;
  target.hidden = !message;
}

async function sendManagement(path, body, messageTarget) {
  if (!canManage || writing) return null;
  writing = true;
  showManagementStatus(messageTarget, "處理中…");
  try {
    const response = await fetch(new URL(`manage/${path}`, api), {
      method: "POST", credentials: "omit", cache: "no-store",
      headers: { Authorization: `Bearer ${managementKey}`, "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (response.status === 403) throw new Error("沒有操作權限，請開啟店家管理連結。");
    if (response.status === 422) throw new Error("請確認填入的日期、時間及人數格式。");
    if (!response.ok) throw new Error("訂位操作暫時失敗，請稍後再試。");
    const result = await response.json();
    showManagementStatus(messageTarget, result.message);
    return result;
  } catch (error) {
    showManagementStatus(messageTarget, error instanceof TypeError ? "無法連上訂位伺服器，請重試。" : error.message);
    return null;
  } finally {
    writing = false;
  }
}

function showManualResult(result, attempt) {
  if (!result || manualAttempt !== attempt) return;
  manualSplits.replaceChildren();
  manualSplits.hidden = !result.options;
  for (const groups of result.options || []) {
    manualSplits.append(actionButton(groups.join("＋"), async () => {
      const choice = await sendManagement("manual/choose", { request_id: attempt.id, offer_id: result.offer_id, groups }, managementStatus);
      showManualResult(choice, attempt);
    }));
  }
  if (result.message.startsWith("訂位成功") || result.message.startsWith("這筆訂位已成立")) {
    manualForm.reset();
    manualAttempt = null;
    get("manual-day").value = attempt.day;
    day.value = attempt.day;
    loadDay();
  }
}

manualForm.addEventListener("input", () => {
  manualSplits.hidden = true;
  manualSplits.replaceChildren();
});
manualForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (writing) return;
  const details = {
    day: get("manual-day").value, time: get("manual-time").value,
    people: get("manual-people").valueAsNumber, contact: get("manual-contact").value.trim(),
  };
  const signature = JSON.stringify(details);
  if (!manualAttempt || manualAttempt.signature !== signature) {
    manualAttempt = { signature, id: crypto.randomUUID(), day: details.day };
  }
  const attempt = manualAttempt;
  const result = await sendManagement("manual", { ...details, request_id: attempt.id }, managementStatus);
  showManualResult(result, attempt);
});

async function cancelBooking(reference) {
  if (writing || !window.confirm(`取消訂位 ${reference} 並釋放桌位？`)) return;
  const result = await sendManagement(`reservations/${encodeURIComponent(reference)}/cancel`, {}, managementStatus);
  if (result) loadDay();
}

function openMove(booking) {
  if (writing) return;
  moveAttempt = { reference: booking.reference };
  get("move-title").textContent = `訂位改期 ${booking.reference}`;
  get("move-day").value = day.value;
  get("move-time").value = booking.start;
  showManagementStatus(moveStatus, "");
  moveDialog.showModal();
}

get("move-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (writing || !moveAttempt) return;
  const details = { day: get("move-day").value, time: get("move-time").value };
  const signature = JSON.stringify(details);
  if (moveAttempt.signature !== signature) {
    moveAttempt.signature = signature;
    moveAttempt.id = crypto.randomUUID();
  }
  const result = await sendManagement(`reservations/${encodeURIComponent(moveAttempt.reference)}/reschedule`,
    { ...details, request_id: moveAttempt.id }, moveStatus);
  if (result?.message.startsWith("改期成功")) {
    day.value = details.day;
    moveDialog.close();
    showManagementStatus(managementStatus, result.message);
    loadDay();
  }
});
get("close-move").addEventListener("click", () => moveDialog.close());

function changeDay(offset) {
  day.value = addDays(day.value || todayInTaipei(), offset);
  loadDay();
}

get("previous").addEventListener("click", () => changeDay(-1));
get("next").addEventListener("click", () => changeDay(1));
get("today").addEventListener("click", () => { day.value = todayInTaipei(); loadDay(); });
refresh.addEventListener("click", loadDay);
day.addEventListener("change", loadDay);
portions.addEventListener("input", showEstimates);
buffer.addEventListener("input", showEstimates);
day.value = todayInTaipei();
get("manual-day").value = day.value;
loadDay();
