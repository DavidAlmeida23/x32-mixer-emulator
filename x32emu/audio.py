"""Level simulation (no audio).

Modulated pink noise enters each channel and goes through gain, low cut, EQ, gate,
compressor, fader, sends and mix. Everything is computed in dB per 50 ms block; the
EQ is applied analytically to the pink noise spectrum (equal energy per octave).
"""
import math, random, cmath

from .formats import Value, level_to_db
from .model import Model

Biquad = tuple[tuple[float, float, float], tuple[float, float, float]]   # (numerator, denominator)

SAMPLE_RATE = 48000.0
BINS = [20.0 * (1000.0 ** (index / 59)) for index in range(60)]       # 20 Hz..20 kHz, log
NEG = -200.0                                                         # "silence" in dB
RTA_FREQS = [20 * 1000 ** (index / 99) for index in range(100)]      # 100 log bands, 20 Hz..20 kHz
GEQ_BANDS = [20, 25, 31.5, 40, 50, 63, 80, 100, 125, 160, 200, 250, 315, 400, 500, 630, 800,
             1000, 1250, 1600, 2000, 2500, 3150, 4000, 5000, 6300, 8000, 10000, 12500, 16000, 20000]
GEQ_TYPES = ("GEQ", "TEQ", "GEQ2", "TEQ2")
DYN_RATIOS = [1.1, 1.3, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 7.0, 10.0, 20.0, 100.0]


def db_sum(levels: list[float]) -> float:
    """Adds levels in dB as powers (two equal signals give +3 dB).

    Args:
        levels: Levels in dB; NEG or lower counts as silence.

    Returns:
        The total level in dB, or NEG if everything is silent.
    """
    power = sum(10 ** (level / 10) for level in levels if level > NEG)
    return 10 * math.log10(power) if power > 0 else NEG


def lin(db: float) -> float:
    """Converts dB to the linear 0..8 value the meter blobs use.

    Args:
        db: Level in dB.

    Returns:
        The linear amplitude (0.0 for silence, capped at 8.0).
    """
    return 0.0 if db <= NEG else min(8.0, 10 ** (db / 20))


def _biquad(kind: str, f0: float, gain_db: float, quality: float) -> Biquad:
    """Computes biquad filter coefficients (RBJ Audio EQ Cookbook).

    Args:
        kind: "PEQ", "LCut", "HCut", "LShv" or "HShv".
        f0: Centre or corner frequency in Hz.
        gain_db: Gain in dB (peaking and shelving filters).
        quality: Quality factor (Q).

    Returns:
        A tuple (numerator, denominator), each with three coefficients.
    """
    amplitude = 10 ** (gain_db / 40)
    omega = 2 * math.pi * f0 / SAMPLE_RATE
    alpha = math.sin(omega) / (2 * quality)
    cos_w = math.cos(omega)

    if kind == "PEQ":
        numerator = (1 + alpha * amplitude, -2 * cos_w, 1 - alpha * amplitude)
        denominator = (1 + alpha / amplitude, -2 * cos_w, 1 - alpha / amplitude)

    elif kind == "LCut":
        numerator = ((1 + cos_w) / 2, -(1 + cos_w), (1 + cos_w) / 2)
        denominator = (1 + alpha, -2 * cos_w, 1 - alpha)

    elif kind == "HCut":
        numerator = ((1 - cos_w) / 2, 1 - cos_w, (1 - cos_w) / 2)
        denominator = (1 + alpha, -2 * cos_w, 1 - alpha)

    else:
        shelf = 2 * math.sqrt(amplitude) * alpha
        plus, minus = amplitude + 1, amplitude - 1
        if kind == "LShv":
            numerator = (amplitude * (plus - minus * cos_w + shelf),
                         2 * amplitude * (minus - plus * cos_w),
                         amplitude * (plus - minus * cos_w - shelf))
            denominator = (plus + minus * cos_w + shelf,
                           -2 * (minus + plus * cos_w),
                           plus + minus * cos_w - shelf)
        else:  # HShv
            numerator = (amplitude * (plus + minus * cos_w + shelf),
                         -2 * amplitude * (minus + plus * cos_w),
                         amplitude * (plus + minus * cos_w - shelf))
            denominator = (plus - minus * cos_w + shelf,
                           2 * (minus - plus * cos_w),
                           plus - minus * cos_w - shelf)

    return numerator, denominator


