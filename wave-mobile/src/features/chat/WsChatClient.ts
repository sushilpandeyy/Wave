/**
 * Thin WebSocket client for the Wave chat stream.
 *
 * Owns the socket lifecycle: connect, parse server frames, auto-reconnect with capped
 * exponential backoff, and a clean close. Frame/status handling is delegated to callbacks
 * so React state lives in the hook (`useChat`), not here.
 */
import type { ConnStatus, ServerFrame } from '@/domain/types';

type Handlers = {
  onFrame: (frame: ServerFrame) => void;
  onStatus: (status: ConnStatus) => void;
};

const MAX_BACKOFF_MS = 15_000;

export class WsChatClient {
  private ws: WebSocket | null = null;
  private closedByUser = false;
  private retry = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(
    private readonly url: string,
    private readonly handlers: Handlers,
  ) {}

  connect(): void {
    this.closedByUser = false;
    this.open();
  }

  private open(): void {
    this.handlers.onStatus('connecting');
    const ws = new WebSocket(this.url);
    this.ws = ws;

    ws.onopen = () => {
      this.retry = 0;
      this.handlers.onStatus('open');
    };

    ws.onmessage = (event) => {
      if (typeof event.data !== 'string') return;
      try {
        this.handlers.onFrame(JSON.parse(event.data) as ServerFrame);
      } catch {
        // ignore malformed frames
      }
    };

    // Errors surface as a close; let onclose drive reconnection.
    ws.onerror = () => {};

    ws.onclose = () => {
      this.ws = null;
      this.handlers.onStatus('closed');
      if (!this.closedByUser) this.scheduleReconnect();
    };
  }

  private scheduleReconnect(): void {
    const delay = Math.min(1000 * 2 ** this.retry, MAX_BACKOFF_MS);
    this.retry += 1;
    this.reconnectTimer = setTimeout(() => this.open(), delay);
  }

  /** Send a user message. Returns false if the socket isn't open yet. */
  send(message: string): boolean {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ message }));
      return true;
    }
    return false;
  }

  close(): void {
    this.closedByUser = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
    this.ws = null;
  }
}
