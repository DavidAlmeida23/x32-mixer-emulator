"""Extracts the parameter tree and the enum lists from the original C code.

The source is pmaillot/X32-Behringer; the result goes to x32emu/data/ (see
x32emu/datafiles.py).

Usage:

    python tools/extract_tables.py <X32-Behringer folder>
"""
import json, re, sys, pathlib
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from x32emu.datafiles import write_tables

HEADERS = ["X32CfgMain.h", "X32Channel.h", "X32Auxin.h", "X32Fxrtn.h", "X32Bus.h",
           "X32Mtx.h", "X32Dca.h", "X32Fx.h", "X32Output.h", "X32Headamp.h",
           "X32PrefStat.h", "X32Misc.h", "X32Show.h", "X32Libs.h"]

# {"address", {type}, FLAGS, {value}, enum_list}
ENTRY = re.compile(r'\{\s*"([^"]+)"\s*,\s*\{([^}]*)\}\s*,\s*([A-Z_| ]+?)\s*,\s*\{([^}]*)\}\s*,\s*(\w+)\s*\}')
# char *Name[] = {"a", "b", ...};
ENUM = re.compile(r'char\s*\*\s*(\w+)\[\]\s*=\s*\{(.*?)\};', re.S)


def strip_comments(text):
    """Removes C comments (/* ... */ and // ...) from source text."""
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.S)
    return re.sub(r'//[^\n]*', '', text)


def read_entries(header_path, fixes):
    """Reads the parameter entries of one C header.

    Fixes node headers with the wrong name (e.g. in X32Libs.h node 040 appears as
    "030" and 050 as "040"): the right name is the parent of the next leaf.

    Args:
        header_path: Path of the .h file.
        fixes: List the (file, wrong name, fixed name) corrections are appended to.

    Returns:
        List of (address, type, flags, value, enum list) tuples.
    """
    text = strip_comments(header_path.read_text(errors="replace"))
    entries = [tuple(group.strip() for group in match.groups()) for match in ENTRY.finditer(text)]

    for index, entry in enumerate(entries):
        is_header = entry[2] == "F_FND"
        next_is_leaf = index + 1 < len(entries) and entries[index + 1][2] != "F_FND"
        if not (is_header and next_is_leaf):
            continue

        leaf = entries[index + 1][0]
        if not leaf.startswith(entry[0] + "/"):
            parent = leaf.rsplit("/", 1)[0]
            fixes.append((header_path.name, entry[0], parent))
            entries[index] = (parent,) + entry[1:]

    return entries


def read_enums(source):
    """Reads every "char *Name[] = {...}" list from X32.c and X32Fx.h.

    Args:
        source: The X32-Behringer folder.

    Returns:
        Dict of list name -> list of texts (the first definition wins).
    """
    enums = {}
    for file_name in ("X32.c", "X32Fx.h"):
        text = strip_comments((source / file_name).read_text(errors="replace"))
        for match in ENUM.finditer(text):
            items = re.findall(r'"((?:[^"\\]|\\.)*)"', match.group(2))
            if items and items[-1] == "":
                items = items[:-1]          # drop the "" terminator
            enums.setdefault(match.group(1), items)
    return enums


def main():
    """Extracts the tables from the folder given on the command line."""
    source = pathlib.Path(sys.argv[1])
    params, seen, fixes = [], set(), []

    for header in HEADERS:
        header_path = source / header
        if not header_path.exists():
            continue

        for addr, type_name, flags, _value, enum in read_entries(header_path, fixes):
            if addr in seen:
                continue
            seen.add(addr)
            params.append({"addr": addr, "type": type_name, "node": flags == "F_FND",
                           "enum": None if enum == "NULL" else enum})

    enums = read_enums(source)

    for fix in fixes:
        print("header fixed in %s: %s -> %s" % fix)
    write_tables(params, enums)

    print(len(params), "parameters;", len(enums), "enums")
    print(Counter(param["type"] for param in params).most_common(15))
    print(Counter(param["node"] for param in params))


if __name__ == "__main__":
    main()