def _mag2(coefficients: Biquad, frequency: float) -> float:
    """Returns the squared magnitude of a biquad's response at one frequency.

    Args:
        coefficients: (numerator, denominator) from _biquad.
        frequency: Frequency in Hz.

    Returns:
        |H(f)|², as a power ratio.
    """
    (b0, b1, b2), (a0, a1, a2) = coefficients
    delay = cmath.exp(-1j * 2 * math.pi * frequency / SAMPLE_RATE)     # z^-1 on the unit circle
    response = (b0 + b1 * delay + b2 * delay ** 2) / (a0 + a1 * delay + a2 * delay ** 2)
    return abs(response) ** 2


def pink_gain_db(filters: list[Biquad]) -> float:
    """Returns how much a chain of filters changes the level of pink noise.

    Uses the mean power over log-spaced bands, which is the same as weighting the
    pink spectrum.

    Args:
        filters: List of (numerator, denominator) coefficient pairs.

    Returns:
        The level change in dB (0.0 with no filters).
    """
    if not filters:
        return 0.0

    total = 0.0
    for frequency in BINS:
        gain = 1.0
        for coefficients in filters:
            gain *= _mag2(coefficients, frequency)
        total += gain
    return 10 * math.log10(max(total / len(BINS), 1e-20))


class Smoother:
    """Envelope with separate attack and release times (for gate and compressor)."""

    def __init__(self) -> None:
        """Starts the envelope at 0 dB of reduction."""
        self.current = 0.0

    def step(self, target: float, attack_ms: float, release_ms: float, dt: float) -> float:
        """Moves the envelope towards a target.

        Args:
            target: Value to move towards.
            attack_ms: Time constant when going down (more reduction).
            release_ms: Time constant when going up.
            dt: Block length in seconds.

        Returns:
            The new envelope value.
        """
        time_constant = max(attack_ms if target < self.current else release_ms, 0.5) / 1000.0
        self.current += (target - self.current) * (1 - math.exp(-dt / time_constant))
        return self.current


class Strip:
    """Simulation state of one channel, bus, matrix or main.

    Attributes:
        pre: Level before the fader, in dB.
        post: Level after the fader, in dB.
        eq_db: Level change from the EQ on pink noise, in dB.
        rta_eq: EQ response at each of the 100 RTA bands, in dB.
        gate_gr: Current gate gain reduction, in dB.
        dyn_gr: Current compressor gain reduction, in dB.
    """

    def __init__(self, seed: int):
        """Creates a strip with its own random signal.

        Args:
            seed: Seed for the strip's random generator.
        """
        self.rng = random.Random(seed)
        self.period = self.rng.uniform(2.0, 7.0)
        self.phase = self.rng.uniform(0, 6.28)
        self.base = self.rng.uniform(-38.0, -28.0)        # dBFS before gain

        self.gate, self.dyn = Smoother(), Smoother()
        self.eq_key, self.eq_db = None, 0.0
        self.filters, self.rta_eq = [], [0.0] * 100
        self.pre = self.post = self.out = NEG
        self.gate_gr = self.dyn_gr = 0.0

    def source(self, now: float) -> float:
        """Returns the input level: pink noise with "phrases" separated by pauses.

        Args:
            now: Simulation time in seconds.

        Returns:
            The level in dBFS.
        """
        wave = math.sin(2 * math.pi * now / self.period + self.phase)
        pause = 0.0 if wave > -0.3 else -35.0
        return self.base + pause + self.rng.gauss(0, 0.4)


