"""The checks verify.yml runs on each lab machine, the same on any Linux with Python
(minimal images lack ping, and ip and getent differ between distributions):

    python3 labcheck.py ping ADDRESS      ADDRESS answers an ICMP echo within two tries
    python3 labcheck.py has ADDRESS       this machine has ADDRESS
    python3 labcheck.py resolve NAME      NAME resolves through the system's DNS

Exit 0 when the check holds. ping uses an unprivileged ICMP socket where the system
allows it, else a raw one (needs root). Standard library only.
"""

import socket
import struct
import sys

IDENT = 0x534E  # "SN"; an unprivileged socket gets its own from the kernel


def checksum(data: bytes) -> int:
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


def ping(address: str, tries: int = 2, timeout: float = 2.0) -> bool:
    try:
        sock, raw = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP), False
    except PermissionError:
        sock, raw = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP), True
    sock.settimeout(timeout)
    with sock:
        for seq in range(tries):
            header = struct.pack("!BBHHH", 8, 0, 0, IDENT, seq)
            payload = b"SNAP lab check"
            packet = struct.pack("!BBHHH", 8, 0, checksum(header + payload), IDENT, seq) + payload
            sock.sendto(packet, (address, 0))
            try:
                while True:
                    data, (sender, _) = sock.recvfrom(1024)
                    if raw:  # every ICMP packet arrives: skip the IP header, match our request
                        data = data[(data[0] & 0x0F) * 4 :]
                        if data[4:8] != struct.pack("!HH", IDENT, seq):
                            continue
                    if sender == address and data[:1] == b"\0":  # echo reply
                        return True
            except TimeoutError:
                continue
    return False


def has(address: str) -> bool:
    """Binding to an address works only when this machine has it."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.bind((address, 0))
            return True
        except OSError:
            return False


def resolve(name: str) -> bool:
    try:
        print(socket.gethostbyname(name))
        return True
    except OSError:
        return False


if __name__ == "__main__":
    check = {"ping": ping, "has": has, "resolve": resolve}[sys.argv[1]]
    sys.exit(0 if check(sys.argv[2]) else 1)
