"""The X32 parameter tree.

The tree is loaded from the tables extracted from the original C code, with the
corrections described in DIFFERENCES.md applied on top.
"""
import json, re, pathlib
from dataclasses import dataclass

from . import formats
from .datafiles import read_tables, read_fx

FX_PAR = re.compile(r"^/fx/([1-8])/par/(\d\d)$")


def fx_formatter(spec: dict) -> formats.Fmt:
    """Builds the format of one effect parameter from its spec in data/fx/.

    Args:
        spec: Dict with "kind" ("enum", "log" or "lin") and the range fields.

    Returns:
        A formats.Fmt instance.
    """
    if spec["kind"] == "enum":
        return formats.Enum(spec["enum"])

    if spec["kind"] == "log":
        digits_after_k = spec["dec"] + 1 if spec["freq"] else None
        return formats.Log(spec["min"], spec["max"], spec["steps"], spec["dec"],
                           freq=spec["freq"], kdig=digits_after_k)

    low, high = spec["min"], spec["max"]
    step = (high - low) / (spec["steps"] - 1)
    return formats.Lin(low, high, step, spec["dec"])      # the console shows no "+" on effects


class FxPar(formats.Fmt):
    """Format of /fx/N/par/MM, which depends on the effect type loaded in the slot."""

    def __init__(self, model: "Model", slot: int, idx: int):
        """Creates the format of one effect parameter.

        Args:
            model: The Model, to look up the current effect type.
            slot: Effect slot, 1..8.
            idx: Parameter index inside the effect, 0-based.
        """
        self.model, self.slot, self.idx = model, slot, idx

    def current(self) -> formats.Fmt:
        """Returns the format for the effect currently loaded in the slot."""
        spec = self.model.fx_spec(self.slot)
        if self.idx < len(spec):
            return self.model.fx_fmt(self.model.fx_type_name(self.slot), self.idx)
        return formats.Unused()

    @property
    def tag(self) -> str:
        """OSC type tag of the current effect parameter format."""
        return self.current().tag

    def fmt(self, value: formats.Value) -> str:
        """Formats the value with the current effect parameter format."""
        return self.current().fmt(value)

    def padded(self, value: formats.Value) -> str:
        """Formats and pads the value with the current effect parameter format."""
        return self.current().padded(value)

    def parse(self, text: str) -> formats.Value:
        """Parses text with the current effect parameter format."""
        return self.current().parse(text)

    def coerce(self, value: formats.Value) -> formats.Value:
        """Converts a received value with the current effect parameter format.

        Some clients send enum effect parameters as a 0..1 float; those are mapped to
        the nearest index.
        """
        current = self.current()

        if isinstance(current, formats.Enum) and isinstance(value, float):
            fraction = min(1.0, max(0.0, value))
            return int(round(fraction * (len(current.names) - 1)))

        if current.tag == "f" and isinstance(value, int):
            value = float(value)
        return current.coerce(value)

    def value(self, fraction: float) -> float:
        """Returns the physical value, when the current format has one."""
        current = self.current()
        return current.value(fraction) if hasattr(current, "value") else fraction


# --- Corrections to the C tables (see DIFFERENCES.md) ----------------------
# Effect type order: the C string array does not match the C enum itself,
# nor the document, nor x32-osc (tested on FW 4.06).
FX_TYPES_1 = ["HALL", "AMBI", "RPLT", "ROOM", "CHAM", "PLAT", "VREV", "VRM", "GATE", "RVRS",
              "DLY", "3TAP", "4TAP", "CRS", "FLNG", "PHAS", "DIMC", "FILT", "ROTA", "PAN", "SUB",
              "D/RV", "CR/R", "FL/R", "D/CR", "D/FL", "MODD", "GEQ2", "GEQ", "TEQ2", "TEQ", "DES2",
              "DES", "P1A", "P1A2", "PQ5", "PQ5S", "WAVD", "LIM", "CMB", "CMB2", "FAC", "FAC1M",
              "FAC2", "LEC", "LEC2", "ULC", "ULC2", "ENH2", "ENH", "EXC2", "EXC", "IMG", "EDI",
              "SON", "AMP2", "AMP", "DRV2", "DRV", "PIT2", "PIT"]
