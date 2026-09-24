"""Single-camera MJPEG server for the PA-800 bench camera.

Serves one live view at http://localhost:9000 and exposes /snapshot.jpg
for frame capture from Cursor via Optic MCP http_save_image.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

import cv2

DEFAULT_CAMERA_INDEX = 1
DEFAULT_PORT = 9000
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
DEFAULT_FPS = 30
STREAM_JPEG_QUALITY = 75
SNAPSHOT_JPEG_QUALITY = 95


class CameraSource:
    def __init__(
        self,
        camera_index: int,
        width: int,
        height: int,
        fps: int,
    ) -> None:
        self.camera_index = camera_index
        self.width = width
        self.height = height
        self.fps = fps
        self._cap: Optional[cv2.VideoCapture] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._lock = threading.Lock()
        self._stream_frame: Optional[bytes] = None
        self._snapshot_frame: Optional[bytes] = None
        self._measured_fps = 0.0
        self._frame_count = 0
        self._fps_window_start = time.time()

    def start(self) -> None:
        if self._running:
            return

        cap = cv2.VideoCapture(self.camera_index, cv2.CAP_MSMF)
        if not cap.isOpened():
            cap.release()
            cap = cv2.VideoCapture(self.camera_index, cv2.CAP_DSHOW)
        if not cap.isOpened():
            raise RuntimeError(f"Could not open camera index {self.camera_index}")

        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
        cap.set(cv2.CAP_PROP_FPS, self.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = cap.get(cv2.CAP_PROP_FPS)
        print(
            f"PA-800 opened: index={self.camera_index} "
            f"{actual_w}x{actual_h} @{actual_fps:.1f}fps (requested {self.fps})"
        )

        self._cap = cap
        self._running = True
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()

        deadline = time.time() + 3.0
        while time.time() < deadline:
            with self._lock:
                if self._stream_frame is not None:
                    return
            time.sleep(0.05)

        self.stop()
        raise RuntimeError("Camera opened but no frames were captured")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    def _capture_loop(self) -> None:
        assert self._cap is not None
        while self._running:
            if not self._cap.grab():
                time.sleep(0.005)
                continue

            ok, frame = self._cap.retrieve()
            if not ok or frame is None:
                continue

            frame = cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)

            stream_ok, stream_buf = cv2.imencode(
                ".jpg",
                frame,
                [cv2.IMWRITE_JPEG_QUALITY, STREAM_JPEG_QUALITY],
            )
            snap_ok, snap_buf = cv2.imencode(
                ".jpg",
                frame,
                [cv2.IMWRITE_JPEG_QUALITY, SNAPSHOT_JPEG_QUALITY],
            )
            if not stream_ok or not snap_ok:
                continue

            now = time.time()
            self._frame_count += 1
            elapsed = now - self._fps_window_start
            if elapsed >= 2.0:
                self._measured_fps = self._frame_count / elapsed
                self._frame_count = 0
                self._fps_window_start = now

            with self._lock:
                self._stream_frame = stream_buf.tobytes()
                self._snapshot_frame = snap_buf.tobytes()

    def get_stream_frame(self) -> Optional[bytes]:
        with self._lock:
            return self._stream_frame

    def get_snapshot_frame(self) -> Optional[bytes]:
        with self._lock:
            return self._snapshot_frame

    @property
    def measured_fps(self) -> float:
        return self._measured_fps


class StreamHandler(BaseHTTPRequestHandler):
    camera: CameraSource

    def log_message(self, format: str, *args) -> None:
        pass

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._serve_viewer()
        elif self.path == "/stream":
            self._serve_mjpeg()
        elif self.path.startswith("/snapshot"):
            self._serve_snapshot()
        elif self.path == "/status":
            self._serve_status()
        else:
            self.send_error(404, "Not Found")

    def _serve_viewer(self) -> None:
        fps = self.camera.measured_fps
        fps_text = f"{fps:.1f} fps" if fps > 0 else "starting..."
        html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>PA-800 Bench Camera</title>
  <style>
    body {{
      margin: 0;
      background: #111;
      color: #eee;
      font-family: sans-serif;
      display: flex;
      flex-direction: column;
      min-height: 100vh;
    }}
    header {{
      padding: 12px 16px;
      border-bottom: 1px solid #333;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    main {{
      flex: 1;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 12px;
    }}
    img {{
      max-width: 100%;
      max-height: calc(100vh - 70px);
      border: 1px solid #333;
      background: #000;
    }}
    .meta {{ color: #9ad17a; font-size: 0.95rem; }}
  </style>
</head>
<body>
  <header>
    <strong>PA-800 Bench Camera</strong>
    <span class="meta">Camera {self.camera.camera_index} · {fps_text}</span>
  </header>
  <main>
    <img src="/stream" alt="PA-800 live stream">
  </main>
</body>
</html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_mjpeg(self) -> None:
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "multipart/x-mixed-replace; boundary=frame",
        )
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()

        try:
            while True:
                frame = self.camera.get_stream_frame()
                if frame is None:
                    time.sleep(0.01)
                    continue
                self.wfile.write(b"--frame\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n\r\n")
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            return

    def _serve_snapshot(self) -> None:
        frame = self.camera.get_snapshot_frame()
        if frame is None:
            self.send_error(503, "No frame available yet")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(frame)

    def _serve_status(self) -> None:
        body = json.dumps(
            {
                "camera_index": self.camera.camera_index,
                "measured_fps": round(self.camera.measured_fps, 2),
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main() -> None:
    parser = argparse.ArgumentParser(description="PA-800 MJPEG stream server")
    parser.add_argument("--camera-index", type=int, default=DEFAULT_CAMERA_INDEX)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH)
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT)
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS)
    args = parser.parse_args()

    camera = CameraSource(
        camera_index=args.camera_index,
        width=args.width,
        height=args.height,
        fps=args.fps,
    )
    camera.start()

    handler = StreamHandler
    handler.camera = camera
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"Live view:  http://127.0.0.1:{args.port}/")
    print(f"Snapshot:   http://127.0.0.1:{args.port}/snapshot.jpg")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        server.shutdown()
        camera.stop()


if __name__ == "__main__":
    main()
