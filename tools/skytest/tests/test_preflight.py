"""Tests for preflight.py: a spec's mistakes found from files alone (made-up level data, made-up source,
made-up runs; no game). Each case is one of the mistakes of the 18 November confirm runs.
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_preflight.py"
"""
from __future__ import annotations

import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import preflight as pf  # noqa: E402

PROBE = """Level manifest: lvl (level request 2 in game.log)
Level start: PlayerStartAll at 0.0 0.0 0.0
       0        0.0       0.0      0.0  CPlayerStartEntity         PlayerStartAll                       [lvl#1] registered
    5000     5000.0       0.0      0.0  CPhysicalEntity            Chest_A                              [lvl#10] asleep
    5000     5000.0     100.0      0.0  CGameEntity                Anchor_A                             [lvl#11] registered
    9000     9000.0       0.0      0.0  CPhysicalEntity            (no name)                            [lvl#12] asleep
   20000    20000.0       0.0      0.0  CPhysicalEntity            Chest_Far                            [lvl#13] asleep
     200      200.0       0.0      0.0  CScriptTriggerEntity       (no name)                            [lvl#14] registered
     250      250.0       0.0      0.0  CPhysicalEntity            Crate_In_Box                         [lvl#15] registered
   30000    30000.0       0.0      0.0  CPhysicalEntity            (no name)                            [lvl#16] skipped
"""
LOGIC = ("StartBox  CScriptTriggerEntity  [trigger: CScriptTriggerEntity]  at (200.0, 0.0, 0.0)  box 200 x 200 x 200 "
         "world (100, -100, -100)..(300, 100, 100) turned 0 deg  triggerFlags 417 = eSTF_ResetOnDeactivate|eSTF_SingleFire\n")
SOURCE = r'''
void A() {
  REXLOG_INFO("Online: spill own {} {} ({})", label, spill, why);
  REXLOG_INFO("Online: chest removed {:08x} ({} entity {:08x}: its tracker is collected)", i, n, m);
  return std::string("Online: start refused ") + kind + " " + label + " (" + why + ")";
}
const char* R(int r) { switch (r) { case 1: return "leased"; } return "none"; }
void B() { Line(lines_, "Online: backend gate.open run " + label + " key " + key); }
void C() { REXLOG_INFO("Dev: {} {} done ({}) after {:.1f} s", cmd, who, how, s); }
void D() { REXLOG_INFO("Online: notice: " "Swap Zones are not available in online play yet."); }
void E() { return "dev_defeat_commands is off"; }
'''


def level() -> pf.Level:
    lv = pf.parse_probe_manifest(PROBE, "lvl")
    lv.static_names = {"basket012": [(9000.0, 0.0, 0.0)]}
    lv.triggers = pf.parse_logic_triggers(LOGIC)
    return lv


def corpus() -> pf.Corpus:
    c = pf.Corpus()
    c.add(pf.extract_formats(SOURCE))
    c.text = SOURCE
    c.lit_text = "\n".join("\x00".join(f.pieces) for f in c.formats)
    return c


def ctx(**kw) -> pf.Context:
    base = dict(levels={"lvl": level()}, corpus=corpus(), wakes=None,
                allow={"dev_authored_probe", "dev_defeat_commands", "online_shared_loot"},
                conditions={"online_barrier", "online_left", "online_alone", "title_ready"})
    base.update(kw)
    return pf.Context(**base)


def spec(text: str) -> dict:
    return tomllib.loads(text)


def texts(probs: list[pf.Problem], sev: str | None = None) -> list[str]:
    return [p.line() for p in probs if sev is None or p.severity == sev]


H_HEAD = 'layout = "server+2"\n[peer.H]\nargs = ["--dev_authored_probe=true"]\nscript = """\nlevel lvl\n'
D_HEAD = '[peer.D]\nargs = ["--dev_authored_probe=true"]\nscript = """\n'


