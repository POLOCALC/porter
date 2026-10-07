import logging
import threading
import time

logger = logging.getLogger(__name__)


class PositionSource:
    """
    Chooses the position used to point the gimbal: the UAV's position (DRTK) or the payload GPS (UBLOX).
    mode "auto" picks the best valid one; "uav" / "payload" force one source (no fallback).
    """

    MODES = ("auto", "payload", "uav")

    def __init__(self, payload_meta=None, uav=None, config=None):
        cfg = config or {}
        self.payload_meta     = payload_meta       # UBX.metadata dict (live reference), or None
        self.uav              = uav                # lager Drone object, or None
        self.uav_max_age      = float(cfg.get("uav_max_age_s", 2.0))
        self.uav_ok_health    = set(cfg.get("uav_ok_health", [1]))
        self.uav_hold         = float(cfg.get("uav_hold_s", 5.0))
        self.payload_max_age  = float(cfg.get("payload_max_age_s", 2.0))
        self.payload_min_fix  = int(cfg.get("payload_min_fix", 3))

        self._lock = threading.Lock()
        self._mode = "auto"
        self.set_mode(cfg.get("mode", "auto"))
        self._active = None              # "uav", "payload" or None
        self._reason = "not read yet"
        self._uav_good_since = None      # monotonic time the UAV position became valid (hold in auto)
        self._snapshot = None            # last (lat, lon, alt, source) for get()

    def set_mode(self, mode: str) -> None:
        mode = str(mode).lower()
        if mode not in self.MODES:
            raise ValueError(f"position_source mode must be one of {self.MODES}, got '{mode}'")
        with self._lock:
            if mode != self._mode:
                logger.warning(f"Position source mode: {self._mode} -> {mode}")
            self._mode = mode

    def _uav_position(self):
        """Return ((lat, lon, alt), None) if the UAV position is usable, else (None, reason)."""
        if self.uav is None:
            return None, "no UAV configured"
        age = self.uav.telemetry_state.age()
        if age > self.uav_max_age:
            return None, "no UAV data yet" if age == float("inf") else f"UAV silent {age:.1f} s"
        st = self.uav.telemetry_state.get()
        lat, lon, alt = st.get("rtk_lat_deg"), st.get("rtk_lon_deg"), st.get("rtk_hfsl_m")
        if None in (lat, lon, alt):
            return None, "no RTK position in UAV data"
        health = st.get("rtk_pos_health")
        if health not in self.uav_ok_health:
            return None, f"UAV RTK health {health}"
        return (lat, lon, alt), None

    def _payload_position(self):
        if self.payload_meta is None:
            return None, "no payload GPS configured"
        pos = self.payload_meta.get("position")
        if pos is None:
            return None, "no payload GPS position yet"
        lat, lon, alt, t = pos
        age = time.monotonic() - t
        if age > self.payload_max_age:
            return None, f"payload GPS silent {age:.1f} s"
        fix = self.payload_meta.get("status")
        if fix is None or fix < self.payload_min_fix or not self.payload_meta.get("status_ok", False):
            return None, f"payload GPS fix {fix}"
        return (lat, lon, alt), None

    def get_position(self):
        """Return (lat, lon, alt, source) from one consistent read, or None if no valid source."""
        with self._lock:
            mode = self._mode
        uav, uav_why = self._uav_position()
        payload, payload_why = self._payload_position()
        now = time.monotonic()

        if uav is None:
            self._uav_good_since = None
        elif self._uav_good_since is None:
            self._uav_good_since = now

        if mode == "uav":
            choice, pos, reason = ("uav", uav, "forced") if uav else (None, None, f"forced uav: {uav_why}")
        elif mode == "payload":
            choice, pos, reason = ("payload", payload, "forced") if payload else (None, None, f"forced payload: {payload_why}")
        else:  # auto
            uav_stable = uav is not None and (
                self._active == "uav" or now - self._uav_good_since >= self.uav_hold)
            if uav_stable:
                choice, pos, reason = "uav", uav, "auto"
            elif payload is not None:
                choice, pos, reason = "payload", payload, f"auto, UAV not usable: {uav_why or 'waiting for stable fix'}"
            else:
                choice, pos, reason = None, None, f"no source: UAV {uav_why}, payload {payload_why}"

        with self._lock:
            if choice != self._active:
                logger.warning(f"Position source: {self._active} -> {choice} ({reason})")
            self._active, self._reason = choice, reason
        if pos is None:
            return None
        self._snapshot = (pos[0], pos[1], pos[2], choice)
        return self._snapshot

    def get(self, key, default=None):
        if key == "latitude":                      # lager asks for latitude first: take a new snapshot
            self.get_position()
        snap = self._snapshot if self._active is not None else None
        if snap is None:
            return default
        return {"latitude": snap[0], "longitude": snap[1], "altitude": snap[2], "source": snap[3]}.get(key, default)

    def status(self) -> dict:
        with self._lock:
            return {"mode": self._mode, "source": self._active or "none", "reason": self._reason}
