import json
import logging
import os
import socket
import threading

logger = logging.getLogger(__name__)

DEFAULT_SOCKET_PATH = "/tmp/porter_cmd.sock"


class CommandServer(threading.Thread):
    def __init__(self, shutdown_flag, socket_path=DEFAULT_SOCKET_PATH, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.shutdown_flag = shutdown_flag
        self.socket_path = socket_path
        self._handlers = {}          # cmd_name -> callable(params: dict) -> dict
        self._lock = threading.Lock()
        self._sock = None

    def register(self, cmd_name: str, handler):
        """
        Register a handler for a command name, e.g. 'gimbal.goto'.
        handler(params: dict) -> dict   (the dict becomes the response payload)
        Raise an exception inside handler to signal failure; the server
        catches it and returns {"ok": false, "error": "..."} automatically.
        """
        with self._lock:
            if cmd_name in self._handlers:
                logger.warning(f"Overwriting existing handler for '{cmd_name}'")
            self._handlers[cmd_name] = handler
        logger.info(f"Registered command handler: {cmd_name}")

    def unregister(self, cmd_name: str):
        with self._lock:
            self._handlers.pop(cmd_name, None)

    def _dispatch(self, request: dict) -> dict:
        cmd = request.get("cmd")
        params = request.get("params", {})

        with self._lock:
            handler = self._handlers.get(cmd)

        if handler is None:
            return {"ok": False, "error": f"unknown command '{cmd}'"}

        try:
            result = handler(**params) or {}
            return {"ok": True, **result}
        except Exception as e:
            logger.error(f"Handler for '{cmd}' raised: {e}")
            return {"ok": False, "error": str(e)}

    def run(self):
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass

        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.socket_path)
        self._sock.listen(5)
        self._sock.settimeout(1.0)

        logger.info(f"Command server listening on {self.socket_path}")

        while not self.shutdown_flag.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            # handle each connection on its own short-lived thread so a slow
            # or misbehaving handler can't block other commands queueing up
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

        self._sock.close()
        try:
            os.unlink(self.socket_path)
        except FileNotFoundError:
            pass
        logger.info("Command server stopped")

    def _handle_conn(self, conn: socket.socket):
        with conn:
            try:
                conn.settimeout(5.0)
                data = b""
                while not data.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                request = json.loads(data.decode("utf-8"))
                response = self._dispatch(request)
            except Exception as e:
                response = {"ok": False, "error": f"malformed request: {e}"}
            try:
                conn.sendall((json.dumps(response) + "\n").encode("utf-8"))
            except Exception:
                pass