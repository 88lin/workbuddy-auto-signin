import { STATUS_LABEL } from "../format.js";

export function createStatusBadge(root) {
  const label = root.querySelector('[data-role="label"]');
  let lastValue = "";
  let lastKind = "";

  return function update(state, connection) {
    const raw = connection === "loading" ? "loading" : state ? state.status : "unavailable";
    const value = STATUS_LABEL[raw] || raw || "未知";
    if (value !== lastValue) {
      label.textContent = value;
      lastValue = value;
    }

    const kind =
      connection === "error" || (state && state.status === "error")
        ? "error"
        : state && (state.paused || state.status === "paused")
          ? "paused"
          : state
            ? "watching"
            : "unavailable";
    if (kind !== lastKind) {
      root.dataset.state = kind;
      lastKind = kind;
    }
  };
}
