export function createToaster(stack) {
  return function show(message, kind = "info") {
    const node = document.createElement("div");
    node.className = "toast";
    node.dataset.kind = kind;
    node.textContent = message;
    stack.appendChild(node);
    setTimeout(() => node.remove(), 3400);
  };
}
