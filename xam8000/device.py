"""Dräger X-am 8000 device interface."""

import json
import re
import struct
import time
from dataclasses import dataclass, field

import serial

from .config import load_config, load_credentials
from .protocol import (
    RESP_ERR, RESP_OK, build_frame, compute_key, frame_connect,
    frame_disconnect, frame_info, frame_keepalive, frame_key, frame_partno,
    frame_seed, frame_status, pb_decode, pb_empty, pb_field, pb_string,
    pb_uint, read_response,
)

# Service IDs
SVC_CALIBRATION = 0x5006
SVC_BT = 0x500F
SVC_STATUS = 0x5010
SVC_MGDAPI = 0x5011
SVC_DATETIME = 0x6001

# Default pump flow rate (ml/min)
DEFAULT_PUMP_FLOW = 350


@dataclass
class DeviceInfo:
    serial_no: str = ""
    part_no: str = ""
    firmware_version: str = ""
    protocol_version: str = ""
    protocol_id: int = 0
    device_address: int = 0
    flags: int = 0
    raw_info: bytes = field(default_factory=bytes, repr=False)
    raw_keepalive: bytes = field(default_factory=bytes, repr=False)

    def __str__(self):
        return (f"Dräger X-am 8000 [S/N: {self.serial_no}, "
                f"Part: {self.part_no}, FW: {self.firmware_version}]")


@dataclass
class DeviceStatus:
    raw: bytes = field(default_factory=bytes)

    @property
    def is_active(self) -> bool:
        return any(b != 0 for b in self.raw)


@dataclass
class GasReading:
    channel: int
    gas_name: str
    value: float
    unit: int = 0
    state: int = 0

    @property
    def is_valid(self) -> bool:
        return self.state > 0 and self.value == self.value  # NaN != NaN

    @property
    def unit_label(self) -> str:
        return {0: "?", 1: "% vol", 2: "ppm", 3: "% LEL"}.get(self.unit, f"unit{self.unit}")

    def __str__(self):
        if not self.is_valid:
            return f"{self.gas_name}: -- ({self.unit_label})"
        return f"{self.gas_name}: {self.value:.1f} {self.unit_label}"


