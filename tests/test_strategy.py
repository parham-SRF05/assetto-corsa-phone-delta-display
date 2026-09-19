"""The pit call: right lap, right tyre, and it keeps up when the race does not go to plan."""
import os
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..')
sys.path.insert(0, ROOT)
import strategy  # noqa: E402

fails = 0


def check(name, ok, detail=''):
    global fails
    fails += not ok
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + str(detail)) if detail else ''))


PLAN = strategy.Plan({
    'name': 'test', 'track': 'silverstone', 'laps': 53, 'warnLapsBefore': 1,
    'stints': [
        {'compound': 'SOFT', 'boxOnLap': 7},
        {'compound': 'HARD', 'boxOnLap': 24},
        {'compound': 'HARD', 'boxOnLap': 41},
        {'compound': 'MEDIUM', 'boxOnLap': None},
    ],
})


def drive(engineer, laps, pit_on=()):
    """Runs through the race and returns {lap completed: call}."""
    out = {}
    for lap in range(laps + 1):
        in_pit = lap in pit_on
        engineer.update(lap, in_pit)
        out[lap] = engineer.call(lap, in_pit, 53)
    return out


# --- the plan itself ---------------------------------------------------------------------------
check('reads the stop laps', PLAN.stops == [7, 24, 41], PLAN.stops)
check('a plan for this race fits', PLAN.fits('ks_silverstone|gp', 53))
check('another track does not fire', not PLAN.fits('ks_monza', 53))
check('another distance does not fire', not PLAN.fits('ks_silverstone', 20))
check('a missing file just means no plan', strategy.load('nope.json') is None)

# --- the calls through a clean race -------------------------------------------------------------
e = strategy.Engineer(PLAN)
calls = drive(e, 53, pit_on=(7, 24, 41))
check('lap 1: quietly shows the first stop', calls[0]['state'] == 'info' and 'L7' in calls[0]['text'], calls[0])
check('lap 5 of 7: nothing yet', calls[4]['state'] == 'info', calls[4])
check('lap 6: box next lap', calls[5]['state'] == 'warn', calls[5])
check('lap 7: BOX BOX BOX', calls[6]['state'] == 'box' and calls[6]['text'] == 'BOX BOX BOX', calls[6])
check('in the lane: fit the hard', calls[7]['state'] == 'pit' and calls[7]['text'] == 'FIT HARD', calls[7])
check('after the stop: points at lap 24', calls[8]['state'] == 'info' and 'L24' in calls[8]['text'], calls[8])
check('lap 24: BOX again', calls[23]['state'] == 'box', calls[23])
check('third stop fits the medium', calls[41]['text'] == 'FIT MEDIUM', calls[41])
check('after the last stop: no more stops', calls[45]['text'] == 'NO MORE STOPS', calls[45])
check('and counts down to the flag', '8 LAPS' in calls[45]['sub'], calls[45])
check('the call is silent on no other lap',
      sum(1 for c in calls.values() if c['state'] == 'box') == 3,
      [lap for lap, c in calls.items() if c['state'] == 'box'])

# --- when the race does not go to plan -----------------------------------------------------------
e = strategy.Engineer(PLAN)
early = drive(e, 12, pit_on=(4,))
check('boxing early still advances the plan', 'L24' in early[5]['text'], early[5])
check('and the early stop fits the hard', early[4]['text'] == 'FIT HARD', early[4])

e = strategy.Engineer(PLAN)
late = drive(e, 12, pit_on=(10,))
check('staying out says how late you are', late[8]['state'] == 'box' and 'LATE' in late[8]['sub'], late[8])
check('one lap late says LAP, not LAPS', '1 LAP LATE' in late[7]['sub'], late[7])
check('three laps late says three', '3 LAPS LATE' in late[9]['sub'], late[9])

e = strategy.Engineer(PLAN)
extra = drive(e, 20, pit_on=(7, 12, 18))
check('an unplanned extra stop does not break it', extra[19] is not None and extra[19]['state'] in ('info', 'box'), extra[19])

e = strategy.Engineer(PLAN)
e.update(30, False)
e.update(0, False)   # a new session restarts the count
check('a restart resets the stops', e.stops_done == 0)

check('no plan means no call', strategy.Engineer(None).call(5, False, 53) is None)

print('\nALL PASS' if not fails else f'\n{fails} FAILED')
sys.exit(1 if fails else 0)
