# Dräger X-am 8000 Serial Communication Library

Python library for reading real-time gas sensor data from a [Dräger X-am 8000](https://www.draeger.com/en_uk/Products/X-am-8000) multi-gas detector over its infrared serial interface.

Connects via the DIRA IV USB-IR adapter (Silicon Labs CP210x, VID `0x10C4`, PID `0x8072`).

> [!CAUTION]
> **Do not send MGDAPI `TurnOff` to the device.**
>
> The MGDAPI `TurnOff` command (service `0x5011`, field 4) puts the device into a deep shutdown
> state that **cannot be reversed over IR or by pressing the power button**. The device becomes
> completely unresponsive — GasVision and CC-Vision cannot detect it, battery removal does not help,
> and placing it on a charging dock does not trigger a restart. Recovery required Draeger service
> intervention. This command should **not** be used.
>
> `StateChange` (service `0x1003`) with states 1, 2, 4, or 5 turns the device off. It can be
> restarted manually, but it will require a zero-flow calibration on boot which may be impractical
> in unattended deployments. Only `StateChange(3)` and `StateChange(6)` are known to be safe.
> Use `ExecPump` (service `0x5006`, field 9) to control the pump without affecting device state.

## Quick start

```bash
pip install pyserial
```

```python
from xam8000 import DragerXam8000

with DragerXam8000("COM4") as device:
    # Read all gas sensors
    for reading in device.get_gas_readings():
        print(reading)
    # CO2: 0.0 % vol
    # CH4: 0.0 % vol
    # O2: 20.9 % vol
    # H2S: 0.0 ppm
    # NH3: 0.0 ppm

    # Device info
    print(device.get_device_info())

    # Query device database (JSON)
    print(device.get_device_json("/device/battery/voltage"))
    # {'voltage': 4096}
```

### CLI

```bash
# Single sample: pump on, wait 60s for gas flow, read sensors, pump off
python -m xam8000 -p COM4 --sample

# Sample with JSON output and custom warmup time
python -m xam8000 -p COM4 --sample --warmup 60 --json

# Read gas sensors (without pump control)
python -m xam8000 -p COM4 --sensors

# JSON output
python -m xam8000 -p COM4 --sensors --json

# Continuous monitoring (every 2s)
python -m xam8000 -p COM4 --monitor

# Pump control
python -m xam8000 -p COM4 --pump on
python -m xam8000 -p COM4 --pump off
python -m xam8000 -p COM4 --pump status

# Query device database
python -m xam8000 -p COM4 --db-path "/device"

# Device info only
python -m xam8000 -p COM4
```

Port is auto-detected if a DIRA IV adapter is connected. Default settings are in `config.json`, credentials in `credentials.json`.

## Configuration

### `xam8000/config.json` — settings

```json
{
    "port": "auto",
    "baudrate": 115200,
    "timeout": 3.0,
    "security_mode": 5,
    "polling_interval": 2.0
}
```

Set `port` to your COM port (e.g. `"COM4"`, `"/dev/ttyUSB0"`) or `"auto"` for auto-detection.

### `xam8000/credentials.json` — passwords (gitignored)

```json
{
    "passwords": {
        "0": "basic-access-password",
        "5": "full-access-password"
    }
}
```

Keys are security mode numbers. Mode 0 provides basic access (device info only). Mode 5 enables protobuf services (sensor data, database queries). A template is provided in `credentials.template.json`.

### Obtaining the passwords

The passwords are not publicly documented. They can be extracted from DLLs shipped with [Dräger GasVision](https://www.draeger.com/en_uk/Products/GasVision).

## Sensors

The X-am 8000 reports up to 6 gas channels:

| Channel | Gas | Unit | Sensor type |
|---------|-----|------|-------------|
| 0 | CO2 | % vol | Infrared |
| 1 | CH4 | % vol | Infrared |
| 4 | O2 | % vol | Electrochemical |
| 6 | H2S | ppm | Electrochemical |
| 7 | CO | ppm | Electrochemical |
| 8 | NH3 | ppm | Electrochemical |

Not all channels may be active depending on installed sensor modules. IR sensors need warmup time after power-on.

## API reference

### `DragerXam8000(port=None, **kwargs)`

Main interface class. Use as a context manager for automatic connect/disconnect. Constructor accepts optional overrides for any config.json key (e.g. `baudrate=9600`, `security_mode=0`).

- **`get_gas_readings() -> list[GasReading]`** — Real-time gas sensor values. Each `GasReading` has `gas_name`, `value` (float), `unit_label`, `is_valid`.
- **`set_pump(flow_ml_per_min=350)`** — Set pump flow rate. Use `0` to stop, `350` for normal operation. Device stays on.
- **`get_flow() -> dict`** — Current pump flow status (`value`, `status`, `isActivated`).
- **`get_device_info() -> DeviceInfo`** — Serial number, firmware version, part number.
- **`get_device_json(path) -> dict`** — Query device database. Paths: `"/device"`, `"/device/battery/voltage"`, etc.
- **`get_status() -> DeviceStatus`** — Raw 9-byte device status.
- **`send_keepalive() -> bytes`** — Maintain connection.
- **`send_raw_command(cmd, payload) -> (cmd, payload)`** — Low-level protocol access.

## Protocol overview

Proprietary binary protocol over 115200 baud 8N1 half-duplex IR link.

### Wire format

```
55 C1 [LEN 2B] [19 00 01 00] [SEQ 4B] [CMD 2B] [PAYLOAD] [CRC16 2B]
```

- `55` sync, `C1` protocol ID
- `LEN` = CMD + PAYLOAD length (LE 16-bit)
- `19 00 01 00` = source address + header
- CRC-16/KERMIT over inner frame (C1 through payload)

### Handshake

```
Connect(0x0A) → Seed(0x0D, mode) → Key(0x0E) → Keepalive(0x04)
```

Key derivation: CRC-32/MPEG-2 of password bytes with seed as initial value, each byte bit-reflected.

### Security modes

| Mode | Access level |
|------|-------------|
| 0x00 | Basic: device info, status, part number |
| 0x05 | Full: protobuf services, sensor data, state changes, database |

### Protobuf services (mode 0x05)

| CMD | Service | Description |
|-----|---------|-------------|
| 0x500F | Bluetooth | `RequestMeasurement` — gas readings with names and units |
| 0x5010 | Status | `RequestGet` — JSON database; `RequestChannels` — raw sensor data |
| 0x5011 | MGDAPI | `GetMeasurements` — readings with alarm states |
| 0x1003 | StateChange | Device state transitions |
| 0x6001 | DateTime | Device clock |

Payloads are serialized protobuf (no external dependency — encoding/decoding is built-in).

## Repository structure

```
├── xam8000/
│   ├── __init__.py               # Public API exports
│   ├── __main__.py               # CLI entry point
│   ├── config.py                 # Config + credential loading
│   ├── protocol.py               # CRC, framing, protobuf helpers
│   ├── device.py                 # DragerXam8000 class + data classes
│   ├── config.json               # Default settings
│   ├── credentials.json          # Passwords (gitignored)
│   └── credentials.template.json # Credential template
│
├── README.md
├── LICENSE.md
├── requirements.txt
└── .gitignore
```

## How we got here

The entire protocol was reverse-engineered from scratch — there is no public documentation for the X-am 8000 serial interface.

**Summary of the journey:**

1. **Serial capture** — Captured GasVision traffic with Device Monitoring Studio. Identified wire format: sync `0x55`, protocol `0xC1`, CRC-16/KERMIT framing.

2. **Basic handshake** — Disassembled `MultigasConnect.dll` (32-bit, shipped with GasVision) using `pefile`/`capstone`. Found the seed-to-key algorithm (CRC-32/MPEG-2 with bit-reflected input).

3. **Command discovery** — Probed all command IDs 0x0000–0xFFFF. Identified basic commands and protobuf services with their access requirements.

4. **Protobuf services** — Found `.proto` files in GasVision installation defining message schemas. Built a minimal protobuf encoder/decoder. Confirmed real-time sensor readings with atmospheric O2 at 20.9% vol.

## Dependencies

- Python 3.10+
- `pyserial >= 3.5`

## License

MIT — see [LICENSE.md](LICENSE.md).
