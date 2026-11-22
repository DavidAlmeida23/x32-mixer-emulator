# Differences from the original X32.c

Sources used for the comparison: the C code itself, the old copy of the document
"Unofficial X32/M32 OSC Remote Protocol" (v0.64, 2016) and the x32-osc library
(TypeScript, which says it was only tested with firmware 4.06). The current version of the document
(FW 4.09+, on x32ram.com) was not consulted. None of this was confirmed on a real console:
"fixed" means the other sources agree with each other and disagree with the C code.

## Main tree tables

| Problem in the C code | What the port does | Confidence |
|---|---|---|
| `/bus/NN/dyn/ratio` uses the OFF/ON list | uses the ratio list | high (doc + x32-osc) |
| `/bus/NN/mix/NN/type` uses OFF/ON | IN/LC, <-EQ, EQ->, PRE, POST | high |
| Effect type order in the string array differs from the C code's own enum | doc/x32-osc order | high |
| Gate mode `EXP` | `EXP4` | high |
| Routing without the FW 4 user blocks (`UIN`, `UOUT`) | added | medium (x32-osc only) |
| Missing `/main/st|m/mix/NN/panFollow`, `/config/dp48/link/NN`, `/config/dp48/aesAB` | added as OFF/ON | medium |
| Strings without a leading space (`"AN17-24"`, `"45%"`, `"MIX"`...) glue words together in `/node` | the port joins with spaces | high |
| `Sroul[]` without a `""` terminator (can read memory past the array) | n/a | high |
| `/` for auxin writes invert over trim (repeated index) | each value in its own parameter | high |
| `/` for `/-stat/screen` and iQ with wrong indices | same | high |
| `case CSOLO` without `break` mixes the solo output with the tape one | n/a | high |
| `/xremote`: clients compared with `strcmp` on a binary `sockaddr` | compares (IP, port) | high |
| `/info` returns the console name in the 2nd field; the doc says `osc-server` | `osc-server` | medium |
| `/formatsubscribe` does not exist in the C code (X32-Edit uses it for states, solos and prefs) | implemented; strings in the blob are sent as 0 (real format unknown) | medium |
| X32-Edit asks for `/-usb/dir/dirpos` and `/-usb/dir/maxpos`; the C code only has `/-usb/dirpos` and `/-usb/maxpos` | X32-Edit's ones added | high (real client) |
| In `X32Libs.h` (ch, fx and r) node 040 is written as "030" and 050 as "040": `/node ,s -libs/ch/040` returns the wrong node and 050 does not exist | the extractor fixes the header name from the next leaf (only these 6 cases in the whole C code) | high |
| `/-libs/.../flags` | 16-bit bitmap, as the C code formats it | high |
| The C code echoes every received `/ ,s ...` command back to the sender; the initial port did not | echo restored, as in the C code | high (C behaviour) |
| Entries without an enum list: `/outputs/main/04/delay/on`, `/outputs/main/15/delay/on`, `/outputs/aux/06/pos`; and `/config/solo/source` declared I32 despite having a list | list inferred from parameters with the same pattern; I32 with a list becomes an enum | high (they failed with X32-Edit) |
| `/config/routing/OUT` node order in the C code: 1-4, 9-12, 5-8, 13-16 | 1-4, 5-8, 9-12, 13-16, as X32-Edit sends it | high (real client) |
| `/config/dp48/scope`, `/-prefs/haflags`, `/-prefs/remote/ioenable`, `/-prefs/rta/options`, `/-show/.../safes` are I32 in the tables but the C `/node` formats them as bitmaps | treated as bitmaps (4, 4, 12, 6 and 9 bits) | high |
| `/-ha/00..39/index` (channel -> headamp) all stay at 0, so X32-Edit links every channel to headamp 0 and the gain looks shared | computed from `/config/routing/IN/...` and `/ch/NN/config/source`; -1 when there is no headamp (card, user in, line aux); updated and sent to the clients when the routing changes | high for the problem; medium for the -1 |
| Mute (button, mute group, DCA) did not cut pre-fader sends in the simulator (a port bug, not a C one) | cuts all sends | high |
| X32-Edit picks the RTA source with `/-action/setrtasrc`; the C code does not link this to `/-stat/rtasource` | `setrtasrc` updates `rtasource` (the emulator RTA follows it) | high (real scene: setrtasrc=98, rtasource=0) |
| `/-action/platrack` in the C code; the document says `playtrack` | both accepted | medium |
| `/-stat/solo` is never updated (TODO in the C code) | updated when a solosw changes | medium |

## Effects

The parameter structure came from x32-osc. The C code was used for comparison; the full
list of divergences is written to `tools/fx_divergences.json` by `tools/build_fx.py`. That file
is not in the repository: see "Regenerating the tables" in README.md.

The most relevant ones:

- **Dual effects with only half the parameters in the C code** (AMP2, CMB2, DRV2, EXC2, LEC2, P1A2,
  PQ5S, ULC2): the C `/node` only returns side A.
