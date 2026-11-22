"""UDP server emulating a Behringer X32 (port 10023)."""
import asyncio, json, logging, re, socket, struct, time

from . import osc, datafiles
from .osc import OscArg
from .formats import Value, tokenize
from .model import Model
from .audio import Simulator

log = logging.getLogger("x32emu")

TICK = 0.05                 # 50 ms, meter and subscription rate
TIMEOUT = 10.0              # /xremote, /meters and /subscribe expire after 10 s
MAX_CLIENTS = 4
METER_ARGS = {5: 2, 6: 1}   # number of "selection" ints before the time factor

Client = tuple[str, int]    # (IP, port)


def local_ip() -> str:
    """Returns the IP address of this machine on the local network.

    Returns:
        The IP used to reach other hosts, or "127.0.0.1" if there is no network.
    """
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("10.255.255.255", 1))    # no packet is sent, it only picks a route
        ip = probe.getsockname()[0]
        probe.close()
        return ip
    except OSError:
        return "127.0.0.1"


class Sub:
    """One active subscription (meters, a parameter or a /formatsubscribe).

    Attributes:
        client: (IP, port) of the subscriber.
        name: Name used by /renew to find the subscription.
        kind: "meters", "param" or "format".
        payload: What to send; its shape depends on ``kind``.
        every: Send once every this many ticks.
        count: Ticks since the subscription started.
        expires: time.monotonic() after which it is dropped.
    """

    def __init__(self, client: Client, name: str, kind: str, payload: tuple | str, tf: int):
        """Creates a subscription.

        Args:
            client: (IP, port) of the subscriber.
            name: Name used by /renew.
            kind: "meters", "param" or "format".
            payload: What to send.
            tf: Time factor sent by the client (send every tf ticks).
        """
        self.client, self.name, self.kind, self.payload = client, name, kind, payload
        self.every = max(1, tf)
        self.count = 0
        self.expires = time.monotonic() + TIMEOUT


