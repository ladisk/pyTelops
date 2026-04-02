"""Force-release CCP control on the camera."""
import socket, struct, time

GVCP_PORT = 3956
GVCP_KEY = 0x42
FLAG_ACK = 0x01
CMD_WRITEREG = 0x0082
CMD_READREG = 0x0080
REG_CCP = 0x0A00

# Find camera IP
from pyTelops import discover
cameras = discover()
if not cameras:
    print("No camera found")
    exit(1)
cam_ip = cameras[0]["ip"]
print(f"Camera: {cam_ip}")

# Find our link-local IP
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.connect((cam_ip, GVCP_PORT))
local_ip = s.getsockname()[0]
s.close()
print(f"Local IP: {local_ip}")

# Read CCP first to see current value
sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
sock.bind((local_ip, 0))
sock.settimeout(2.0)

# Read CCP
payload = struct.pack(">I", REG_CCP)
header = struct.pack(">BBHHH", GVCP_KEY, FLAG_ACK, CMD_READREG, len(payload), 1)
sock.sendto(header + payload, (cam_ip, GVCP_PORT))
data, _ = sock.recvfrom(8192)
status = struct.unpack(">H", data[0:2])[0]
val = struct.unpack(">I", data[8:12])[0]
print(f"CCP read status=0x{status:04X}, value=0x{val:08X}")

# Try writing CCP=0 (release)
payload = struct.pack(">II", REG_CCP, 0)
header = struct.pack(">BBHHH", GVCP_KEY, FLAG_ACK, CMD_WRITEREG, len(payload), 2)
sock.sendto(header + payload, (cam_ip, GVCP_PORT))
try:
    data, _ = sock.recvfrom(8192)
    status = struct.unpack(">H", data[0:2])[0]
    print(f"CCP=0 write status=0x{status:04X}")
except socket.timeout:
    print("CCP=0 write: timeout (no response)")

# Try writing CCP=2 (take exclusive)
payload = struct.pack(">II", REG_CCP, 2)
header = struct.pack(">BBHHH", GVCP_KEY, FLAG_ACK, CMD_WRITEREG, len(payload), 3)
sock.sendto(header + payload, (cam_ip, GVCP_PORT))
try:
    data, _ = sock.recvfrom(8192)
    status = struct.unpack(">H", data[0:2])[0]
    print(f"CCP=2 write status=0x{status:04X}")
except socket.timeout:
    print("CCP=2 write: timeout")

# Release
payload = struct.pack(">II", REG_CCP, 0)
header = struct.pack(">BBHHH", GVCP_KEY, FLAG_ACK, CMD_WRITEREG, len(payload), 4)
sock.sendto(header + payload, (cam_ip, GVCP_PORT))
try:
    data, _ = sock.recvfrom(8192)
    status = struct.unpack(">H", data[0:2])[0]
    print(f"CCP=0 release status=0x{status:04X}")
except socket.timeout:
    print("CCP=0 release: timeout")

sock.close()
