"""Tests for the level pass's derived end (levelpass_end.py and its steps in levelpass.py).

Made-up logic first (the walk's rules: a trigger, a counted fight, a timed level, the self role, another
zone, the counter's own unit). Then the stored data of tutorial_a, tutorial_b and ww1: the end steps the
generator makes for them, checked against the worked answer (logs/level-ends-plan.txt, 18 November 2026).
The stored data is derived from the disc and lives outside the repository (tests/skytest.toml's
[levelpass] folder, or SFO_LEVELPASS_FOLDER); those tests are skipped where it is not. Their probe ids are
the probe manifests' of 18 November: a new probe run renumbers them, and the expected ids here with it.
Run: python -I -m unittest discover -s tools/skytest/tests -p "test_levelpass_end.py"
"""
from __future__ import annotations

import os
import re
import sys
import tomllib
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import levelpass as lp  # noqa: E402
import levelpass_end as le  # noqa: E402


def node(key, cls="CEntity", group="other", pos=(0.0, 0.0, 0.0), zone="lvl"):
    unit, _, name = key.partition(".")
    return {"key": key, "name": name, "unit": unit, "zone": zone, "class": cls, "group": group, "kind": cls,
            "position": list(pos)}


def edge(frm, to, message, when, fields=None, roles=None):
    return {"from": frm, "to": to, "message": message, "fields": fields or {}, "when": when, "roles": roles}


ON = "messenger on {m}  activationFlags 0 = none (default)  count {n}  max 1  delay 0 (default)"
ENTER = "messenger activationFlags 8 = eMAF_OnEnter  count 1 (default)  max 1  delay 0 (default)"
DEATH = "messenger activationFlags 4 = eMAF_OnDeath  count 1 (default)  max 1  delay 0 (default)"
SPAWNEE = "on a spawnee: " + DEATH


def trigger_level():
    """A trigger starts a conversation; its end plays a cutscene; the cutscene's end loads the map."""
    nodes = [node("u.Outro_Trigger", "CScriptTriggerEntity", "trigger"), node("u.Npc", "CActor", "npc"),
             node("u.Movie", "CEntity", "cutscene"), node("u.Gate", "CGameEntity", "lock")]
    edges = [
        edge("u.Outro_Trigger", ["u.Npc"], "CConversationMessage",
             "messenger on Scripts.StringMessage {MessageString: InteractionMessage}  activationFlags 40 = eMAF_OnEnter|0x20  count 1"),
        edge("u.Gate", ["u.Npc"], "CFireBehaviorEventMessage", ON.format(m="Scripts.StringMessage {MessageString: EnemyDead}", n=14)),
        edge("u.Npc", ["u.Movie"], "Scripts.StringMessage", "CJuiceConversationData._endTelegrams", {"MessageString": "Go"}),
        edge("u.Movie", [], "Scripts.CutscenePlayerMessage", ON.format(m="Scripts.StringMessage {MessageString: Go}", n=1), roles="1"),
        edge("u.Movie", ["u.Movie"], "Scripts.CutscenePlayerMessage", "when its cutscene X ends (inferred)", {"MessageType": "Done"}),
        edge("u.Movie", [], "CLoadMapComponentMessage", ON.format(m="Scripts.CutscenePlayerMessage {MessageType: Done}", n=1), roles="4"),
    ]
    chain = ("{Npc} -[Scripts.StringMessage]-> Movie  =>  (inferred) Movie -[Scripts.CutscenePlayerMessage {MessageType: Done}]-> "
             "Movie  =>  Movie -[CLoadMapComponentMessage {_fade: true}]-> roles 4")
    return [chain], {"schema": 1, "nodes": nodes, "edges": edges}


