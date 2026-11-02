#!/usr/bin/env python3
"""Bounded IPv4/IPv6 STUN transport probe, not a browser ICE audit.

No response means UNKNOWN, never proof of a blocked or safe path. Callers must
validate routing before sending probes and compare each family with a known
egress address. Importing this module does not start network activity.
"""
import argparse
import hmac
import ipaddress
import os
import socket
import struct
import threading
import time


COOKIE = 0x2112A442
SERVERS = [
    ("stun.miwifi.com", 3478),
    ("stun.chat.bilibili.com", 3478),
    ("stun.l.google.com", 19302),
    ("stun.cloudflare.com", 3478),
]


def decode_response(data, transaction):
    """Return a mapped address only after validating the entire STUN message."""
    if len(transaction) != 12 or len(data) < 20:
        return None
    message_type, length, cookie = struct.unpack("!HHI", data[:8])
    if (message_type != 0x0101 or cookie != COOKIE or length % 4 or
            len(data) != 20 + length or not hmac.compare_digest(data[8:20], transaction)):
        return None
    result = None
    result_is_xor = False
    position = 20
    while position < len(data):
        if position + 4 > len(data):
            return None
        kind, size = struct.unpack("!HH", data[position:position + 4])
        end = position + 4 + size
        padded_end = end + (-size % 4)
        if end > len(data) or padded_end > len(data):
            return None
        value = data[position + 4:end]
        if kind in (0x0001, 0x0020):
            if len(value) < 4 or value[0] != 0 or value[1] not in (1, 2):
                return None
            expected = 8 if value[1] == 1 else 20
            if size != expected:
                return None
            packed = value[4:]
            if kind == 0x0020:
                mask = struct.pack("!I", COOKIE) + transaction
                packed = bytes(a ^ b for a, b in zip(packed, mask))
            address = str(ipaddress.ip_address(packed))
            if result is not None and result_is_xor and kind == 0x0020 and address != result:
                return None
            if result is None or kind == 0x0020:
                result, result_is_xor = address, kind == 0x0020
        position = padded_end
    return result


def same_endpoint(actual, expected):
    try:
        return (ipaddress.ip_address(actual[0].split("%")[0]) ==
                ipaddress.ip_address(expected[0].split("%")[0]) and actual[1] == expected[1] and
                (len(expected) < 4 or not expected[3] or len(actual) >= 4 and actual[3] == expected[3]))
    except (IndexError, ValueError, TypeError):
        return False


def stun_public_ip(host, port, timeout=2.0, family=socket.AF_INET):
    deadline = time.monotonic() + timeout
    try:
        addresses = socket.getaddrinfo(host, port, family, socket.SOCK_DGRAM)
    except OSError:
        return None
    for af, socktype, protocol, _, endpoint in addresses[:3]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        transaction = os.urandom(12)
        request = struct.pack("!HHI", 0x0001, 0, COOKIE) + transaction
        try:
            with socket.socket(af, socktype, protocol) as connection:
                connection.settimeout(remaining)
                connection.sendto(request, endpoint)
                while time.monotonic() < deadline:
                    connection.settimeout(max(0.001, deadline - time.monotonic()))
                    data, sender = connection.recvfrom(4096)
                    if not same_endpoint(sender, endpoint):
                        continue
                    result = decode_response(data, transaction)
                    if result is not None:
                        return result
        except OSError:
            continue
    return None


def probe(families, budget=9.0):
    found = set()
    lock = threading.Lock()
    deadline = time.monotonic() + budget
    def worker(host, port, family):
        result = stun_public_ip(host, port, family=family)
        if result:
            with lock:
                found.add(result)
    threads = [threading.Thread(target=worker, args=(host, port, family), daemon=True)
               for host, port in SERVERS for family in families]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(max(0, deadline - time.monotonic()))
    with lock:
        return sorted(found)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--family", choices=("ipv4", "ipv6", "both"), default="ipv4")
    args = parser.parse_args(argv)
    families = {"ipv4": [socket.AF_INET], "ipv6": [socket.AF_INET6],
                "both": [socket.AF_INET, socket.AF_INET6]}[args.family]
    addresses = probe(families)
    print("RESULT:" + (",".join(addresses) if addresses else "NONE"))


if __name__ == "__main__":
    main()
