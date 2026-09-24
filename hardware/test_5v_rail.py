import serial
import sys
import time

PORT = "COM16"
BAUD = 115200


def read_for(ser: serial.Serial, seconds: float) -> str:
    buf = ""
    end = time.time() + seconds
    while time.time() < end:
        data = ser.read(4096)
        if data:
            text = data.decode("utf-8", errors="replace")
            sys.stdout.write(text)
            sys.stdout.flush()
            buf += text
    return buf


def send_cmd(ser: serial.Serial, cmd: bytes, wait: float = 2.0) -> str:
    ser.write(cmd)
    ser.flush()
    return read_for(ser, wait)


def main() -> int:
    ser = serial.Serial(PORT, BAUD, timeout=0.5)
    try:
        print("=== 5V rail serial test ===")
        read_for(ser, 1.0)
        send_cmd(ser, b"p", 1.5)
        send_cmd(ser, b"4", 1.5)
        send_cmd(ser, b"p", 1.5)
        send_cmd(ser, b"5", 1.5)
        buf = send_cmd(ser, b"p", 1.5)
        if "5V rail (GPIO4): OFF" in buf or "5V rail (GPIO4): ON" in buf:
            return 0
        return 1
    finally:
        ser.close()


if __name__ == "__main__":
    raise SystemExit(main())