def fight_level():
    """A counter of 3 deaths in unit `a` (two placed enemies, one spawner), a far sender of another unit,
    a start trigger, a wave the counter starts; the outro loads the map."""
    nodes = [node("a.Counter", pos=(0, 0, 400)), node("a.Lobber", "CActor", "enemy", (100, 0, 0)),
             node("a.Chompy", "CActor", "enemy", (300, 0, 0)), node("a.SpawnPoint", "CEntity", "spawner", (180, 20, 10)),
             node("a.Plane", "CPhysicalEntity", "spawner", (3000, 0, 500)),
             node("far.Bomber", "CPhysicalEntity", "spawner", (90000, 0, 0)),
             node("h.Start_Fight", "CScriptTriggerEntity", "trigger", (50, 50, 0)),
             node("swap.Zone_Trigger", "CScriptTriggerEntity", "trigger", (60, 60, 0), zone="swapzone"),
             node("a.Outro", "CEntity", "cutscene")]
    im = {"MessageString": "ImDead"}
    edges = [edge(s, ["a.Counter"], "Scripts.StringMessage", DEATH, im) for s in ("a.Lobber", "a.Chompy")]
    edges += [edge(s, ["a.Counter"], "Scripts.StringMessage", SPAWNEE, im) for s in ("a.SpawnPoint", "a.Plane", "far.Bomber")]
    edges += [edge("h.Start_Fight", ["a.Lobber"], "Scripts.LeaveJobStateMessage", ENTER),
              edge("swap.Zone_Trigger", ["a.Chompy"], "Scripts.LeaveJobStateMessage", ENTER),
              edge("a.Counter", ["a.Plane"], "Scripts.StringMessage", ON.format(m="Scripts.StringMessage {MessageString: ImDead}", n=1),
                   {"MessageString": "StartSpawnPlane"}),
              edge("a.Counter", ["a.Outro"], "Scripts.CutscenePlayerMessage", ON.format(m="Scripts.StringMessage {MessageString: ImDead}", n=3)),
              edge("a.Outro", ["a.Outro"], "Scripts.CutscenePlayerMessage", "when its cutscene Y ends (inferred)", {"MessageType": "Done"}),
              edge("a.Outro", [], "CLoadMapComponentMessage", ON.format(m="Scripts.CutscenePlayerMessage {MessageType: Done}", n=1), roles="4")]
    chain = ("{Lobber, Chompy} -[Scripts.StringMessage {MessageString: ImDead}]-> Counter  =>  Counter [x3] -[Scripts.CutscenePlayerMessage]-> Outro"
             "  =>  Outro -[CLoadMapComponentMessage {_fade: true}]-> roles 4")
    return [chain], {"schema": 1, "nodes": nodes, "edges": edges}


class Walk(unittest.TestCase):
    def test_a_trigger_through_a_conversation_and_a_cutscene_played_by_itself(self):
        chains, logic = trigger_level()
        e = le.derive_end(chains, logic, "lvl")
        self.assertEqual((e.kind, e.trigger), (le.TRIGGER, "u.Outro_Trigger"))
        self.assertEqual(e.path, ["Outro_Trigger", "Npc", "Movie"])
        # The gate's count of 14 sends a behavior event, not what starts the conversation: not on the path.
        self.assertNotIn("Gate", e.path)

    def test_without_the_self_role_the_cutscene_has_no_cause(self):
        chains, logic = trigger_level()
        for ed in logic["edges"]:
            if ed["roles"] == "1":
                ed["roles"] = None
        self.assertEqual(le.derive_end(chains, logic, "lvl").kind, le.TIMED)

    def test_a_trigger_of_another_zone_is_never_the_end(self):
        chains, logic = trigger_level()
        logic["nodes"][0]["zone"] = "swapzone"
        self.assertEqual(le.derive_end(chains, logic, "lvl").kind, le.TIMED)

    def test_a_counted_fight(self):
        chains, logic = fight_level()
        e = le.derive_end(chains, logic, "lvl")
        self.assertEqual((e.kind, e.counter, e.count), (le.FIGHT, "a.Counter", 3))
        # Its own unit's senders only (far.Bomber names the same counter from another unit).
        self.assertEqual(e.counted, ["a.Chompy", "a.Lobber", "a.Plane", "a.SpawnPoint"])
        self.assertEqual(e.enemies, ["a.Chompy", "a.Lobber"])
        # The start trigger of the level's own zone; the swap zone's is never one.
        self.assertEqual(e.starters, ["h.Start_Fight"])
        # The anchor does not die (a spawner, not an enemy) and is nearest the placed enemies (not the plane).
        self.assertEqual(e.anchor, "a.SpawnPoint")
        self.assertEqual(e.waves, [1])
        self.assertEqual(e.path, ["Counter", "Outro"])

    def test_a_fight_of_one(self):
        chains, logic = fight_level()
        for ed in logic["edges"]:
            if ed["from"] == "a.Counter" and ed["to"] == ["a.Outro"]:
                ed["when"] = ON.format(m="Scripts.StringMessage {MessageString: ImDead}", n=1)
        e = le.derive_end(chains, logic, "lvl")
        self.assertEqual(e.kind, le.FIGHT)
        self.assertEqual(e.count, 1)
        self.assertEqual(e.counter, "")

    def test_no_chain_or_no_logic(self):
        self.assertIsNone(le.derive_end([], trigger_level()[1], "lvl"))
        self.assertIsNone(le.derive_end(trigger_level()[0], {"nodes": [], "edges": []}, "lvl"))
        e = le.derive_end(["A -[CLoadMapComponentMessage]-> roles 4"], trigger_level()[1], "lvl")
        self.assertEqual(e.kind, le.TIMED)


