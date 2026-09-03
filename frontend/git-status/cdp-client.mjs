const DEFAULT_REQUEST_TIMEOUT_MS = 15_000;

/** A small CDP request multiplexer with isolated per-request deadlines. */
export class Cdp {
  constructor(socket, { requestTimeoutMs = DEFAULT_REQUEST_TIMEOUT_MS } = {}) {
    this.socket = socket;
    this.requestTimeoutMs = requestTimeoutMs;
    this.nextId = 1;
    this.pending = new Map();
    this.closed = false;
    socket.addEventListener("message", (event) => this.#onMessage(event));
    socket.addEventListener("close", () => this.#failAll(new Error("CDP browser disconnected")));
    socket.addEventListener("error", () => this.#failAll(new Error("CDP WebSocket error")));
  }

  command(method, params = {}, sessionId = undefined, { timeoutMs = this.requestTimeoutMs } = {}) {
    if (this.closed) return Promise.reject(new Error("CDP browser disconnected"));
    const id = this.nextId++;
    return new Promise((resolveResponse, rejectResponse) => {
      const timer = setTimeout(() => {
        if (!this.pending.delete(id)) return;
        rejectResponse(new Error(`CDP request ${method} (${id}) timed out after ${timeoutMs}ms`));
      }, timeoutMs);
      // Register before send: an immediate Target.createTarget response must
      // never be discarded as an unknown ID.
      this.pending.set(id, { resolveResponse, rejectResponse, timer });
      try {
        this.socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
      } catch (error) {
        this.#reject(id, error instanceof Error ? error : new Error(String(error)));
      }
    });
  }

  close() { this.#failAll(new Error("CDP client closed")); }

  #onMessage(event) {
    let message;
    try { message = JSON.parse(String(event.data)); }
    catch { this.#failAll(new Error("CDP received malformed JSON response")); return; }
    if (!message || typeof message !== "object") { this.#failAll(new Error("CDP received malformed response")); return; }
    if (!Object.hasOwn(message, "id")) return;
    if (!Number.isInteger(message.id)) { this.#failAll(new Error("CDP received malformed response ID")); return; }
    const request = this.pending.get(message.id);
    // A late response must never settle a newer request after an old timeout.
    if (!request) return;
    this.pending.delete(message.id);
    clearTimeout(request.timer);
    if (message.error) request.rejectResponse(new Error(typeof message.error.message === "string" ? message.error.message : "CDP request failed"));
    else if (Object.hasOwn(message, "result")) request.resolveResponse(message.result);
    else request.rejectResponse(new Error("CDP received response without result"));
  }

  #reject(id, error) {
    const request = this.pending.get(id);
    if (!request) return;
    this.pending.delete(id);
    clearTimeout(request.timer);
    request.rejectResponse(error);
  }

  #failAll(error) {
    if (this.closed) return;
    this.closed = true;
    for (const [id, request] of this.pending) {
      this.pending.delete(id);
      clearTimeout(request.timer);
      request.rejectResponse(error);
    }
  }
}
