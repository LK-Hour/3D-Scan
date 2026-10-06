"""Pairing: a long random token stored on the PC, shown as a QR code, plus LAN address discovery and mDNS."""
from __future__ import annotations

import io
import json
import secrets
import socket
from pathlib import Path


def load_or_create_token(config_dir: Path, rotate: bool = False) -> str:
    config_dir.mkdir(parents=True, exist_ok=True)
    f = config_dir / "token"
    if f.exists() and not rotate:
        tok = f.read_text().strip()
        if len(tok) >= 32:
            return tok
    tok = secrets.token_urlsafe(32)
    f.write_text(tok)
    try:
        f.chmod(0o600)
    except OSError:  # Windows
        pass
    return tok


def lan_addresses() -> list[str]:
    """IPv4 addresses of this machine that other devices on the LAN could reach (best effort, no internet needed)."""
    addrs: list[str] = []
    try:  # the address used for the default route; connecting a UDP socket sends no packets
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            addrs.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            a = info[4][0]
            if a not in addrs and not a.startswith("127."):
                addrs.append(a)
    except OSError:
        pass
    return addrs or ["127.0.0.1"]


def pairing_payload(host: str, port: int, token: str, name: str) -> dict:
    return {"v": 1, "name": name, "host": host, "port": port, "token": token}


def qr_png(payload: dict) -> bytes:
    import qrcode
    img = qrcode.make(json.dumps(payload, separators=(",", ":")))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def qr_ascii(payload: dict) -> str:
    import qrcode
    qr = qrcode.QRCode(border=1)
    qr.add_data(json.dumps(payload, separators=(",", ":")))
    qr.make(fit=True)
    out = io.StringIO()
    qr.print_ascii(out=out, invert=True)
    return out.getvalue()


class Advertiser:
    """Advertise `_scanpc._tcp` over mDNS so the app can find the PC even if its IP changes (best effort)."""

    def __init__(self, name: str, port: int):
        self.name, self.port = name, port
        self._zc = None
        self._info = None

    def start(self) -> bool:
        try:
            from zeroconf import ServiceInfo, Zeroconf
            addrs = [socket.inet_aton(a) for a in lan_addresses() if not a.startswith("127.")]
            if not addrs:
                return False
            self._info = ServiceInfo("_scanpc._tcp.local.", f"{self.name}._scanpc._tcp.local.", addresses=addrs,
                                     port=self.port, properties={"v": "1"})
            self._zc = Zeroconf()
            self._zc.register_service(self._info)
            return True
        except Exception:
            return False

    def stop(self) -> None:
        try:
            if self._zc and self._info:
                self._zc.unregister_service(self._info)
            if self._zc:
                self._zc.close()
        except Exception:
            pass