FX_TYPES_2 = ["GEQ2", "GEQ", "TEQ2", "TEQ", "DES2", "DES", "P1A", "P1A2", "PQ5", "PQ5S", "WAVD",
              "LIM", "FAC", "FAC1M", "FAC2", "LEC", "LEC2", "ULC", "ULC2", "ENH2", "ENH", "EXC2",
              "EXC", "IMG", "EDI", "SON", "AMP2", "AMP", "DRV2", "DRV", "PHAS", "FILT", "PAN", "SUB"]


def _user_blocks(prefix: str, first: int, stop: int, size: int) -> list[str]:
    """Names of routing blocks, e.g. _user_blocks("UIN", 1, 33, 8) -> ["UIN1-8", ..., "UIN25-32"]."""
    return [f"{prefix}{start}-{start + size - 1}" for start in range(first, stop, size)]


ENUM_FIXES = {
    "Xgmode": ["EXP2", "EXP3", "EXP4", "GATE", "DUCK"],
    "Sfxtyp1": FX_TYPES_1,
    "Sfxtyp2": FX_TYPES_2,
}

# FW4 routing: "user in/out" blocks missing from the C code (source: x32-osc, FW 4.06)
ENUM_APPEND = {
    "XRtgin": _user_blocks("UIN", 1, 33, 8),
    "XRtina": ["UIN1-2", "UIN1-4", "UIN1-6"],
    "XRtaea": _user_blocks("UOUT", 1, 49, 8) + _user_blocks("UIN", 1, 33, 8),
    "XRout1": _user_blocks("UOUT", 1, 48, 4)[::2] + _user_blocks("UIN", 1, 32, 4)[::2],
    "XRout5": _user_blocks("UOUT", 5, 49, 4)[::2] + _user_blocks("UIN", 5, 33, 4)[::2],
}

# Parameters with the wrong enum list in the C code
PARAM_ENUM_FIXES = [
    # real capture: "/-prefs/rta ... 3s", "/-prefs/key QWERTY", "/-stat ... OFF" (lock)
    (r"^/-prefs/rta/peakhold$", ["OFF", "1s", "2s", "3s", "4s", "5s", "6s", "7s", "8s"]),
    (r"^/-prefs/key/layout$", ["QWERTY", "AZERTY", "QWERTZ"]),
    (r"^/-stat/lock$", ["OFF", "ON", "SHUTDOWN"]),
    (r"^/bus/\d\d/mix/\d\d/type$", ["IN/LC", "<-EQ", "EQ->", "PRE", "POST"]),
    (r"^/bus/\d\d/dyn/ratio$", "Xdyrat"),
    (r"^/main/(st|m)/mix/\d\d/type$", ["IN/LC", "<-EQ", "EQ->", "PRE", "POST"]),
]

