"""Voice control over WiFi, by pretending to be a WeMo smart plug.

Why not Bluetooth: the Echo advertises AVRCP and the protocol link is fine,
but Alexa's voice layer never turns "stop"/"pause" into an AVRCP command for a
Bluetooth *source* -- verified with btmon, 116k packets captured and not one
AVCTP frame, while the Echo answered "I'm not sure how to help you with that".

So the trigger comes over the LAN instead. An Echo discovers WeMo sockets
natively, with no Amazon account, no skill and no cloud round-trip:

    "Alexa, turn off adhan"  ->  SSDP-discovered socket  ->  SetBinaryState 0

Standard library only: an SSDP responder on UDP 1900 and a small HTTP server
speaking just enough SOAP.
"""

from __future__ import annotations

import http.server
import logging
import socket
import struct
import threading
import uuid
from typing import Callable

log = logging.getLogger(__name__)

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900


def local_ip() -> str:
    """Address the Echo will be able to reach us on. No traffic is sent."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        return probe.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


SETUP_XML = """<?xml version="1.0"?>
<root xmlns="urn:Belkin:device-1-0">
  <device>
    <deviceType>urn:Belkin:device:controllee:1</deviceType>
    <friendlyName>{name}</friendlyName>
    <manufacturer>Belkin International Inc.</manufacturer>
    <modelName>Emulated Socket</modelName>
    <modelNumber>3.1415</modelNumber>
    <UDN>uuid:Socket-1_0-{serial}</UDN>
    <serialNumber>{serial}</serialNumber>
    <serviceList>
      <service>
        <serviceType>urn:Belkin:service:basicevent:1</serviceType>
        <serviceId>urn:Belkin:serviceId:basicevent1</serviceId>
        <controlURL>/upnp/control/basicevent1</controlURL>
        <eventSubURL>/upnp/event/basicevent1</eventSubURL>
        <SCPDURL>/eventservice.xml</SCPDURL>
      </service>
    </serviceList>
  </device>
</root>
"""

SOAP_RESPONSE = """<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
 s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<s:Body>
<u:{action}Response xmlns:u="urn:Belkin:service:basicevent:1">
<BinaryState>{state}</BinaryState>
</u:{action}Response>
</s:Body>
</s:Envelope>
"""


class AlexaDevice:
    """A single virtual socket. `on_change(True/False)` fires when Alexa turns
    it on or off; `get_state()` answers Alexa asking whether it is on."""

    def __init__(
        self,
        name: str,
        port: int,
        on_change: Callable[[bool], None],
        get_state: Callable[[], bool],
    ) -> None:
        self.name = name
        self.port = port
        self.on_change = on_change
        self.get_state = get_state
        # Stable across restarts so the Echo does not rediscover a new device
        # every reboot.
        self.serial = str(
            uuid.uuid5(uuid.NAMESPACE_DNS, f"adhan-{name}")
        ).replace("-", "")[:12]
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._httpd: http.server.HTTPServer | None = None

    # ------------------------------------------------------------- http

    def _make_handler(self):
        device = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # noqa: A003 - silence stderr
                log.debug("wemo http: " + fmt, *args)

            def _send(self, body: str, content_type: str) -> None:
                payload = body.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):  # noqa: N802 - http.server API
                if self.path.lower().startswith("/setup.xml"):
                    self._send(
                        SETUP_XML.format(name=device.name, serial=device.serial),
                        "text/xml",
                    )
                else:
                    self.send_error(404)

            def do_POST(self):  # noqa: N802 - http.server API
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length).decode("utf-8", "replace")

                if "GetBinaryState" in body:
                    state = 1 if device.get_state() else 0
                    self._send(
                        SOAP_RESPONSE.format(action="GetBinaryState", state=state),
                        "text/xml",
                    )
                    return

                if "SetBinaryState" in body:
                    # Alexa sends <BinaryState>1</BinaryState> for on, 0 for off.
                    turn_on = "<BinaryState>1</BinaryState>" in body.replace(" ", "")
                    log.info(
                        "alexa: turn %s %s", "on" if turn_on else "off", device.name
                    )
                    self._send(
                        SOAP_RESPONSE.format(
                            action="SetBinaryState", state=1 if turn_on else 0
                        ),
                        "text/xml",
                    )
                    try:
                        device.on_change(turn_on)
                    except Exception:  # noqa: BLE001 - never kill the server
                        log.exception("alexa callback failed")
                    return

                self.send_error(400)

        return Handler

    def _serve_http(self) -> None:
        self._httpd = http.server.HTTPServer(("", self.port), self._make_handler())
        self._httpd.serve_forever(poll_interval=0.5)

    # ------------------------------------------------------------- ssdp

    def _serve_ssdp(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except (AttributeError, OSError):
            pass
        sock.bind(("", SSDP_PORT))

        # Join on every interface, not INADDR_ANY. A Pi with both Ethernet and
        # WiFi up sits on one subnet through two interfaces; INADDR_ANY joins
        # the group on whichever one the routing table picks, and M-SEARCH
        # arriving on the other is silently dropped -- so the Echo never
        # discovers us even though the responder is working perfectly.
        joined = []
        for index, name in socket.if_nameindex():
            try:
                sock.setsockopt(
                    socket.IPPROTO_IP,
                    socket.IP_ADD_MEMBERSHIP,
                    struct.pack(
                        "4s4si",
                        socket.inet_aton(SSDP_ADDR),
                        socket.inet_aton("0.0.0.0"),
                        index,
                    ),
                )
                joined.append(name)
            except OSError:
                continue  # interface is down or has no multicast
        log.info("alexa: SSDP listening on %s", ", ".join(joined) or "no interface")
        sock.settimeout(1.0)

        ip = local_ip()
        log.info(
            'alexa: "%s" discoverable at http://%s:%d/setup.xml',
            self.name,
            ip,
            self.port,
        )

        while not self._stop.is_set():
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break

            message = data.decode("utf-8", "replace")
            if not message.startswith("M-SEARCH"):
                continue
            lowered = message.lower()
            # Echo probes for Belkin devices specifically; ssdp:all sweeps too.
            if "belkin" not in lowered and "ssdp:all" not in lowered:
                continue

            response = (
                "HTTP/1.1 200 OK\r\n"
                "CACHE-CONTROL: max-age=86400\r\n"
                "EXT:\r\n"
                f"LOCATION: http://{ip}:{self.port}/setup.xml\r\n"
                'OPT: "http://schemas.upnp.org/upnp/1/0/"; ns=01\r\n'
                f"01-NLS: {self.serial}\r\n"
                "SERVER: Unspecified, UPnP/1.0, Unspecified\r\n"
                "ST: urn:Belkin:device:**\r\n"
                f"USN: uuid:Socket-1_0-{self.serial}::urn:Belkin:device:**\r\n"
                "\r\n"
            )
            try:
                sock.sendto(response.encode("utf-8"), addr)
            except OSError:
                pass

        sock.close()

    # ------------------------------------------------------------ control

    def start(self) -> None:
        for target in (self._serve_http, self._serve_ssdp):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        if self._httpd is not None:
            self._httpd.shutdown()
