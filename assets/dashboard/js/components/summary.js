export function createSummary(root) {
  const selectors = {
    current: '[data-role="current"]',
    clients: '[data-role="clients"]',
    lastAction: '[data-role="last-action"]',
    lastSync: '[data-role="last-sync"]',
    balancesAt: '[data-role="balances-at"]',
    lastError: '[data-role="last-error"]',
  };
  const nodes = Object.fromEntries(
    Object.entries(selectors).map(([key, selector]) => [
      key,
      root.querySelector(selector),
    ])
  );
  const last = {};

  function set(key, next) {
    const node = nodes[key];
    if (node && last[key] !== next) {
      node.textContent = next;
      last[key] = next;
    }
  }

  return function update(state) {
    const uid = (state && state.current_uid) || "";
    set(
      "current",
      state && state.current_account
        ? `${state.current_account} / ${uid.slice(0, 8)}`
        : uid || "—"
    );
    set(
      "clients",
      state && state.clients && state.clients.length
        ? state.clients.join(", ")
        : "未运行"
    );
    set("lastAction", (state && state.last_action) || "—");
    set("lastSync", (state && state.last_sync) || "—");
    set("balancesAt", (state && state.balances_at) || "—");
    set("lastError", (state && state.last_error) || "无");
    if (nodes.lastError) {
      nodes.lastError.classList.toggle(
        "summary__val--error",
        Boolean(state && state.last_error)
      );
    }
  };
}
