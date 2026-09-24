import serial
import sys
import time

PORT = "COM16"
BAUD = 115200
# B + OOO + 000 + ooo + m
WIFI_LINE = "0,GRM119,BOOO000ooom\n"


def main() -> int:
    ser = serial.Serial(PORT, BAUD, timeout=0.5)
    try:
        start = time.time()
        buf = ""
        while time.time() - start < 180:
            data = ser.read(4096)
            if data:
                text = data.decode("utf-8", errors="replace")
                sys.stdout.write(text)
                sys.stdout.flush()
                buf += text
            if "Commands:" in buf:
                break
            if "WiFi: connected IP=" in buf:
                return 0
            time.sleep(0.05)

        time.sleep(0.5)
        ser.write(b"w")
        ser.write(WIFI_LINE.encode("ascii"))
        ser.flush()

        start = time.time()
        while time.time() - start < 60:
            data = ser.read(4096)
            if data:
                text = data.decode("utf-8", errors="replace")
                sys.stdout.write(text)
                sys.stdout.flush()
                if "WiFi: connected IP=" in text:
                    return 0
                if "WiFi: saved but connection failed" in text:
                    return 1
        return 1
    finally:
        ser.close()


if __name__ == "__main__":
    raise SystemExit(main())
