"""UPnP IGD (Internet Gateway Device) discovery and control.

Implements just enough of the UPnP spec to discover routers on the LAN and
call WANIPConnection/WANPPPConnection actions (AddPortMapping,
DeletePortMapping, GetGenericPortMappingEntry, GetExternalIPAddress).

前作 PortForwardManager (github.com/Simohayhe/port-forward-manager, MIT) の
upnp.py をそのまま取り込んだもの。ゲームサーバーマネージャーからは core/upnp.py
(このモジュールの薄いアダプタ)経由で使う。更新時は上流から取り直すこと。
"""
import os
import socket
import time
import traceback
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from urllib.parse import urljoin
from xml.sax.saxutils import escape

_LOG_PATH = os.path.join(
    os.environ.get("APPDATA", os.path.expanduser("~")), "port-forward-manager", "debug.log"
)


def _log(msg):
    try:
        os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except OSError:
        pass


SSDP_ADDR = ("239.255.255.250", 1900)
SEARCH_TARGETS = [
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "upnp:rootdevice",
]
DEVICE_NS = {"d": "urn:schemas-upnp-org:device-1-0"}


class UPnPError(Exception):
    pass


def _text(el):
    return el.text.strip() if el is not None and el.text else None


def _parse_device_description(xml_bytes, base_url):
    root = ET.fromstring(xml_bytes)
    device = root.find("d:device", DEVICE_NS)
    if device is None:
        return None

    info = {
        "friendlyName": _text(device.find("d:friendlyName", DEVICE_NS)),
        "manufacturer": _text(device.find("d:manufacturer", DEVICE_NS)),
        "modelName": _text(device.find("d:modelName", DEVICE_NS)),
        "modelNumber": _text(device.find("d:modelNumber", DEVICE_NS)),
        "control_url": None,
        "service_type": None,
    }

    def walk(dev_el):
        service_list = dev_el.find("d:serviceList", DEVICE_NS)
        if service_list is not None:
            for svc in service_list.findall("d:service", DEVICE_NS):
                st = _text(svc.find("d:serviceType", DEVICE_NS))
                if st and ("WANIPConnection" in st or "WANPPPConnection" in st):
                    control_url = _text(svc.find("d:controlURL", DEVICE_NS))
                    if control_url:
                        info["control_url"] = urljoin(base_url, control_url)
                        info["service_type"] = st
        device_list = dev_el.find("d:deviceList", DEVICE_NS)
        if device_list is not None:
            for child in device_list.findall("d:device", DEVICE_NS):
                walk(child)

    walk(device)
    return info


