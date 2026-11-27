/* 账号清单：按账号名复用行节点，只写入变化的字段，避免轮询时整表重绘 */

import { value, shortUid, expiry } from "../format.js";

const ROW_TEMPLATE = `
  <div class="account__main">
    <span class="account__name" data-role="name"></span>
    <span class="badge" data-role="badge" hidden></span>
    <span class="account__uid mono" data-role="uid"></span>
  </div>
  <div class="account__balance num" data-role="balance"></div>
  <div class="account__meta">
    <div class="account__expiry mono" data-role="expiry"></div>
    <div class="account__sessions" data-role="sessions"></div>
  </div>
  <div class="account__action">
    <button class="btn btn--secondary btn--small" type="button" data-role="switch"></button>
  </div>
`;

export function createLedger(root, { onSwitch }) {
  const list = root.querySelector(".ledger__list");
  const rows = new Map();

  function setText(node, next) {
    if (node && node.textContent !== next) node.textContent = next;
  }

  function createRow(account) {
    const row = document.createElement("li");
    row.className = "account";
    row.innerHTML = ROW_TEMPLATE;
    row
      .querySelector('[data-role="switch"]')
      .addEventListener("click", () => onSwitch(account.name));
    return row;
  }

  function fillRow(row, account) {
    row.classList.toggle("is-current", Boolean(account.current));
    setText(row.querySelector('[data-role="name"]'), value(account.name, "未命名"));

    const badge = row.querySelector('[data-role="badge"]');
    const badgeText = account.current ? "当前" : account.active ? "active" : "";
    badge.hidden = !badgeText;
    setText(badge, badgeText);
    const badgeClass =
      "badge" + (!account.current && account.active ? " badge--warn" : "");
    if (badge.className !== badgeClass) badge.className = badgeClass;

    setText(row.querySelector('[data-role="uid"]'), shortUid(account.uid));

    const balance = row.querySelector('[data-role="balance"]');
    balance.classList.toggle("account__balance--error", Boolean(account.balance_error));
    setText(balance, account.balance_error || value(account.balance));

    setText(row.querySelector('[data-role="expiry"]'), expiry(account.next_expiry));
    setText(row.querySelector('[data-role="sessions"]'), `${account.sessions || 0} 会话`);

    const button = row.querySelector('[data-role="switch"]');
    setText(button, account.current ? "已是当前" : "切换");
    button.disabled = Boolean(account.current);
  }

  return function update(state) {
    if (state === null) return;
    list.querySelectorAll(".skeleton-row").forEach((node) => node.remove());

    const accounts = Array.isArray(state.accounts) ? state.accounts : [];
    if (!accounts.length) {
      rows.forEach((row) => row.remove());
      rows.clear();
      list.hidden = true;
      if (!root.querySelector(".empty")) {
        const empty = document.createElement("div");
        empty.className = "empty";
        empty.innerHTML =
          '<div class="empty__title">还没有账号</div>' +
          "<div>在客户端登录后执行 python signin.py bind &lt;名字&gt; 添加账号</div>";
        root.appendChild(empty);
      }
      return;
    }

    list.hidden = false;
    const empty = root.querySelector(".empty");
    if (empty) empty.remove();

    accounts.forEach((account, index) => {
      let row = rows.get(account.name);
      if (!row) {
        row = createRow(account);
        rows.set(account.name, row);
      }
      fillRow(row, account);
      if (list.children[index] !== row) {
        list.insertBefore(row, list.children[index] || null);
      }
    });

    const names = new Set(accounts.map((account) => account.name));
    rows.forEach((row, name) => {
      if (!names.has(name)) {
        row.remove();
        rows.delete(name);
      }
    });
  };
}
