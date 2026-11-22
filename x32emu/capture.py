"""Read-only capture of a real X32 (or of the emulator).

Usage:

    python -m x32emu.capture --ip 192.168.1.50

Guarantees that nothing on the console is changed:
  * only sends read requests: /info, /xinfo, /status, /showdump, /node ,s <path>
    and parameter "get"s (address WITHOUT arguments);
  * the send function refuses any other command or arguments (see _safe);
  * never sends "/", /save, /load, /copy, /delete, nor anything in /-action;
  * /xremote only with --listen (registers the client to receive changes, changes nothing).

Output (folder capture_<date>/):

    info.json              /info, /xinfo, /status
    nodes/<section>.txt    /node replies, one line per node (scene format)
    values/<section>.json  {address: [OSC type, value]} for each parameter
    showdump.txt           reply to /showdump
    no_reply.txt           requests with no reply (probably not in this firmware)
    heard.txt              messages received with --listen
    summary.txt
"""
import argparse, json, pathlib, select, socket, time

from typing import Callable

from . import osc
from .osc import OscArg
from .model import Model

READ_NO_ARGS = {"/info", "/xinfo", "/status", "/showdump", "/xremote"}
EXCLUDE = ("/-action",)          # even a "get" here is not worth the risk


def _safe(address: str, tags: str, args: tuple | list) -> None:
    """Last barrier: only lets read requests out.

    Args:
        address: OSC address about to be sent.
        tags: OSC type tags.
        args: OSC arguments.

    Raises:
        RuntimeError: If the message could change something on the console.
    """
    if address == "/node":
        target = str(args[0]).lstrip("/") if args else ""
        allowed = tags == "s" and len(args) == 1 and not target.startswith("-action")
    elif address in READ_NO_ARGS:
        allowed = not tags and not args
    else:
        allowed = (not tags and not args and address.startswith("/") and address != "/"
                   and not address.startswith(EXCLUDE))

    if not allowed:
        raise RuntimeError(f"refused (not read-only): {address} {tags} {args}")


