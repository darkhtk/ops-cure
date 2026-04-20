from __future__ import annotations

import socket

from .base import PowerActionResult, PowerProvider, PowerTarget


class WakeOnLanPowerProvider(PowerProvider):
    provider_name = "wol"

    def wake(self, target: PowerTarget) -> PowerActionResult:
        if not target.mac_address:
            return PowerActionResult(
                state="unknown",
                detail=f"Wake-on-LAN target `{target.name}` is missing a MAC address.",
            )
        mac = target.mac_address.replace("-", "").replace(":", "").strip()
        if len(mac) != 12:
            return PowerActionResult(
                state="unknown",
                detail=f"Wake-on-LAN target `{target.name}` has an invalid MAC address.",
            )

        packet = bytes.fromhex("FF" * 6 + mac * 16)
        broadcast_ip = target.broadcast_ip or "255.255.255.255"
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.sendto(packet, (broadcast_ip, 9))
        return PowerActionResult(
            state="waking",
            detail=f"Wake-on-LAN packet sent to `{target.name}` via {broadcast_ip}.",
        )

    def status(self, target: PowerTarget) -> PowerActionResult:
        del target
        return PowerActionResult(
            state="unknown",
            detail="Wake-on-LAN does not provide authoritative power-state feedback.",
        )
