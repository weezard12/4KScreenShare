from __future__ import annotations

import http.client
import random
import socket
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Iterable


SSDP_ADDRESS = ("239.255.255.250", 1900)
SSDP_MX = 2
SERVICE_TYPES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)


class PortMappingError(RuntimeError):
    """Raised when UPnP port mapping could not be established."""


@dataclass(slots=True)
class PortMappingLease:
    control_url: str
    service_type: str
    external_port: int
    internal_port: int
    internal_client: str
    description: str
    external_ip: str | None = None
    protocol: str = "TCP"

    def release(self, *, timeout: float = 3.0) -> None:
        _soap_request(
            self.control_url,
            self.service_type,
            "DeletePortMapping",
            {
                "NewRemoteHost": "",
                "NewExternalPort": str(self.external_port),
                "NewProtocol": self.protocol,
            },
            timeout=timeout,
        )


def try_add_tcp_port_mapping(
    *,
    internal_client: str,
    internal_port: int,
    preferred_external_port: int,
    description: str,
    lease_duration: int = 43_200,
    timeout: float = 3.0,
) -> PortMappingLease | None:
    services = _discover_gateway_services(timeout=timeout)
    if not services:
        return None

    candidate_ports = [preferred_external_port]
    candidate_ports.extend(_fallback_external_ports())

    for service_type, control_url in services:
        external_ip = _get_external_ip(control_url, service_type, timeout=timeout)
        for external_port in candidate_ports:
            try:
                _soap_request(
                    control_url,
                    service_type,
                    "AddPortMapping",
                    {
                        "NewRemoteHost": "",
                        "NewExternalPort": str(external_port),
                        "NewProtocol": "TCP",
                        "NewInternalPort": str(internal_port),
                        "NewInternalClient": internal_client,
                        "NewEnabled": "1",
                        "NewPortMappingDescription": description,
                        "NewLeaseDuration": str(lease_duration),
                    },
                    timeout=timeout,
                )
                return PortMappingLease(
                    control_url=control_url,
                    service_type=service_type,
                    external_port=external_port,
                    internal_port=internal_port,
                    internal_client=internal_client,
                    description=description,
                    external_ip=external_ip,
                )
            except PortMappingError:
                if _mapping_matches_existing(
                    control_url,
                    service_type,
                    external_port=external_port,
                    internal_client=internal_client,
                    internal_port=internal_port,
                    timeout=timeout,
                ):
                    return PortMappingLease(
                        control_url=control_url,
                        service_type=service_type,
                        external_port=external_port,
                        internal_port=internal_port,
                        internal_client=internal_client,
                        description=description,
                        external_ip=external_ip,
                    )
                continue
    return None


def _fallback_external_ports() -> Iterable[int]:
    seen: set[int] = set()
    for _ in range(6):
        candidate = random.randint(20_000, 60_000)
        if candidate in seen:
            continue
        seen.add(candidate)
        yield candidate


def _discover_gateway_services(*, timeout: float) -> list[tuple[str, str]]:
    locations: dict[str, None] = {}
    for service_type in SERVICE_TYPES:
        for location in _ssdp_search(service_type, timeout=timeout):
            locations[location] = None

    services: list[tuple[str, str]] = []
    for location in locations:
        try:
            with urllib.request.urlopen(location, timeout=timeout) as response:
                document = response.read()
            services.extend(_parse_service_descriptions(location, document))
        except Exception:
            continue
    return services


def _ssdp_search(service_type: str, *, timeout: float) -> list[str]:
    payload = "\r\n".join(
        [
            "M-SEARCH * HTTP/1.1",
            f"HOST:{SSDP_ADDRESS[0]}:{SSDP_ADDRESS[1]}",
            'MAN:"ssdp:discover"',
            f"MX:{SSDP_MX}",
            f"ST:{service_type}",
            "",
            "",
        ]
    ).encode("ascii")

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.settimeout(timeout)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
    try:
        sock.sendto(payload, SSDP_ADDRESS)
        locations: list[str] = []
        while True:
            try:
                response, _ = sock.recvfrom(65535)
            except TimeoutError:
                break
            location = _extract_location(response.decode("utf-8", errors="ignore"))
            if location and location not in locations:
                locations.append(location)
        return locations
    finally:
        sock.close()


