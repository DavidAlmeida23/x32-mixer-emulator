"""Command-line entry point: python -m x32emu."""
import argparse, asyncio, logging

from .server import run


def main() -> None:
    """Parses the options and runs the emulator until Ctrl-C or /-stat/lock 2."""
    parser = argparse.ArgumentParser(prog="x32emu", description="Behringer X32 emulator (OSC/UDP)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=10023)
    parser.add_argument("--name", default="X32 Emulator")
    parser.add_argument("--console", default="X32", help="X32, X32RACK, X32C, X32P, X32CORE...")
    parser.add_argument("--version", default="4.06", help="advertised firmware version")
    parser.add_argument("--state", default="x32_state",
                        help="state folder (or .json file in the old format; \"\" to not save)")
    parser.add_argument("--no-sim", action="store_true",
                        help="turn off the level simulation (meters and RTA stay silent)")
    parser.add_argument("-v", "--verbose", action="count", default=0)
    options = parser.parse_args()

    log_level = [logging.WARNING, logging.INFO, logging.DEBUG][min(options.verbose, 2)]
    logging.basicConfig(level=log_level, format="%(asctime)s %(message)s")

    try:
        asyncio.run(run(options.host, options.port, name=options.name, console=options.console,
                        version=options.version, state_file=options.state or None,
                        simulate=not options.no_sim))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