class Reader:
    """Sends read requests to a console and collects the replies.

    Attributes:
        window: How many requests may be in flight at once.
        timeout: Seconds to wait for a reply before resending.
        retries: How many times a request is sent before giving up.
    """

    def __init__(self, ip: str, port: int = 10023, window: int = 12, timeout: float = 0.4,
                 retries: int = 3):
        """Opens a UDP socket towards the console.

        Args:
            ip: Console IP.
            port: Console OSC port.
            window: See the class attributes.
            timeout: See the class attributes.
            retries: See the class attributes.
        """
        self.destination = (ip, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("0.0.0.0", 0))
        self.window, self.timeout, self.retries = window, timeout, retries
        self.heard = []

    def send(self, address: str, tags: str = "", *args: OscArg) -> None:
        """Sends a message, after checking it is read-only.

        Raises:
            RuntimeError: If the message is not read-only.
        """
        _safe(address, tags, args)
        self.sock.sendto(osc.encode(address, tags, *args), self.destination)

    def receive(self, wait: float) -> tuple[str, str, list[OscArg]] | None:
        """Waits for one message.

        Args:
            wait: Maximum time to wait, in seconds.

        Returns:
            (address, tags, args), or None on timeout or an invalid packet.
        """
        ready, _, _ = select.select([self.sock], [], [], wait)
        if not ready:
            return None

        data, _ = self.sock.recvfrom(65536)
        try:
            return osc.decode(data)
        except Exception:
            return None

    def request_one(self, address: str) -> tuple[str, str, list[OscArg]] | None:
        """Sends one request and waits for the reply with the same address.

        Args:
            address: The address to request.

        Returns:
            The reply (address, tags, args), or None if it never came.
        """
        for _ in range(self.retries):
            self.send(address)
            deadline = time.monotonic() + self.timeout * 2
            while time.monotonic() < deadline:
                message = self.receive(deadline - time.monotonic())
                if message and message[0] == address:
                    return message
        return None

    def request_many(self, requests: list[tuple[str, str, str, tuple]],
                     reply_key: Callable[[tuple], str | None],
                     progress: str = "") -> tuple[dict, list[str], list[tuple]]:
        """Sends many requests, keeping up to ``window`` of them in flight.

        Requests that time out are resent. If some still fail, a second pass sends
        them one at a time and with more patience, to separate lost packets (Wi-Fi
        bursts) from what the console really does not have.

        Args:
            requests: List of (key, address, tags, args).
            reply_key: Function that returns the key a reply answers, or None.
            progress: Label for the progress line; empty for no output.

        Returns:
            A tuple (done, failed, extra): {key: reply}, [keys with no reply],
            [replies that matched no pending request].
        """
        queue = list(requests)
        pending, done, failed, extra = {}, {}, [], []
        total, started, last_shown = len(queue), time.monotonic(), -1

        while queue or pending:
            while queue and len(pending) < self.window:
                key, address, tags, args = queue.pop(0)
                self.send(address, tags, *args)
                pending[key] = (time.monotonic(), 1, address, tags, args)

            message = self.receive(0.05)
            if message:
                key = reply_key(message)
                if key in pending:
                    done[key] = message
                    del pending[key]
                else:
                    extra.append(message)

            now = time.monotonic()
            for key, (sent_at, attempts, address, tags, args) in list(pending.items()):
                if now - sent_at <= self.timeout:
                    continue
                if attempts < self.retries:
                    self.send(address, tags, *args)
                    pending[key] = (now, attempts + 1, address, tags, args)
                else:
                    failed.append(key)
                    del pending[key]

            finished = len(done) + len(failed)
            if progress and finished // 500 != last_shown:
                last_shown = finished // 500
                print(f"\r{progress}: {finished}/{total}", end="", flush=True)

        if progress:
            print(f"\r{progress}: {total}/{total} in {time.monotonic() - started:.0f} s")

        if failed and self.window > 1:
            window, timeout = self.window, self.timeout
            self.window, self.timeout = 1, timeout * 2
            failed_keys = set(failed)
            redo = [request for request in requests if request[0] in failed_keys]
            retry_label = progress and progress + " (retry)"
            retry_done, failed, retry_extra = self.request_many(redo, reply_key, retry_label)
            self.window, self.timeout = window, timeout
            done.update(retry_done)
            extra += retry_extra

        return done, failed, extra


def section(addr: str) -> str:
    """Returns the section name of an address ("/-prefs/rta/gain" -> "prefs")."""
    first = addr.strip("/").split("/")[0]
    return first.lstrip("-") or "root"


