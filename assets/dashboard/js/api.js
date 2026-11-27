/* HTTP 封装：统一错误处理，支持取消以避免轮询请求堆积 */

async function readJson(response) {
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  try {
    return await response.json();
  } catch {
    throw new Error("返回内容不是有效 JSON");
  }
}

export async function getState(signal) {
  const payload = await readJson(
    await fetch("/api/state", { cache: "no-store", signal })
  );
  if (payload.error) throw new Error(payload.error);
  return payload;
}

export async function post(action, data = {}) {
  const payload = await readJson(
    await fetch(`/api/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(data).toString(),
    })
  );
  if (payload.ok === false) throw new Error(payload.error || "操作未完成");
  return payload;
}
