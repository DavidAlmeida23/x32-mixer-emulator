"""Reading and writing the data files in x32emu/data/ and the saved state.

Layout of data/:

    index.json             order of the tree sections (the order matters for the nodes)
    params/<section>.json  parameters of each section (config, ch, bus, prefs, ...)
    enums.json             value lists (colours, EQ types, routing, ...)
    frequencies.json       exact frequency tables used in /node (f201, f121, f101)
    fx/<TYPE>.json         parameters of each effect type (HALL.json, D-RV.json, ...)
"""
import json, logging, os, pathlib, re, tempfile
from typing import Any

log = logging.getLogger("x32emu")

DATA = pathlib.Path(__file__).with_name("data")
FREQ_TABLES = ("f201", "f121", "f101")


def write_atomic(path: str | os.PathLike, text: str) -> None:
    """Writes a file so that it is never left empty or truncated.

    The text goes to a temporary file first, which then replaces the original, so a
    Ctrl-C or a crash in the middle of the write keeps the old file intact.

    Args:
        path: The file to write.
        text: The full contents.
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_path = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")

    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def dump_lines(obj: list | dict, path: str | os.PathLike, key: str | None = None) -> None:
    """Writes readable JSON, with one element per line.

    Args:
        obj: A list, or a dict.
        path: The file to write.
        key: If given, obj is a dict and obj[key] is the "big" list that gets one
            element per line; the other keys go at the top.
    """
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    def one(value: Any) -> str:
        """Encodes one value as compact JSON."""
        return json.dumps(value, ensure_ascii=False)

    if isinstance(obj, list):
        body = "[\n" + ",\n".join(" " + one(item) for item in obj) + "\n]\n"

    elif key is None:
        body = "{\n" + ",\n".join(f" {one(name)}: {one(item)}" for name, item in obj.items()) + "\n}\n"

    else:
        head = {name: item for name, item in obj.items() if name != key}
        parts = [f" {one(name)}: {one(item)}" for name, item in head.items()]
        big_list = ",\n".join("  " + one(item) for item in obj[key])
        parts.append(f" {one(key)}: [\n" + big_list + "\n ]")
        body = "{\n" + ",\n".join(parts) + "\n}\n"

    write_atomic(path, body)


def section_file(prefix: str) -> str:
    """Returns the file name of a tree section ("/-prefs" -> "prefs.json")."""
    return prefix.lstrip("/").lstrip("-") + ".json"


def fx_file(name: str) -> str:
    """Returns the file name of an effect type ("D/RV" -> "D-RV.json")."""
    return name.replace("/", "-") + ".json"


# ---------------- tables ----------------
def write_tables(params: list[dict], enums: dict[str, list[str]],
                 root: str | os.PathLike = DATA) -> None:
    """Writes the parameter tree and the enum lists.

    Args:
        params: List of parameter dicts (addr, type, node, enum), in tree order.
        enums: Dict of enum name -> list of texts (frequency tables included).
        root: The data folder.
    """
    root = pathlib.Path(root)
    sections, order = {}, []

    for param in params:
        section = "/" + param["addr"].split("/")[1]
        if section not in sections:
            sections[section] = []
            order.append(section)
        sections[section].append(param)

    for section in order:
        dump_lines({"section": section, "params": sections[section]},
                   root / "params" / section_file(section), key="params")

    dump_lines([{"prefix": section, "file": "params/" + section_file(section)} for section in order],
               root / "index.json")
    dump_lines({name: values for name, values in enums.items() if name not in FREQ_TABLES},
               root / "enums.json")
    dump_lines({name: enums[name] for name in FREQ_TABLES if name in enums},
               root / "frequencies.json")


def read_tables(root: str | os.PathLike = DATA) -> tuple[list[dict], dict[str, list[str]]]:
    """Reads the parameter tree and the enum lists.

    Args:
        root: The data folder.

    Returns:
        A tuple (params, enums), as given to write_tables.
    """
    root = pathlib.Path(root)
    params = []

    for entry in json.loads((root / "index.json").read_text(encoding="utf-8")):
        section = json.loads((root / entry["file"]).read_text(encoding="utf-8"))
        params += section["params"]

    enums = json.loads((root / "enums.json").read_text(encoding="utf-8"))
    enums.update(json.loads((root / "frequencies.json").read_text(encoding="utf-8")))
    return params, enums


def write_fx(fx: dict[str, list[dict]], root: str | os.PathLike = DATA) -> None:
    """Writes one file per effect type, replacing the existing ones.

    Args:
        fx: Dict of effect type name -> list of parameter specs.
        root: The data folder.
    """
    fx_folder = pathlib.Path(root) / "fx"
    for old_file in fx_folder.glob("*.json") if fx_folder.exists() else []:
        old_file.unlink()

    for name, params in fx.items():
        dump_lines({"type": name, "params": params}, fx_folder / fx_file(name), key="params")


def read_fx(root: str | os.PathLike = DATA) -> dict[str, list[dict]]:
    """Reads the effect types.

    Args:
        root: The data folder.

    Returns:
        Dict of effect type name -> list of parameter specs.
    """
    effects = {}
    for fx_path in sorted((pathlib.Path(root) / "fx").glob("*.json")):
        content = json.loads(fx_path.read_text(encoding="utf-8"))
        effects[content["type"]] = content["params"]
    return effects


# ---------------- saved state ----------------
STRIPS = ("ch", "auxin", "fxrtn", "bus", "mtx", "dca", "fx", "main")


def state_key(addr: str) -> tuple[str, str]:
    """Returns the file and relative key where a parameter is saved.

    Examples: /ch/01/mix/fader -> ("ch/01.json", "mix/fader");
    /-prefs/rta/gain -> ("prefs.json", "rta/gain").

    Args:
        addr: The parameter address.

    Returns:
        A tuple (file path relative to the state folder, key inside that file).
    """
    parts = addr.strip("/").split("/")
    if parts[0] in STRIPS and len(parts) > 2:
        return f"{parts[0]}/{parts[1]}.json", "/".join(parts[2:])
    return parts[0].lstrip("-") + ".json", "/".join(parts[1:])


def save_state(params: dict[str, Any], folder: str | os.PathLike) -> None:
    """Saves the state as a tree of small files, one per section or strip.

    Args:
        params: Dict of address -> stored value.
        folder: The state folder.
    """
    folder = pathlib.Path(folder)
    files = {}

    for addr, value in params.items():
        file_name, key = state_key(addr)
        prefix = addr[: len(addr) - len(key)].rstrip("/") if key else addr
        content = files.setdefault(file_name, {"_prefix": prefix, "values": {}})
        content["values"][key] = value

    for file_name, content in files.items():
        value_lines = ",\n".join(
            f"  {json.dumps(key, ensure_ascii=False)}: {json.dumps(value, ensure_ascii=False)}"
            for key, value in content["values"].items())
        lines = [f' "_prefix": {json.dumps(content["_prefix"], ensure_ascii=False)}',
                 ' "values": {\n' + value_lines + "\n }"]
        write_atomic(folder / file_name, "{\n" + ",\n".join(lines) + "\n}\n")


def load_state(path: str | os.PathLike) -> dict[str, Any]:
    """Loads a saved state.

    Empty or broken files are skipped with a warning, so they never stop the
    emulator from starting.

    Args:
        path: The state folder, or the old single file (x32_state.json).

    Returns:
        Dict of address -> stored value.

    Raises:
        FileNotFoundError: If the path does not exist.
    """
    path = pathlib.Path(path)

    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            log.warning("state: %s ignored (empty or invalid: %s)", path, error)
            return {}

    state = {}
    for state_path in sorted(path.rglob("*.json")):
        try:
            content = json.loads(state_path.read_text(encoding="utf-8"))
            prefix, values = content["_prefix"], content["values"]
        except (ValueError, KeyError, TypeError, UnicodeDecodeError) as error:
            log.warning("state: %s ignored (empty or invalid: %s); that section keeps the "
                        "default values and will be rewritten on exit", state_path, error)
            continue

        for key, value in values.items():
            state[prefix + "/" + key if key else prefix] = value

    return state
