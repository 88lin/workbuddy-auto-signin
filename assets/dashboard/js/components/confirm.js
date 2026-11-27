/* 换号确认：使用原生 dialog，保留 Esc 取消与键盘焦点管理 */

export function createConfirm(dialog) {
  const title = dialog.querySelector('[data-role="title"]');
  const text = dialog.querySelector('[data-role="text"]');
  const cancel = dialog.querySelector('[data-action="cancel"]');
  const accept = dialog.querySelector('[data-action="accept"]');
  let resolvePending = null;

  cancel.addEventListener("click", () => dialog.close("cancel"));
  accept.addEventListener("click", () => dialog.close("accept"));
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close("cancel");
  });
  dialog.addEventListener("close", () => {
    if (!resolvePending) return;
    const resolve = resolvePending;
    resolvePending = null;
    resolve(dialog.returnValue === "accept");
  });

  return function ask(name) {
    title.textContent = `切换到 ${name}？`;
    text.textContent =
      "会关闭并重开客户端，未保存的编辑会丢失；其他账号的会话会一并搬到该账号。";
    dialog.returnValue = "";
    return new Promise((resolve) => {
      resolvePending = resolve;
      dialog.showModal();
    });
  };
}