class Steps(unittest.TestCase):
    def test_a_named_object_the_probe_saw_unnamed_takes_its_id_when_it_is_the_only_one_there(self):
        o = lp.Obj("LevelOutro_Trigger", "CScriptTriggerEntity", (10.0, 20.0, 30.0), lp.GROUP_TRIGGERS)
        probe = [lp.Obj("", "CScriptTriggerEntity", (10.0, 21.0, 30.0), lp.GROUP_TRIGGERS, ident="ww1#13618"),
                 lp.Obj("", "CGameEntity", (10.0, 20.0, 30.0), lp.GROUP_OTHER, ident="ww1#1")]
        self.assertEqual(lp.attach_refs([o], probe), 1)
        self.assertEqual(o.ref, "auth:ww1:13618")
        # Two unnamed ones of its class at its place: neither (never a guess).
        probe.append(lp.Obj("", "CScriptTriggerEntity", (11.0, 20.0, 30.0), lp.GROUP_TRIGGERS, ident="ww1#13619"))
        o.ref = ""
        lp.attach_refs([o], probe)
        self.assertEqual(o.ref, "")
        # --no-probe: names only.
        lp.attach_refs([o], probe[:1], names_only=True)
        self.assertFalse(o.ref.startswith("auth:"))

    def test_an_end_with_no_live_reference_fails_at_once(self):
        chains, logic = trigger_level()
        content = lp.Content(level="lvl", source="made up", start=(0.0, 0.0, 0.0), objects=[
            lp.Obj("Outro_Trigger", "CScriptTriggerEntity", (5.0, 0.0, 0.0), lp.GROUP_TRIGGERS, ident="u"),
            lp.Obj("Lever", "CGameEntity", (50.0, 0.0, 0.0), lp.GROUP_INTERACT, ident="u", ref="Lever")])
        content.level_end_chains, content.logic = chains, logic
        p = lp.make_plan(content, late_join=False)
        self.assertEqual(p.end.kind, le.TRIGGER)
        self.assertIn("no live reference", p.end_missing)
        h = lp.script_h(p, None)
        self.assertIn(f"log {lp.END_IMPOSSIBLE}: ", h)
        self.assertNotIn("enter_trigger", h.split("phase exit")[1])
        forbid = lp.exit_forbid(h)
        self.assertTrue(any(re.search(rx, "[t1] Input script: " + lp.END_IMPOSSIBLE + ": no live reference") for rx in forbid))
        # With its reference the trigger is entered, by it.
        content.objects[0].ref = "auth:lvl:7"
        h = lp.script_h(lp.make_plan(content, late_join=False), None)
        self.assertIn("enter_trigger auth:lvl:7\nwaitfor online_level_end", h)
        self.assertNotIn(lp.END_IMPOSSIBLE, h)


def levelpass_folder() -> Path | None:
    env = os.environ.get("SFO_LEVELPASS_FOLDER")
    if env:
        return Path(env)
    for up in HERE.parents:
        cfg = up / "tests" / "skytest.toml"
        if cfg.is_file():
            try:
                folder = tomllib.loads(cfg.read_text(encoding="utf-8")).get("levelpass", {}).get("folder")
            except (OSError, tomllib.TOMLDecodeError):
                return None
            return Path(os.path.expanduser(folder)) if folder else None
    return None


FOLDER = levelpass_folder()


def stored(level: str) -> bool:
    return FOLDER is not None and (FOLDER / level / "manifest.json").is_file() and \
        (FOLDER / level / "logic.json").is_file() and (FOLDER / f"{level}-manifest.txt").is_file()


