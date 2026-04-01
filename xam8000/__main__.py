"""CLI for Dräger X-am 8000 gas detector."""

import argparse
import json
import time

from .config import load_config
from .device import DragerXam8000, find_dira_port


def main():
    cfg = load_config()

    parser = argparse.ArgumentParser(
        description="Dräger X-am 8000 via DIRA IV USB-IR adapter")
    parser.add_argument("-p", "--port", help="Serial port (auto-detected if omitted)")
    parser.add_argument("-b", "--baudrate", type=int, default=cfg["baudrate"])
    parser.add_argument("--mode", type=lambda x: int(x, 0), default=cfg["security_mode"],
                        help="Security mode (default: 0x05)")
    parser.add_argument("-j", "--json", action="store_true", help="JSON output")
    parser.add_argument("-s", "--status", action="store_true", help="Device status")
    parser.add_argument("-g", "--sensors", action="store_true", help="Gas sensor readings")
    parser.add_argument("-m", "--monitor", action="store_true", help="Continuous sensor monitoring")
    parser.add_argument("-i", "--interval", type=float, default=cfg["polling_interval"],
                        help="Monitor interval in seconds")
    parser.add_argument("--sample", action="store_true",
                        help="Single sample: pump on, wait, read sensors, pump off")
    parser.add_argument("--warmup", type=float, default=60.0,
                        help="Pump warmup time in seconds before reading (default: 60)")
    parser.add_argument("--pump-flow", type=int, default=350,
                        help="Pump flow rate in ml/min (default: 350)")
    parser.add_argument("--pump", choices=["on", "off", "status"],
                        help="Pump control: on, off, or status")
    parser.add_argument("--db-path", help="Query device database JSON path")
    parser.add_argument("--raw-cmd", type=lambda x: int(x, 0), help="Raw command (hex)")
    parser.add_argument("--raw-payload", default="", help="Raw payload hex")
    args = parser.parse_args()

    port = args.port or cfg["port"]
    if port == "auto":
        port = find_dira_port()
    if not port:
        print("Error: No DIRA IV adapter found. Specify port with -p.")
        from serial.tools import list_ports
        for p in list_ports.comports():
            print(f"  {p.device}: {p.description} [VID:PID={p.vid}:{p.pid}]")
        return 1

    print(f"Connecting to {port} at {args.baudrate} baud...")

    with DragerXam8000(port, baudrate=args.baudrate, security_mode=args.mode) as dev:
        if args.raw_cmd is not None:
            payload = bytes.fromhex(args.raw_payload) if args.raw_payload else b""
            rc, rd = dev.send_raw_command(args.raw_cmd, payload)
            print(f"Response: cmd=0x{rc:04X}")
            if rd:
                print(f"Payload ({len(rd)} bytes): {rd.hex()}")
                if any(32 <= b < 127 for b in rd):
                    print(f"ASCII: {rd.decode('ascii', errors='replace')}")
            return 0

        if args.pump:
            if args.pump == "on":
                dev.set_pump(args.pump_flow)
                print(f"Pump on ({args.pump_flow} ml/min)")
            elif args.pump == "off":
                dev.set_pump(0)
                print("Pump off")
            else:
                print(json.dumps(dev.get_flow(), indent=2))
            return 0

        if args.sample:
            info = dev.get_device_info()
            print(f"{info}\n")
            print(f"Pump on ({args.pump_flow} ml/min), waiting {args.warmup}s...")
            dev.set_pump(args.pump_flow)
            time.sleep(args.warmup)
            dev.send_keepalive()
            time.sleep(0.1)
            readings = dev.get_gas_readings()
            dev.set_pump(0)
            print("Pump off. Readings:")
            if args.json:
                print(json.dumps({
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "serial_no": info.serial_no,
                    "sensors": [
                        {"channel": r.channel, "gas": r.gas_name,
                         "value": r.value if r.is_valid else None,
                         "unit": r.unit_label, "valid": r.is_valid}
                        for r in readings
                    ],
                }, indent=2))
            else:
                for r in readings:
                    print(f"  {r}")
            return 0

        if args.db_path:
            print(json.dumps(dev.get_device_json(args.db_path), indent=2))
            return 0

        info = dev.get_device_info()

        if args.sensors or args.monitor:
            readings = dev.get_gas_readings()
            if args.json:
                print(json.dumps({
                    "serial_no": info.serial_no,
                    "sensors": [
                        {"channel": r.channel, "gas": r.gas_name,
                         "value": r.value if r.is_valid else None,
                         "unit": r.unit_label, "valid": r.is_valid}
                        for r in readings
                    ],
                }, indent=2))
            else:
                print(f"\n{info}\n\nGas Sensors:")
                for r in readings:
                    print(f"  [{'x' if not r.is_valid else ' '}] {r}")

            if args.monitor:
                print(f"\nMonitoring every {args.interval}s (Ctrl+C to stop)...")
                try:
                    while True:
                        time.sleep(args.interval)
                        try:
                            dev.send_keepalive()
                            time.sleep(0.1)
                            readings = dev.get_gas_readings()
                            vals = " | ".join(str(r) for r in readings if r.is_valid)
                            print(f"[{time.strftime('%H:%M:%S')}] {vals}")
                        except Exception as e:
                            print(f"Error: {e}")
                except KeyboardInterrupt:
                    print("\nStopped.")
            return 0

        if args.json:
            d = {"serial_no": info.serial_no, "part_no": info.part_no,
                 "firmware_version": info.firmware_version,
                 "protocol_version": info.protocol_version,
                 "device_address": info.device_address, "flags": info.flags}
            if args.status:
                st = dev.get_status()
                d["status"] = {"raw": st.raw.hex(), "active": st.is_active}
            print(json.dumps(d, indent=2))
        else:
            print(f"\n{info}")
            print(f"  Device address: 0x{info.device_address:04X}")
            print(f"  Protocol:       {info.protocol_version} (ID: 0x{info.protocol_id:02X})")
            print(f"  Flags:          0x{info.flags:04X}")
            if args.status:
                st = dev.get_status()
                print(f"  Status:         {st.raw.hex()} ({'active' if st.is_active else 'standby'})")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
