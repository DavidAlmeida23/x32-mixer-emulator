"""Builds x32emu/data/fx/<TYPE>.json: the parameters of each effect type.

Usage:

    python tools/build_fx.py <X32-Behringer folder> <x32FxConditional export.json>

Sources:
  - original C (GetFxPar1 in X32.c): format of each parameter, enums, decimal places
  - original C (Sflookup/Sflookup2 in X32Fx.h): OSC type ('i' or 'f') of each parameter
  - x32-osc (TypeScript, tested on FW 4.06): parameter names and number of steps

Divergences between the two sources go to tools/fx_divergences.json, a report for review
that is not versioned (see "Regenerating the tables" in README.md).
"""
import json, re, sys, pathlib
from collections import Counter

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from x32emu.datafiles import read_tables, write_fx

NUMBER = r"([-+]?[0-9.]+)"
C_TYPE_NAMES = {"D_RV": "D/RV", "CR_R": "CR/R", "FL_R": "FL/R", "D_CR": "D/CR", "D_FL": "D/FL",
                "3TAP": "3TAP", "4TAP": "4TAP"}
TS_KINDS = {"linear": "lin", "exponential": "log", "enum": "enum"}
# Enum texts confirmed by X32-Edit library files (Xlib2/libfx.*), which win over x32-osc.
ENUM_OVERRIDES = {("FILT", 13): ["OFF", "ON"]}


def parse_get_fx_par(x32_lines, enums):
    """Reads the parameter formats of each effect from GetFxPar1 in X32.c.

    Args:
        x32_lines: The lines of X32.c.
        enums: Enum lists from the C tables, to resolve "Xxx[...]" lookups.

    Returns:
        Dict of (group 1 or 2, effect name) -> list of parameter specs.
    """
    start = next(index for index, line in enumerate(x32_lines)
                 if line.startswith("void GetFxPar1(") and line.rstrip().endswith("{"))
    end = next(index for index in range(start, len(x32_lines)) if x32_lines[index].startswith("}"))

    specs, labels, current, repeat = {}, [], [], 1

    def flush():
        """Stores the parameters read so far under every pending effect label."""
        for label in labels:
            specs[label] = current

    for raw_line in x32_lines[start:end]:
        line = raw_line.strip()

        case_match = re.match(r"case _([12])_(\w+):", line)
        if case_match:
            if current and labels:
                flush()
                labels, current = [], []
            name = C_TYPE_NAMES.get(case_match.group(2), case_match.group(2))
            labels.append((int(case_match.group(1)), name))
            continue

        loop_match = re.match(r"for \(i = 0; i < (\d+); i\+\+\)", line)
        if loop_match:
            repeat = int(loop_match.group(1))
            continue

        range_match = re.search(r"S(linf|logf)\(command\[ipar\+\+\]\.value\.ff, "
                                + NUMBER + r", " + NUMBER + r", (\d)\)", line)
        if range_match:
            kind = "lin" if range_match.group(1) == "linf" else "log"
            spec = {"kind": kind, "min": float(range_match.group(2)),
                    "max": float(range_match.group(3)), "dec": int(range_match.group(4))}
            current += [spec] * repeat
            repeat = 1
            continue

        on_off_match = re.search(r'command\[ipar\+\+\]\.value\.ii \? " ([^"]*)" : " ([^"]*)"', line)
        if on_off_match:
            current.append({"kind": "enum", "enum": [on_off_match.group(2), on_off_match.group(1)]})
            continue

        lookup_match = re.search(r"(\w+)\[command\[ipar\+\+\]\.value\.ii\]", line)
        if lookup_match:
            enum_name = lookup_match.group(1)
            current.append({"kind": "enum", "enum": [text.strip() for text in enums[enum_name]],
                            "c_enum": enum_name})
            continue

        if line == "break;" and labels:
            flush()
            labels, current = [], []

    if current and labels:
        flush()
    return specs


def parse_sflookup(fx_header):
    """Reads the OSC type string of each effect from Sflookup/Sflookup2 in X32Fx.h.

    Args:
        fx_header: The text of X32Fx.h.

    Returns:
        Dict of (group 1 or 2, effect name) -> type string such as "fffi".
    """
    tags = {}
    for table, group in (("Sflookup", 1), ("Sflookup2", 2)):
        body = re.search(r"char\*\s*" + table + r"\[\]\s*=\s*\{(.*?)\};", fx_header, re.S).group(1)
        for type_string, name in re.findall(r'"([fies-]+)",?\s*/\*\s*_?([^ *]+)\s*\*/', body):
            tags[(group, name.strip("_"))] = type_string
    return tags