def end_part(script: str) -> list[str]:
    lines = script.splitlines()
    return lines[lines.index("log LEVELPASS phase exit"):]


@unittest.skipUnless(FOLDER is not None, "no levelpass folder (tests/skytest.toml [levelpass] folder)")
class StoredLevels(unittest.TestCase):
    """The generated end steps on the stored data (the worked answer of logs/level-ends-plan.txt)."""

    def plan(self, level: str, **kw):
        if not stored(level):
            self.skipTest(f"no stored data for {level} in {FOLDER}")
        return lp.make_plan(lp.load_content(FOLDER, level), **kw)

    def check_common(self, plan, h: str) -> None:
        self.assertEqual(lp.position_literals(h), [], "the end names objects, never positions")
        for line in h.splitlines():
            self.assertNotIn(";", line)
        # Nothing of the end is acted on during the sweep (it would end the level under the sweep).
        sweep = h.split("log LEVELPASS phase exit")[0]
        for k, o in plan.end_objs.items():
            if o.ref:
                self.assertNotIn(f" {o.ref}\n", sweep.replace(f" {o.ref} ", f" {o.ref}\n"), k)

    def test_ww1_enters_the_outro_trigger_by_its_probe_id(self):
        p = self.plan("ww1")
        self.assertEqual(p.end.kind, le.TRIGGER)
        self.assertEqual(p.end.path[:4], ["LevelOutro_Trigger", "CouncilGillmen", "CouncilYeti", "CouncilEsmeralda"])
        h = lp.script_h(p, lp.LATE_JOIN_MARK)
        self.check_common(p, h)
        steps = [l for l in end_part(h) if not l.startswith("log ")]
        self.assertEqual(steps[:2], ["enter_trigger auth:ww1:13618", f"waitfor online_level_end {lp.TRIGGER_END_S}"])
        self.assertNotIn("LevelOutro_Trigger", "\n".join(steps))  # by its id, never the literal of 18 November
        self.assertEqual(lp.exit_forbid(h), [r"Dev: \S+ (?:auth:ww1:13618) failed \(no entity"])
        # Not entered before the exit: the council's NPCs are not talked to in the sweep either.
        self.assertNotIn("CouncilGillmen", h.split("log LEVELPASS phase exit")[0])

    def test_tutorial_b_gets_to_the_arena_and_fights_the_counted_fight(self):
        p = self.plan("tutorial_b")
        e = p.end
        self.assertEqual((e.kind, e.count, e.counter.split(".")[-1]), (le.FIGHT, 11, "EndEncounter_Manager"))
        self.assertEqual(sorted(k.split(".")[-1] for k in e.counted),
                         ["BomberSpawner_01", "BomberSpawner_02", "ChompyPlant", "ChompyPlant001", "CrashSP_01",
                          "CrashSP_02", "GreebleLobber", "GreebleLobber001"])
        self.assertEqual({k.split(".")[0] for k in e.counted}, {"tutorial_b_encounter_j"})  # no ambient bomber
        self.assertEqual([k.split(".")[-1] for k in e.starters], ["Start_Fight"])
        self.assertEqual(e.waves, [1, 3, 5])
        self.assertIn(e.anchor.split(".")[-1], ("CrashSP_01", "CrashSP_02"))  # a spawn point on the arena floor
        h = lp.script_h(p, lp.LATE_JOIN_MARK)
        self.check_common(p, h)
        steps = [l for l in end_part(h) if not l.startswith("log ")]
        anchor = p.end_objs[e.anchor].ref
        self.assertTrue(anchor.startswith("auth:tutorial_b:"))
        self.assertEqual(steps[:3], [f"goto_entity {anchor}", "wait_free 60", "enter_trigger auth:tutorial_b:4148"])
        fights = [l for l in steps if l.startswith("fight_near ")]
        self.assertEqual(len(fights), 5)  # the first round, one per wave, one for late arrivals
        self.assertTrue(all(l.startswith(f"fight_near {anchor} ") for l in fights))
        self.assertNotIn("11560", h)  # never the flying bomber spawner of the run of 18 November
        self.assertEqual(steps[len(fights) + 3], f"waitfor online_level_end {int(lp.EXIT_WAIT_S)}")

    def test_tutorial_a_ends_on_its_own(self):
        p = self.plan("tutorial_a")
        self.assertEqual(p.end.kind, le.TIMED)
        self.assertTrue(p.timed)
        h = lp.script_h(p, lp.LATE_JOIN_MARK)
        self.check_common(p, h)
        self.assertIn(f"waitfor online_level_end {lp.TIMED_END_S}", h)
        self.assertEqual(lp.exit_forbid(h), [])

    def test_the_passes_fit_the_budget_and_no_end_still_says_the_end(self):
        for level in ("ww1", "tutorial_b", "tutorial_a"):
            p = self.plan(level)
            self.assertLessEqual(p.estimate_s, lp.BUDGET_S, level)
            n = self.plan(level, no_end=True)
            h = lp.script_h(n, lp.LATE_JOIN_MARK)
            self.assertNotIn("online_level_end", h)
            self.assertRegex(h, re.escape("exit not attempted (--no-end), the end would be: ")
                             + {"ww1": "enter the trigger", "tutorial_b": "the counted fight",
                                "tutorial_a": "the level ends on its own"}[level])


