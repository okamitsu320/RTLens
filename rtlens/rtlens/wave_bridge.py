from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple


class WaveBridgeError(RuntimeError):
    pass


class WaveBridge:
    kind: str = "none"

    def open(self, wave_path: str) -> bool:
        return False

    def add_signal(self, signal_name: str) -> bool:
        return False

    def jump_time(self, t: int) -> bool:
        return False

    def reload(self) -> bool:
        return False

    def poll_events(self) -> List[Tuple[str, str]]:
        return []


class NullWaveBridge(WaveBridge):
    pass


@dataclass
class GtkWaveBridge(WaveBridge):
    kind: str = "gtkwave"
    gtkwave_cmd: str = "gtkwave"
    wave_path: str = ""
    signals: List[str] = field(default_factory=list)
    marker_time: Optional[int] = None
    _proc: Optional[subprocess.Popen] = None

    def _ensure_cmd(self) -> str:
        p = shutil.which(self.gtkwave_cmd)
        if not p:
            raise WaveBridgeError(f"gtkwave not found: {self.gtkwave_cmd}")
        return p

    def _build_script(self) -> str:
        # Arguments are passed after "-- --":
        #   __MARKER__=<time>
        #   signal names...
        script = r"""
set argv [gtkwave::getArgv]
set sigs [list]
set marker -1
foreach a $argv {
    if {[string first "__MARKER__=" $a] == 0} {
        set marker [string range $a 11 end]
    } else {
        lappend sigs $a
    }
}
if {[llength $sigs] > 0} {
    gtkwave::addSignalsFromList $sigs
    gtkwave::/Edit/Set_Trace_Max_Hier 0
}
if {$marker >= 0} {
    gtkwave::setMarker $marker
}
gtkwave::/Time/Zoom/Zoom_Full
"""
        return script

    def _launch(self) -> bool:
        if not self.wave_path or not os.path.isfile(self.wave_path):
            return False
        exe = self._ensure_cmd()

        with tempfile.NamedTemporaryFile(prefix="rtlens_gtkw_", suffix=".tcl", delete=False) as tf:
            tf.write(self._build_script().encode("utf-8"))
            script_path = tf.name

        args = [exe, "-f", self.wave_path, "-S", script_path, "--", "--"]
        if self.marker_time is not None:
            args.append(f"__MARKER__={self.marker_time}")
        args.extend(self.signals)

        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True

    def open(self, wave_path: str) -> bool:
        if not wave_path:
            return False
        abs_path = os.path.abspath(wave_path)
        if abs_path != self.wave_path:
            self.signals = []
            self.marker_time = None
        self.wave_path = abs_path
        return self._launch()

    def add_signal(self, signal_name: str) -> bool:
        if not signal_name:
            return False
        if signal_name not in self.signals:
            self.signals.append(signal_name)
        if not self.wave_path:
            return False
        return self._launch()

    def jump_time(self, t: int) -> bool:
        self.marker_time = int(t)
        if not self.wave_path:
            return False
        return self._launch()

    def reload(self) -> bool:
        if not self.wave_path:
            return False
        return self._launch()


