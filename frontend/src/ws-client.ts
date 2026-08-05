/**
 * Backend WebSocket client for app-wide status events.
 */

class PolymarketWSClient {
  private ws: WebSocket | null = null;
  private url: string;
  private reconnectInterval: number = 5000;
  private listeners: Record<string, Function[]> = {};
  private _connected: boolean = false;
  private reconnectTimer: number | null = null;
  private shouldReconnect: boolean = true;

  constructor(url: string = `${wsBase()}/ws/market`) {
    this.url = url;
  }

  get connected(): boolean {
    return this._connected;
  }

  on(event: string, callback: Function): void {
    if (!this.listeners[event]) {
      this.listeners[event] = [];
    }
    this.listeners[event].push(callback);
  }

  private emit(event: string, data?: any): void {
    if (this.listeners[event]) {
      this.listeners[event].forEach(cb => cb(data));
    }
  }

  connect(): void {
    console.log(`Connecting to ${this.url}...`);

    this.ws = new WebSocket(this.url);

    this.ws.onopen = () => {
      console.log(`Connection established: ${this.url}`);
      this._connected = true;
      this.emit('open');
    };

    this.ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        this.emit('message', data);
        this.handleMessage(data);
      } catch (error) {
        console.error("Failed to parse message:", error);
      }
    };

    this.ws.onclose = () => {
      console.log("Disconnected from backend");
      this._connected = false;
      this.emit('close');
      if (this.shouldReconnect) {
        console.log(`Reconnecting in ${this.reconnectInterval / 1000}s...`);
        this.reconnectTimer = window.setTimeout(() => this.connect(), this.reconnectInterval);
      }
    };

    this.ws.onerror = (error) => {
      console.error("WebSocket error:", error);
    };
  }

  private handleMessage(data: any): void {
    if (data.event_type) {
      this.emit(data.event_type, data);
    } else if (data.error) {
      console.error(`Error: ${data.error}`);
    }
  }

  disconnect(): void {
    this.shouldReconnect = false;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.listeners = {};
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this._connected = false;
  }

  reconnect(): void {
    this.shouldReconnect = true;
    this.disconnect();
    this.connect();
  }
}

function wsBase(): string {
  if (import.meta.env.VITE_WS_BASE) {
    return import.meta.env.VITE_WS_BASE as string;
  }
  return `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}`;
}

(window as any).PolymarketWSClient = PolymarketWSClient;