class Simulator:
    """Computes the level of every strip from the parameters of the model.

    Attributes:
        model: The parameter tree.
        ch, aux, bus, mtx, fxrtn: Strips of each kind.
        main: [LR (mono-summed), M/C].
        dca: Meter level of each DCA, in dB.
        fx_send, fx_ret, fx_gr: Per effect slot, input and output level and
            gain reduction, in dB.
    """

    def __init__(self, model: Model):
        """Creates the simulation for a model.

        Args:
            model: The parameter tree.
        """
        self.model = model
        self.time = 0.0
        self.ch = [Strip(index) for index in range(32)]
        self.aux = [Strip(100 + index) for index in range(8)]
        self.bus = [Strip(200 + index) for index in range(16)]
        self.mtx = [Strip(300 + index) for index in range(6)]
        self.main = [Strip(400), Strip(401)]
        self.fxrtn = [Strip(500 + index) for index in range(8)]
        self.dca = [NEG] * 8
        self.fx_send = [NEG] * 8
        self.fx_ret = [NEG] * 8
        self.fx_gr = [0.0] * 8
        self._geq_cache = {}

    # ---- parameter reading helpers ----
    def raw(self, addr: str) -> Value:
        """Returns the stored value of a parameter."""
        return self.model.params[addr].value

    def physical(self, addr: str) -> float:
        """Returns the physical value of a parameter (Hz, dB, ms...), when known."""
        param = self.model.params[addr]
        return param.fmt.value(param.value) if hasattr(param.fmt, "value") else param.value

    def level_db(self, addr: str) -> float:
        """Returns a fader or send level in dB."""
        return level_to_db(self.raw(addr))

    # ---- processing blocks ----
    def _eq(self, strip: Strip, prefix: str, bands: int) -> float:
        """Returns the EQ level change of a strip, recomputing it only when it changes.

        Args:
            strip: The Strip.
            prefix: Address prefix, e.g. "/ch/01".
            bands: Number of EQ bands (4 for inputs, 6 for buses and mains).

        Returns:
            The level change in dB.
        """
        filters = []
        if self.model.params.get(prefix + "/preamp/hpon") and self.raw(prefix + "/preamp/hpon"):
            filters.append(_biquad("LCut", self.physical(prefix + "/preamp/hpf"), 0, 0.707))

        if self.raw(prefix + "/eq/on"):
            for band in range(1, bands + 1):
                band_addr = f"{prefix}/eq/{band}"
                kind = self.model.params[band_addr + "/type"].fmt.fmt(self.raw(band_addr + "/type"))
                kind = {"VEQ": "PEQ"}.get(kind, kind)
                if kind not in ("PEQ", "LCut", "HCut", "LShv", "HShv"):
                    continue
                filters.append(_biquad(kind, self.physical(band_addr + "/f"),
                                       self.physical(band_addr + "/g"), self.physical(band_addr + "/q")))

        key = tuple(tuple(coefficient) for pair in filters for coefficient in pair)
        if key != strip.eq_key:
            strip.eq_key, strip.eq_db = key, pink_gain_db(filters)
            strip.rta_eq = [
                10 * math.log10(max(math.prod(_mag2(pair, frequency) for pair in filters), 1e-20))
                if filters else 0.0
                for frequency in RTA_FREQS]
        return strip.eq_db

    def _gate(self, strip: Strip, prefix: str, level: float, dt: float) -> float:
        """Applies the gate of a strip.

        Args:
            strip: The Strip.
            prefix: Address prefix.
            level: Input level in dB.
            dt: Block length in seconds.

        Returns:
            The output level in dB.
        """
        if (prefix + "/gate/on") not in self.model.params or not self.raw(prefix + "/gate/on"):
            target = 0.0
        else:
            below = level < self.physical(prefix + "/gate/thr")
            target = -self.physical(prefix + "/gate/range") if below else 0.0

        strip.gate_gr = strip.gate.step(target, self.physical(prefix + "/gate/attack"),
                                        self.physical(prefix + "/gate/release"), dt)
        return level + strip.gate_gr

    def _dyn(self, strip: Strip, prefix: str, level: float, dt: float) -> float:
        """Applies the compressor/expander of a strip.

        Args:
            strip: The Strip.
            prefix: Address prefix.
            level: Input level in dB.
            dt: Block length in seconds.

        Returns:
            The output level in dB, after the dry/wet mix.
        """
        if not self.raw(prefix + "/dyn/on"):
            strip.dyn_gr = strip.dyn.step(0.0, 1, 50, dt)
            return level

        threshold = self.physical(prefix + "/dyn/thr")
        ratio = DYN_RATIOS[self.raw(prefix + "/dyn/ratio") % 12]

        if self.raw(prefix + "/dyn/mode") == 0:                       # COMP
            target = -(level - threshold) * (1 - 1 / ratio) if level > threshold else 0.0
        else:                                                         # EXP
            target = -min((threshold - level) * (ratio - 1), 60.0) if level < threshold else 0.0

        strip.dyn_gr = strip.dyn.step(target, self.physical(prefix + "/dyn/attack"),
                                      self.physical(prefix + "/dyn/release"), dt)

        mix = self.physical(prefix + "/dyn/mix") / 100.0
        wet = level + strip.dyn_gr + self.physical(prefix + "/dyn/mgain")
        mixed_power = mix * 10 ** (wet / 10) + (1 - mix) * 10 ** (level / 10)
        return 10 * math.log10(max(mixed_power, 1e-20))

    # ---------------- effects ----------------
    def _fx_name(self, slot: int) -> str:
        """Returns the effect type loaded in a slot."""
        return self.model.fx_type_name(slot)

    def _fx_par(self, slot: int, name_part: str) -> float | None:
        """Returns the physical value of the first parameter whose name contains name_part.

        Args:
            slot: Effect slot, 1..8.
            name_part: Part of the parameter name, e.g. "Out Gain".

        Returns:
            The value, or None if the effect has no such parameter.
        """
        for index, spec in enumerate(self.model.fx_spec(slot)):
            if name_part.lower() in spec["name"].lower() and spec["kind"] != "enum":
                return self.physical(f"/fx/{slot}/par/{index + 1:02d}")
        return None

    def _geq_db(self, slot: int) -> float:
        """GEQ/TEQ: 31 third-octave peaking filters over pink noise (side A).

        Args:
            slot: Effect slot, 1..8.

        Returns:
            The level change in dB, including the output gain.
        """
        gains = tuple(self.physical(f"/fx/{slot}/par/{index + 1:02d}") for index in range(32))
        if gains not in self._geq_cache:
            filters = [_biquad("PEQ", centre, gain, 4.32)
                       for centre, gain in zip(GEQ_BANDS, gains[:31]) if abs(gain) > 0.01]
            self._geq_cache = {gains: pink_gain_db(filters) + gains[31]}
        return self._geq_cache[gains]

    def _fx_insert(self, prefix: str, level: float, dt: float) -> float:
        """Applies an effect inserted on a strip (insert/on + insert/sel = FXnL/FXnR).

        Args:
            prefix: Address prefix of the strip.
            level: Input level in dB.
            dt: Block length in seconds.

        Returns:
            The output level in dB.
        """
        if prefix + "/insert/on" not in self.model.params or not self.raw(prefix + "/insert/on"):
            return level
        selection = self.raw(prefix + "/insert/sel")
        if not 1 <= selection <= 16:
            return level

        slot = (selection - 1) // 2 + 1
        self.fx_send[slot - 1] = level
        name = self._fx_name(slot)

        if name in GEQ_TYPES:
            output = level + self._geq_db(slot)
        elif name in ("LIM",):
            gain = self._fx_par(slot, "Input Gain") or 0.0
            ceiling = self._fx_par(slot, "Out Gain") or 0.0
            output = min(level + gain, ceiling)
            self.fx_gr[slot - 1] = output - (level + gain)
        else:
            output_gain = self._fx_par(slot, "Out Gain") or self._fx_par(slot, "Output") or 0.0
            output = level + output_gain

        self.fx_ret[slot - 1] = output
        return output

    def _fx_returns(self, prev_bus: list[float], prev_mono: float) -> None:
        """Computes FX1-4 fed by buses (source l/r = MIXn); the output goes to the fxrtn.

        Uses the previous block's levels to avoid bus -> fx -> bus loops.

        Args:
            prev_bus: Post-fader level of each bus in the previous block.
            prev_mono: Post-fader level of the mono bus in the previous block.
        """
        for slot in range(1, 5):
            source = self.raw(f"/fx/{slot}/source/l")
            if source == 0:                                  # INS: handled in the insert
                continue

            send = prev_bus[source - 1] if source <= 16 else prev_mono
            self.fx_send[slot - 1] = send
            if send <= NEG:
                self.fx_ret[slot - 1] = NEG
                continue

            wet = 0.0
            level = self._fx_par(slot, "Level")
            mix = self._fx_par(slot, "Mix")
            if level is not None:
                wet += level
            if mix is not None:
                wet += 20 * math.log10(max(mix, 0.5) / 100.0)
            if self._fx_name(slot) in GEQ_TYPES:
                wet += self._geq_db(slot)

            self.fx_ret[slot - 1] = send + wet - 3.0     # typical losses of a 100 % wet

    def _dca_mute(self, prefix: str) -> float:
        """Returns the gain from the strip's DCAs, or NEG if a DCA or mute group mutes it.

        Args:
            prefix: Address prefix of the strip.
        """
        gain = 0.0
        if prefix + "/grp/dca" in self.model.params:
            dca_bits = self.raw(prefix + "/grp/dca")
            for dca in range(8):
                if dca_bits >> dca & 1:
                    if not self.raw(f"/dca/{dca + 1}/on"):
                        return NEG
                    gain += self.level_db(f"/dca/{dca + 1}/fader")

            mute_bits = self.raw(prefix + "/grp/mute")
            for group in range(6):
                if mute_bits >> group & 1 and self.raw(f"/config/mute/{group + 1}"):
                    return NEG
        return gain

    def _muted(self, prefix: str) -> bool:
        """Whether a strip is muted (button, mute group or muted DCA).

        On the X32 mute cuts all sends, including pre-fader ones.
        """
        return not self.raw(prefix + "/mix/on") or self._dca_mute(prefix) <= NEG

    def _fader(self, prefix: str, level: float) -> float:
        """Applies mute, DCAs and fader to a level, in dB."""
        if not self.raw(prefix + "/mix/on"):
            return NEG
        dca_gain = self._dca_mute(prefix)
        return NEG if dca_gain <= NEG else level + self.level_db(prefix + "/mix/fader") + dca_gain

    # ---------------- one block ----------------
    def step(self, dt: float) -> None:
        """Advances the simulation by one block.

        Args:
            dt: Block length in seconds.
        """
        self.time += dt
        bus_in = [[] for _ in range(16)]
        lr_in, mono_in = [], []

        prev_bus = [bus.post for bus in self.bus]
        self._fx_returns(prev_bus, self.main[1].post)

        def input_strip(strip: Strip, prefix: str, gain_db: float, has_dyn: bool,
                        level: float | None = None) -> None:
            """Processes one input strip: gain, trim, EQ, insert, gate, compressor, fader, sends."""
            signal = (strip.source(self.time) if level is None else level) + gain_db
            if prefix + "/preamp/trim" in self.model.params:
                signal += self.physical(prefix + "/preamp/trim")
            signal += self._eq(strip, prefix, 4) if signal > NEG else 0.0
            signal = self._fx_insert(prefix, signal, dt)
            if has_dyn:
                signal = self._gate(strip, prefix, signal, dt)
                signal = self._dyn(strip, prefix, signal, dt)
            strip.pre = signal
            strip.post = self._fader(prefix, signal)
            self._sends(prefix, strip, bus_in, lr_in, mono_in)

        def headamp_gain(channel: int, default: float) -> float:
            """Returns the gain of the headamp feeding a channel, or a default if none."""
            headamp = self.raw(f"/-ha/{channel:02d}/index")
            return self.physical(f"/headamp/{headamp:03d}/gain") if 0 <= headamp <= 127 else default

        # inputs
        for index, strip in enumerate(self.ch):
            input_strip(strip, f"/ch/{index + 1:02d}", headamp_gain(index, 0.0), True)
        for index, strip in enumerate(self.aux):
            input_strip(strip, f"/auxin/{index + 1:02d}", headamp_gain(32 + index, 10.0), False)
        for index, strip in enumerate(self.fxrtn):
            input_strip(strip, f"/fxrtn/{index + 1:02d}", 0.0, False, level=self.fx_ret[index // 2])

        # buses
        for index, strip in enumerate(self.bus):
            prefix = f"/bus/{index + 1:02d}"
            signal = db_sum(bus_in[index])
            if signal > NEG:
                signal = self._fx_insert(prefix, signal + self._eq(strip, prefix, 6), dt)
            signal = self._dyn(strip, prefix, signal, dt) if signal > NEG else NEG
            strip.pre, strip.post = signal, self._fader(prefix, signal) if signal > NEG else NEG

            if strip.post > NEG:
                if self.raw(prefix + "/mix/st"):
                    lr_in.append(strip.post)
                if self.raw(prefix + "/mix/mono"):
                    mono_in.append(strip.post + self.level_db(prefix + "/mix/mlevel"))

        # main LR and mono
        main, mono = self.main
        signal = db_sum(lr_in)
        main.pre = signal
        if signal > NEG:
            with_eq = self._fx_insert("/main/st", signal + self._eq(main, "/main/st", 6), dt)
            main.post = self._fader("/main/st", with_eq)
        else:
            main.post = NEG

        signal = db_sum(mono_in)
        mono.pre = signal
        mono.post = self._fader("/main/m", signal + self._eq(mono, "/main/m", 6)) if signal > NEG else NEG

        # Matrices: fed by the 16 buses, main LR and mono (M/C), each with its own
        # on/level/type in <source>/mix/01..06 (pre/post type as on channels).
        sources = [(f"/bus/{index + 1:02d}", bus) for index, bus in enumerate(self.bus)] + \
                  [("/main/st", main), ("/main/m", mono)]
        for index, strip in enumerate(self.mtx):
            prefix = f"/mtx/{index + 1:02d}"
            inputs = []
            for source_prefix, source in sources:
                send = f"{source_prefix}/mix/{index + 1:02d}"
                if send + "/on" not in self.model.params or not self.raw(send + "/on") or self._muted(source_prefix):
                    continue
                type_addr = f"{source_prefix}/mix/{(index & ~1) + 1:02d}/type"
                send_type = self.raw(type_addr) if type_addr in self.model.params else 4
                level = source.post if send_type >= 4 else source.pre
                if level > NEG:
                    inputs.append(level + self.level_db(send + "/level"))

            signal = db_sum(inputs)
            if signal > NEG:
                signal = self._dyn(strip, prefix, signal + self._eq(strip, prefix, 6), dt)
            strip.pre, strip.post = signal, self._fader(prefix, signal) if signal > NEG else NEG

        # DCAs: the meter shows the members' highest post-fader level
        members = [(f"/ch/{index + 1:02d}", strip) for index, strip in enumerate(self.ch)] + \
                  [(f"/auxin/{index + 1:02d}", strip) for index, strip in enumerate(self.aux)] + \
                  [(f"/fxrtn/{index + 1:02d}", strip) for index, strip in enumerate(self.fxrtn)] + \
                  [(f"/bus/{index + 1:02d}", strip) for index, strip in enumerate(self.bus)]
        self.dca = []
        for dca in range(8):
            levels = [strip.post for prefix, strip in members if self.raw(prefix + "/grp/dca") >> dca & 1]
            self.dca.append(max(levels) if levels else NEG)

    def _sends(self, prefix: str, strip: Strip, bus_in: list[list[float]], lr_in: list[float],
               mono_in: list[float]) -> None:
        """Adds a strip's sends to the bus, LR and mono inputs.

        Args:
            prefix: Address prefix of the strip.
            strip: The Strip.
            bus_in: Per bus, list of incoming levels (appended to).
            lr_in: Levels going to main LR (appended to).
            mono_in: Levels going to mono (appended to).
        """
        if self._muted(prefix):
            return

        if strip.post > NEG and self.raw(prefix + "/mix/st"):
            lr_in.append(strip.post)
        if strip.post > NEG and self.raw(prefix + "/mix/mono"):
            mono_in.append(strip.post + self.level_db(prefix + "/mix/mlevel"))

        for bus in range(16):
            send = f"{prefix}/mix/{bus + 1:02d}"
            if not self.raw(send + "/on"):
                continue
            type_addr = f"{prefix}/mix/{(bus & ~1) + 1:02d}/type"
            send_type = self.raw(type_addr) if type_addr in self.model.params else 4
            level = strip.post if send_type >= 4 else strip.pre          # POST/GRP vs. pre-fader
            if level > NEG:
                bus_in[bus].append(level + self.level_db(send + "/level"))

    # ---- RTA ----
    def rta_strip(self) -> tuple[Strip, bool]:
        """Returns the RTA source given by /-stat/rtasource.

        0..71 are pre-EQ and 98..169 post-EQ (channels, aux, fx returns, buses,
        matrices, LR, mono); 72/170 is the monitor.

        Returns:
            A tuple (strip, post_eq).
        """
        if "/-stat/rtasource" in self.model.params:
            rta_source = self.raw("/-stat/rtasource")
        else:
            rta_source = 0
        post_eq = rta_source >= 98
        index = rta_source - 98 if post_eq else rta_source

        all_strips = self.ch + self.aux + self.fxrtn + self.bus + self.mtx + self.main
        strip = all_strips[index] if 0 <= index < len(all_strips) else self.main[0]
        return strip, post_eq

    def rta(self) -> list[float]:
        """Returns the 100 RTA values for /meters/15.

        Pink noise (equal per band) shaped by the source EQ if the RTA is post-EQ.

        Returns:
            100 values in dB, in -128..0.
        """
        strip, post_eq = self.rta_strip()
        if strip.pre <= NEG:
            return [-128.0] * 100

        base = strip.pre - (0.0 if post_eq else strip.eq_db) - 20.0     # 100 bands: -20 dB each
        values = []
        for band in range(100):
            db = base + (strip.rta_eq[band] if post_eq else 0.0) + strip.rng.gauss(0, 1.5)
            values.append(max(-128.0, min(0.0, db)))
        return values

    # ---- meter blocks (format from the protocol document) -----------------
    def meters(self, meter_id: int, args: list[int]) -> list[float]:
        """Returns the values of one /meters/N block.

        Args:
            meter_id: The N in /meters/N.
            args: Selection arguments (banks for /meters/5, strip for /meters/6).

        Returns:
            The list of meter values (linear levels and gain reductions).
        """
        def levels(dbs: list[float]) -> list[float]:
            """Converts a list of dB values to linear meter values."""
            return [lin(db) for db in dbs]

        channel_pre = [strip.pre for strip in self.ch]

        if meter_id == 0:
            return levels(channel_pre + [strip.pre for strip in self.aux] + [strip.pre for strip in self.fxrtn] +
                          [strip.post for strip in self.bus] + [strip.post for strip in self.mtx])

        if meter_id == 1:
            return (levels(channel_pre) + levels([strip.gate_gr for strip in self.ch])
                    + levels([strip.dyn_gr for strip in self.ch]))

        if meter_id == 2:
            main, mono = self.main
            outputs = [strip.post for strip in self.bus] + [strip.post for strip in self.mtx] + \
                      [main.post, main.post, mono.post]
            reductions = [strip.dyn_gr for strip in self.bus] + [strip.dyn_gr for strip in self.mtx] + [0.0, 0.0]
            return levels(outputs) + levels(reductions)

        if meter_id == 3:
            return levels([NEG] * 6 + [strip.pre for strip in self.aux] + [strip.pre for strip in self.fxrtn])

        if meter_id == 4:
            return levels(channel_pre + [strip.pre for strip in self.aux] + [NEG] * 42)

        if meter_id == 5:
            channel_bank = args[0] if args else 0
            group_bank = args[1] if len(args) > 1 else 0
            # banks as in /-stat/chfaderbank and /-stat/grpfaderbank
            strips = {0: self.ch[:16], 1: self.ch[16:], 2: self.aux + self.fxrtn, 3: self.bus}.get(
                channel_bank, self.ch[:16])
            if group_bank == 0:
                group_levels = list(self.dca)
            else:
                groups = {1: self.bus[:8], 2: self.bus[8:], 3: self.mtx + [self.main[1]]}.get(group_bank, [])
                group_levels = [strip.post for strip in groups] + [NEG] * (8 - len(groups))
            main, mono = self.main
            return levels([strip.pre for strip in strips][:16] + group_levels + [main.post, main.post, mono.post])

        if meter_id == 6:
            index = args[0] if args else 0
            all_strips = self.ch + self.aux + self.fxrtn + self.bus + self.mtx + self.main
            strip = all_strips[index] if 0 <= index < len(all_strips) else self.ch[0]
            return levels([strip.pre]) + levels([strip.gate_gr, strip.dyn_gr]) + levels([strip.post])

        if meter_id == 7:
            return levels([strip.pre for strip in self.bus])

        if meter_id == 8:
            return levels([strip.pre for strip in self.mtx])

        if meter_id == 9:
            values = []
            for slot in range(8):
                values += [self.fx_send[slot]] * 2 + [self.fx_ret[slot]] * 2
            return levels(values)

        sizes = {10: 32, 11: 5, 12: 4, 13: 48, 14: 80}
        return [0.0] * sizes.get(meter_id, 0)