class Formats(unittest.TestCase):
    def test_fmt_placeholders_and_concatenation(self):
        c = corpus()
        fits = lambda r: any(pf.compatible(r, f) for f in c.formats)  # noqa: E731
        self.assertTrue(fits("spill own TreasureChest_3 w"))           # a placeholder takes the name
        self.assertTrue(fits("chest removed "))
        self.assertTrue(fits("start refused start.personal Chest_1#"))  # `+` concatenation: free end
        self.assertTrue(fits(" key "))
        self.assertTrue(fits("Swap Zones are not available in online play yet."))  # adjacent literals joined
        self.assertFalse(fits("spill own TreasureChest_3 w0 0#1 (S's selection"))   # "selection" sits in a field
        self.assertFalse(any(pf.compatible("spill own a b (c) and more", f, share=0.75) for f in c.formats))
        self.assertFalse(fits("Swap Zones are now available"))  # a literal with no field: its text only
        self.assertFalse(fits("collected tracker"))
        self.assertFalse(fits("chest moved "))

    def test_literal_runs(self):
        self.assertEqual(pf.literal_runs(r"Online: chest removed \S+ \(TreasureChest_3 "),
                         ["Online: chest removed ", " (TreasureChest_3 "])
        self.assertEqual(pf.literal_runs(r"a (b|c) dd"), ["a ", " dd"])
        self.assertEqual(pf.literal_runs(r"x|y"), [])
        self.assertEqual(pf.literal_runs(r"items?, cap"), ["item", ", cap"])

    def test_pattern_problem(self):
        c = corpus()
        self.assertEqual(pf.pattern_problem(r"Online: start refused start\.personal \S+ \(leased\)", c), "")
        self.assertIn("no log text", pf.pattern_problem(r"Online: chest vanished \S+", c))
        self.assertIn("no log statement", pf.pattern_problem(r"Online: collected tracker \S+", c))
        self.assertEqual(pf.pattern_problem(r"Online: chest removed in a spill", c), "")   # half its letters fit
        # two parts that fit two different statements
        self.assertIn("different log statements",
                      pf.pattern_warning(r"Online: spill own \S+ \S+ \S+ entity \S+: its tracker is collected", c))
        # a word the script itself passes is a value, not wording
        self.assertIn("no log text", pf.pattern_problem(r"Dev: probe coldscorp armed", c))


class Objects(unittest.TestCase):
    def test_unnamed_name_and_missing_auth(self):
        d = spec(H_HEAD + 'goto_entity Basket012\ngoto_entity auth:lvl:99\nonline_end\n"""\n')
        out = texts(pf.check_spec("c", d, ctx()), "error")
        self.assertTrue(any("Basket012: has no run-time name" in x and "auth:lvl:12" in x for x in out), out)
        self.assertTrue(any("no object lvl#99" in x for x in out), out)

    def test_asleep_by_name_error_unless_streamed(self):
        d = spec(H_HEAD + 'interact Chest_A 30\n"""\n')
        self.assertTrue(any("Chest_A is asleep at the load" in x for x in texts(pf.check_spec("c", d, ctx()), "error")))
        d = spec(H_HEAD + 'goto_entity Anchor_A\ninteract Chest_A 30\n"""\n')
        p = pf.check_spec("c", d, ctx())
        self.assertFalse(texts(p, "error"))
        self.assertTrue(any("streamed its area in" in x for x in texts(p, "warn")))
        d = spec(H_HEAD + 'goto_entity auth:lvl:10\ninteract Chest_A 30\n"""\n')
        self.assertFalse(texts(pf.check_spec("c", d, ctx()), "error"))

    def test_skipped_and_never_awake(self):
        w = pf.WakeIndex(zone_hash={"lvl": "aaaaaaaa"}, awake={("aaaaaaaa", 10)}, runs_of={"aaaaaaaa": 3})
        d = spec(H_HEAD + 'goto_entity auth:lvl:13\ngoto_entity auth:lvl:10\ngoto_entity auth:lvl:16\n"""\n')
        p = pf.check_spec("c", d, ctx(wakes=w))
        self.assertTrue(any("Chest_Far lvl#13 is asleep at the load and was never seen awake in 3" in x
                            for x in texts(p, "warn")))
        self.assertFalse(any("lvl#10" in x for x in texts(p)))
        self.assertTrue(any("lvl#16 is not registered" in x for x in texts(p, "error")))

    def test_single_fire(self):
        d = spec(H_HEAD + 'enter_trigger auth:lvl:14\ngoto_entity Crate_In_Box\n"""\n' + D_HEAD
                 + 'enter_trigger auth:lvl:14\n"""\n')
        w = texts(pf.check_spec("c", d, ctx()), "warn")
        self.assertTrue(any("single-fire and was already entered" in x and " D:" in x for x in w), w)
        self.assertTrue(any("inside single-fire trigger StartBox" in x for x in w), w)


