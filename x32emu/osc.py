"""Minimal OSC encoding and decoding, with the X32 quirks.

The X32 accepts messages without a type tag (the old OSC format) and sends meter
values as binary blobs; both are handled here.
"""
import struct

OscArg = int | float | str | bytes


def _pad(data: bytes) -> bytes:
    """Pads an OSC string: always adds at least one NUL, up to a multiple of 4.

    Args:
        data: The encoded string.

    Returns:
        The string with its terminator and padding.
    """
    return data + b"\0" * (4 - len(data) % 4)


def _pad_blob(data: bytes) -> bytes:
    """Pads an OSC blob to a multiple of 4 bytes (no terminator).

    Args:
        data: The blob contents.

    Returns:
        The blob with padding.
    """
    return data + b"\0" * (-len(data) % 4)


def encode(address: str, tags: str = "", *args: OscArg) -> bytes:
    """Builds an OSC message.

    Args:
        address: OSC address, e.g. "/ch/01/mix/fader".
        tags: Type tags without the comma, one per argument ("i", "f", "s" or "b").
        *args: The arguments, in the same order as the tags.

    Returns:
        The message as bytes, ready to send over UDP.

    Raises:
        ValueError: If a tag is not one of i, f, s or b.
    """
    message = _pad(address.encode()) + _pad(("," + tags).encode())

    for tag, argument in zip(tags, args):
        if tag == "i":
            message += struct.pack(">i", int(argument))
        elif tag == "f":
            message += struct.pack(">f", float(argument))
        elif tag == "s":
            message += _pad(str(argument).encode("latin-1", "replace"))
        elif tag == "b":
            message += struct.pack(">i", len(argument)) + _pad_blob(argument)
        else:
            raise ValueError(f"unsupported OSC type: {tag}")

    return message


def _read_str(data: bytes, offset: int) -> tuple[str, int]:
    """Reads an OSC string starting at an offset.

    Args:
        data: The whole packet.
        offset: Where the string starts.

    Returns:
        A tuple (text, offset of the next field).
    """
    end = data.find(b"\0", offset)
    if end < 0:                 # no terminator or padding (e.g. b"/xremote")
        end = len(data)
    return data[offset:end].decode("latin-1"), (end + 4) & ~3


def decode(data: bytes) -> tuple[str, str, list[OscArg]]:
    """Parses an OSC message.

    Args:
        data: The packet as received.

    Returns:
        A tuple (address, tags, args). Messages without a type tag, which the X32
        accepts, give empty tags and no arguments.

    Raises:
        ValueError: If a tag is not supported.
        struct.error: If the packet is shorter than its tags say.
    """
    address, offset = _read_str(data, 0)
    if offset >= len(data) or data[offset:offset + 1] != b",":
        return address, "", []          # no type tag: accepted by the X32

    tags, offset = _read_str(data, offset)
    tags = tags[1:]
    args = []

    for tag in tags:
        if tag == "i":
            args.append(struct.unpack_from(">i", data, offset)[0])
            offset += 4
        elif tag == "f":
            args.append(struct.unpack_from(">f", data, offset)[0])
            offset += 4
        elif tag == "s":
            text, offset = _read_str(data, offset)
            args.append(text)
        elif tag == "b":
            size = struct.unpack_from(">i", data, offset)[0]
            offset += 4
            args.append(data[offset:offset + size])
            offset += (size + 3) & ~3
        else:
            raise ValueError(f"unsupported OSC type: {tag}")

    return address, tags, args


def meter_blob(values: list[float] | list[int], shorts: bool = False) -> bytes:
    """Builds the blob the X32 sends for meters.

    Args:
        values: The meter values.
        shorts: True for /meters/15 (RTA), where the values are little-endian
            short ints packed two per 32-bit word.

    Returns:
        <int count, little-endian> followed by the values, little-endian.
    """
    if shorts:
        word_count = len(values) // 2
        body = struct.pack("<%dh" % len(values), *values)
        return struct.pack("<i", word_count) + body

    return struct.pack("<i", len(values)) + struct.pack("<%df" % len(values), *values)