def merge_parameter(name, index, c_param, ts_param, divergences):
    """Builds the final spec of one parameter and records C/x32-osc divergences.

    Args:
        name: Effect type name.
        index: Parameter index, 0-based.
        c_param: Spec from the C code, or None.
        ts_param: Spec from x32-osc, or None.
        divergences: List the divergences are appended to.

    Returns:
        The parameter spec to write.
    """
    param = dict(c_param) if c_param else {}

    if ts_param:
        param["name"] = ts_param["_name"]
        ts_kind = TS_KINDS.get(ts_param["_type"], ts_param["_type"])

        if not c_param:
            param.update(kind=ts_kind, min=ts_param.get("_min"), max=ts_param.get("_max"),
                         dec=ts_param.get("_decimals", 0), enum=ts_param.get("_enum"))
            param["origin"] = "x32-osc"
        else:
            base = {"fx": name, "par": index + 1, "name": ts_param["_name"]}
            if ts_kind != param["kind"]:
                divergences.append({**base, "issue": "type", "C": param["kind"], "x32-osc": ts_kind})
            elif ts_kind != "enum" and (abs(ts_param["_min"] - param["min"]) > 1e-6
                                        or abs(ts_param["_max"] - param["max"]) > 1e-6):
                divergences.append({**base, "issue": "range", "C": [param["min"], param["max"]],
                                    "x32-osc": [ts_param["_min"], ts_param["_max"]]})
            elif ts_kind == "enum" and [text.strip() for text in ts_param["_enum"]] != param["enum"]:
                divergences.append({**base, "issue": "enum", "C": param["enum"], "x32-osc": ts_param["_enum"]})

            if "_decimals" in ts_param and ts_param["_decimals"] != param.get("dec") and param["kind"] != "enum":
                param["dec_c"] = param["dec"]
                param["dec"] = ts_param["_decimals"]

        if param["kind"] == "lin" and "_step" in ts_param:
            param["steps"] = int(round((param["max"] - param["min"]) / ts_param["_step"])) + 1
        elif param["kind"] == "log" and "_steps" in ts_param:
            param["steps"] = ts_param["_steps"] + 1
        param["freq"] = bool(ts_param.get("_freq"))

        # Decision: the final structure comes from x32-osc (count, order, ranges, steps);
        # the C code only fills what x32-osc lacks. See DIFFERENCES.md.
        ts_kind = TS_KINDS[ts_param["_type"]]
        final = {"name": ts_param["_name"], "kind": ts_kind, "freq": bool(ts_param.get("_freq"))}
        if ts_kind == "enum":
            final["enum"] = ENUM_OVERRIDES.get((name, index + 1)) or [text.strip() for text in ts_param["_enum"]]
        else:
            final.update(min=ts_param["_min"], max=ts_param["_max"], dec=ts_param.get("_decimals", 0))
            if ts_kind == "lin":
                final["steps"] = int(round((ts_param["_max"] - ts_param["_min"]) / ts_param["_step"])) + 1
            else:
                final["steps"] = ts_param["_steps"] + 1
        param = final

    param["tag"] = "i" if param["kind"] == "enum" else "f"
    return param


def main():
    """Builds the effect files from the two sources given on the command line."""
    c_source = pathlib.Path(sys.argv[1])
    with open(sys.argv[2]) as export_file:
        ts_fx = json.load(export_file)

    x32_lines = (c_source / "X32.c").read_text(errors="replace").splitlines()
    fx_header = (c_source / "X32Fx.h").read_text(errors="replace")
    enums = read_tables()[1]

    specs = parse_get_fx_par(x32_lines, enums)
    tags = parse_sflookup(fx_header)    # kept for reference; the final tags follow the kind

    effects, divergences = {}, []
    names = sorted({name for _, name in specs} | set(ts_fx))

    for name in names:
        c_params = specs.get((1, name)) or specs.get((2, name))
        ts_params = ts_fx.get(name)

        if c_params is None or ts_params is None:
            divergences.append({"fx": name, "issue": "only exists in one of the sources",
                                "C": c_params is not None, "x32-osc": ts_params is not None})

        base = c_params or []
        if ts_params and len(ts_params) != len(base):
            divergences.append({"fx": name, "issue": "different number of parameters",
                                "C": len(base), "x32-osc": len(ts_params)})

        params = []
        # x32-osc decides the count: extra C entries are padding (X32-Edit presets confirm it).
        for index in range(len(ts_params) if ts_params else len(base)):
            c_param = base[index] if index < len(base) else None
            ts_param = ts_params[index] if ts_params and index < len(ts_params) else None
            params.append(merge_parameter(name, index, c_param, ts_param, divergences))
        effects[name] = params

    write_fx(effects)
    with open("tools/fx_divergences.json", "w") as out:
        json.dump(divergences, out, indent=1, ensure_ascii=False)

    print(len(effects), "effect types;", sum(len(params) for params in effects.values()), "parameters")
    print("divergences:", Counter(entry["issue"] for entry in divergences))


if __name__ == "__main__":
    main()
