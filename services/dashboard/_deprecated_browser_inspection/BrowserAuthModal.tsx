import { useEffect, useState, useRef, useCallback } from "react";

interface BrowserAuthModalProps {
  open: boolean;
  onClose: () => void;
  onAuthComplete: () => void;
}

export default function BrowserAuthModal({ open, onClose, onAuthComplete }: BrowserAuthModalProps) {
  const [connected, setConnected] = useState(false);
  const [currentUrl, setCurrentUrl] = useState("");
  const [authComplete, setAuthComplete] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const blobUrlRef = useRef<string | null>(null);

  const sendWs = useCallback((msg: object) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(msg));
    }
  }, []);

  useEffect(() => {
    if (!open) return;

    let cancelled = false;
    setStarting(true);
    setConnected(false);
    setAuthComplete(false);
    setError(null);
    setCurrentUrl("");

    (async () => {
      try {
        const res = await fetch("/api/inspect/browser-session/start", {
          method: "POST",
          credentials: "same-origin",
        });
        if (cancelled) return;
        if (!res.ok) {
          const body = await res.json().catch(() => ({ detail: res.statusText }));
          setError(body.detail || `Failed to start browser session (${res.status})`);
          setStarting(false);
          return;
        }
        setStarting(false);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : "Failed to start browser session");
        setStarting(false);
        return;
      }

      const proto = location.protocol === "https:" ? "wss:" : "ws:";
      const ws = new WebSocket(`${proto}//${location.host}/api/inspect/browser-session/stream`);
      ws.binaryType = "blob";
      wsRef.current = ws;

      ws.onopen = () => {
        if (!cancelled) setConnected(true);
      };

      ws.onmessage = (ev) => {
        if (cancelled) return;
        if (ev.data instanceof Blob) {
          const url = URL.createObjectURL(ev.data);
          const img = new Image();
          img.onload = () => {
            const canvas = canvasRef.current;
            if (canvas) {
              const ctx = canvas.getContext("2d");
              if (ctx) ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
            }
            if (blobUrlRef.current) URL.revokeObjectURL(blobUrlRef.current);
            blobUrlRef.current = url;
          };
          img.src = url;
        } else {
          try {
            const msg = JSON.parse(ev.data);
            if (msg.type === "status") {
              setCurrentUrl(msg.url || "");
            } else if (msg.type === "auth_saved") {
              setAuthComplete(true);
              onAuthComplete();
            } else if (msg.type === "error") {
              setError(msg.message || "Browser session error");
            } else if (msg.type === "timeout") {
              setError("Session closed due to inactivity");
              setConnected(false);
            }
          } catch {
            // ignore non-JSON text
          }
        }
      };

      ws.onerror = () => {
        if (!cancelled) setError("WebSocket connection error");
      };

      ws.onclose = () => {
        if (!cancelled) setConnected(false);
      };
    })();

    return () => {
      cancelled = true;
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      if (blobUrlRef.current) {
        URL.revokeObjectURL(blobUrlRef.current);
        blobUrlRef.current = null;
      }
    };
  }, [open, onAuthComplete]);

  useEffect(() => {
    if (open && canvasRef.current) {
      canvasRef.current.focus();
    }
  }, [open, connected]);

  const scaleCoords = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    return {
      x: (e.clientX - rect.left) * (1920 / rect.width),
      y: (e.clientY - rect.top) * (1080 / rect.height),
    };
  }, []);

  const handleClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const { x, y } = scaleCoords(e);
    sendWs({ type: "click", x, y, button: "left" });
  }, [scaleCoords, sendWs]);

  const handleDblClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const { x, y } = scaleCoords(e);
    sendWs({ type: "dblclick", x, y });
  }, [scaleCoords, sendWs]);

  const handleKeyDown = useCallback((e: React.KeyboardEvent<HTMLCanvasElement>) => {
    if (e.ctrlKey || e.altKey || e.metaKey) return;
    e.preventDefault();
    if (e.key.length === 1) {
      sendWs({ type: "type", text: e.key });
    } else if (["Enter", "Tab", "Backspace", "Escape", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Delete", "Home", "End"].includes(e.key)) {
      sendWs({ type: "keypress", key: e.key });
    }
  }, [sendWs]);

  const handleWheel = useCallback((e: React.WheelEvent<HTMLCanvasElement>) => {
    const { x, y } = scaleCoords(e);
    sendWs({ type: "scroll", x, y, deltaY: e.deltaY });
  }, [scaleCoords, sendWs]);

  const handleClose = useCallback(() => {
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    fetch("/api/inspect/browser-session/close", {
      method: "POST",
      credentials: "same-origin",
    }).catch(() => {});
    onClose();
  }, [onClose]);

  if (!open) return null;

  let statusText: string;
  let statusClass: string;
  if (starting) {
    statusText = "Starting browser...";
    statusClass = "status-starting";
  } else if (!connected) {
    statusText = "Connecting...";
    statusClass = "status-connecting";
  } else if (authComplete) {
    statusText = "Authenticated";
    statusClass = "status-success";
  } else {
    statusText = "Active";
    statusClass = "status-active";
  }

  return (
    <div className="browser-auth-overlay">
      <div className="browser-auth-modal">
        <div className="browser-auth-header">
          <h3>Browser Authentication</h3>
          <span className={`browser-auth-status ${statusClass}`}>
            {statusText}
          </span>
          <button onClick={handleClose} className="browser-auth-close-btn">Close</button>
        </div>
        {currentUrl && <div className="browser-auth-url">{currentUrl}</div>}
        {error && <div className="browser-auth-error">{error}</div>}
        <div className="browser-auth-canvas-container">
          <canvas
            ref={canvasRef}
            width={1920}
            height={1080}
            tabIndex={0}
            onClick={handleClick}
            onDoubleClick={handleDblClick}
            onKeyDown={handleKeyDown}
            onWheel={handleWheel}
            onContextMenu={(e) => e.preventDefault()}
          />
        </div>
        {authComplete && (
          <div className="browser-auth-success">
            Authentication successful — cookies saved. You can close this window.
          </div>
        )}
      </div>
    </div>
  );
}
