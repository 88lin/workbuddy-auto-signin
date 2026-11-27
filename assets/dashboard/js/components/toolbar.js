import { ACTION_LABEL } from "../format.js";

export function createToolbar(root, { onAction }) {
  const buttons = Array.from(root.querySelectorAll("[data-action]"));
  buttons.forEach((button) => {
    button.addEventListener("click", () => onAction(button.dataset.action, button));
  });

  return function update(state, busy) {
    const paused = Boolean(state && state.paused);
    const toggle = root.querySelector(
      '[data-action="pause"], [data-action="resume"]'
    );
    if (toggle) {
      const next = paused ? "resume" : "pause";
      if (toggle.dataset.action !== next) {
        toggle.dataset.action = next;
        toggle.textContent = ACTION_LABEL[next];
      }
    }
    buttons.forEach((button) => {
      button.disabled = busy;
    });
  };
}