class Budget(unittest.TestCase):
    """R3 of the 18 November run audit: the pass budgets its own time and is never a pass when it did not
    cover its plan."""

    def plan(self, no_end=False):
        chains, logic = trigger_level()
        content = lp.Content(level="lvl", source="made up", start=(0.0, 0.0, 0.0), objects=[
            lp.Obj("Outro_Trigger", "CScriptTriggerEntity", (5.0, 0.0, 0.0), lp.GROUP_TRIGGERS, ident="u", ref="auth:lvl:7")]
            + [lp.Obj(f"Box_Destructible_{i}", "C", (i * 3000.0, 0.0, 0.0), lp.GROUP_INTERACT, ident="u",
                      ref=f"auth:lvl:{100 + i}") for i in range(1, 60)])
        content.level_end_chains, content.logic = chains, logic
        return lp.make_plan(content, late_join=False, no_end=no_end)

    def test_the_estimate_keeps_what_the_deadline_keeps(self):
        p = self.plan()
        self.assertLessEqual(p.estimate_s, lp.BUDGET_S + 1)
        sweep = p.estimate_s - (lp.BOOT_S + lp.LEVEL_LOAD_S + lp.exit_seconds(p) + lp.SWEEP_MARGIN_S)
        self.assertLessEqual(sweep, lp.sweep_share(p) + 0.5)  # a sweep the estimate fits is one the deadline does not cut
        self.assertEqual(lp.CASE_TIMEOUT_S, lp.BUDGET_S + lp.SOFT_GRACE_S)
        self.assertIn(f"timeout = {lp.CASE_TIMEOUT_S}", lp.spec_toml(p))

    def test_a_visit_and_its_action_teleport_once(self):
        o = lp.Obj("Box", "C", (1.0, 2.0, 3.0), lp.GROUP_INTERACT, ref="auth:lvl:5")
        a = lp.Action(lp.ATTACK, o, "x")
        self.assertIn("goto_entity auth:lvl:5", lp.action_lines(a, "v1_1"))
        self.assertNotIn("goto_entity auth:lvl:5", lp.action_lines(a, "v1_1", after="auth:lvl:5"))
        stop = lp.Stop(label="v1", cluster=lp.Cluster(0, o.pos, [o]), actions=[a], visit_only=[])
        self.assertEqual(lp.stop_lines(stop).count("goto_entity auth:lvl:5"), 1)

    def test_the_verdict(self):
        p = self.plan()
        done = "Input script: LEVELPASS phase next-level\n"
        self.assertEqual(lp.pass_verdict("pass", [], done, p), (lp.PASS, []))
        v, missed = lp.pass_verdict("pass", [], "[t1] Dev: attack_until auth:lvl:9 12 skipped: the sweep's time is up\n" + done, p)
        self.assertEqual(v, lp.INCOMPLETE)
        self.assertIn("1 step(s) skipped at the sweep's deadline: attack_until auth:lvl:9", missed[0])
        v, missed = lp.pass_verdict("pass", [], "", p)
        self.assertEqual((v, missed), (lp.INCOMPLETE, ["the level's end and the next level (not reached)"]))
        # Stopped at the soft budget: incomplete, not a failure of the game.
        v, missed = lp.pass_verdict("fail", [f"did not quit within {lp.CASE_TIMEOUT_S} s; ended by the runner",
                                             "peers failed: H"], "", p)
        self.assertEqual(v, lp.INCOMPLETE)
        self.assertIn(f"stopped by the runner at the soft budget ({lp.CASE_TIMEOUT_S} s)", missed)
        # Any other failure is a failure.
        self.assertEqual(lp.pass_verdict("fail", ["stopped early: H ... (fail fast)"], done, p)[0], lp.FAIL)
        # --no-end: no end planned, none missed.
        self.assertEqual(lp.pass_verdict("pass", [], "", self.plan(no_end=True)), (lp.PASS, []))


