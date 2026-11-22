"""Compares, node by node, the /node replies of the original C emulator and the Python port.

Usage: start the C one (X32 -i 127.0.0.1) on port 10023, then run this script. It
starts the Python one on 10124 without default values, applies the same random
writes to both and compares the text of every node. The differences are written to
/tmp/parity_diff.json.
"""
import asyncio, collections, json, random, re, socket, sys, threading, time

sys.path.insert(0, ".")
from x32emu import osc
from x32emu.model import Model
from x32emu.server import X32Emulator

C_EMULATOR, PY_EMULATOR = ("127.0.0.1", 10023), ("127.0.0.1", 10124)

# Only a sample of the tree, to keep the run short.
KEEP = re.compile(r"^/(ch/(01|17)|auxin/01|fxrtn/01|bus/(01|16)|mtx/01|main|dca/1|fx/(1|5)|config|"
                  r"outputs/(main|aux|p16)/01|headamp/00[0-2]|-prefs|-stat)(/|$)")
SKIP = ("/-show", "/-libs", "/-usb", "/-action", "/-stat/lock", "/-prefs/ip")


def start_python_emulator():
    """Starts the Python emulator in a background thread.

    Returns:
        The X32Emulator instance.
    """
    emulator = X32Emulator(Model())

    def serve():
        """Runs the emulator event loop."""
        loop = asyncio.new_event_loop()

        async def main():
            """Opens the UDP endpoint and waits until the emulator stops."""
            await loop.create_datagram_endpoint(lambda: emulator, local_addr=PY_EMULATOR)
            await emulator.stopped.wait()

        loop.run_until_complete(main())

    threading.Thread(target=serve, daemon=True).start()
    time.sleep(0.3)
    return emulator


def node(sock, destination, path):
    """Requests one node and returns its text, or None if there is no reply."""
    sock.sendto(osc.encode("/node", "s", path), destination)
    try:
        return osc.decode(sock.recv(65536))[2][0]
    except socket.timeout:
        return None


def random_writes(sock, model, rng):
    """Sends the same random value to both emulators for about half the parameters.

    Returns:
        The number of writes.
    """
    writes = 0
    for addr, param in model.params.items():
        if addr.startswith(SKIP) or not KEEP.match(addr) or rng.random() > 0.5:
            continue

        tag = param.fmt.tag
        if tag == "f":
            message = osc.encode(addr, "f", rng.random())
        elif tag == "i":
            choices = len(param.fmt.names) if hasattr(param.fmt, "names") else 2
            message = osc.encode(addr, "i", rng.randrange(choices))
        else:
            message = osc.encode(addr, "s", "T" + str(rng.randrange(99)))

        sock.sendto(message, C_EMULATOR)
        sock.sendto(message, PY_EMULATOR)
        writes += 1
        if writes % 200 == 0:
            time.sleep(0.05)
    return writes


def main():
    """Runs the comparison and prints a summary."""
    emulator = start_python_emulator()
    model = emulator.model

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.3)

    writes = random_writes(sock, model, random.Random(1))
    time.sleep(0.5)

    same, diff, only_c, only_python = 0, [], 0, 0
    for header in model.nodes:
        if not model.nodes[header] or not KEEP.match(header):
            continue

        path = header.lstrip("/")
        c_text, python_text = node(sock, C_EMULATOR, path), node(sock, PY_EMULATOR, path)
        if c_text is None and python_text is None:
            continue
        if c_text is None:
            only_python += 1
        elif python_text is None:
            only_c += 1
        elif c_text.split() == python_text.split():
            same += 1
        else:
            diff.append((path, c_text.strip(), python_text.strip()))

    print(f"writes={writes} same nodes={same} different={len(diff)} "
          f"Python-only={only_python} C-only={only_c}")

    def group(path):
        """Groups node paths by removing the numeric parts."""
        parts = path.split("/")
        return "/".join(parts[:1] + [part for part in parts[1:] if not part.isdigit()])

    print(collections.Counter(group(entry[0]) for entry in diff).most_common(40))
    with open("/tmp/parity_diff.json", "w") as out:
        json.dump(diff, out, indent=1)


if __name__ == "__main__":
    main()