def _extract_location(response_text: str) -> str | None:
    for line in response_text.splitlines():
        if line.lower().startswith("location:"):
            return line.split(":", 1)[1].strip()
    return None


def _parse_service_descriptions(location: str, document: bytes) -> list[tuple[str, str]]:
    root = ET.fromstring(document)
    namespace = {"upnp": root.tag.split("}")[0].strip("{")} if "}" in root.tag else {}
    base_url = _xml_text(root, "upnp:URLBase", namespace) or location

    services: list[tuple[str, str]] = []
    for service in root.findall(".//upnp:service", namespace) or root.findall(".//service"):
        service_type = _xml_text(service, "upnp:serviceType", namespace) or _xml_text(service, "serviceType", {})
        control_url = _xml_text(service, "upnp:controlURL", namespace) or _xml_text(service, "controlURL", {})
        if not service_type or not control_url or service_type not in SERVICE_TYPES:
            continue
        services.append((service_type, urllib.parse.urljoin(base_url, control_url)))
    return services


def _xml_text(node: ET.Element, path: str, namespace: dict[str, str]) -> str | None:
    child = node.find(path, namespace)
    if child is None or child.text is None:
        return None
    return child.text.strip()


def _get_external_ip(control_url: str, service_type: str, *, timeout: float) -> str | None:
    try:
        result = _soap_request(
            control_url,
            service_type,
            "GetExternalIPAddress",
            {},
            timeout=timeout,
        )
    except PortMappingError:
        return None
    return result.get("NewExternalIPAddress")


def _mapping_matches_existing(
    control_url: str,
    service_type: str,
    *,
    external_port: int,
    internal_client: str,
    internal_port: int,
    timeout: float,
) -> bool:
    try:
        result = _soap_request(
            control_url,
            service_type,
            "GetSpecificPortMappingEntry",
            {
                "NewRemoteHost": "",
                "NewExternalPort": str(external_port),
                "NewProtocol": "TCP",
            },
            timeout=timeout,
        )
    except PortMappingError:
        return False
    return (
        result.get("NewInternalClient", "").strip() == internal_client
        and int(result.get("NewInternalPort", "0") or 0) == internal_port
    )


def _soap_request(
    control_url: str,
    service_type: str,
    action: str,
    arguments: dict[str, str],
    *,
    timeout: float,
) -> dict[str, str]:
    parsed = urllib.parse.urlparse(control_url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    envelope = _build_soap_envelope(service_type, action, arguments)
    headers = {
        "Content-Type": 'text/xml; charset="utf-8"',
        "SOAPAction": f'"{service_type}#{action}"',
        "Connection": "close",
    }

    connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=timeout)
    try:
        connection.request("POST", path, body=envelope.encode("utf-8"), headers=headers)
        response = connection.getresponse()
        payload = response.read()
        if response.status >= 400:
            raise PortMappingError(f"UPnP {action} failed with HTTP {response.status}.")
        return _parse_soap_response(payload)
    except OSError as exc:
        raise PortMappingError(str(exc)) from exc
    finally:
        connection.close()


def _build_soap_envelope(service_type: str, action: str, arguments: dict[str, str]) -> str:
    fields = "".join(f"<{name}>{value}</{name}>" for name, value in arguments.items())
    return (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{service_type}">{fields}</u:{action}>'
        "</s:Body>"
        "</s:Envelope>"
    )


def _parse_soap_response(payload: bytes) -> dict[str, str]:
    if not payload:
        return {}
    root = ET.fromstring(payload)
    values: dict[str, str] = {}
    for element in root.iter():
        if element.text and element.text.strip():
            values[element.tag.split("}")[-1]] = element.text.strip()
    return values
