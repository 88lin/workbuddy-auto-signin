export function createActivity(root) {
  let stickToBottom = true;
  let lastText = "";

  root.addEventListener("scroll", () => {
    stickToBottom =
      root.scrollTop + root.clientHeight >= root.scrollHeight - 28;
  });

  return function update(state) {
    const lines = state && Array.isArray(state.log) ? state.log : [];
    const next = lines.join("\n");
    if (next === lastText) return;
    lastText = next;
    root.textContent = next;
    if (stickToBottom) root.scrollTop = root.scrollHeight;
  };
}