class DragerXam8000:
    """Interface to a Dräger X-am 8000 gas detector via DIRA IV USB-IR adapter.

    Usage:
        with DragerXam8000("COM4") as device:
            for reading in device.get_gas_readings():
                print(reading)
    """

    def __init__(self, port: str | None = None, **kwargs):
        cfg = load_config()
        cfg.update({k: v for k, v in kwargs.items() if v is not None})
        creds = load_credentials()

        self.port = port or cfg["port"]
        self.baudrate = cfg["baudrate"]
        self.timeout = cfg["timeout"]
        self.mode = cfg["security_mode"]
        self.password = creds.get(self.mode, "")
        self._ser = None
        self._seq = 0x00523CF0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *_):
        self.disconnect()

    def _seq_next(self) -> int:
        s = self._seq
        self._seq = (s + 1) & 0xFFFFFFFF
        return s

    def _txrx(self, frame: bytes, timeout: float = None) -> tuple[int, int, bytes]:
        timeout = timeout or self.timeout
        self._ser.write(frame)
        time.sleep(0.05)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                seq, cmd, payload = read_response(self._ser, deadline - time.monotonic())
                if cmd & RESP_OK or cmd == RESP_ERR:
                    return seq, cmd, payload
            except TimeoutError:
                break
        raise TimeoutError("No response")

    def _wake(self):
        if not self._ser:
            return
        try:
            self._ser.send_break(duration=0.3)
        except Exception:
            pass
        time.sleep(0.1)
        self._ser.dtr = False
        time.sleep(0.3)
        self._ser.dtr = True
        time.sleep(0.5)
        self._ser.reset_input_buffer()

    def connect(self):
        """Open serial port and perform 4-step handshake."""
        if self.port == "auto":
            self.port = find_dira_port()
            if not self.port:
                raise RuntimeError("No DIRA IV adapter found")

        self._ser = serial.Serial(
            self.port, self.baudrate,
            bytesize=8, parity="N", stopbits=1,
            timeout=self.timeout, write_timeout=self.timeout,
        )
        self._ser.reset_input_buffer()
        self._ser.reset_output_buffer()
        self._wake()

        # Connect
        try:
            _, cmd, p = self._txrx(frame_connect(self._seq_next()))
        except TimeoutError:
            self._wake()
            _, cmd, p = self._txrx(frame_connect(self._seq_next()))
        if cmd == RESP_ERR:
            raise RuntimeError(f"Connect rejected: 0x{p[-1]:02X}" if p else "Connect rejected")

        # Seed + Key
        time.sleep(0.1)
        _, _, p = self._txrx(frame_seed(self._seq_next(), self.mode))
        if len(p) < 4:
            raise RuntimeError("Seed too short")
        seed = struct.unpack_from("<I", p, 0)[0]

        time.sleep(0.1)
        _, cmd, p = self._txrx(frame_key(self._seq_next(), compute_key(seed, self.password)))
        if cmd == RESP_ERR:
            raise RuntimeError(f"Auth failed: 0x{(p[-1] if p else 0):02X}")

        # Keepalive
        time.sleep(0.1)
        self._txrx(frame_keepalive(self._seq_next()))

    def disconnect(self):
        if self._ser and self._ser.is_open:
            try:
                self._ser.write(frame_disconnect(self._seq_next()))
            except Exception:
                pass
            self._ser.close()
        self._ser = None

    def send_keepalive(self) -> bytes:
        _, _, p = self._txrx(frame_keepalive(self._seq_next()))
        return p

    def _service(self, svc: int, proto: bytes, timeout: float = 5.0) -> bytes:
        _, cmd, p = self._txrx(
            build_frame(self._seq_next(), struct.pack("<H", svc), proto), timeout)
        if cmd == RESP_ERR:
            err = p[2] if len(p) >= 3 else (p[-1] if p else 0)
            raise RuntimeError(f"Service 0x{svc:04X} error: 0x{err:02X}")
        return bytes(p)

    def set_pump(self, flow_ml_per_min: int = DEFAULT_PUMP_FLOW):
        """Set pump flow rate in ml/min. Use 0 to stop the pump.

        The device stays in ON state — no reboot or recalibration needed.
        Normal operating flow is ~350 ml/min.
        """
        proto = pb_field(9, pb_uint(1, flow_ml_per_min))
        data = self._service(SVC_CALIBRATION, proto)
        for fn, wt, fv in pb_decode(data):
            if fn == 9 and wt == 2:
                fields = {n: v for n, w, v in pb_decode(fv) if w == 0}
                if fields.get(2, 0):  # isRequestError
                    raise RuntimeError(f"ExecPump({flow_ml_per_min}) rejected")

    def get_flow(self) -> dict:
        """Get current pump flow status from device database.

        Returns dict with 'value' (ml/min), 'status', 'isActivated', etc.
        """
        return self.get_device_json("/device/flowcontrol").get("flowcontrol", {})

    def get_gas_readings(self) -> list[GasReading]:
        """Read real-time gas sensor values from all channels.

        Uses BT Measurement as primary source, then fills in channels
        marked inactive (state=0) with raw data from Status.Channels.
        Some sensors (e.g. CO, NH3) report state=0 via BT Measurement
        even when hardware is present and reading valid values.
        """
        # Primary: BT Measurement (has gas names and units)
        data = self._service(SVC_BT, pb_empty(2))
        readings = []
        for fn, wt, fv in pb_decode(data):
            if fn == 2 and wt == 2:
                for mn, mw, mv in pb_decode(fv):
                    if mn == 1 and mw == 2:
                        readings.append(_parse_measurement(mv))

        # Supplement: fill inactive channels from Status.Channels (raw sensor data)
        inactive = [r for r in readings if not r.is_valid and r.gas_name]
        if inactive:
            raw = self._get_raw_channels()
            for r in inactive:
                if r.channel in raw:
                    ppm = raw[r.channel]
                    if ppm == ppm:  # not NaN
                        r.value = _raw_to_display(ppm, r.unit)
                        r.state = 1

        return readings

    def _get_raw_channels(self) -> dict[int, float]:
        """Get raw ppm_capped values per channel from Status.Channels."""
        data = self._service(SVC_STATUS, pb_empty(3))
        channels = {}
        for fn, wt, fv in pb_decode(data):
            if fn == 3 and wt == 2:
                for cn, cw, cv in pb_decode(fv):
                    if cn == 2 and cw == 2:
                        ch_id, ppm = None, None
                        for n, w, v in pb_decode(cv):
                            if n == 1 and w == 0: ch_id = v
                            elif n == 12 and w == 5: ppm = struct.unpack("<f", v)[0]
                        if ch_id is not None and ppm is not None:
                            channels[ch_id] = ppm
        return channels

    def get_device_json(self, path: str = "/device") -> dict:
        """Query device database, returns parsed JSON."""
        data = self._service(SVC_STATUS, pb_field(1, pb_string(1, path)))
        for fn, wt, fv in pb_decode(data):
            if fn == 1 and wt == 2:
                for ifn, iwt, iv in pb_decode(fv):
                    if ifn == 1 and iwt == 2:
                        return json.loads(iv.decode("utf-8", errors="replace").rstrip("\x00"))
        return {}

    def get_device_info(self) -> DeviceInfo:
        info = DeviceInfo()

        # Device info (serial, firmware)
        time.sleep(0.1)
        _, cmd, p = self._txrx(frame_info(self._seq_next()))
        if cmd & RESP_OK:
            info.raw_info = bytes(p)
            info.protocol_id = p[0] if p else 0
            text = p[2:].decode("ascii", errors="replace").rstrip("\x00")
            m = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{1,2})$', text)
            if m:
                major = m.group(1).lstrip('0') or '0'
                info.firmware_version = f"{major}.{m.group(2)}.{m.group(3)}"
                trimmed = len(m.group(1)) - len(major)
                info.serial_no = text[:m.start() + trimmed]
            else:
                info.serial_no = text

        # Keepalive (address, version)
        time.sleep(0.1)
        _, cmd, p = self._txrx(frame_keepalive(self._seq_next()))
        if cmd & RESP_OK and len(p) >= 9:
            info.raw_keepalive = bytes(p)
            info.device_address = struct.unpack_from("<H", p, 0)[0]
            info.flags = struct.unpack_from("<H", p, 2)[0]
            info.protocol_version = p[4:7].decode("ascii", errors="replace")

        # Part number
        time.sleep(0.1)
        _, cmd, p = self._txrx(frame_partno(self._seq_next()))
        if cmd & RESP_OK:
            info.part_no = p.decode("ascii", errors="replace").rstrip("\x00")

        return info

    def get_status(self) -> DeviceStatus:
        time.sleep(0.1)
        _, cmd, p = self._txrx(frame_status(self._seq_next()))
        if cmd == RESP_ERR:
            raise RuntimeError(f"Status error: 0x{(p[-1] if p else 0):02X}")
        return DeviceStatus(raw=bytes(p))

    def send_raw_command(self, cmd: int, payload: bytes = b"") -> tuple[int, bytes]:
        _, rc, rp = self._txrx(build_frame(self._seq_next(), struct.pack("<H", cmd), payload))
        return rc, bytes(rp)