class Pause(unittest.TestCase):
    """ww1 20261118-195819: a Soul Gem's found screen paused H after its pickup; every later step failed after
    23 s for 3.5 minutes. The lines below are that run's."""
    P = "[2026-11-18 20:01:35.152] [info] [core] [t3768] "
    PICKUP_HELD = P + "Dev: pickup (null)#3594 held too long (paused, controls locked, frames stopped, at -799.75,118.96,3890.20 kill z -8000)"
    NEXT = P + "Dev: goto_entity auth:ww1:4666 no player frame took a read for 3 s (paused, controls locked): waiting"

    def test_b_follows_every_step_that_lands_on_or_picks_up_a_collectible(self):
        """soulgem-found-screen 20261118-212923: B closes the found screen, A does not; landing on the gem
        (the visit's goto_entity) already collects it."""
        gem = lp.Obj("Gem", "CPhysicalEntity", (1.0, 2.0, 3.0), lp.GROUP_PICKUPS, ref="auth:ww1:12086",
                     components=["Scripts.CollectibleComponentData"])
        lines = lp.action_lines(lp.Action(lp.PICKUP, gem, "x"), "v19_1")
        self.assertEqual(lines[1:], ["goto_entity auth:ww1:12086", "tap b", "wait_free 15",
                                     f"pickup auth:ww1:12086 {lp.ACTION_TIMEOUT[lp.PICKUP]}", "tap b", "wait_free 15"])
        # The visit's teleport to it: B before anything else runs (the pickup's pause line would end the pass).
        stop = lp.Stop(label="v19", cluster=lp.Cluster(0, gem.pos, [gem]), actions=[lp.Action(lp.PICKUP, gem, "x")],
                       visit_only=[])
        s = lp.stop_lines(stop)
        self.assertEqual(s[1:4], ["goto_entity auth:ww1:12086", "tap b", "wait_free 15"])
        self.assertLess(s.index("tap b"), next(i for i, l in enumerate(s) if l.startswith("pickup ")))
        # Keyed on the component (or the probe's collectible class), never the name; no A, back or start.
        box = lp.Obj("Gem_Box", "CPhysicalEntity", (1.0, 2.0, 3.0), lp.GROUP_INTERACT, ref="auth:ww1:5",
                     components=["Scripts.DestructibleComponentData"])
        self.assertEqual(lp.dismiss_after(box), [])
        self.assertEqual(lp.dismiss_after(lp.Obj("x", "CCollectible", (0.0, 0.0, 0.0), lp.GROUP_PICKUPS)), ["tap b", "wait_free 15"])
        text = "\n".join(s + lines)
        for press in ("tap a", "tap back", "tap start"):
            self.assertNotIn(press, text)

    def test_the_next_step_finding_the_game_paused_ends_the_pass(self):
        rx = re.compile(lp.PAUSE_FATAL)
        self.assertTrue(rx.search(self.NEXT))
        self.assertFalse(rx.search(self.PICKUP_HELD))  # the pickup's own hold: the press comes after it
        self.assertFalse(rx.search(self.P + "Dev: goto_entity auth:ww1:1 no player frame took a read for 3 s (controls locked): waiting"))
        self.assertIn(lp.PAUSE_FATAL, lp.spec_toml(lp.make_plan(lp.Content("x", "x", (0.0, 0.0, 0.0), []), late_join=False)))

    def test_the_verdict_names_the_step_after_which_the_game_paused(self):
        h = "\n".join([self.P + "Input script: LEVELPASS visit v19 objects 4 (SoulGem_lockjaw) ref auth:ww1:12341",
                       self.P + "Input script: LEVELPASS act v19_1 pickup SoulGem_lockjaw CPhysicalEntity ref auth:ww1:12086",
                       self.P + "Dev: pickup (null)#3594 held (controls locked)", self.PICKUP_HELD,
                       self.P + "Input script: LEVELPASS visit v22 objects 18 (Barrel04) ref auth:ww1:4666", self.NEXT])
        self.assertEqual(lp.paused_after(h), "act v19_1 pickup SoulGem_lockjaw CPhysicalEntity ref auth:ww1:12086")
        plan = lp.make_plan(lp.Content("ww1", "x", (0.0, 0.0, 0.0), []), late_join=False)
        verdict, missed = lp.pass_verdict("fail", [f"stopped early: H {self.NEXT} (fail fast)", "peers failed: H"], h, plan)
        self.assertEqual(verdict, lp.INCOMPLETE)
        self.assertIn("paused with its frames stopped after `act v19_1 pickup SoulGem_lockjaw", missed[0])
        # Another failure stays a failure.
        self.assertEqual(lp.pass_verdict("fail", ["stopped early: H Crash: x (fail fast)"], h, plan)[0], lp.FAIL)


