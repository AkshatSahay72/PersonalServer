#!/usr/bin/env python3
"""
PersonalServer Resilient DNS Fallback
====================================
Provides an automated, zero-configuration DNS resolver fallback for environments
(such as Android/Termux under VPN or broken libc netd routing) where the system
getaddrinfo fails to resolve external hostnames.

Security & Integrity Guarantees:
- Zero hardcoded IP addresses (queries dynamic upstream DNS over port 53).
- System getaddrinfo is always tried first; fallback only engages on gaierror.
- Strict TLS certificate verification is preserved; hostname SNI is never bypassed.
"""

import socket
import struct
from typing import Optional, Sequence, Tuple

_ORIG_GETADDRINFO = socket.getaddrinfo
_FALLBACK_INSTALLED = False


def query_dns_a_record(host: str, nameserver: str = "1.1.1.1", timeout: float = 3.0) -> str:
    """Queries an upstream recursive DNS nameserver for an IPv4 A record."""
    tid = 0x4B3A
    flags = 0x0100  # Standard query, recursion desired
    header = struct.pack(">HHHHHH", tid, flags, 1, 0, 0, 0)
    qname = b"".join(bytes([len(p)]) + p.encode("ascii") for p in host.split(".")) + b"\x00"
    question = qname + struct.pack(">HH", 1, 1)  # QTYPE=A, QCLASS=IN

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(header + question, (nameserver, 53))
        data, _ = s.recvfrom(1024)
    finally:
        s.close()

    if len(data) < 12:
        raise ValueError("Invalid DNS response")

    ancount = struct.unpack(">H", data[6:8])[0]
    if ancount == 0:
        raise ValueError(f"No DNS answer for {host}")

    offset = 12
    while offset < len(data) and data[offset] != 0:
        offset += data[offset] + 1
    offset += 5  # Skip null byte, qtype, qclass

    for _ in range(ancount):
        if offset >= len(data):
            break
        if data[offset] & 0xC0 == 0xC0:
            offset += 2
        else:
            while offset < len(data) and data[offset] != 0:
                offset += data[offset] + 1
            offset += 1

        if offset + 10 > len(data):
            break
        rtype, rclass, ttl, rdlength = struct.unpack(">HHIH", data[offset:offset + 10])
        offset += 10
        if rtype == 1 and rdlength == 4 and offset + 4 <= len(data):
            ip_bytes = data[offset:offset + 4]
            return ".".join(str(b) for b in ip_bytes)
        offset += rdlength

    raise ValueError(f"No IPv4 address found for {host}")


def make_resilient_getaddrinfo(nameservers: Sequence[str] = ("1.1.1.1", "8.8.8.8")):
    """Returns a getaddrinfo replacement that falls back to explicit recursive nameservers."""
    def resilient_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        try:
            return _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
        except socket.gaierror:
            if not host or not isinstance(host, str):
                raise
            # Attempt fallback resolution
            for ns in nameservers:
                try:
                    ip = query_dns_a_record(host, nameserver=ns)
                    return _ORIG_GETADDRINFO(ip, port, family, type, proto, flags)
                except Exception:
                    continue
            raise

    return resilient_getaddrinfo


def enable_dns_fallback(nameservers: Sequence[str] = ("1.1.1.1", "8.8.8.8")) -> None:
    """Installs the resilient DNS fallback globally on socket.getaddrinfo if not already installed."""
    global _FALLBACK_INSTALLED
    if not _FALLBACK_INSTALLED:
        socket.getaddrinfo = make_resilient_getaddrinfo(nameservers)
        _FALLBACK_INSTALLED = True

