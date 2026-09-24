import serial
import sys
import time

PORT = "COM16"
BAUD = 115200

def reset_board(ser: serial.Serial) -> None:
    ser.dtr = False
    ser.rts = True
    time.sleep(0.1)
    ser.rts = False
    time.sleep(0.1)
    ser.dtr = True

def read_until(deadline: float, ser: serial.Serial) -> str:
    buf = ""
    while time.time() < deadline:
        data = ser.read(4096)
        if data:
            text = data.decode("utf-8", errors="replace")
            sys.stdout.write(text)
            sys.stdout.flush()
            buf += text
    return buf

def main() -> int:
    ser = serial.Serial(PORT, BAUD, timeout=0.5)
    try:
        reset_board(ser)
        time.sleep(2)
        buf = read_until(time.time() + 150, ser)
        if "Modem init failed" not in buf:
            time.sleep(1)
            ser.write(b"d")
            read_until(time.time() + 120, ser)
        else:
            time.sleep(1)
            ser.write(b"r")
            buf = read_until(time.time() + 120, ser)
            if "Modem init failed" not in buf:
                ser.write(b"d")
                read_until(time.time() + 120, ser)
    finally:
        ser.close()
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
