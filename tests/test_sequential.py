"""Sequence steps with alternative log types, normalized inputs and ID filtering."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nyaatriggers.trigger_engine import Trigger
from nyaatriggers.sequential import SequentialRunner

FAILS = []


def check(name, cond):
    print(("PASS  " if cond else "FAIL  ") + name)
    if not cond:
        FAILS.append(name)


def make_runner(sequence):
    """A runner over `sequence` with its completion/expire callbacks recorded."""
    done, expired = [], []
    r = SequentialRunner(Trigger(sequence=sequence), {},
                         lambda runner, cap: done.append(cap),
                         lambda runner: expired.append(runner))
    return r, done, expired


def ability_line(lt, ability_id, name="Some Ability", source="Boss",
                 target="Tini Poutini"):
    """A 21/22-shaped line: source at 3, ability id at 4, name at 5, target at 7."""
    return [lt, "ts", "40001234", source, ability_id, name,
            "10001111", target]


# a pipe-separated step advances on either concrete type
seq = [{"log_type": "21|22", "ability_id": "A55B"}]

r, done, _expired = make_runner(seq)
check("pipe step advances on a 21 line",
      r.try_advance(ability_line("21", "A55B")) is True and len(done) == 1)
check("21 line captures resolve by its indexes",
      done[0]["source"] == "Boss" and done[0]["target"] == "Tini Poutini")

r, done, _expired = make_runner(seq)
check("pipe step advances on a 22 line (id case-insensitive)",
      r.try_advance(ability_line("22", "a55b")) is True and len(done) == 1)
check("22 line captures resolve by its indexes",
      done[0]["source"] == "Boss" and done[0]["target"] == "Tini Poutini")

r, done, _expired = make_runner(seq)
check("pipe step ignores a non-member log type",
      r.try_advance(ability_line("20", "A55B")) is False and done == [])
check("pipe step ignores a wrong id on a member type",
      r.try_advance(ability_line("22", "A55C")) is False and done == [])

# the ability regex also resolves against the concrete line's name field
r, done, _expired = make_runner([{"log_type": "21|22", "ability_regex": "Exaflare"}])
check("pipe step regex advances on a 22 line",
      r.try_advance(ability_line("22", "1234", name="Exaflare")) is True
      and len(done) == 1)
r, done, _expired = make_runner([{"log_type": "21|22", "ability_regex": "Exaflare"}])
check("pipe step regex rejects a non-matching 21 line",
      r.try_advance(ability_line("21", "1234", name="Glare")) is False
      and done == [])

# a bare pipe step advances mid-sequence on either type
r, done, _expired = make_runner([{"log_type": "21|22"}, {"log_type": "20"}])
check("bare pipe step advances on 22 without completing the sequence",
      r.try_advance(ability_line("22", "9999")) is False and done == [])
check("the next step still waits for its own type",
      r.try_advance(ability_line("21", "9999")) is False and done == [])
check("the next step completes on its own type",
      r.try_advance(ability_line("20", "9999")) is True and len(done) == 1)

# whitespace around the pipe parts is tolerated, as in Trigger.matches
r, done, _expired = make_runner([{"log_type": "21 | 22"}])
check("spaced pipe parts still match",
      r.try_advance(ability_line("22", "9999")) is True and len(done) == 1)

# a null step log_type falls back to "20" instead of matching nothing
r, done, _expired = make_runner([{"log_type": None}])
check("null step log_type advances on a 20 line",
      r.try_advance(ability_line("20", "9999")) is True and len(done) == 1)

# an ability_id on a type with no ID field is ignored, regex decides
chat_line = ["00", "ts", "10001111", "Tini Poutini", "resonance is up"]

r, done, _expired = make_runner([{"log_type": "00", "ability_id": "1234"}])
check("unindexed type ignores the id and advances on the type alone",
      r.try_advance(chat_line) is True and len(done) == 1)

r, done, _expired = make_runner([{"log_type": "00", "ability_id": "1234",
                                  "ability_regex": "resonance"}])
check("unindexed type with a regex advances on matching chat text",
      r.try_advance(chat_line) is True and len(done) == 1)

r, done, _expired = make_runner([{"log_type": "00", "ability_id": "1234",
                                  "ability_regex": "meteor"}])
check("unindexed type with a regex rejects non-matching chat text",
      r.try_advance(chat_line) is False and done == [])

# a hand edited step log_type is stripped at match time
r, done, _expired = make_runner([{"log_type": " 21 "}])
check("padded step log_type strips and matches",
      r.try_advance(ability_line("21", "9999")) is True and len(done) == 1)

r, done, _expired = make_runner([{"log_type": " "}])
check("whitespace only step log_type defaults to 20",
      r.try_advance(ability_line("20", "9999")) is True and len(done) == 1)

r, done, _expired = make_runner([{"log_type": " 21 | 22 "}])
check("padded pipe parts strip per part",
      r.try_advance(ability_line("22", "9999")) is True and len(done) == 1)

r, done, _expired = make_runner([{"log_type": " 21 | 22 "}])
check("padded pipe still rejects a non member type",
      r.try_advance(ability_line("20", "9999")) is False and done == [])

print()
if FAILS:
    print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
    sys.exit(1)
print("all tests passed")