class X32Emulator(asyncio.DatagramProtocol):
    """The emulated console: receives OSC commands and answers like an X32.

    Attributes:
        model: The parameter tree.
        sim: The level simulation that feeds the meters.
        clients: (IP, port) -> /xremote expiry time.
        subs: Active subscriptions.
        stopped: Set when the emulator should shut down.
    """

    def __init__(self, model: Model, name: str = "X32 Emulator", console: str = "X32",
                 version: str = "4.06", state_file: str | None = None, simulate: bool = True):
        """Creates the emulator.

        Args:
            model: The parameter tree (usually Model().defaults()).
            name: Console name returned by /xinfo and /status.
            console: Console model returned by /info ("X32", "X32RACK", ...).
            version: Firmware version returned by /info.
            state_file: State folder or .json file, or None to not save.
            simulate: False keeps the meters silent.
        """
        self.model, self.sim = model, Simulator(model)
        self.name, self.console, self.version = name, console, version
        self.state_file = state_file
        model.params["/-prefs/_firmware"].value = version
        self.simulate = simulate
        self.ip = local_ip()
        self.clients: dict[Client, float] = {}
        self.subs: list[Sub] = []
        self.transport = None
        self.stopped = asyncio.Event()

    # ---------------- network ----------------
    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        """Keeps the UDP transport (asyncio callback)."""
        self.transport = transport

    def send(self, client: Client, data: bytes) -> None:
        """Sends a packet to one client.

        Args:
            client: (IP, port).
            data: The encoded OSC message.
        """
        log.debug("X-> %s %r", client, data[:80])
        self.transport.sendto(data, client)

    def broadcast(self, data: bytes, exclude: Client | None = None) -> None:
        """Sends a change to every client with /xremote active.

        Args:
            data: The encoded OSC message.
            exclude: The client that made the change, which is not told about it.
        """
        now = time.monotonic()
        for client, expiry in list(self.clients.items()):
            if expiry < now:
                del self.clients[client]
            elif client != exclude:
                self.send(client, data)

    def datagram_received(self, data: bytes, client: Client) -> None:
        """Decodes and handles one incoming packet (asyncio callback).

        Args:
            data: The packet.
            client: (IP, port) of the sender.
        """
        try:
            address, tags, args = osc.decode(data)
        except Exception:
            log.warning("invalid packet from %s: %r", client, data[:60])
            return

        log.debug("->X %s %s %s %s", client, address, tags, args)
        self._raw = data

        try:
            self.dispatch(client, address, tags, args)
        except Exception:
            log.exception("error handling %s", address)

    # ---------------- commands ----------------
    def dispatch(self, client: Client, address: str, tags: str, args: list[OscArg]) -> None:
        """Routes a command to its handler.

        Args:
            client: (IP, port) of the sender.
            address: OSC address.
            tags: OSC type tags.
            args: OSC arguments.
        """
        handler = {
            "/info": self.cmd_info, "/xinfo": self.cmd_xinfo, "/status": self.cmd_status,
            "/xremote": self.cmd_xremote, "/node": self.cmd_node, "/": self.cmd_slash,
            "/meters": self.cmd_meters, "/subscribe": self.cmd_subscribe,
            "/batchsubscribe": self.cmd_batchsubscribe, "/renew": self.cmd_renew,
            "/unsubscribe": self.cmd_unsubscribe, "/formatsubscribe": self.cmd_formatsubscribe,
            "/showdump": self.cmd_showdump,
        }.get(address)

        if handler:
            return handler(client, tags, args)
        if address in self.model.params:
            return self.get_or_set(client, address, tags, args)
        if address in self.model.nodes and args:                 # e.g. /ch/01/eq/1 ,ifff ...
            return self.set_many(client, self.model.nodes[address], args)

        log.info("unknown command: %s %s %s", address, tags, args)

    def cmd_info(self, client: Client, *_) -> None:
        """/info: server version, console model and firmware."""
        self.send(client, osc.encode("/info", "ssss", "V2.07", "osc-server", self.console, self.version))

    def cmd_xinfo(self, client: Client, *_) -> None:
        """/xinfo: IP, name, console model and firmware."""
        self.send(client, osc.encode("/xinfo", "ssss", self.ip, self.name, self.console, self.version))

    def cmd_status(self, client: Client, *_) -> None:
        """/status: state, IP and name."""
        self.send(client, osc.encode("/status", "sss", "active", self.ip, self.name))

    def cmd_xremote(self, client: Client, *_) -> None:
        """/xremote: registers the client to receive changes for TIMEOUT seconds."""
        now = time.monotonic()
        self.clients = {known: expiry for known, expiry in self.clients.items() if expiry > now}

        if client in self.clients or len(self.clients) < MAX_CLIENTS:
            self.clients[client] = now + TIMEOUT
        else:
            log.warning("/xremote refused for %s: there are already %d clients", client, MAX_CLIENTS)

    def cmd_node(self, client: Client, tags: str, args: list[OscArg]) -> None:
        """/node ,s <path>: replies with the node as text."""
        if not args:
            return

        text = self.model.node_text(str(args[0]))
        if text is not None:
            self.send(client, osc.encode("node", "s", text))
        else:
            log.info("/node with no reply: %s", args[0])

    def cmd_slash(self, client: Client, tags: str, args: list[OscArg]) -> None:
        """/ ,s "<path> <values...>": writes a whole node as text."""
        if not args:
            return
        tokens = tokenize(str(args[0]))
        if not tokens:
            return

        header, leaves = self.model.node_for(tokens[0])
        if header is None:
            log.info("/ for unknown node: %s", tokens[0])
            return

        # The C code echoes the received command back to the sender (X32-Edit expects this).
        self.send(client, self._raw)

        for addr, token in zip(leaves, tokens[1:]):
            param = self.model.params[addr]
            try:
                self.apply(client, addr, param.fmt.parse(token))
            except (ValueError, IndexError):
                log.info("invalid value %r for %s", token, addr)   # moves on to the next

    def get_or_set(self, client: Client, address: str, tags: str, args: list[OscArg]) -> None:
        """A parameter address: without arguments it is a get, with one it is a set."""
        param = self.model.params[address]
        if not args:
            self.send(client, osc.encode(address, param.fmt.tag, param.value))
            return

        try:
            self.apply(client, address, param.fmt.coerce(args[0]))
        except (ValueError, TypeError):
            log.info("invalid value %r for %s", args[0], address)

    def set_many(self, client: Client, leaves: list[str], args: list[OscArg]) -> None:
        """Sets several leaves of a node at once (multi-argument set).

        Stops at the first invalid value.
        """
        for addr, value in zip(leaves, args):
            try:
                self.apply(client, addr, self.model.params[addr].fmt.coerce(value))
            except (ValueError, TypeError):
                break

    def apply(self, client: Client | None, addr: str, value: Value) -> None:
        """Stores a new value and runs its side effects.

        Changes are sent to the other /xremote clients. Some parameters also update
        others: solo switches, routing (headamp indices), the RTA source, the effect
        type (resets its parameters) and /-stat/lock (shutdown).

        Args:
            client: Who made the change (not told about it).
            addr: Parameter address.
            value: The value to store, already converted.
        """
        params = self.model.params
        param = params[addr]
        if param.value == value:
            return                      # no change: not propagated (like the original)

        param.value = value
        self.broadcast(osc.encode(addr, param.fmt.tag, value), exclude=client)

        if addr.startswith("/-stat/solosw/"):
            any_solo = int(any(params[f"/-stat/solosw/{index:02d}"].value for index in range(1, 81)))
            if params["/-stat/solo"].value != any_solo:
                params["/-stat/solo"].value = any_solo
                self.broadcast(osc.encode("/-stat/solo", "i", any_solo))

        routing_changed = addr.startswith(("/config/routing/IN/", "/config/userrout/in/"))
        if routing_changed or re.match(r"^/ch/\d\d/config/source$", addr):
            for ha_addr in self.model.update_ha():           # channel -> headamp changed
                self.broadcast(osc.encode(ha_addr, "i", params[ha_addr].value))

        if addr == "/-action/setrtasrc":                    # X32-Edit picks the RTA source this way
            rta_source = params["/-stat/rtasource"]
            if rta_source.value != value:
                rta_source.value = value
                self.broadcast(osc.encode("/-stat/rtasource", "i", value))

        fx_type_match = re.match(r"^/fx/([1-8])/type$", addr)
        if fx_type_match:                                   # new effect: parameters go back to neutral
            slot = int(fx_type_match.group(1))
            self.model.fx_reset(slot)
            for index in range(len(self.model.fx_spec(slot))):
                par_addr = f"/fx/{slot}/par/{index + 1:02d}"
                fx_param = params[par_addr]
                self.broadcast(osc.encode(par_addr, fx_param.fmt.tag, fx_param.value), exclude=client)

        if addr == "/-stat/lock" and value == 2:
            log.info("shutdown requested (/-stat/lock 2)")
            self.stopped.set()

    # ---------------- subscriptions and meters ----------------
    def _add_sub(self, sub: Sub) -> None:
        """Adds a subscription, replacing one with the same client and name."""
        def same(other: Sub) -> bool:
            """Whether another subscription has the same client and name."""
            return other.client == sub.client and other.name == sub.name

        if not any(same(other) for other in self.subs):
            detail = sub.payload if sub.kind != "format" else sub.name
            log.info("new subscription from %s: %s %s", sub.client, sub.kind, detail)

        self.subs = [other for other in self.subs if not same(other)]
        self.subs.append(sub)

    def cmd_meters(self, client: Client, tags: str, args: list[OscArg]) -> None:
        """/meters ,s[i...] /meters/N [selection...] [time factor]."""
        if not args or not isinstance(args[0], str):
            return
        meters_match = re.fullmatch(r"/?meters/(\d+)", args[0])
        if not meters_match:
            return

        meter_id = int(meters_match.group(1))
        ints = [arg for arg in args[1:] if isinstance(arg, int)]
        selection_count = METER_ARGS.get(meter_id, 0)
        selection = ints[:selection_count]
        tf = ints[selection_count] if len(ints) > selection_count else 1

        payload = (f"/meters/{meter_id}", meter_id, selection)
        self._add_sub(Sub(client, f"meters/{meter_id}", "meters", payload, tf))

    def cmd_batchsubscribe(self, client: Client, tags: str, args: list[OscArg]) -> None:
        """/batchsubscribe ,ssiii <alias> </meters/N> <i0> <i1> <tf> (meters only)."""
        if len(args) >= 2 and isinstance(args[1], str) and args[1].startswith("/meters/"):
            meter_id = int(args[1].split("/")[-1])
            ints = [arg for arg in args[2:] if isinstance(arg, int)]
            tf = ints[-1] if ints else 1
            payload = (args[0], meter_id, ints[:1])
            self._add_sub(Sub(client, args[0].lstrip("/"), "meters", payload, tf))
        else:
            log.info("non-meter /batchsubscribe not supported yet: %s", args)

    def cmd_formatsubscribe(self, client: Client, tags: str, args: list[OscArg]) -> None:
        """/formatsubscribe ,ss[s...]iii <alias> <cmd> [<cmd>...] <i0> <i1> <tf>.

        The '*' in each command are replaced by the indices i0..i1 (zero-padded to
        the width of the '*'). The reply, sent with the <alias> address, is a
        blob: <number of values LE> + 32-bit LE values.
        """
        strings = [arg for arg in args if isinstance(arg, str)]
        ints = [arg for arg in args if isinstance(arg, int)]
        if len(strings) < 2:
            return

        alias, commands = strings[0], strings[1:]
        first, last, tf = (ints + [0, 0, 1])[:3] if len(ints) >= 3 else (0, 0, 1)

        addrs = []
        for command in commands:
            stars = command.count("*")
            if stars:
                pattern = "*" * stars
                addrs += [command.replace(pattern, f"{index:0{stars}d}") for index in range(first, last + 1)]
            else:
                addrs.append(command)

        missing = [addr for addr in addrs if addr not in self.model.params]
        if missing:
            log.info("/formatsubscribe %s: %d unknown address(es), e.g.: %s",
                     alias, len(missing), missing[:3])

        self._add_sub(Sub(client, alias.lstrip("/"), "format", (alias, addrs), tf))

    def format_blob(self, addrs: list[str]) -> bytes:
        """Builds the /formatsubscribe blob for a list of addresses.

        Args:
            addrs: The parameter addresses.

        Returns:
            <count LE> followed by one 32-bit LE value per address.
        """
        values = []
        for addr in addrs:
            param = self.model.params.get(addr)
            if param is None or param.fmt.tag == "s":
                values.append(struct.pack("<i", 0))      # real format for strings: unknown
            elif param.fmt.tag == "i":
                values.append(struct.pack("<i", int(param.value)))
            else:
                values.append(struct.pack("<f", float(param.value)))
        return struct.pack("<i", len(values)) + b"".join(values)

    def cmd_showdump(self, client: Client, *_) -> None:
        """/showdump: show header, then only the cues, scenes and snippets that exist.

        Like the real console, e.g. '/-show/showfile/cue/000 100 "1 Culto" 0 2 -1 0 1 0 0'.
        """
        params = self.model.params
        lines = [self.model.node_text("-show/showfile/show")]

        for index in range(500):
            base = f"/-show/showfile/cue/{index:03d}"
            if base + "/numb" in params and (params[base + "/numb"].value or params[base + "/name"].value):
                lines.append(self.model.node_text(base[1:]))

        for kind in ("scene", "snippet"):
            for index in range(100):
                base = f"/-show/showfile/{kind}/{index:03d}"
                if base + "/hasdata" in params and params[base + "/hasdata"].value:
                    lines.append(self.model.node_text(base[1:]))

        for text in lines:
            if text:
                self.send(client, osc.encode("node", "s", text))

    def cmd_subscribe(self, client: Client, tags: str, args: list[OscArg]) -> None:
        """/subscribe ,si <address> [time factor]."""
        if args and args[0] in self.model.params:
            tf = args[1] if len(args) > 1 and isinstance(args[1], int) else 1
            self._add_sub(Sub(client, args[0], "param", args[0], tf))

    def cmd_renew(self, client: Client, tags: str, args: list[OscArg]) -> None:
        """/renew [name]: extends one subscription, or all of the client's."""
        name = str(args[0]).lstrip("/") if args else ""
        for sub in self.subs:
            if sub.client == client and (not name or sub.name.lstrip("/") == name):
                sub.expires = time.monotonic() + TIMEOUT

    def cmd_unsubscribe(self, client: Client, *_) -> None:
        """/unsubscribe: drops every subscription of the client."""
        self.subs = [sub for sub in self.subs if sub.client != client]

    async def ticker(self) -> None:
        """Runs every TICK: advances the simulation and sends subscriptions."""
        loop = asyncio.get_running_loop()
        next_tick = loop.time()

        while not self.stopped.is_set():
            if self.simulate:
                self.sim.step(TICK)

            now = time.monotonic()
            self.subs = [sub for sub in self.subs if sub.expires > now]

            for sub in self.subs:
                sub.count += 1
                if sub.count % sub.every:
                    continue
                self._send_subscription(sub)

            next_tick += TICK
            await asyncio.sleep(max(0.0, next_tick - loop.time()))

    def _send_subscription(self, sub: Sub) -> None:
        """Sends the current data of one subscription."""
        if sub.kind == "format":
            alias, addrs = sub.payload
            self.send(sub.client, osc.encode(alias, "b", self.format_blob(addrs)))

        elif sub.kind == "meters":
            addr, meter_id, selection = sub.payload
            if meter_id == 15:       # RTA: 100 LE shorts = dB * 256
                shorts = [int(round(db * 256)) for db in self.sim.rta()]
                blob = osc.meter_blob(shorts, shorts=True)
            else:
                blob = osc.meter_blob(self.sim.meters(meter_id, selection))
            self.send(sub.client, osc.encode(addr, "b", blob))

        else:
            param = self.model.params[sub.payload]
            self.send(sub.client, osc.encode(sub.payload, param.fmt.tag, param.value))

    # ---------------- state ----------------
    def load_state(self) -> None:
        """Loads the saved state into the model, if there is one."""
        if not self.state_file:
            return
        try:
            state = datafiles.load_state(self.state_file)
        except FileNotFoundError:
            return

        for addr, value in state.items():
            if addr in self.model.params:
                self.model.params[addr].value = value
        self.model.update_ha()
        log.info("state loaded from %s", self.state_file)

    def save_state(self) -> None:
        """Saves the model state to the state folder (or old single file)."""
        if not self.state_file:
            return

        values = {addr: param.value for addr, param in self.model.params.items()}
        if str(self.state_file).endswith(".json"):      # old format: a single file
            datafiles.write_atomic(self.state_file, json.dumps(values, ensure_ascii=False, indent=0))
        else:
            datafiles.save_state(values, self.state_file)
            log.info("state saved to %s", self.state_file)


async def run(host: str = "0.0.0.0", port: int = 10023, **options) -> None:
    """Runs the emulator until it is stopped, then saves the state.

    Args:
        host: Address to listen on.
        port: UDP port.
        **options: Passed to X32Emulator (name, console, version, state_file, simulate).
    """
    emulator = X32Emulator(Model().defaults(), **options)
    emulator.load_state()

    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: emulator, local_addr=(host, port))
    log.info("X32 emulated on %s:%d (advertised IP %s)", host, port, emulator.ip)
    task = asyncio.create_task(emulator.ticker())

    try:
        await emulator.stopped.wait()
    finally:
        task.cancel()
        transport.close()
        emulator.save_state()