# Parameters that exist in FW 4.x according to x32-osc but are missing from the C code
# (address, type, enum, insert after) — the position matters for the /node text
EXTRA_PARAMS = (
    # real capture (X32P 4.13): "/main/st/mix/01 ON 0.0 +0 POST 1"
    [(f"/main/{main}/mix/{send:02d}/panFollow", "I32", None, f"/main/{main}/mix/{send:02d}/type")
     for main in ("st", "m") for send in (1, 3, 5)]
    # real capture: "/config/dp48 %0000 0 AESA"
    + [("/config/dp48/aesAB", "E32", ["AESA", "AESB"], "/config/dp48/broadcast")]
    + [(f"/config/dp48/link/{link:02d}", "E32", "OffOn", None) for link in range(1, 25)]
    # X32-Edit asks for /-usb/dir/...; the C code only has /-usb/dirpos and /-usb/maxpos (the console does not reply)
    + [("/-usb/dir/dirpos", "I32", None, None), ("/-usb/dir/maxpos", "I32", None, None)]
    # the C code writes "/-action/platrack"; the document says "playtrack"
    + [("/-action/playtrack", "I32", None, None)]
    # real capture: the show header ends with the version ("4.06"); the parameter name is mine
    + [("/-show/showfile/show/version", "S32", None, "/-show/showfile/show/effects")]
    # real capture: /-prefs and /-prefs/rta have 2 more values at the end; names unknown
    # (these addresses are mine, the console does not reply to "get" on them)
    + [("/-prefs/_unknown1", "I32", None, "/-prefs/rec_control"),
       ("/-prefs/_firmware", "S32", None, "/-prefs/_unknown1"),
       ("/-prefs/rta/_unknown1", "I32", None, "/-prefs/rta/peakhold"),
       ("/-prefs/rta/_unknown2", "I32", None, "/-prefs/rta/_unknown1")]
)

# C frequency table entries that differ from the real console (observed)
FREQ_TABLE_FIXES = {"f201": {132: "1k91"}}

# C addresses a real X32P 4.13 does not reply to (capture from 2026-11)
REMOVED = re.compile(r"^/outputs/rec/\d\d/invert$|^/-prefs/key/\d\d$|^/-prefs/iQ(/|$)|"
                     r"^/-usb/(maxpos|dirpos)$|^/-usb/dir/000(/|$)|^/insert(/|$)|^/undo(/|$)|"
                     r"^/-stat/urec$")

# They exist (reply to "get") but do not appear in the node text
NOT_IN_NODE = re.compile(r"^/-stat/dcaspill$|^/-usb/dir/")

# Leaf order in a node: the C code has 1-4, 9-12, 5-8, 13-16; X32-Edit (and x32-osc) use
# 1-4, 5-8, 9-12, 13-16 when sending the scene.
NODE_ORDER_FIXES = {
    "/config/routing/OUT": ["/config/routing/OUT/1-4", "/config/routing/OUT/5-8",
                            "/config/routing/OUT/9-12", "/config/routing/OUT/13-16"],
}

# --- Float conversions per address (transcribed from function_node in the C code) ---
FLOAT_FORMATS = [
    (r"/mix/fader$|^/dca/\d/fader$", formats.Level(1024)),
    (r"/level$|/mlevel$", formats.Level(161)),
    (r"/preamp/trim$", formats.Lin(-18, 18, 0.25, 1, True, zero_sign=True, hup=True)),
    (r"/preamp/hpf$", formats.Log(20, 400, 101, style="hpf", width=3)),
    (r"/delay/time$|/delaytime$", formats.Lin(0.3, 500, 0.1, 1, width=5)),
    (r"/gate/thr$", formats.Lin(-80, 0, 0.5, 1)),
    (r"/gate/range$", formats.Lin(3, 60, 1, 1)),
    (r"/(gate|dyn)/attack$", formats.Lin(0, 120, 1, 0)),
    (r"/(gate|dyn)/hold$", formats.Log(0.02, 2000, 101, style="sig3", width=4)),
    (r"/(gate|dyn)/release$", formats.Log(5, 4000, 101, 0, width="release")),
    (r"/filter/f$|/eq/\d/f$", formats.Log(20, 20000, 201, freq=True)),
    (r"/dyn/thr$", formats.Lin(-60, 0, 0.5, 1)),
    (r"/dyn/knee$", formats.Lin(0, 5, 1, 0)),
    (r"/dyn/mgain$", formats.Lin(0, 24, 0.5, 2, dec_big=1)),
    (r"/dyn/mix$", formats.Lin(0, 100, 5, 0)),
    (r"/eq/\d/g$", formats.Lin(-15, 15, 0.25, 2, True, zero_sign=True, dec_big=1)),
    (r"/eq/\d/q$", formats.Log(10, 0.3, 72, style="q", width=3)),
    (r"/pan$", formats.Lin(-100, 100, 2, 0, True, zero_sign=True)),
    (r"^/headamp/\d+/gain$", formats.Lin(-12, 60, 0.5, 1, True, zero_sign=True)),
    (r"/automix/weight$", formats.Lin(-12, 12, 0.5, 1, True, zero_sign=True, width=5)),
    (r"/solo/sourcetrim$", formats.Lin(-18, 18, 0.5, 1, True)),
    (r"/solo/dimatt$", formats.Lin(-40, 0, 1, 0)),
    (r"^/config/osc/f[12]$", formats.Log(20, 20000, 121, freq=True)),
    (r"^/config/tape/gain[LR]$", formats.Lin(-6, 24, 0.5, 1)),
    (r"^/-prefs/(bright|ledbright)$", formats.Lin(10, 100, 5, 0)),
    (r"^/-prefs/lcdcont$", formats.Lin(0, 100, 2, 0)),
    (r"^/-prefs/lamp$", formats.Lin(10, 100, 10, 0)),
    (r"^/-prefs/rta/gain$", formats.Lin(0, 60, 6, 0)),
    (r"^/-prefs/rta/decay$", formats.Log(0.25, 16, 19, 2)),
]

BITMAP_WIDTHS = [(r"^/-prefs/haflags$", 4), (r"^/-prefs/remote/ioenable$", 14),
                 (r"^/-prefs/rta/options$", 6), (r"^/-show/showfile/scene/\d+/safes$", 9),
                 (r"^/config/dp48/scope$", 4), (r"^/-libs/(ch|fx|r)/\d+/flags$", 16),
                 (r"/grp/dca$", 8), (r"/grp/mute$", 6), (r"/destmap$", 18)]

TYPE_TAG = {"I32": "i", "E32": "i", "P32": "i", "F32": "f", "FX32": "f", "S32": "s"}


@dataclass
class Param:
    """One parameter of the tree.

    Attributes:
        addr: OSC address, e.g. "/ch/01/mix/fader".
        fmt: How the value is converted to and from text.
        value: The stored OSC value.
    """

    addr: str
    fmt: formats.Fmt
    value: formats.Value


class Model:
    """The whole X32 parameter tree and its /node groups.

    Attributes:
        enums: Enum name -> list of texts, after the corrections.
        fx: Effect type name -> list of parameter specs.
        params: Address -> Param, for every leaf parameter.
        order: Every address (headers and leaves) in the original tree order.
        headers: Addresses of node headers (F_FND entries in the C tables).
        nodes: Node header -> list of leaf addresses shown in its /node text.
        inferred: (address, enum name) pairs whose enum list had to be guessed.
    """

    def __init__(self) -> None:
        """Loads the tables and applies the corrections."""
        params, enums = read_tables()

        self.enums = {name: [text.strip() for text in values] for name, values in enums.items()}
        self.enums.update(ENUM_FIXES)
        for name, extra in ENUM_APPEND.items():
            self.enums[name] = self.enums[name] + extra

        # Exact C frequency tables (f201, f121): the 2nd real capture confirmed them
        # ("1k21", "403.7", "6k39"). The low cut (f101) uses integers on the console ("49", "173").
        for name, fixes in FREQ_TABLE_FIXES.items():
            table = list(self.enums[name])
            for index, text in fixes.items():
                table[index] = text
            self.enums[name] = table

        for _, fmt in FLOAT_FORMATS:
            if isinstance(fmt, formats.Log) and fmt.freq and fmt.step_count in (201, 121):
                fmt.table = self.enums.get(f"f{fmt.step_count}")

        self.fx = read_fx()
        self._fx_fmt_cache = {}
        self.params: dict[str, Param] = {}
        self.order: list[str] = []
        self.headers: set[str] = set()

        # Some C entries have no enum list (or are I32 with a list): infer the list
        # from parameters with the same address pattern.
        self.enum_by_pattern = {}
        for param in params:
            if param["enum"]:
                self.enum_by_pattern.setdefault(re.sub(r"\d+", "N", param["addr"]), param["enum"])
        self.inferred = []

        params = [param for param in params if not REMOVED.match(param["addr"])]

        for addr, type_name, enum, after in EXTRA_PARAMS:
            if any(param["addr"] == addr for param in params):
                continue
            item = {"addr": addr, "type": type_name, "node": False, "enum": enum}
            position = next((index for index, param in enumerate(params) if param["addr"] == after), None)
            params.insert(position + 1 if position is not None else len(params), item)

        for param in params:
            self._add(param["addr"], param["type"], "F_FND" if param["node"] else "F_XET", param["enum"])

        self._build_nodes()

    # ---- headamps ----
    def _user_in_ha(self, user_input: int) -> int:
        """Returns the headamp behind a FW 4 "user in" (UIN) input.

        /config/userrout/in/NN picks the source of each UIN: 1-32 local inputs,
        33-80 AES50 A, 81-128 AES50 B (the rest has no headamp). Confirmed on an
        X32P 4.13: UIN1 = 33 gives headamp 32 (A1).

        Args:
            user_input: UIN number, 1-based.

        Returns:
            The headamp index, or -1 if the source has no headamp.
        """
        param = self.params.get(f"/config/userrout/in/{user_input:02d}")
        source = param.value if param else 0

        if 1 <= source <= 32:
            return source - 1
        if 33 <= source <= 80:
            return 32 + source - 33
        if 81 <= source <= 128:
            return 80 + source - 81
        return -1

    def _block_ha(self, block: str, offset: int) -> int:
        """Returns the headamp of one input inside a routing block.

        Args:
            block: Block name, e.g. "AN1-8", "A9-16" or "UIN1-8".
            offset: Input inside the block, 0-based.

        Returns:
            The headamp index, or -1 if the source has no headamp (card, line aux).
        """
        user_match = re.fullmatch(r"UIN(\d+)-\d+", block)
        if user_match:
            return self._user_in_ha(int(user_match.group(1)) + offset)

        for prefix, base in (("AN", 0), ("A", 32), ("B", 80)):
            block_match = re.fullmatch(prefix + r"(\d+)-\d+", block)
            if block_match:
                return base + int(block_match.group(1)) - 1 + offset
        return -1

    def ha_index(self, channel: int) -> int:
        """Computes /-ha/NN/index: the headamp feeding a channel.

        Args:
            channel: 0..31 for channels, 32..39 for aux inputs.

        Returns:
            The headamp index, or -1 if there is none.
        """
        if channel < 32:
            source = self.params[f"/ch/{channel + 1:02d}/config/source"].value
            if not 1 <= source <= 32:
                return -1

            input_index = source - 1
            block = ["1-8", "9-16", "17-24", "25-32"][input_index // 8]
            routing = self.params[f"/config/routing/IN/{block}"]
            return self._block_ha(routing.fmt.fmt(routing.value), input_index % 8)

        routing = self.params["/config/routing/IN/AUX"]
        block_name, aux_index = routing.fmt.fmt(routing.value), channel - 32
        aux_match = re.fullmatch(r"(AN|A|B|UIN)1-(\d)", block_name)
        if aux_match and aux_index < int(aux_match.group(2)):
            return self._block_ha(f"{aux_match.group(1)}1-8", aux_index)
        return -1

    def update_ha(self) -> list[str]:
        """Recomputes every /-ha/00..39/index.

        Returns:
            The addresses whose value changed.
        """
        changed = []
        for channel in range(40):
            addr = f"/-ha/{channel:02d}/index"
            if addr in self.params:
                headamp = self.ha_index(channel)
                if self.params[addr].value != headamp:
                    self.params[addr].value = headamp
                    changed.append(addr)
        return changed

    # ---- effects ----
    def fx_type_name(self, slot: int) -> str:
        """Returns the name of the effect loaded in a slot (1..8), e.g. "HALL"."""
        param = self.params[f"/fx/{slot}/type"]
        return param.fmt.fmt(param.value)

    def fx_spec(self, slot: int) -> list[dict]:
        """Returns the parameter specs of the effect loaded in a slot (1..8)."""
        return self.fx.get(self.fx_type_name(slot), [])

    def fx_fmt(self, name: str, idx: int) -> formats.Fmt:
        """Returns the (cached) format of one parameter of an effect type.

        Args:
            name: Effect type name.
            idx: Parameter index, 0-based.
        """
        key = (name, idx)
        if key not in self._fx_fmt_cache:
            self._fx_fmt_cache[key] = fx_formatter(self.fx[name][idx])
        return self._fx_fmt_cache[key]

    def fx_reset(self, slot: int) -> None:
        """Puts the parameters of a slot back to a neutral value.

        Called when the effect type changes (the real default values of each
        effect are not documented).

        Args:
            slot: Effect slot, 1..8.
        """
        spec = self.fx_spec(slot)
        for index in range(64):
            param = self.params[f"/fx/{slot}/par/{index + 1:02d}"]
            if index < len(spec):
                is_enum = spec[index]["kind"] == "enum"
                param.value = 0 if is_enum else param.fmt.coerce(0.5)   # console step
            else:
                param.value = 0.0

    def _formatter(self, addr: str, type_name: str, enum: str | list[str] | None) -> formats.Fmt:
        """Chooses the format of a parameter.

        Args:
            addr: Parameter address.
            type_name: Type in the C tables (I32, E32, P32, F32, FX32, S32).
            enum: Enum list name or list from the C tables, or None.

        Returns:
            A formats.Fmt instance.
        """
        fx_match = FX_PAR.match(addr)
        if fx_match:
            return FxPar(self, int(fx_match.group(1)), int(fx_match.group(2)) - 1)

        for pattern, fix in PARAM_ENUM_FIXES:
            if re.search(pattern, addr):
                return formats.Enum(fix if isinstance(fix, list) else self.enums[fix])

        if isinstance(enum, list):
            return formats.Enum(enum)
        if type_name in ("E32", "I32") and enum in self.enums:
            return formats.Enum(self.enums[enum])

        if type_name == "E32" and not enum:
            guess = self.enum_by_pattern.get(re.sub(r"\d+", "N", addr))
            if guess in self.enums:
                self.inferred.append((addr, guess))
                return formats.Enum(self.enums[guess])

        if type_name == "S32":
            return formats.Str()

        if type_name in ("P32", "I32"):
            for pattern, bits in BITMAP_WIDTHS:
                if re.search(pattern, addr):
                    return formats.Bitmap(bits)
            if type_name == "P32":
                return formats.Int()

        if type_name in ("F32", "FX32"):
            for pattern, fmt in FLOAT_FORMATS:
                if re.search(pattern, addr):
                    return fmt
            return formats.Raw()

        return formats.Int()

    def _add(self, addr: str, type_name: str, flags: str, enum: str | list[str] | None) -> None:
        """Adds one entry of the C tables to the tree.

        Args:
            addr: Address.
            type_name: Type in the C tables.
            flags: "F_FND" for a node header, anything else for a leaf.
            enum: Enum list name or list, or None.
        """
        self.order.append(addr)
        if flags == "F_FND":
            self.headers.add(addr)
            return

        fmt = self._formatter(addr, type_name, enum)
        if isinstance(fmt, formats.Str):
            initial = ""
        else:
            initial = 0.0 if fmt.tag == "f" else 0
        self.params[addr] = Param(addr, fmt, initial)

    def _build_nodes(self) -> None:
        """Groups the leaves under node headers.

        Each F_FND header groups the leaves that follow it up to the next header.
        """
        self.nodes: dict[str, list[str]] = {}
        current = None

        for addr in self.order:
            if addr in self.headers:
                current = addr
                self.nodes.setdefault(addr, [])
            elif current and addr.startswith(current + "/") and not NOT_IN_NODE.match(addr):
                self.nodes[current].append(addr)

        for header, order in NODE_ORDER_FIXES.items():
            if sorted(self.nodes.get(header, [])) == sorted(order):
                self.nodes[header] = order

    def node_for(self, path: str) -> tuple[str | None, list[str]]:
        """Resolves a /node request.

        If the header only contains sub-nodes, goes down to the first one with
        leaves (like the C code does when skipping consecutive F_FND entries).

        Args:
            path: Node or parameter path, with or without the leading "/".

        Returns:
            A tuple (header, leaf addresses), or (None, []) if the path is unknown.
        """
        path = "/" + path.lstrip("/")
        if path in self.params:
            return path, [path]
        if path not in self.nodes:
            return None, []

        index = self.order.index(path)
        while (not self.nodes.get(self.order[index]) and index + 1 < len(self.order)
               and self.order[index + 1] in self.headers):
            index += 1

        header = self.order[index]
        return header, self.nodes.get(header, [])

    def node_text(self, path: str) -> str | None:
        """Builds the /node reply text for a path.

        Args:
            path: Node or parameter path.

        Returns:
            The line the console would send ("/ch/01/mix ON  -oo ...\\n"), or None.
        """
        header, leaves = self.node_for(path)
        if header is None:
            return None

        values = [self.params[addr].fmt.padded(self.params[addr].value) for addr in leaves]
        return " ".join([header] + values) + "\n"

    def defaults(self) -> "Model":
        """Sets plausible initial values (the C code leaves almost everything at zero).

        Returns:
            The model itself, so it can be chained: Model().defaults().
        """
        for addr, param in self.params.items():
            if re.search(r"/eq/on$|/mix/on$|^/dca/\d/on$|^/(ch|auxin|fxrtn|bus)/\d\d/mix/st$", addr):
                param.value = 1
            elif re.search(r"/mix/fader$|^/dca/\d/fader$", addr):
                param.value = 0.75                                  # 0 dB
            elif re.search(r"/mix/\d\d/level$|/mlevel$", addr):
                param.value = 0.0
            elif re.search(r"/eq/\d/f$", addr):
                band = int(addr.split("/")[-2])
                param.value = {1: 0.2, 2: 0.4, 3: 0.6, 4: 0.8, 5: 0.9, 6: 0.95}.get(band, 0.5)
            elif re.search(r"/eq/\d/g$|/pan$", addr):
                param.value = 0.5
            elif re.search(r"/eq/\d/q$", addr):
                param.value = 0.465
            elif re.search(r"/eq/\d/type$", addr):
                param.value = 2                                     # PEQ
            elif re.search(r"/gate/range$", addr):
                param.value = 0.5
            elif re.search(r"/dyn/ratio$", addr):
                param.value = 6                                     # 4.0
            elif re.search(r"/dyn/mix$", addr):
                param.value = 1.0
            elif re.search(r"^/ch/(\d\d)/config/source$", addr):
                param.value = int(addr[4:6])
            elif re.search(r"/config/name$", addr):
                param.value = ""
            elif re.search(r"/preamp/trim$", addr):
                param.value = 0.5
            elif re.search(r"^/headamp/\d+/gain$", addr):
                param.value = 32 / 72                               # +20 dB
            elif re.search(r"^/config/routing/(IN|PLAY)/(\d+)-\d+$", addr):
                first_input = int(addr.split("/")[-1].split("-")[0])
                param.value = (first_input - 1) // 8

        for addr, param in self.params.items():           # align with the console steps
            if param.fmt.tag == "f" and not FX_PAR.match(addr):
                param.value = param.fmt.coerce(param.value)

        self.params["/-prefs/rta/_unknown1"].value = 1     # values seen on the console
        self.params["/-prefs/rta/_unknown2"].value = 3
        self.params["/-show/showfile/show/version"].value = "4.06"

        self.update_ha()
        for slot in range(1, 9):
            self.fx_reset(slot)
        return self
