import { vi } from "vitest";

// Lifecycle is driven explicitly by each test so stale callbacks and delayed
// reconnects can be delivered in the order the scenario needs.
export class Socket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;
  static instances: Socket[] = [];
  private static initialReadyState = Socket.CONNECTING;

  static reset(readyState = Socket.CONNECTING) {
    Socket.instances = [];
    Socket.initialReadyState = readyState;
  }

  readyState = Socket.initialReadyState;
  onopen: (() => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  send = vi.fn<(data: string) => void>();

  constructor() { Socket.instances.push(this); }
  close() { this.readyState = Socket.CLOSED; }
}