def capture(ip: str, port: int, folder: str | pathlib.Path, values: bool = True, nodes: bool = True,
            listen: int = 0, window: int = 12) -> pathlib.Path:
    """Captures the whole state of a console into a folder.

    Args:
        ip: Console IP.
        port: Console OSC port.
        folder: Output folder (created if needed).
        values: Request every parameter one by one.
        nodes: Request every node with /node.
        listen: Seconds to listen for changes with /xremote at the end (0 = no).
        window: Simultaneous requests (lower on Wi-Fi).

    Returns:
        The output folder, as a Path.

    Raises:
        SystemExit: If the console does not reply to /info.
    """
    model = Model()
    reader = Reader(ip, port, window=window)
    folder = pathlib.Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    summary = []

    # 1. identification
    info = {}
    for address in ("/info", "/xinfo", "/status"):
        reply = reader.request_one(address)
        info[address] = reply[2] if reply else None
    if not info["/info"]:
        raise SystemExit(f"No reply from {ip}:{port} to /info. Is the console on and reachable?")
    (folder / "info.json").write_text(json.dumps(info, indent=1, ensure_ascii=False))
    summary.append(f"console: {info['/info']}")

    # 2. node texts
    node_failed = []
    if nodes:
        headers = [header for header, leaves in model.nodes.items()
                   if leaves and not header.startswith(EXCLUDE)]
        requests = [(header, "/node", "s", (header.lstrip("/"),)) for header in headers]

        def node_key(message: tuple) -> str | None:
            """Returns the node a /node reply answers (its first word)."""
            if message[0] != "node" or not message[2]:
                return None
            return str(message[2][0]).split(" ", 1)[0].strip()

        done, node_failed, extra = reader.request_many(requests, node_key, "nodes")

        by_section = {}
        for header in headers:
            if header in done:
                by_section.setdefault(section(header), []).append(str(done[header][2][0]).rstrip("\n"))
        for message in extra:
            if message[0] == "node" and message[2]:
                by_section.setdefault("_unexpected", []).append(str(message[2][0]).rstrip("\n"))

        (folder / "nodes").mkdir(exist_ok=True)
        for section_name, lines in by_section.items():
            (folder / "nodes" / f"{section_name}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        summary.append(f"nodes: {len(done)} replied, {len(node_failed)} no reply")

    # 3. parameter values
    value_failed = []
    if values:
        leaves = [addr for addr in model.params if not addr.startswith(EXCLUDE)]
        requests = [(addr, addr, "", ()) for addr in leaves]
        done, value_failed, _ = reader.request_many(requests, lambda message: message[0], "values")

        by_section = {}
        for addr in leaves:
            if addr in done:
                _, tags, args = done[addr]
                value = args[0] if args else None
                if isinstance(value, bytes):
                    value = value.hex()
                by_section.setdefault(section(addr), {})[addr] = [tags, value]

        (folder / "values").mkdir(exist_ok=True)
        for section_name, section_values in by_section.items():
            body = ",\n".join(f" {json.dumps(addr)}: {json.dumps(entry, ensure_ascii=False)}"
                              for addr, entry in section_values.items())
            (folder / "values" / f"{section_name}.json").write_text("{\n" + body + "\n}\n", encoding="utf-8")
        summary.append(f"values: {len(done)} replied, {len(value_failed)} no reply")

    # 4. show dump
    reader.send("/showdump")
    lines, deadline = [], time.monotonic() + 2.0
    while time.monotonic() < deadline:
        message = reader.receive(deadline - time.monotonic())
        if message and message[0] == "node" and message[2]:
            lines.append(str(message[2][0]).rstrip("\n"))
            deadline = time.monotonic() + 0.5
    (folder / "showdump.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    summary.append(f"showdump: {len(lines)} lines")

    (folder / "no_reply.txt").write_text(
        "".join(f"node {header}\n" for header in node_failed)
        + "".join(f"get {addr}\n" for addr in value_failed))

    # 5. optional listening
    if listen:
        print(f"listening for changes for {listen} s (/xremote)...")
        heard, deadline, renew_at = [], time.monotonic() + listen, 0
        while time.monotonic() < deadline:
            if time.monotonic() > renew_at:
                reader.send("/xremote")
                renew_at = time.monotonic() + 8
            message = reader.receive(0.2)
            if message:
                heard.append(f"{time.strftime('%H:%M:%S')} {message[0]} {message[1]} {message[2]}")
        (folder / "heard.txt").write_text("\n".join(heard) + "\n", encoding="utf-8")
        summary.append(f"heard: {len(heard)} messages")

    (folder / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("\n".join(summary))
    print(f"capture in {folder}")
    return folder


def main() -> None:
    """Command-line entry point (python -m x32emu.capture)."""
    parser = argparse.ArgumentParser(prog="x32emu.capture",
                                     description="Reads the state of an X32 without changing anything.")
    parser.add_argument("--ip", required=True, help="console IP")
    parser.add_argument("--port", type=int, default=10023)
    parser.add_argument("--folder", default=time.strftime("capture_%Y%m%d_%H%M%S"))
    parser.add_argument("--no-values", action="store_true", help="do not request parameter by parameter")
    parser.add_argument("--no-nodes", action="store_true", help="do not request /node")
    parser.add_argument("--listen", type=int, default=0, metavar="SEC",
                        help="at the end, listen for changes for SEC seconds via /xremote")
    parser.add_argument("--window", type=int, default=12,
                        help="simultaneous requests (lower to 4 on Wi-Fi)")
    options = parser.parse_args()

    capture(options.ip, options.port, options.folder, values=not options.no_values,
            nodes=not options.no_nodes, listen=options.listen, window=options.window)


if __name__ == "__main__":
    main()