class CountedFightNotes(unittest.TestCase):
    """tutorial_b 20261118-200515: S's counter heard 7 of 11; the rest were spawns no client had. Its lines."""
    P = "[2026-11-18 20:07:28.127] [info] [core] [t39348] Online: "

    def test_the_counter_heard_and_what_no_client_could_fight(self):
        chains, logic = fight_level()
        content = lp.Content(level="lvl", source="made up", start=(0.0, 0.0, 0.0), objects=[
            lp.Obj("SpawnPoint", "CEntity", (180.0, 20.0, 10.0), lp.GROUP_ENEMIES, ident="a", ref="auth:lvl:9"),
            lp.Obj("Lobber", "CActor", (100.0, 0.0, 0.0), lp.GROUP_ENEMIES, ident="a", ref="auth:lvl:10")])
        content.level_end_chains, content.logic = chains, logic
        p = lp.make_plan(content, late_join=False)
        s = "\n".join([
            self.P + "relay activator Lobber@0xB8488C1C +16 -1 -> PeerPuppet_1#3829 (relay Counter#2633, native activator Counter#2633, x",
            self.P + "relay activator Lobber@0xB8488C1C +16 -1 -> PeerPuppet_1#3829 (relay Counter#2633, native activator Counter#2633, y",
            self.P + "relay activator (null)@0xB847D414 +16 -1 -> PeerPuppet_1#3829 (relay Counter#2633, native activator Counter#2633, x",
            self.P + "relay native - +16 -1 -> native (relay CrashSP_01#2630, native activator CrashSP_01#2630, x",
            self.P + "relay activator Counter@0xB8482298 +16 -1 -> PeerPuppet_1#3829 (relay Plane#2616, native activator Plane#2616, x",
            self.P + "enemy run-time spawns of Plane not shared (its spawner can move)",
            self.P + "runtime spawn SpawnPoint_Spawned1 not claimed (ambiguous parent)",
            self.P + "enemy run-time spawns of Area3_CageStep_LG_02 not shared (its spawner can move)"])
        h = "\n".join([self.P + "enemy bind a00000f1b.53ddf3dd.79d73383@2 (pending past grace: missing)",
                       "[t1] Input script: LEVELPASS phase exit",
                       self.P + "enemy bind s72f3a0bd.52540001#1/0@2 (pending past grace: missing)"])
        notes = lp.counted_fight_notes(p, s, h)
        self.assertIn("S's Counter heard 2 of its 3 counted deaths (S's relay lines: Lobber, (null))", notes[0])
        self.assertIn("did not share the run-time spawns of Plane, SpawnPoint with the clients", notes[1])
        self.assertNotIn("Area3_CageStep", notes[1])  # not a sender of the counted fight
        self.assertIn("H never bound 1 enemy spawn(s) S shared during the exit", notes[2])
        self.assertIn("s72f3a0bd.52540001#1/0@2", notes[2])
        self.assertEqual(lp.counted_fight_notes(lp.make_plan(lp.Content("x", "x", (0.0, 0.0, 0.0), []), late_join=False), s, h), [])


if __name__ == "__main__":
    unittest.main()
