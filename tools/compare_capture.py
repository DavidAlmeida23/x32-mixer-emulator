"""Compares a capture of a real X32 (python -m x32emu.capture) with the emulator.

Usage:

    python tools/compare_capture.py capture_20261102_1830

Steps:
    1. Loads the captured values into the emulator (the same state as the console).
    2. For each captured node, compares the console text with the emulator's /node.
    3. For each parameter, compares the console OSC type (i/f/s) with the emulator's.

Writes <capture>/comparison.txt and prints a summary.
"""
import json, pathlib, re, sys, collections

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from x32emu.model import Model
from x32emu.formats import tokenize


def pattern(addr):
    """Replaces the numbers in an address with N, to group similar ones."""
    return re.sub(r"\d+", "N", addr)


def node_lines(capture_folder):
    """Yields every non-empty line of the captured nodes/*.txt files."""
    for node_file in sorted((capture_folder / "nodes").glob("*.txt")):
        for line in node_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield line


def load_values(model, values):
    """Applies the captured "get" values to the model.

    Effect types go first, so the effect parameters get the right format.

    Args:
        model: The Model to change.
        values: {address: [OSC type, value]} from the capture.
    """
    for addr in sorted(values, key=lambda address: not address.endswith("/type")):
        _, value = values[addr]
        param = model.params.get(addr)
        if param is None or value is None:
            continue
        try:
            param.value = param.fmt.coerce(value)
        except (ValueError, TypeError):
            pass
    model.update_ha()


def load_nodes(model, capture_folder, values):
    """Applies the captured node texts to the model.

    The node text is the reference: the console may have changed between the node
    phase and the values phase (someone touching it).

    Args:
        model: The Model to change.
        capture_folder: The capture folder.
        values: The captured "get" values, to detect what changed between phases.

    Returns:
        Lines describing the parameters whose node text and "get" value differ.
    """
    changed = []
    for line in node_lines(capture_folder):
        tokens = tokenize(line)
        if tokens[0] not in model.nodes and tokens[0] not in model.params:
            continue

        _, leaves = model.node_for(tokens[0])
        for addr, token in zip(leaves, tokens[1:]):
            param = model.params[addr]
            before = param.fmt.fmt(param.value)
            try:
                param.value = param.fmt.parse(token)
            except (ValueError, IndexError):
                continue
            if addr in values and before != token and param.fmt.fmt(param.value) != before:
                changed.append(f"{addr}: node={token} get={before}")

    model.update_ha()
    return changed


def compare_types(model, values):
    """Counts the parameters whose OSC type differs, grouped by address pattern.

    Returns:
        Counter of (pattern, console tag, emulator tag) -> count.
    """
    types = collections.Counter()
    for addr, (tags, _) in values.items():
        param = model.params.get(addr)
        if param and tags and tags != param.fmt.tag:
            types[(pattern(addr), tags, param.fmt.tag)] += 1
    return types


def compare_nodes(model, capture_folder):
    """Compares every captured node line with the emulator's /node text.

    Returns:
        Dict of (kind of difference, address pattern) -> list of example lines.
    """
    diffs = collections.defaultdict(list)

    for line in node_lines(capture_folder):
        real = tokenize(line)
        emulated = model.node_text(real[0])
        if emulated is None:
            diffs[("node missing in emulator", pattern(real[0]))].append(line)
            continue

        emulated = tokenize(emulated)
        if len(real) != len(emulated):
            diffs[("number of values", pattern(real[0]))].append(
                f"console: {line}\n   emu:     {' '.join(emulated)}")
            continue

        _, leaves = model.node_for(real[0])
        for index, (real_value, emulated_value) in enumerate(zip(real[1:], emulated[1:])):
            if real_value != emulated_value:
                leaf = leaves[index] if index < len(leaves) else "?"
                diffs[("value/format", pattern(leaf))].append(
                    f"{leaf}: console={real_value} emulator={emulated_value}")

    return diffs


def main():
    """Runs the comparison for the capture folder given on the command line."""
    capture_folder = pathlib.Path(sys.argv[1])
    model = Model()

    values = {}
    for values_file in (capture_folder / "values").glob("*.json"):
        values.update(json.loads(values_file.read_text(encoding="utf-8")))

    load_values(model, values)
    changed = load_nodes(model, capture_folder, values)
    types = compare_types(model, values)
    diffs = compare_nodes(model, capture_folder)

    no_reply_file = capture_folder / "no_reply.txt"
    no_reply = [line for line in no_reply_file.read_text().split("\n") if line] if no_reply_file.exists() else []

    report = ["== Different OSC types (pattern, console, emulator, count) =="]
    report += [f"{key[0]}  console={key[1]} emulator={key[2]}  x{count}" for key, count in types.most_common()]

    report.append("\n== Differences in node text ==")
    for (kind, address_pattern), examples in sorted(diffs.items(), key=lambda item: -len(item[1])):
        report.append(f"[{kind}] {address_pattern}  x{len(examples)}")
        report += ["   " + example for example in examples[:3]]

    report.append(f"\n== Values that changed during the capture (node vs get): {len(changed)} ==")
    report += ["   " + line for line in changed[:40]]

    report.append(f"\n== Requests with no reply from the console: {len(no_reply)} (see no_reply.txt) ==")
    no_reply_patterns = collections.Counter(pattern(line) for line in no_reply)
    report += [f"   {key}  x{count}" for key, count in no_reply_patterns.most_common(30)]

    (capture_folder / "comparison.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    print(f"different types: {sum(types.values())}, nodes with differences: "
          f"{sum(len(examples) for examples in diffs.values())}, no reply: {len(no_reply)}")
    print(f"report in {capture_folder / 'comparison.txt'}")


if __name__ == "__main__":
    main()
