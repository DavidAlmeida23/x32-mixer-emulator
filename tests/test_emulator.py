"""Integration tests: start the emulator and talk to it over UDP."""
import asyncio, socket, struct, threading, time, unittest

from x32emu import osc
from x32emu.server import X32Emulator
from x32emu.model import Model

PORT = 10123


class Client:
    """A minimal OSC client that talks to the emulator under test."""

    def __init__(self):
        """Opens a UDP socket with a 1 s timeout."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(1.0)

    def send(self, addr, tags="", *args):
        """Sends an OSC message to the emulator."""
        self.sock.sendto(osc.encode(addr, tags, *args), ("127.0.0.1", PORT))

    def raw(self, data):
        """Sends raw bytes (to test packets without padding or type tag)."""
        self.sock.sendto(data, ("127.0.0.1", PORT))

    def recv(self):
        """Waits for one message and returns (address, tags, args)."""
        return osc.decode(self.sock.recv(65536))

    def slash(self, text):
        """Sends / ,s <text> and checks the echo (like the original C does).

        Args:
            text: A node line, e.g. '/ch/02/config "Vox" 12 RD 2'.
        """
        self.send("/", "s", text)
        address, _, args = self.recv()
        assert (address, args) == ("/", [text]), (address, args)

    def ask(self, addr, tags="", *args):
        """Sends a message and returns the first reply."""
        self.send(addr, tags, *args)
        return self.recv()

    def node(self, path):
        """Returns the /node text of a path."""
        return self.ask("/node", "s", path)[2][0]

    def get(self, addr):
        """Returns the arguments of a parameter "get"."""
        return self.ask(addr)[2]

    def drain(self):
        """Discards everything that arrives in the next 0.2 s."""
        self.sock.settimeout(0.2)
        try:
            while True:
                self.sock.recv(65536)
        except socket.timeout:
            pass
        self.sock.settimeout(1.0)


class EmulatorTest(unittest.TestCase):
    """Tests against one emulator shared by the whole class."""

    @classmethod
    def setUpClass(cls):
        """Starts the emulator (with defaults and simulation) in a background thread."""
        cls.emu = X32Emulator(Model().defaults())

        def serve():
            """Runs the emulator event loop."""
            loop = asyncio.new_event_loop()
            cls.loop = loop

            async def main():
                """Opens the UDP endpoint and runs the ticker."""
                await loop.create_datagram_endpoint(lambda: cls.emu, local_addr=("127.0.0.1", PORT))
                await cls.emu.ticker()

            loop.run_until_complete(main())

        threading.Thread(target=serve, daemon=True).start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        """Stops the emulator."""
        cls.loop.call_soon_threadsafe(cls.emu.stopped.set)

    # ---- protocol basics ----
    def test_info(self):
        """/info replies with four strings, the second being "osc-server"."""
        address, tags, args = Client().ask("/info")
        self.assertEqual((address, tags), ("/info", "ssss"))
        self.assertEqual(args[1], "osc-server")

    def test_info_without_typetag(self):
        """A message without a type tag is accepted."""
        client = Client()
        client.raw(b"/info\0\0\0")
        self.assertEqual(client.recv()[0], "/info")

    def test_get_set_fader(self):
        """A float set is read back with the same value."""
        client = Client()
        client.send("/ch/05/mix/fader", "f", 0.5)
        _, tags, args = client.ask("/ch/05/mix/fader")
        self.assertEqual(tags, "f")
        self.assertAlmostEqual(args[0], 0.5, places=3)

    def test_enum_by_text(self):
        """An enum can be set with its name."""
        client = Client()
        client.send("/ch/01/gate/mode", "s", "GATE")
        self.assertEqual(client.get("/ch/01/gate/mode"), [3])
        self.assertIn("GATE", client.node("ch/01/gate"))

    def test_node_and_slash(self):
        """A node written with "/" comes back as the same text."""
        client = Client()
        client.slash('/ch/02/config "Vox" 12 RD 2')
        self.assertEqual(client.node("ch/02/config"), '/ch/02/config "Vox" 12 RD 2\n')

        client.slash("/ch/02/mix ON -10.0 ON -20 OFF -oo")
        self.assertEqual(client.node("ch/02/mix"),
                         "/ch/02/mix ON -10.0 ON -20 OFF   -oo\n")   # aligned like the console

    def test_frequencies(self):
        """Frequencies above 1 kHz use the "1k02" format."""
        client = Client()
        client.slash("/ch/03/eq/2 PEQ 1k02 +3.50 2.0")
        self.assertIn("1k02 +3.50", client.node("ch/03/eq/2"))

    def test_multi_tag(self):
        """A multi-argument set on a node sets each leaf."""
        client = Client()
        client.send("/ch/04/eq/1", "ifff", 4, 0.75, 0.6, 0.3)
        self.assertEqual(client.get("/ch/04/eq/1/type"), [4])

    # ---- /xremote ----
    def test_xremote_propagates(self):
        """A change is sent to /xremote clients, but only when the value changes."""
        writer, listener = Client(), Client()
        listener.send("/xremote")
        time.sleep(0.05)
        listener.drain()

        writer.send("/ch/10/mix/on", "i", 0)
        address, _, args = listener.recv()
        self.assertEqual((address, args), ("/ch/10/mix/on", [0]))

        writer.send("/ch/10/mix/on", "i", 0)          # no change: nothing is sent
        listener.sock.settimeout(0.2)
        with self.assertRaises(socket.timeout):
            listener.recv()

    def test_xremote_without_padding(self):
        """b"/xremote" without padding registers the client too."""
        writer, listener = Client(), Client()
        listener.raw(b"/xremote")
        time.sleep(0.05)
        listener.drain()

        writer.send("/ch/11/mix/on", "i", 0)
        self.assertEqual(listener.recv()[0], "/ch/11/mix/on")

    # ---- meters ----
    def test_meters(self):
        """/meters/1 sends a blob with 96 values."""
        client = Client()
        client.send("/meters", "s", "/meters/1")
        address, tags, args = client.recv()
        self.assertEqual((address, tags), ("/meters/1", "b"))

        value_count = struct.unpack_from("<i", args[0])[0]
        self.assertEqual(value_count, 96)
        client.send("/unsubscribe")
        client.drain()

    def test_meters_react_to_fader(self):
        """Lowering the fader lowers the post-fader meter relative to pre-fader."""
        client = Client()
        client.send("/ch/07/mix/on", "i", 1)
        client.send("/ch/07/mix/fader", "f", 0.75)

        def channel_meters():
            """Returns (pre, gate, dyn, post) of channel 7 from /meters/6."""
            client.send("/meters", "si", "/meters/6", 6)
            time.sleep(0.3)
            client.drain()
            client.send("/meters", "si", "/meters/6", 6)
            blob = client.recv()[2][0]
            client.send("/unsubscribe")
            client.drain()
            return struct.unpack_from("<4f", blob, 4)

        pre_at_0db, _, _, post_at_0db = channel_meters()
        client.send("/ch/07/mix/fader", "f", 0.5)        # -10 dB
        pre_at_minus10, _, _, post_at_minus10 = channel_meters()

        self.assertGreater(post_at_0db, 0)
        self.assertLess(post_at_minus10 / pre_at_minus10, post_at_0db / pre_at_0db)

    def test_meter_dca(self):
        """A DCA meter shows its member's post-fader level."""
        sim = self.emu.sim
        client = Client()
        client.send("/ch/20/grp/dca", "i", 0b10000000)          # DCA 8
        time.sleep(0.3)

        self.assertEqual(sim.dca[7], sim.ch[19].post)
        self.assertEqual(len(sim.meters(5, [0, 0])), 27)

    def test_without_simulation(self):
        """With simulate=False the meters and RTA stay silent."""
        emulator = X32Emulator(Model().defaults(), simulate=False)
        values = emulator.sim.meters(1, [])                       # no step(): nothing sounding
        self.assertEqual(len(values), 96)
        self.assertTrue(all(value == 0.0 for value in values[:32]))
        self.assertTrue(all(value == -128.0 for value in emulator.sim.rta()))

    # ---- effects ----
    def test_fx_type_corrected(self):
        """Effect type 39 is CMB (corrected order)."""
        client = Client()
        client.send("/fx/1/type", "i", 39)
        self.assertEqual(client.node("fx/1"), "/fx/1 CMB\n")

    def test_fx_parameters_by_type(self):
        """Effect parameters take the format of the loaded effect type."""
        client = Client()
        client.send("/fx/2/type", "s", "DLY")
        words = client.node("fx/2/par").split()
        self.assertEqual(len(words), 1 + 64)                # the console always shows all 64

        client.send("/fx/2/par/03", "s", "X")              # Mode: enum {ST, X, M}
        _, tags, args = client.ask("/fx/2/par/03")
        self.assertEqual((tags, args), ("i", [1]))

        client.send("/fx/2/par/01", "f", 1.0)              # Mix 0..100 %
        self.assertEqual(client.node("fx/2/par").split()[1], "100")

    def test_fx_dual_complete(self):
        """Dual effects have both sides (P1A2 uses 22 parameters)."""
        client = Client()
        client.send("/fx/6/type", "s", "P1A2")
        words = client.node("fx/6/par").split()
        self.assertEqual(len(words), 1 + 64)
        self.assertNotEqual(words[22], "0")

    def test_fx_slash_text(self):
        """Effect parameters can be written as text."""
        client = Client()
        client.send("/fx/3/type", "s", "HALL")
        client.slash("/fx/3/par 40 2.00 60 5k00 10 +0.0 85 8k00 1.00 25 100 20")
        text = client.node("fx/3/par")
        self.assertIn("/fx/3/par 40 ", text)
        self.assertIn(" 60 ", text)

    def test_inserted_geq_changes_meters(self):
        """Raising the bands of an inserted GEQ raises its level change."""
        sim = self.emu.sim
        client = Client()
        client.send("/fx/7/type", "s", "GEQ")
        client.send("/ch/09/mix/03/on", "i", 1)
        client.send("/ch/09/mix/03/level", "f", 0.75)
        client.send("/bus/03/insert/sel", "s", "FX7L")
        client.send("/bus/03/insert/on", "i", 1)
        time.sleep(0.3)
        before = sim._geq_db(7)

        for band in range(1, 16):
            client.send(f"/fx/7/par/{band:02d}", "f", 1.0)
        time.sleep(0.3)
        self.assertGreater(sim._geq_db(7), before + 6)

    # ---- subscriptions and show ----
    def test_formatsubscribe(self):
        """/formatsubscribe expands the "*" and sends the values as a blob."""
        client = Client()
        client.send("/-stat/solosw/03", "i", 1)
        client.send("/formatsubscribe", "ssiii", "hidden/solo", "/-stat/solosw/**", 1, 80, 1)
        address, tags, args = client.recv()
        client.send("/unsubscribe")
        client.drain()

        self.assertEqual((address, tags), ("hidden/solo", "b"))
        values = struct.unpack("<81i", args[0])
        self.assertEqual(values[0], 80)
        self.assertEqual(values[1:5], (0, 0, 1, 0))

    def test_showdump(self):
        """/showdump starts with the show header."""
        _, _, args = Client().ask("/showdump")
        self.assertTrue(args[0].startswith("/-show/showfile/show"))

    def test_libs_040_and_050(self):
        """The library nodes 040 and 050 exist (wrong names in the C tables)."""
        client = Client()
        for number in ("030", "040", "050"):
            self.assertEqual(client.node(f"-libs/ch/{number}"),
                             f'/-libs/ch/{number} 0 "" 0 %0000000000000000 0\n')

    def test_no_empty_node(self):
        """Every node header returns values or goes down to a node with values."""
        model = self.emu.model
        empty = [header for header in model.nodes if model.node_for(header)[1] == []]
        self.assertEqual(empty, [])

    # ---- RTA ----
    def test_rta_shows_eq(self):
        """A post-EQ RTA shows a +15 dB boost around 1 kHz."""
        client = Client()
        client.send("/-stat/rtasource", "i", 98 + 11)            # channel 12, post-EQ
        client.send("/ch/12/eq/on", "i", 1)
        client.send("/", "s", "/ch/12/eq/3 PEQ 1k02 +15.00 2.0")
        client.recv()
        client.send("/meters", "si", "/meters/15", 0)
        time.sleep(0.2)
        client.drain()

        client.send("/meters", "si", "/meters/15", 0)
        _, _, args = client.recv()
        client.send("/unsubscribe")
        client.drain()

        word_count = struct.unpack_from("<i", args[0])[0]
        shorts = struct.unpack_from("<100h", args[0], 4)
        self.assertEqual(word_count, 50)

        band_db = [value / 256 for value in shorts]
        near_1k = max(band_db[55:65])
        lows = sum(band_db[0:20]) / 20
        self.assertGreater(near_1k, lows + 8)

    def test_setrtasrc(self):
        """/-action/setrtasrc also sets /-stat/rtasource."""
        client = Client()
        client.send("/-action/setrtasrc", "i", 98)
        self.assertEqual(client.get("/-stat/rtasource"), [98])

    # ---- X32-Edit and text round trips ----
    def test_x32edit_lines(self):
        """Lines that failed with X32-Edit (PC -> mixer)."""
        client = Client()
        client.slash("/config/solo -oo LRPFL -18.0 PFL PFL PFL OFF OFF OFF -40 OFF OFF OFF 0.3 OFF OFF OFF")
        self.assertEqual(client.get("/config/solo/source"), [3])

        client.slash("/config/routing/OUT AN1-4 OUT5-8 OUT9-12 AN13-16")
        self.assertIn("AN1-4 OUT5-8 OUT9-12 AN13-16", client.node("config/routing/OUT"))

        client.slash("/config/dp48 %0101 0")
        self.assertEqual(client.get("/config/dp48/scope"), [5])

        client.slash("/outputs/main/04/delay ON 12.0")
        self.assertEqual(client.get("/outputs/main/04/delay/on"), [1])

        client.slash("/outputs/aux/06 3 POST OFF")
        self.assertIn(" POST ", client.node("outputs/aux/06"))

    def test_round_trip_of_all_nodes(self):
        """For every node: text -> values -> text must give the same."""
        model = Model().defaults()
        failures = []

        for header, leaves in model.nodes.items():
            if not leaves or header.startswith("/fx/"):
                continue
            for addr in leaves:
                param = model.params[addr]
                text = param.fmt.fmt(param.value)
                try:
                    if param.fmt.fmt(param.fmt.parse(text)) != text:
                        failures.append((addr, text))
                except Exception as error:
                    failures.append((addr, text, repr(error)))

        self.assertEqual(failures[:10], [])

    def test_real_console_lines(self):
        """Lines from a capture of an X32 Producer, firmware 4.13.

        The emulator must accept them and return them unchanged (up to whitespace).
        """
        real_lines = [
            '/main/st/mix/01 ON 0.0 +0 POST 1',
            '/main/m/mix/01 ON -oo +0 POST 0',
            '/config/dp48 %0000 0 AESA',
            '/outputs/rec/01 31 PRE',
            '/-prefs/key QWERTY',
            '/-show/showfile/show "MyShow" 0 0 0 0 0 0 0 0 0 0 "4.06"',
            '/-usb "" ""',
            '/-stat Ch15 2 3 OFF 0 0 ON OFF 0 ON OFF OFF OFF BAR BAR OFF OFF 0 2 ON OFF 0',
            '/-show/showfile/cue/000 100 "1 Culto" 0 2 -1 0 1 0 0',
            '/-show/showfile/scene/011 "3 Domingo 2.0" "" %000000000 1',
            '/-show/showfile/snippet/000 "CCJ" 134217727 -1 -1 65535 1',
            '/-prefs/rta 80% 18 ON 0 PRE BAR %001100 PEAK 1.00 3s 1 3',
            '/ch/13/gate ON EXP4 -62.5 60.0 10 447 1677 0',
            '/ch/17/dyn ON COMP RMS LOG -33.0 5.0 1 6.00 71 28.2 101 POST 0 100 ON',
            '/ch/21/eq/3 PEQ 1k91 +0.00 1.9',
            '/config/routing/IN UIN1-8 A9-16 A17-24 A25-32 AUX1-4',
        ]
        client = Client()
        for line in real_lines:
            client.slash(line)
            path = line.split(" ", 1)[0].lstrip("/")
            self.assertEqual(client.node(path).split(), line.split(), line)

        # single values, in the console format
        single_values = [
            ("/ch/30/preamp/hpf", "173"), ("/ch/30/gate/hold", "50.2"),
            ("/ch/30/dyn/hold", "0.09"), ("/ch/30/dyn/mgain", "3.50"),
            ("/ch/31/dyn/mgain", "18.5"), ("/ch/30/eq/4/f", "403.7"),
            ("/headamp/016/gain", "+0.0"), ("/ch/31/preamp/trim", "+0.0"),
            ("/ch/31/preamp/hpf", "49"), ("/config/osc/f1", "632.5"),
            ("/ch/30/eq/1/g", "-11.2"), ("/ch/30/eq/2/g", "+0.00"),
            ("/ch/30/eq/2/q", "10"), ("/ch/30/eq/3/f", "1k21"),
            ("/ch/30/mix/01/level", "-9.3"), ("/ch/30/preamp/trim", "+2.3"),
            ("/ch/30/mix/fader", "0.0"), ("/config/tape/gainL", "11.0"),
            ("/config/solo/sourcetrim", "0.0"),
        ]
        for path, text in single_values:
            param = self.emu.model.params[path]
            param.value = param.fmt.parse(text)
            self.assertEqual(param.fmt.fmt(param.value), text, path)

        client.send("/fx/4/type", "s", "HALL")
        self.assertEqual(len(client.node("fx/4/par").split()), 1 + 64)
        client.slash("/config/routing/IN AN1-8 AN9-16 AN17-24 AN25-32 AUX1-4")

    def test_exact_real_console_text(self):
        """Lines from an X32P 4.13, compared character by character (spaces included)."""
        real_lines = [
            '/bus/05/dyn ON COMP RMS LOG -37.0 2.5 0 0.00 98 39.9   9 POST 53 100 OFF',
            '/bus/01/dyn OFF COMP RMS LOG -45.5 2.0 1 4.50 10 10.0  173 POST 0 100 ON',
            '/bus/01/eq/2 VEQ 990.9 -1.25  10',
            '/bus/09/mix ON -17.9 OFF +0 OFF  +0.3',
            '/outputs/main/02/delay OFF   1.6',
            '/dca/1 ON  +5.5',
            '/main/st/mix/01 ON   0.0 +0 POST 1',
            '/fx/2/par 20 1.00 60 5k74 25 0.0 83 7k2 0.95 25 50 30' + ' 0' * 52,
            '/fx/4/par 20 1.4 100 ON REAR 0.0 76 11k3 0.90 0.50' + ' 0' * 54,
            '/fx/6/par ON -8.0 5.0 2 50 -8.5 55' + ' 0' * 57,
        ]
        client = Client()
        for command in ("/fx/2/type HALL", "/fx/4/type VREV", "/fx/6/type FAC"):
            address, effect = command.split()
            client.send(address, "s", effect)

        for line in real_lines:
            client.slash(line)
            path = line.split(" ", 1)[0].lstrip("/")
            self.assertEqual(client.node(path), line + "\n")

    # ---- routing and headamps ----
    def test_headamp_per_channel(self):
        """/-ha/NN/index follows the routing, and a headamp gain only affects its channel."""
        client = Client()
        self.assertEqual(client.get("/-ha/04/index"), [4])
        self.assertEqual(client.get("/-ha/31/index"), [31])

        client.send("/config/routing/IN/25-32", "s", "A1-8")   # channels 25-32 come from AES50 A
        self.assertEqual(client.get("/-ha/27/index"), [35])
        client.send("/config/routing/IN/25-32", "s", "AN25-32")

        sim = self.emu.sim
        for strip in sim.ch[29:31]:
            strip.source = lambda now: -60.0                    # constant signal to compare
        time.sleep(0.2)
        channel_30_before, channel_31_before = sim.ch[29].pre, sim.ch[30].pre

        client.send("/headamp/029/gain", "f", 1.0)              # +60 dB on channel 30 (was +20)
        time.sleep(0.2)
        self.assertAlmostEqual(sim.ch[29].pre - channel_30_before, 40, delta=1)
        self.assertAlmostEqual(sim.ch[30].pre, channel_31_before, delta=0.1)

        client.send("/headamp/029/gain", "f", 32 / 72)
        for strip in sim.ch[29:31]:
            del strip.source

    def test_real_scene_routing_stagebox(self):
        """Excerpt of the real scene sent by X32-Edit.

        Inputs 1-32 from AES50 A (A1-8..A25-32) and aux 1-6 from the local
        analogue inputs (AN1-6).
        """
        client = Client()
        for block, value in (("1-8", 4), ("9-16", 5), ("17-24", 6), ("25-32", 7)):
            client.send(f"/config/routing/IN/{block}", "i", value)
        client.send("/config/routing/IN/AUX", "i", 3)

        indices = [client.get(f"/-ha/{channel:02d}/index")[0] for channel in (0, 31, 32, 37, 38)]
        self.assertEqual(indices, [32, 63, 0, 5, -1])

        for block, value in (("1-8", 0), ("9-16", 1), ("17-24", 2), ("25-32", 3)):
            client.send(f"/config/routing/IN/{block}", "i", value)
        client.send("/config/routing/IN/AUX", "i", 0)

    def test_headamp_by_user_routing(self):
        """Real capture (X32P 4.13): IN 1-8 = UIN1-8, userrout/in/01 = 33 -> headamp 32."""
        client = Client()
        client.send("/config/userrout/in/01", "i", 33)
        client.send("/config/userrout/in/07", "i", 135)          # card: no headamp
        client.send("/config/routing/IN/1-8", "s", "UIN1-8")

        self.assertEqual(client.get("/-ha/00/index"), [32])
        self.assertEqual(client.get("/-ha/06/index"), [-1])

        client.send("/config/routing/IN/1-8", "s", "AN1-8")
        client.send("/config/userrout/in/01", "i", 0)
        client.send("/config/userrout/in/07", "i", 0)

    # ---- simulation ----
    def test_matrix_fed_from_main(self):
        """A matrix fed post-fader from main LR has main LR's level."""
        sim = self.emu.sim
        client = Client()
        client.send("/main/st/mix/04/on", "i", 1)
        client.send("/main/st/mix/04/type", "i", 4)            # POST
        client.send("/main/st/mix/04/level", "f", 0.75)
        time.sleep(0.3)

        self.assertGreater(sim.mtx[3].pre, -100)
        self.assertAlmostEqual(sim.mtx[3].pre, sim.main[0].post, delta=0.5)

    def test_eq_changes_level(self):
        """A +15 dB low shelf raises the level of pink noise."""
        sim = self.emu.sim
        client = Client()
        strip = sim.ch[22]
        time.sleep(0.2)
        before = strip.eq_db

        client.slash("/ch/23/eq/1 LShv 200.0 +15.00 0.7")
        time.sleep(0.2)
        self.assertGreater(strip.eq_db, before + 8)          # ~+9.7 dB on pink noise

    def test_mute_group(self):
        """A mute group cuts post-fader and pre-fader sends, but not the pre-fader meter."""
        sim = self.emu.sim
        client = Client()
        client.send("/ch/13/grp/mute", "i", 0b000100)          # channels 13 and 14 in mute group 3
        client.send("/ch/14/grp/mute", "i", 0b000100)
        client.send("/ch/13/mix/12/on", "i", 1)
        client.send("/ch/13/mix/12/level", "f", 0.75)
        time.sleep(0.2)
        self.assertGreater(sim.ch[12].post, -100)

        client.send("/config/mute/3", "s", "ON")
        time.sleep(0.2)
        self.assertLess(sim.ch[12].post, -150)            # post-fader muted
        self.assertLess(sim.ch[13].post, -150)
        self.assertGreater(sim.ch[12].pre, -100)          # pre-fader keeps going (like the console)
        self.assertLess(sim.bus[11].pre, -150)            # but the pre-fader send is also cut
        client.send("/config/mute/3", "i", 0)

    # ---- state and capture ----
    def test_state_in_folder(self):
        """The state is saved as a folder tree and read back; the old single file still works."""
        import tempfile, json, pathlib
        from x32emu import datafiles

        model = Model().defaults()
        model.params["/ch/01/config/name"].value = "Mic1"
        folder = pathlib.Path(tempfile.mkdtemp())
        datafiles.save_state({addr: param.value for addr, param in model.params.items()}, folder / "state")

        self.assertTrue((folder / "state" / "ch" / "01.json").exists())
        loaded = datafiles.load_state(folder / "state")
        self.assertEqual(loaded["/ch/01/config/name"], "Mic1")
        self.assertEqual(len(loaded), len(model.params))

        # old format (a single file) is still read
        (folder / "old.json").write_text(json.dumps({"/ch/02/config/name": "Vox"}))
        self.assertEqual(datafiles.load_state(folder / "old.json")["/ch/02/config/name"], "Vox")

    def test_state_with_empty_file(self):
        """An empty or truncated state file must not prevent startup."""
        import tempfile, pathlib
        from x32emu import datafiles

        folder = pathlib.Path(tempfile.mkdtemp()) / "state"
        model = Model().defaults()
        model.params["/ch/03/config/name"].value = "Vox"
        datafiles.save_state({addr: param.value for addr, param in model.params.items()}, folder)

        (folder / "ch" / "01.json").write_text("")                                 # empty
        (folder / "bus" / "02.json").write_text('{"_prefix": "/bus/02", "valu')   # truncated

        with self.assertLogs("x32emu", "WARNING") as logs:
            loaded = datafiles.load_state(folder)
        self.assertEqual(len(logs.output), 2)
        self.assertEqual(loaded["/ch/03/config/name"], "Vox")
        self.assertNotIn("/ch/01/config/name", loaded)
        self.assertEqual(list(folder.rglob("*.tmp")), [])       # atomic write leaves no leftovers

    def test_capture_read_only(self):
        """The capture must not change anything and refuses any write."""
        import tempfile
        from x32emu.capture import capture, _safe

        writes = [("/ch/01/mix/fader", "f", (0.5,)), ("/", "s", ("/ch/01/mix ON",)),
                  ("/save", "siss", ("scene", 1, "a", "b")), ("/-action/initall", "", ()),
                  ("/node", "s", ("-action",))]
        for message in writes:
            with self.assertRaises(RuntimeError):
                _safe(*message)

        params = self.emu.model.params
        before = {addr: param.value for addr, param in params.items()}
        folder = capture("127.0.0.1", PORT, tempfile.mkdtemp() + "/cap", window=8)
        after = {addr: param.value for addr, param in params.items()}

        changed = [addr for addr in before if before[addr] != after[addr] and not addr.startswith("/-stat/")]
        self.assertEqual(changed, [])
        self.assertTrue((folder / "nodes" / "ch.txt").exists())
        self.assertEqual((folder / "no_reply.txt").read_text().strip(), "")


class FxSpecTest(unittest.TestCase):
    def test_every_fx_param_builds_a_format(self):
        """Every effect spec has the fields fx_formatter needs (a missing "steps" broke libfx loads)."""
        from x32emu.model import fx_formatter
        for name, params in Model().fx.items():
            for index, spec in enumerate(params):
                with self.subTest(fx=name, par=index + 1):
                    fx_formatter(spec)


if __name__ == "__main__":
    unittest.main(verbosity=2)