@dataclass
class SurferBridge(WaveBridge):
    kind: str = "surfer"
    surfer_cmd: str = "surfer"
    wave_path: str = ""
    signals: List[str] = field(default_factory=list)
    marker_time: Optional[int] = None
    host: str = "127.0.0.1"
    _port: Optional[int] = None
    _proc: Optional[subprocess.Popen] = None
    _listen_sock: Optional[socket.socket] = None
    _sock: Optional[socket.socket] = None
    _io_lock: threading.Lock = field(default_factory=threading.Lock)
    _event_lock: threading.Lock = field(default_factory=threading.Lock)
    _pending_events: List[Tuple[str, str]] = field(default_factory=list)
    _rx_buf: bytearray = field(default_factory=bytearray)

    def _ensure_cmd(self) -> str:
        p = shutil.which(self.surfer_cmd)
        if not p:
            raise WaveBridgeError(f"surfer not found: {self.surfer_cmd}")
        return p

    def _free_port(self) -> int:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind((self.host, 0))
            return int(s.getsockname()[1])
        finally:
            s.close()

    def _close_socket(self) -> None:
        _log = logging.getLogger(__name__)
        if self._sock:
            try:
                self._sock.close()
            except Exception as e:
                _log.debug("cleanup error closing _sock (ignored): %s", e)
            self._sock = None
        if self._listen_sock:
            try:
                self._listen_sock.close()
            except Exception as e:
                _log.debug("cleanup error closing _listen_sock (ignored): %s", e)
            self._listen_sock = None

    def _stop_proc(self) -> None:
        if self._proc and self._proc.poll() is None:
            try:
                self._proc.terminate()
            except Exception as e:
                logging.getLogger(__name__).debug("cleanup error terminating proc (ignored): %s", e)
        self._proc = None

    def _teardown(self) -> None:
        self._close_socket()
        self._stop_proc()
        self._rx_buf.clear()

    def _recv_frame(self, timeout_sec: float = 0.5) -> Optional[dict]:
        if not self._sock:
            return None
        self._sock.settimeout(timeout_sec)
        while True:
            z = self._rx_buf.find(0)
            if z >= 0:
                frame = bytes(self._rx_buf[:z])
                del self._rx_buf[: z + 1]
                if not frame:
                    return None
                try:
                    return json.loads(frame.decode("utf-8", errors="ignore"))
                except json.JSONDecodeError:
                    return None
            try:
                chunk = self._sock.recv(4096)
            except (socket.timeout, BlockingIOError):
                return None
            if not chunk:
                raise WaveBridgeError("surfer WCP socket closed")
            self._rx_buf.extend(chunk)

    def _record_event(self, msg: Any) -> None:
        if not isinstance(msg, dict):
            return
        if msg.get("type") != "event":
            return
        ev = str(msg.get("event", "")).strip()
        payload = ""
        if ev == "waveforms_loaded":
            payload = str(msg.get("source", "")).strip()
        elif ev in {"goto_declaration", "add_drivers", "add_loads"}:
            payload = str(msg.get("variable", "")).strip()
        if not ev:
            return
        with self._event_lock:
            self._pending_events.append((ev, payload))

    def _send_message(self, msg: dict) -> None:
        if not self._sock:
            raise WaveBridgeError("surfer WCP socket is not connected")
        payload = json.dumps(msg, separators=(",", ":")).encode("utf-8") + b"\0"
        self._sock.sendall(payload)

    def _send_command(self, command: dict) -> None:
        with self._io_lock:
            self._send_message({"type": "command", **command})
            # Best-effort wait for ack/error while draining events.
            deadline = time.time() + 0.8
            while time.time() < deadline:
                resp = self._recv_frame(timeout_sec=0.2)
                if not resp:
                    continue
                self._record_event(resp)
                if resp.get("type") == "error":
                    msg = resp.get("message", "unknown WCP error")
                    raise WaveBridgeError(f"surfer WCP error: {msg}")
                if resp.get("type") == "response":
                    return

    def _start_session(self) -> None:
        self._teardown()
        self._port = self._free_port()

        lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lsock.bind((self.host, self._port))
        lsock.listen(1)
        lsock.settimeout(8.0)
        self._listen_sock = lsock

        exe = self._ensure_cmd()
        args = [exe]
        if self.wave_path:
            args.append(self.wave_path)
        args.extend(["--wcp-initiate", str(self._port)])

        self._proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        try:
            conn, _addr = lsock.accept()
            conn.settimeout(0.5)
            self._sock = conn
        except socket.timeout as exc:
            raise WaveBridgeError("surfer WCP connect timeout") from exc

        with self._io_lock:
            self._send_message(
                {
                    "type": "greeting",
                    "version": "0",
                    "commands": ["waveforms_loaded", "goto_declaration", "add_drivers", "add_loads"],
                }
            )
            deadline = time.time() + 2.0
            got_greeting = False
            while time.time() < deadline:
                msg = self._recv_frame(timeout_sec=0.25)
                if not msg:
                    continue
                self._record_event(msg)
                if msg.get("type") == "greeting":
                    got_greeting = True
                    break
                if msg.get("type") == "error":
                    raise WaveBridgeError(msg.get("message", "surfer WCP greeting failed"))
            if not got_greeting:
                raise WaveBridgeError("surfer WCP greeting timeout")

    def _ensure_session(self) -> None:
        if self._proc and self._proc.poll() is not None:
            self._teardown()
        if self._sock:
            return
        self._start_session()

    def _resync(self) -> bool:
        self._ensure_session()
        if self.wave_path:
            self._send_command({"command": "load", "source": self.wave_path})
        if self.signals:
            self._send_command({"command": "add_items", "items": self.signals, "recursive": False})
        if self.marker_time is not None:
            self._send_command({"command": "set_cursor", "timestamp": int(self.marker_time)})
        return True

    def open(self, wave_path: str) -> bool:
        if not wave_path:
            return False
        abs_path = os.path.abspath(wave_path)
        # Keep the current Surfer window / displayed list when reopening the same wave.
        if abs_path == self.wave_path and self._sock and self._proc and self._proc.poll() is None:
            return True
        if abs_path != self.wave_path:
            self.wave_path = abs_path
            self.signals = []
            self.marker_time = None
        if not os.path.isfile(self.wave_path):
            return False
        return self._resync()

    def add_signal(self, signal_name: str) -> bool:
        if not signal_name:
            return False
        if signal_name not in self.signals:
            self.signals.append(signal_name)
        if not self.wave_path:
            return False
        self._ensure_session()
        self._send_command({"command": "add_items", "items": [signal_name], "recursive": False})
        return True

    def jump_time(self, t: int) -> bool:
        self.marker_time = int(t)
        if not self.wave_path:
            return False
        self._ensure_session()
        self._send_command({"command": "set_cursor", "timestamp": int(t)})
        return True

    def reload(self) -> bool:
        if not self.wave_path:
            return False
        self._ensure_session()
        self._send_command({"command": "reload"})
        return True

    def poll_events(self) -> List[Tuple[str, str]]:
        if not self._sock:
            return []
        with self._io_lock:
            while True:
                msg = self._recv_frame(timeout_sec=0.0)
                if not msg:
                    break
                self._record_event(msg)
        with self._event_lock:
            if not self._pending_events:
                return []
            out = list(self._pending_events)
            self._pending_events.clear()
            return out


def create_wave_bridge(
    kind: str = "auto",
    gtkwave_cmd: str = "gtkwave",
    surfer_cmd: str = "surfer",
) -> WaveBridge:
    k = (kind or "auto").strip().lower()
    if k in {"none", "off", "disabled"}:
        return NullWaveBridge()
    if k == "surfer":
        if shutil.which(surfer_cmd):
            return SurferBridge(surfer_cmd=surfer_cmd)
        raise WaveBridgeError(f"surfer not found: {surfer_cmd}")
    if k == "gtkwave":
        if shutil.which(gtkwave_cmd):
            return GtkWaveBridge(gtkwave_cmd=gtkwave_cmd)
        raise WaveBridgeError(f"gtkwave not found: {gtkwave_cmd}")
    if k == "auto":
        if shutil.which(surfer_cmd):
            return SurferBridge(surfer_cmd=surfer_cmd)
        if shutil.which(gtkwave_cmd):
            return GtkWaveBridge(gtkwave_cmd=gtkwave_cmd)
        return NullWaveBridge()
    raise WaveBridgeError(f"unknown wave viewer kind: {kind}")
