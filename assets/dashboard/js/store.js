/* 极简状态容器：单一数据源，订阅者只在状态变化时被通知 */

export function createStore(initial = null) {
  let state = initial;
  const listeners = new Set();

  return {
    get: () => state,
    set(next) {
      const previous = state;
      state = next;
      listeners.forEach((listener) => listener(state, previous));
    },
    subscribe(listener) {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
  };
}
