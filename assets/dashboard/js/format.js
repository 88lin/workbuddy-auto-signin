/* 纯格式化函数：不接触 DOM，便于单独验证 */

export function value(v, fallback = "—") {
  return v === null || v === undefined || v === "" ? fallback : String(v);
}

export function shortUid(uid) {
  return uid ? uid.slice(0, 8) + "…" : "";
}

export function shortTime(iso) {
  if (!iso) return "—";
  const parts = String(iso).split(" ");
  if (parts.length < 2) return parts[0];
  return `${parts[0].slice(5)} ${parts[1].slice(0, 5)}`;
}

export function expiry(exp) {
  if (!exp) return "—";
  return `${shortTime(exp.at)} · ${value(exp.amount, "0")}`;
}

export const ACTION_LABEL = {
  sync: "会话同步",
  balance: "余额刷新",
  pause: "暂停监控",
  resume: "恢复监控",
  "open/backups": "备份目录",
  "open/log": "完整日志",
  switch: "账号切换",
};

export const STATUS_LABEL = {
  watching: "监控中",
  paused: "已暂停",
  error: "异常",
  unavailable: "未连接",
  loading: "连接中",
};