def _local_ip():
    """Best-guess LAN IP of this machine, used to force multicast out the
    right NIC. On multi-adapter machines (VPN, Bluetooth PAN, virtual
    switches, ...) Windows can otherwise pick an interface with no route
    to the router, silently dropping the SSDP multicast."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def discover_devices(timeout=3.0):
    """Broadcast SSDP M-SEARCH and return a list of IGD devices found."""
    _log("discover_devices: start")
    locations = set()
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        local_ip = _local_ip()
        _log(f"discover_devices: local_ip={local_ip}")
        if local_ip:
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(local_ip))
            except OSError as e:
                _log(f"discover_devices: IP_MULTICAST_IF failed: {e}")

        for st in SEARCH_TARGETS:
            msg = (
                "M-SEARCH * HTTP/1.1\r\n"
                "HOST: 239.255.255.250:1900\r\n"
                'MAN: "ssdp:discover"\r\n'
                "MX: 2\r\n"
                f"ST: {st}\r\n"
                "\r\n"
            ).encode()
            try:
                sock.sendto(msg, SSDP_ADDR)
            except OSError as e:
                _log(f"discover_devices: sendto failed for {st}: {e}")

        deadline = time.time() + timeout
        sock.settimeout(0.5)
        raw_count = 0
        while time.time() < deadline:
            try:
                data, addr = sock.recvfrom(65507)
            except socket.timeout:
                continue
            except OSError as e:
                _log(f"discover_devices: recvfrom error: {e}")
                break
            raw_count += 1
            _log(f"discover_devices: response #{raw_count} from {addr}")
            for line in data.decode(errors="ignore").split("\r\n"):
                if line.lower().startswith("location:"):
                    locations.add(line.split(":", 1)[1].strip())
        sock.close()
        _log(f"discover_devices: raw_responses={raw_count} unique_locations={len(locations)} {sorted(locations)}")
    except Exception:
        _log(f"discover_devices: EXCEPTION\n{traceback.format_exc()}")
        raise

    devices = []
    seen = set()
    for loc in locations:
        info = fetch_device_info(loc)
        if not info or not info.get("control_url"):
            _log(f"discover_devices: {loc} -> no usable control_url (info={info})")
            continue
        if info["control_url"] in seen:
            continue
        seen.add(info["control_url"])
        info["location"] = loc
        devices.append(info)
    _log(f"discover_devices: done, {len(devices)} device(s)")
    return devices


def fetch_device_info(location_url, timeout=3.0):
    """Fetch and parse a device description XML at the given URL."""
    try:
        with urllib.request.urlopen(location_url, timeout=timeout) as resp:
            xml_bytes = resp.read()
        return _parse_device_description(xml_bytes, location_url)
    except Exception as e:
        _log(f"fetch_device_info: {location_url} failed: {e}")
        return None


def _soap_call(control_url, service_type, action, args=None, timeout=5.0):
    args = args or {}
    arg_xml = "".join(f"<{k}>{escape(str(v))}</{k}>" for k, v in args.items())
    body = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        f'<u:{action} xmlns:u="{service_type}">{arg_xml}</u:{action}>'
        "</s:Body></s:Envelope>"
    ).encode("utf-8")

    req = urllib.request.Request(control_url, data=body, method="POST")
    req.add_header("Content-Type", 'text/xml; charset="utf-8"')
    req.add_header("SOAPAction", f'"{service_type}#{action}"')

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _parse_soap_response(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="ignore")
        parsed = _parse_soap_response(detail.encode())
        code = parsed.get("errorCode", "?")
        desc = parsed.get("errorDescription", detail[:200])
        raise UPnPError(f"{action} failed: {code} {desc}") from None


def _parse_soap_response(xml_bytes):
    result = {}
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return result
    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if elem.text and not list(elem):
            result[tag] = elem.text
    return result


class IGDClient:
    """Thin wrapper around one router's WANIPConnection/WANPPPConnection service."""

    def __init__(self, control_url, service_type):
        self.control_url = control_url
        self.service_type = service_type

    def get_external_ip(self):
        result = _soap_call(self.control_url, self.service_type, "GetExternalIPAddress")
        return result.get("NewExternalIPAddress")

    def list_port_mappings(self, max_entries=200):
        entries = []
        for i in range(max_entries):
            try:
                result = _soap_call(
                    self.control_url,
                    self.service_type,
                    "GetGenericPortMappingEntry",
                    {"NewPortMappingIndex": i},
                )
            except UPnPError:
                break
            if not result:
                break
            entries.append(
                {
                    "index": i,
                    "external_port": result.get("NewExternalPort"),
                    "protocol": result.get("NewProtocol"),
                    "internal_client": result.get("NewInternalClient"),
                    "internal_port": result.get("NewInternalPort"),
                    "description": result.get("NewPortMappingDescription"),
                    "enabled": result.get("NewEnabled"),
                    "lease_duration": result.get("NewLeaseDuration"),
                }
            )
        return entries

    def add_port_mapping(
        self,
        external_port,
        internal_port,
        internal_client,
        protocol="TCP",
        description="port-forward-manager",
        lease_duration=0,
    ):
        _soap_call(
            self.control_url,
            self.service_type,
            "AddPortMapping",
            {
                "NewRemoteHost": "",
                "NewExternalPort": external_port,
                "NewProtocol": protocol.upper(),
                "NewInternalPort": internal_port,
                "NewInternalClient": internal_client,
                "NewEnabled": 1,
                "NewPortMappingDescription": description,
                "NewLeaseDuration": lease_duration,
            },
        )

    def delete_port_mapping(self, external_port, protocol="TCP"):
        _soap_call(
            self.control_url,
            self.service_type,
            "DeletePortMapping",
            {
                "NewRemoteHost": "",
                "NewExternalPort": external_port,
                "NewProtocol": protocol.upper(),
            },
        )