class Waits(unittest.TestCase):
    def test_handover_asleep_and_level_start(self):
        d = spec(H_HEAD + 'online_wait_peer_near_entity auth:lvl:10 300 60\n'
                 'online_wait_peer_near_entity PlayerStartAll 300 60\n"""\n')
        p = pf.check_spec("c", d, ctx())
        self.assertTrue(any("is asleep on this game: the check fails at once" in x for x in texts(p, "error")))
        self.assertTrue(any("S's hero stands" in x for x in texts(p, "warn")))

    def test_conditions_end_peers_markers(self):
        d = spec('layout = "server+1"\n[peer.H]\nscript = """\nwaitfor bogus 10\nonline_wait_peers 2 60\n'
                 'online_end\nwaitfor online_barrier 60\nlog MARK_A\n"""\n'
                 '[peer.S]\nstart_after = "Input script: NEVER_LOGGED"\n')
        e = texts(pf.check_spec("c", d, ctx()), "error")
        self.assertTrue(any("bogus is not a registered condition" in x for x in e), e)
        self.assertTrue(any("starts 1 client" in x for x in e), e)
        self.assertTrue(any("after this script's own online_end" in x for x in e), e)
        self.assertTrue(any("no script logs that marker" in x for x in e), e)

    def test_online_left_race(self):
        h = H_HEAD + 'goto_entity Anchor_A\nonline_end\n"""\n'
        d_ = D_HEAD + 'goto_entity Anchor_A\ninteract Anchor_A 10\nwaitfor online_left 600\n"""\n'
        self.assertTrue(any("arms only on a check while connected" in x
                            for x in texts(pf.check_spec("c", spec(h + d_), ctx()), "warn")))
        h2 = H_HEAD + 'goto_entity Anchor_A\nwaitfor online_alone 120\nonline_end\n"""\n'
        self.assertFalse(any("arms only" in x for x in texts(pf.check_spec("c", spec(h2 + d_), ctx()))))


class Lines(unittest.TestCase):
    def test_markers(self):
        d = spec(H_HEAD + 'log FIRST_MARK\nlog SECOND_MARK\n"""\n'
                 "expect_order = ['Input script: SECOND_MARK', 'Input script: FIRST_MARK']\n"
                 + D_HEAD + 'log D_MARK\n"""\n' + "expect = ['Input script: FIRST_MARK']\n")
        e = texts(pf.check_spec("c", d, ctx()), "error")
        self.assertTrue(any("in another order" in x for x in e), e)
        self.assertTrue(any("peer H does" in x for x in e), e)

    def test_unnamed_object_in_pattern(self):
        d = spec(H_HEAD + 'goto_entity auth:lvl:12\n"""\n' + "expect = ['Online: spill own Basket012 w']\n")
        self.assertTrue(any("Basket012 has no run-time name" in x for x in texts(pf.check_spec("c", d, ctx()), "error")))


class Settings(unittest.TestCase):
    def test_allow_fatal_off_and_probe(self):
        d = spec('layout = "server+1"\n[peer.H]\nargs = ["--made_up=true"]\nscript = """\nlevel lvl\n'
                 'fight_near Anchor_A 1500 30\ngoto_entity auth:lvl:10\n"""\n')
        e = texts(pf.check_spec("c", d, ctx()), "error")
        self.assertTrue(any("made_up: no row" in x for x in e), e)
        self.assertTrue(any("needs --dev_defeat_commands=true" in x for x in e), e)
        self.assertTrue(any("needs --dev_authored_probe=true" in x for x in e), e)

    def test_table_rows_rest_on_source_text(self):
        stale = pf.check_table(corpus())
        self.assertFalse(any("fight_near" in x for x in stale))      # its text is in SOURCE
        self.assertTrue(any("dev_lock_watch" in x for x in stale))   # not in SOURCE: reported stale


class RecordedRuns(unittest.TestCase):
    def test_wake_scan_replay_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, extra in (("20261118-200000-c", ""), ("20261118-210000-c", "Online: X done\n")):
                h = root / name / "H"
                h.mkdir(parents=True)
                (h / "game.log").write_text(
                    "Online: authored walk lvl request 2 zone a\\b\\lvl hash 0629f659 walk 1\n"
                    "Online: authored wake 0629f659 1 10 mid none -> f5c pos 1 2 3 Chest_A#1\n" + extra,
                    encoding="utf-8")
            w = pf.build_wake_index([root], root / "cache.json")
            self.assertTrue(w.seen_awake("lvl", 10))
            self.assertFalse(w.seen_awake("lvl", 13))
            self.assertIsNone(w.seen_awake("other", 1))
            self.assertTrue((root / "cache.json").is_file())
            self.assertEqual(pf.build_wake_index([root], root / "cache.json").awake, w.awake)  # from the cache
            run = pf.latest_run([root], "c")
            self.assertEqual(run.name, "20261118-210000-c")
            d = spec("[peer.H]\nexpect = ['Online: X done', 'Online: Y done']\nforbid = ['authored wake']\n")
            out = texts(pf.replay("c", d, run))
            self.assertTrue(any("'Online: Y done' would fail" in x for x in out), out)
            self.assertTrue(any("forbid 'authored wake' would hit" in x for x in out), out)
            self.assertFalse(any("X done" in x for x in out))


if __name__ == "__main__":
    unittest.main()