- **Misaligned parameters** in VREV, FILT, MODD, FL/R, FAC2, PIT2: one parameter is missing or extra
  in the C code and all the following ones shift (e.g. in VREV the C "Lo Cut" has the
  "Hi Cut" range).
- **Different ranges**: DLY Mix 0–200 in the C code vs 0–100; EXC Timbre and Mix swapped;
  LIM Input Gain 0–10 vs 0–18; CHAM Size up to 72 vs 76.
- **Enums in reverse order**: ROTA Stop/Speed, CMB Meter Mode.
- **Capitalisation**: P1A uses `3K`, `10K` in the C code and `3k`, `10k` in x32-osc.

To be confirmed on a real console:

- whether enum-type effect parameters come as `,i` (the C code says so) or `,f`;
  the port replies with `,i` and accepts both;
- the default values of each effect (the port uses the middle of the range);
- decimal places in `/node` (the C code and x32-osc do not always agree).

## How to confirm

With access to an X32: capture the `/node` replies of every node (`tools/parity.py`
already requests node by node; just point it at the console instead of the C emulator) and compare.

## Confirmed on a real console (X32 Producer, firmware 4.13, November 2026 capture)

OSC types: the ~16,000 parameters that replied have the same type (i/f/s) in the emulator,
including the enum effect parameters, which arrive as integers. `/info` returns
"osc-server"; `/status` returns the console name.

Fixed in the emulator from the capture:

| What the console does | Before in the emulator |
|---|---|
| 0 dB on faders and sends comes out as "0.0" (unsigned); levels round half up (-9.25 → "-9.3") | "+0.0", "-9.2" |
| EQ, filter and oscillator frequencies: the C tables are right ("1k21", "403.7", "6k39"; confirmed in the 2nd capture) | — |
| Low cut always an integer ("49", "173") | "49.1", "172.9" |
| Gate/compressor hold: from 10 up it truncates ("447", "28.2", "11.2"), below it rounds ("0.09", "0.04") | "448", "28.3", "0.08" |
| The C frequency table has at least one wrong entry: the console shows "1k91" where the C code has "1k90" | "1k90" |
| FW 4 user routing: with `IN 1-8 = UIN1-8`, each channel's headamp comes from `/config/userrout/in/NN` (33 = AES50 A1 = headamp 32) | `/-ha/00/index` = -1 |
| `/-prefs` ends with an integer and the firmware version; `/-prefs/rta` with two more integers ("1 3"). Names unknown: in the emulator they are `/-prefs/_unknown1`, `/-prefs/_firmware` and `/-prefs/rta/_unknown1/2` | missing |
| Make-up gain: 2 decimals below 10 dB, 1 above ("3.50", "18.5"); EQ gain "-11.2" above 10 dB and "+0.00" at zero; Q "10" and "2.1" | "3.5", "18.50", "-11.25", "10.0" |
| Trim, headamp gain and automix weight signed at zero too ("+0.0"); trim rounds half up ("+2.3"); tape gain and solo source trim unsigned ("11.0", "0.0") | "0.0", "+2.2", "+11.0" |
| `/fx/N/par` always shows all 64 values (unused ones as "0") | only the effect's |
| `/main/st|m/mix/01,03,05` have a 5th integer value (panFollow) | missing |
| `/config/dp48` ends with AESA/AESB | missing |
| `/outputs/rec/NN` has no `invert`; `/-stat` does not include `dcaspill`; `/-usb` only has path and title | extra |
| `/-prefs/key` is only the layout ("QWERTY"); `/-prefs/rta/peakhold` in seconds ("3s"); `/-stat/lock` OFF/ON | extra keys 00–99, "3", "0" |
| `/-prefs/remote/ioenable` has 14 bits | 12 |
| `/showdump`: header with the show version and only the cues/scenes/snippets that exist | header only |

No reply from the console and removed: `/-prefs/iQ/...`, `/-prefs/key/00..99`, `/-usb/maxpos`,
`/-usb/dirpos`, `/-usb/dir/000/name`, `/insert/aux/N`, `/undo/time`, `/-stat/urec`.

Note: the 1st capture suggested the C frequency tables truncated ("1k90" vs "1k91");
it was an artefact of comparing with values read at a different time. The 2nd capture, nodes only,
showed the tables are right.

With all the `nodes/` files of the 2nd capture (3,147 lines), the emulator reproduces 3,146
character by character, spaces included. The missing one is a "/-stat 0" reply from the console to a
request for the card recorder part (`/-stat/urec`), with no known meaning.

Observed alignment rules (the console right-aligns): levels and faders 5 characters
("  -oo", " +5.5"), delay times 5 ("  1.6"), automix weight 5, hold 4, release 3 below
100 ms and 4 from there on ("   9", "  173"), low cut 3, Q 3 (" 10").

Effects: no "+" on positive values ("55"); frequencies above 1 kHz with as many digits
after the "k" as the parameter's decimals plus one, truncated ("4k47", "5k0", "7k2", "11k3").

Still to be resolved: nothing in the node text. What remains are questions the capture cannot reach
(names of the extra `/-prefs` fields, complete lists of some enums, effect default values,
DCA meters, `/formatsubscribe` with strings and `/save`/`/load`).
