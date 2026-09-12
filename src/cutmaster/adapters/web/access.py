"""Direct-peer access policy; forwarded headers never grant write access."""

from ipaddress import ip_address

from fastapi import Request


def can_write(request: Request) -> bool:
    if request.client is None:
        return False
    try:
        address = ip_address(request.client.host)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback
