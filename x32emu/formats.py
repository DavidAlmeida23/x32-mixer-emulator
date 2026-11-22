"""Conversions between OSC values and the text the X32 uses.

The console stores every parameter as an OSC value (an int, a float in 0..1 or a
string). In /node replies and in scene files the same value is shown as text, for
example "-oo", "+3.5" or "1k78". Each class in this module knows how to go from one
representation to the other for one kind of parameter.
"""
import math
from decimal import Decimal, ROUND_HALF_UP
from typing import Callable

Value = int | float | str          # a stored OSC value


def half_up(number: float, decimals: int) -> str:
    """Rounds half up, like the console does.

    Python's round() uses banker's rounding; the console shows -9.25 dB as "-9.3".

    Args:
        number: The value to round.
        decimals: Number of decimal places to keep.

    Returns:
        The rounded value as text, with exactly ``decimals`` decimal places.
    """
    quantum = Decimal(1).scaleb(-decimals)
    exact = Decimal(repr(round(number, 9)))
    return str(exact.quantize(quantum, rounding=ROUND_HALF_UP))


def _quantize(fraction: float, step_count: int) -> float:
    """Snaps a 0..1 value to one of the steps the console knows.

    Args:
        fraction: Value in 0..1 (values outside are clamped).
        step_count: Number of distinct positions the parameter has.

    Returns:
        The nearest step position, as a 0..1 float.
    """
    fraction = min(1.0, max(0.0, fraction))
    if step_count <= 1:
        return fraction
    return round(fraction * (step_count - 1)) / (step_count - 1)


def level_to_db(level: float) -> float:
    """Converts a fader/send position (0..1) to dB, using the X32 fader law.

    Args:
        level: Fader position in 0..1.

    Returns:
        The level in dB (-inf for 0, +10 dB at the top).
    """
    if level <= 0.0:
        return -math.inf
    if level <= 0.0625:
        return 480.0 * level - 90.0
    if level <= 0.25:
        return 160.0 * (level - 0.0625) - 60.0
    if level < 0.5:
        return 80.0 * (level - 0.25) - 30.0
    return 40.0 * (level - 0.5) - 10.0


def db_to_level(db: float) -> float:
    """Converts dB to a fader/send position (0..1); the inverse of level_to_db.

    Args:
        db: Level in dB.

    Returns:
        The fader position in 0..1.
    """
    if db <= -90.0:
        return 0.0
    if db < -60.0:
        return (db + 90.0) / 480.0
    if db < -30.0:
        return (db + 60.0) / 160.0 + 0.0625
    if db < -10.0:
        return (db + 30.0) / 80.0 + 0.25
    return min(1.0, (db + 10.0) / 40.0 + 0.5)


def fmt_freq(hertz: float) -> str:
    """Formats a frequency the X32 way.

    Args:
        hertz: Frequency in Hz.

    Returns:
        "403.7" below 1 kHz, "1k78" (kHz and two digits of hundredths) above.
    """
    if hertz < 1000.0:
        return f"{hertz:.1f}"

    kilohertz = int(hertz // 1000)
    hundredths = round((hertz % 1000) / 10)
    if hundredths == 100:
        kilohertz, hundredths = kilohertz + 1, 0
    return f"{kilohertz}k{hundredths:02d}"


def parse_num(text: str) -> float:
    """Reads a number written in the X32 format.

    Args:
        text: Something like "12", "-3.5", "1k78" or "-oo".

    Returns:
        The number as a float ("-oo" gives -inf, "1k78" gives 1780.0).

    Raises:
        ValueError: If the text is not a number.
    """
    text = text.strip()
    if text in ("-oo", "-inf"):
        return -math.inf

    if "k" in text:
        thousands, rest = text.split("k", 1)
        fraction = float(rest) * 10 ** (3 - len(rest)) if rest else 0.0
        return float(thousands or 0) * 1000.0 + fraction

    return float(text)


class Fmt:
    """Base class for a parameter format.

    Attributes:
        tag: OSC type tag of the stored value ("f", "i" or "s").
        width: Minimum width of the value in /node text (the console right-aligns).
            It can also be a function of the value.
    """

    tag = "f"
    width: int | Callable[[Value], int] = 0

    def padded(self, value: Value) -> str:
        """Returns the value as text, right-aligned to the format's width.

        Args:
            value: The stored OSC value.

        Returns:
            The text, padded with spaces on the left if needed.
        """
        text = self.fmt(value)
        width = self.width(value) if callable(self.width) else self.width
        return text.rjust(width) if width else text

    def fmt(self, value: Value) -> str:
        """Converts a stored value to the console text.

        Args:
            value: The stored OSC value.

        Returns:
            The text the console would show.
        """
        raise NotImplementedError

    def parse(self, text: str) -> Value:
        """Converts console text to the stored value.

        Args:
            text: Text as found in /node replies or scene files.

        Returns:
            The OSC value to store.
        """
        raise NotImplementedError

    def coerce(self, value: Value) -> Value:
        """Converts a received OSC argument to the value to store.

        Args:
            value: The argument as received over OSC.

        Returns:
            The value to store (unchanged by default).
        """
        return value


class Level(Fmt):
    """Fader and send levels in dB ("-oo", "0.0", "+5.5")."""

    width = 5          # console: "  -oo", "  0.0", "-11.1"

    def __init__(self, steps: int):
        """Creates a level format.

        Args:
            steps: Number of fader positions (1024 for faders, 161 for sends).
        """
        self.steps = steps

    def fmt(self, value: float) -> str:
        """Returns "-oo", "0.0" or a signed dB value such as "+5.5"."""
        db = level_to_db(value)
        if db == -math.inf:
            return "-oo"

        text = half_up(db, 1)
        if float(text) == 0:
            return "0.0"                   # real console: 0 dB comes out as "0.0", unsigned
        return text if text.startswith("-") else "+" + text

    def parse(self, text: str) -> float:
        """Reads a dB text and snaps it to the nearest fader step."""
        return _quantize(db_to_level(parse_num(text)), self.steps)

    def coerce(self, value: Value) -> float:
        """Snaps a received 0..1 float to the nearest fader step."""
        return _quantize(float(value), self.steps)

    def to_db(self, value: float) -> float:
        """Returns the level in dB for a stored 0..1 value."""
        return level_to_db(value)


class Lin(Fmt):
    """A value spread linearly between a minimum and a maximum.

    Attributes:
        signed: Positive values get a "+".
        zero_sign: Zero also gets a "+" (pan "+0", EQ gain "+0.00").
        dec_big: Decimal places to use when |value| >= 10 (EQ gain "-11.2").
        hup: Round half up instead of Python's rounding (trim "+2.3").
    """

    def __init__(self, low: float, high: float, step: float, dec: int = 1, signed: bool = False,
                 zero_sign: bool = False, dec_big: int | None = None, hup: bool = False,
                 width: int = 0):
        """Creates a linear format.

        Args:
            low: Value shown at 0.
            high: Value shown at 1.
            step: Size of one step, in display units.
            dec: Decimal places.
            signed: See the class attributes.
            zero_sign: See the class attributes.
            dec_big: See the class attributes.
            hup: See the class attributes.
            width: Minimum text width in /node.
        """
        self.low, self.high, self.dec, self.signed = low, high, dec, signed
        self.width = width
        self.zero_sign, self.dec_big, self.hup = zero_sign, dec_big, hup
        self.step_count = int(round((high - low) / step)) + 1

    def value(self, fraction: float) -> float:
        """Returns the physical value for a stored 0..1 value."""
        return self.low + (self.high - self.low) * fraction

    def fmt(self, value: float) -> str:
        """Returns the physical value as text, with the sign and decimal rules."""
        physical = self.value(value)
        use_big = self.dec_big is not None and abs(physical) >= 10
        decimals = self.dec_big if use_big else self.dec
        text = half_up(physical, decimals) if self.hup else f"{physical:.{decimals}f}"

        if float(text) == 0:
            text = text.lstrip("-")
            return ("+" + text) if self.signed and self.zero_sign else text
        return ("+" + text) if self.signed and not text.startswith("-") else text

    def parse(self, text: str) -> float:
        """Reads a physical value and snaps it to the nearest step."""
        fraction = (parse_num(text) - self.low) / (self.high - self.low)
        return _quantize(fraction, self.step_count)

    def coerce(self, value: Value) -> float:
        """Snaps a received 0..1 float to the nearest step."""
        return _quantize(float(value), self.step_count)


class Log(Fmt):
    """A value spread logarithmically between a minimum and a maximum.

    Attributes:
        style: How to write the number. None uses ``dec`` places (frequencies below
            1 kHz with 1 decimal: "403.7"); "hpf" writes an integer ("49", "173");
            "sig3" writes 3 digits ("50.2", "10.0", "0.09"); "q" drops the decimals
            from 10 up.
        table: Exact console texts (f201, f121), when they are known.
    """

    def __init__(self, low: float, high: float, steps: int, dec: int = 1, freq: bool = False,
                 style: str | None = None, width: int | str = 0, kdig: int | None = None):
        """Creates a logarithmic format.

        Args:
            low: Value shown at 0.
            high: Value shown at 1.
            steps: Number of positions.
            dec: Decimal places.
            freq: The value is a frequency ("1k78" above 1 kHz).
            style: See the class attributes.
            width: Minimum text width, or "release" for the 3/4 character rule.
            kdig: For effects, digits after the "k" in frequencies.
        """
        self.low, self.high, self.step_count, self.dec, self.freq = low, high, steps, dec, freq
        self.style, self.kdig = style, kdig
        if width == "release":
            self.width = lambda value: 4 if self.value(value) >= 100 else 3
        else:
            self.width = width
        self.table = None

    def value(self, fraction: float) -> float:
        """Returns the physical value for a stored 0..1 value."""
        return self.low * math.exp(fraction * math.log(self.high / self.low))

    def fmt(self, value: float) -> str:
        """Returns the physical value as text, following ``style`` or ``table``."""
        if self.table:
            index = round(min(1.0, max(0.0, value)) * (self.step_count - 1))
            return self.table[index].strip()

        physical = self.value(value)

        if self.style == "hpf":
            return f"{physical:.0f}"

        if self.freq and self.kdig and physical >= 1000:
            # effects: digits after the "k" = parameter decimals + 1, truncated
            # (console: "4k47", "5k0", "7k2", "11k3")
            kilohertz = int(physical // 1000)
            remainder = physical - kilohertz * 1000
            digits = int(remainder / 10 ** (3 - self.kdig) + 1e-9)
            return f"{kilohertz}k{digits:0{self.kdig}d}"

        if self.freq:
            return fmt_freq(physical) if physical >= 1000 else f"{physical:.1f}"

        if self.style == "sig3":
            # observed on an X32P 4.13: >= 10 truncates ("447", "28.2", "11.2"),
            # below that rounds ("0.09", "0.04")
            if physical >= 10:
                decimals = 0 if physical >= 100 else 1
                scale = 10 ** decimals
                return f"{math.floor(physical * scale + 1e-9) / scale:.{decimals}f}"
            return f"{physical:.2f}"

        if self.style == "q":
            return f"{physical:.0f}" if physical >= 10 else f"{physical:.1f}"

        return f"{physical:.{self.dec}f}"

    def parse(self, text: str) -> float:
        """Reads a physical value (or an exact table text) and snaps it to a step."""
        if self.table:
            known_texts = [entry.strip() for entry in self.table]
            if text.strip() in known_texts:
                return known_texts.index(text.strip()) / (self.step_count - 1)

        fraction = math.log(parse_num(text) / self.low) / math.log(self.high / self.low)
        return _quantize(fraction, self.step_count)

    def coerce(self, value: Value) -> float:
        """Snaps a received 0..1 float to the nearest step."""
        return _quantize(float(value), self.step_count)


class Raw(Fmt):
    """Float with no known conversion (e.g. effect parameters not mapped yet)."""

    def fmt(self, value: float) -> str:
        """Returns the raw 0..1 value with 4 decimals."""
        return f"{value:.4f}"

    def parse(self, text: str) -> float:
        """Reads a raw 0..1 value (clamped)."""
        return min(1.0, max(0.0, float(text)))

    def coerce(self, value: Value) -> float:
        """Clamps a received float to 0..1."""
        return min(1.0, max(0.0, float(value)))


class Unused(Raw):
    """Effect parameter not used by the current type: the console shows "0"."""

    def fmt(self, value: float) -> str:
        """Returns "0" for a zero value, like the console."""
        return "0" if not value else f"{value:g}"


class Enum(Fmt):
    """A choice from a list of names, stored as its index."""

    tag = "i"

    def __init__(self, names: list[str]):
        """Creates an enum format.

        Args:
            names: The texts the console shows, in index order.
        """
        self.names = names

    def fmt(self, value: Value) -> str:
        # A stale float can arrive here when an effect type changes under a parameter.
        """Returns the name for an index (or the value itself if it is not a valid index)."""
        if isinstance(value, int) and 0 <= value < len(self.names):
            return self.names[value]
        return str(value)

    def parse(self, text: str) -> int:
        """Returns the index of a name (exact match first, then ignoring case)."""
        text = text.strip()
        for index, name in enumerate(self.names):
            if name == text:
                return index
        for index, name in enumerate(self.names):
            if name.lower() == text.lower():
                return index
        return int(text)

    def coerce(self, value: Value) -> int:
        """Accepts an index or a name.

        Raises:
            ValueError: If the index is outside the list.
        """
        if isinstance(value, str):
            return self.parse(value)

        index = int(value)
        if not 0 <= index < len(self.names):
            raise ValueError("enum out of range")
        return index


class Int(Fmt):
    """A plain integer."""

    tag = "i"

    def fmt(self, value: int) -> str:
        """Returns the integer as text."""
        return str(value)

    def parse(self, text: str) -> int:
        """Reads an integer (a decimal text is truncated)."""
        return int(float(text))

    def coerce(self, value: Value) -> int:
        """Converts a received value to int."""
        return int(value)


class Bitmap(Fmt):
    """An integer shown as bits ("%00000101")."""

    tag = "i"

    def __init__(self, bits: int):
        """Creates a bitmap format.

        Args:
            bits: Number of bits shown.
        """
        self.bits = bits

    def fmt(self, value: int) -> str:
        """Returns the value as "%" followed by ``bits`` binary digits."""
        mask = (1 << self.bits) - 1
        return "%" + format(value & mask, f"0{self.bits}b")

    def parse(self, text: str) -> int:
        """Reads "%0101" or a plain integer."""
        text = text.strip()
        return int(text[1:], 2) if text.startswith("%") else int(text)

    def coerce(self, value: Value) -> int:
        """Converts a received value to int."""
        return int(value)


class Str(Fmt):
    """A text value, shown between quotes."""

    tag = "s"

    def fmt(self, value: str) -> str:
        """Returns the text between double quotes."""
        return '"' + value + '"'

    def parse(self, text: str) -> str:
        """Removes the surrounding quotes."""
        return text.strip().strip('"')

    def coerce(self, value: Value) -> str:
        """Converts a received value to str."""
        return str(value)


def tokenize(text: str) -> list[str]:
    """Splits a /node line into tokens, keeping quoted text together.

    Args:
        text: A line such as '/ch/01/config "Vox" 1 RD 1'.

    Returns:
        The list of tokens, quotes included ('"Vox"').
    """
    tokens, current, inside_quotes = [], "", False

    for char in text.strip():
        if char == '"':
            inside_quotes = not inside_quotes
            current += char
        elif char == " " and not inside_quotes:
            if current:
                tokens.append(current)
                current = ""
        else:
            current += char

    if current:
        tokens.append(current)
    return tokens
