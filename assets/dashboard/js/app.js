/* 编排层：连接状态、轮询、操作反馈与各视图的单一入口 */

import { getState, post } from "./api.js";
import { createStore } from "./store.js";
import { ACTION_LABEL } from "./format.js";
import { createStatusBadge } from "./components/status-badge.js";
import { createToolbar } from "./components/toolbar.js";
import { createLedger } from "./components/ledger.js";
import { createSummary } from "./components/summary.js";
import { createActivity } from "./components/activity.js";
import { createToaster } from "./components/toast.js";
import { createConfirm } from "./components/confirm.js";

const $ = (selector) => document.querySelector(selector);
const store = createStore(null);
const toast = createToaster($("#toasts"));
const askConfirm = createConfirm($("#confirm"));

const statusBadge = createStatusBadge($("#status"));
const toolbar = createToolbar($("#toolbar"), { onAction: handleAction });
const ledger = createLedger($("#ledger"), { onSwitch: handleSwitch });
const summary = createSummary($("#summary"));
const activity = createActivity($("#log"));

const banner = $("#banner");
const bannerDetail = $("#banner-detail");
const updatedAt = $("#updated-at");

let connection = "loading";
let connectionError = "";
let inflight = false;
let activeController = null;

function render() {
  const state = store.get();
  statusBadge(state, connection);
  toolbar(state, inflight);
  ledger(state);
  summary(state);
  activity(state);

  banner.hidden = connection !== "error";
  if (connection === "error") bannerDetail.textContent = connectionError;

  updatedAt.textContent =
    state && state.updated_at
      ? `更新于 ${String(state.updated_at).slice(11, 19)}`
      : "—";
}

async function refresh() {
  if (inflight) return;
  if (activeController) activeController.abort();
  const controller = new AbortController();
  activeController = controller;
  inflight = true;

  try {
    const state = await getState(controller.signal);
    store.set(state);
    connection = "ok";
    connectionError = "";
  } catch (error) {
    if (error.name === "AbortError") return;
    connection = "error";
    connectionError = error.message || "未知错误";
  } finally {
    if (activeController === controller) activeController = null;
    inflight = false;
    render();
  }
}

async function handleAction(action) {
  try {
    await post(action);
    toast(`${ACTION_LABEL[action] || action}已提交`, "success");
    setTimeout(refresh, 600);
  } catch (error) {
    toast(`${ACTION_LABEL[action] || action}失败：${error.message}`, "error");
  }
}

async function handleSwitch(name) {
  const accepted = await askConfirm(name);
  if (!accepted) return;
  try {
    await post("switch", { name });
    toast(`已提交切换到 ${name}`, "success");
    setTimeout(refresh, 900);
  } catch (error) {
    toast(`切换失败：${error.message}`, "error");
  }
}

$("#retry").addEventListener("click", refresh);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") refresh();
});

setInterval(() => {
  if (document.visibilityState === "visible") refresh();
}, 3000);

refresh();