def _raw_to_display(ppm: float, unit: int) -> float:
    """Convert raw ppm_capped value to display unit.

    Status.Channels reports all values in ppm (or ppm * 1000 for % vol).
    BT Measurement uses unit 1 (% vol) or 2 (ppm).
    """
    if unit == 1:  # % vol — raw is ppm * 1000 (e.g. 209000 = 20.9%)
        return ppm / 10000.0
    return ppm  # ppm, LEL, etc. — raw value is direct


def _parse_measurement(data: bytes) -> GasReading:
    ch, name, val, unit, state = 0, "", float("nan"), 0, 0
    for fn, wt, fv in pb_decode(data):
        if fn == 1 and wt == 0: ch = fv
        elif fn == 3 and wt == 0: unit = fv
        elif fn == 4 and wt == 5: val = struct.unpack("<f", fv)[0]
        elif fn == 5 and wt == 0: state = fv
        elif fn == 7 and wt == 2: name = fv.decode("utf-8", errors="replace")
    return GasReading(channel=ch, gas_name=name, value=val, unit=unit, state=state)


def find_dira_port() -> str | None:
    from serial.tools import list_ports
    for p in list_ports.comports():
        if p.vid == 0x10C4 and p.pid == 0x8072:
            return p.device
        if p.description and "DIRA" in p.description.upper():
            return p.device
    return None
