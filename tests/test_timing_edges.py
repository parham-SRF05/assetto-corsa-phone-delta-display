"""Edge cases for timing.py: race starts, mid-lap starts, jumps, clock resets, stalls, offset line."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import ac_dash as ad
import timing as tm

fails = 0


def check(name, ok, detail=''):
    global fails
    fails += not ok
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + str(detail)) if detail else ''))


KEY, TRACK = ('c', 't', 2), 't'
DT = 1000 / 60


class Sim:
    """The player driving at constant speed: lap_ms per lap."""

    def __init__(self, T, line=0.0, lap_ms=60000.0, splits=(1 / 3, 2 / 3), q=0.0, t=0.0, delay=0, name='X'):
        self.T, self.line, self.lap_ms, self.splits = T, line, lap_ms, splits
        self.q, self.t, self.done, self.last = q, t, 0, 0
        self.now, self.delay, self.pend = 0.0, delay, None
        self.invalid, self.name = False, name

    def frame(self, move=True, update=True):
        if move:
            self.t += DT
            self.q += DT / self.lap_ms
            if self.q >= 1.0:
                self.q -= 1.0
                self.done += 1
                self.pend = [self.delay, int(round(self.t - self.q * self.lap_ms))]
                self.t = self.q * self.lap_ms
        if self.pend:
            if self.pend[0] <= 0:
                self.last, self.pend = self.pend[1], None
            else:
                self.pend[0] -= 1
        self.now += DT / 1000
        if update:
            sector = sum(1 for s in self.splits if self.q >= s) if self.q >= 0 else 0
            car = ad.CarData((self.q + self.line) % 1.0, int(self.t), self.last, 0, self.done, 0, False,
                             True, self.invalid, self.name)
            self.T.update(self.now, KEY, TRACK, len(self.splits) + 1, [car], sector)

    def laps(self, n):
        target = self.done + n
        while self.done < target:
            self.frame()

    def until(self, q):
        while self.q < q:
            self.frame()

    def settle(self, frames=90):
        for _ in range(frames):
            self.frame()


def cached(line=0.0, sectors=(1 / 3, 2 / 3)):
    path = os.path.join(tempfile.mkdtemp(), 'tracks.json')
    with open(path, 'w') as f:
        json.dump({TRACK: {'line': line, 'sectors': list(sectors)}}, f)
    return path


# 1. From the line, AC reports each lap time 3 frames late
T = tm.Timing(None)
s = Sim(T, delay=3)
s.laps(3); s.settle()
check('late lap times: every lap recorded (lap 1 too)', [l.time for l in T.laps] == [60000] * 3, [l.time for l in T.laps])
check('timing line found', abs(T.line) < 1e-4, T.line)
check('sector splits learned on lap 1', T.bounds and all(abs(a - b) < 0.001 for a, b in zip(T.bounds, (1 / 3, 2 / 3))), T.bounds)

# 2. Timing line at track position 0.37 on a track never seen before
T = tm.Timing(None)
s = Sim(T, line=0.37)
s.laps(3); s.settle()
check('offset line found', abs(T.line - 0.37) < 1e-4, T.line)
check('offset line: sectors placed correctly', T.bounds and all(abs(a - b) < 0.001 for a, b in zip(T.bounds, (1 / 3, 2 / 3))), T.bounds)
check('offset line: laps 2 and 3 recorded with 20 s sectors',
      len(T.laps) == 2 and all(abs(x - 20000) < 20 for l in T.laps for x in l.sectors), [l.sectors for l in T.laps])

# 3. Race start from the grid, 1% of a lap behind the line, clock waits for the lights
T = tm.Timing(cached())
T.reset(KEY, 3, TRACK)
s = Sim(T, q=-0.01)
for _ in range(120):
    s.frame(move=False)
check('on the grid: sectors ready before the start', T.mini_bounds is not None)
s.until(0.5)
live_colors = list(T.live['colors']) if T.live else []
check('race lap 1: mini sectors live during the lap', len(live_colors) >= 11 and all(c for c in live_colors), live_colors)
s.laps(1)
s.settle(5)
prev = T.previous[0] if T.previous else None
check('race lap 1: all 24 mini sectors and 3 sectors shown', prev and len(prev['colors']) == 24 and all(prev['colors'])
      and all(x['time'] for x in prev['sectors']), prev and [x['time'] for x in prev['sectors']])
check('race lap 1: sector 1 includes the run from the grid (like AC)', prev and abs(prev['sectors'][0]['time'] - 20600) < 20,
      prev and prev['sectors'][0]['time'])
check('race lap 1 recorded (60.6 s)', [l.time for l in T.laps] == [60600], [l.time for l in T.laps])
s.until(0.5)
check('lap 2 delta vs lap 1 = -0.600', abs(T.delta_to(T.player_best()) + 600) <= 2, T.delta_to(T.player_best()))

# 4. Dashboard started mid-lap: later mini sectors still work, earlier ones marked as not seen
T = tm.Timing(cached())
T.reset(KEY, 3, TRACK)
s = Sim(T, q=0.5, t=30000)
s.until(0.9)
colors = T.live['colors'] if T.live else []
check('mid-lap start: minis before the start not seen, after it coloured',
      len(colors) >= 20 and all(c is None for c in colors[:12]) and all(colors[13:]), colors)
sec = T.sectors_view(s.now)
check('mid-lap start: S1 shown as not seen, S3 live', sec[0]['state'] == 'done' and sec[0]['time'] is None and sec[2]['state'] == 'live')
s.laps(1); s.settle(5)
check('mid-lap start: that lap is not used as a reference', T.laps == [] and T.previous is not None)

# 5. Car jumps forward mid-lap (pit lane) with the clock running: delta and minis carry on
T = tm.Timing(cached())
T.reset(KEY, 3, TRACK)
s = Sim(T)
s.laps(2); s.until(0.4)
s.q += 0.1
s.frame()
s.until(0.55)
check('after a jump: delta still shown', T.delta_to(T.player_best()) is not None, T.delta_to(T.player_best()))
s.laps(1); s.settle(5)
check('after a jump: that lap is not recorded, the others are', [l.time for l in T.laps] == [60000, 60000], [l.time for l in T.laps])

# 6. Back to the pits: lap clock resets without a lap being counted
T = tm.Timing(cached())
T.reset(KEY, 3, TRACK)
s = Sim(T)
s.laps(2); s.until(0.5)
s.t = 0.0
s.frame()
s.frame()
check('clock reset: no nonsense delta', T.delta_to(T.player_best()) is None)
s.laps(2); s.settle(5)
s.until(0.3)
check('clock reset: timing recovers on the next lap', len(T.laps) >= 3 and T.delta_to(T.player_best()) is not None,
      [l.time for l in T.laps])

# 7. The dashboard stalls for 3 seconds mid-lap (laptop busy): not a jump, lap still recorded
T = tm.Timing(cached())
T.reset(KEY, 3, TRACK)
s = Sim(T)
s.laps(1); s.until(0.5)
for _ in range(180):
    s.frame(update=False)
s.laps(1); s.settle(60)  # equal lap times: AC's lap time is accepted after 0.5 s
check('3 s stall: lap still recorded', len(T.laps) == 2, [l.time for l in T.laps])
check('3 s stall: its sector times do not set bests', len(T.laps) == 2 and T.laps[1].gappy and not T.laps[0].gappy)

# 8. Invalid lap never becomes a best
T = tm.Timing(cached())
T.reset(KEY, 3, TRACK)
s = Sim(T)
s.laps(1); s.until(0.2); s.invalid = True; s.until(0.3); s.invalid = False
s.laps(2); s.settle(5)
check('invalid lap recorded but not a best', sum(not l.valid for l in T.laps) == 1 and T.player_best().valid,
      [(l.time, l.valid) for l in T.laps])

# 9. Session restart (lap count drops) clears the session
T = tm.Timing(cached())
T.reset(KEY, 3, TRACK)
s = Sim(T)
s.laps(2); s.settle(5)
T.update(s.now, KEY, TRACK, 3, [ad.CarData(0.01, 100, 0, 0, 0, 0, False, True, False, 'X')], 0)
check('restart clears laps and bests', not T.laps and T.fastest is None and T.mini_bounds is not None)

# 10. Learned splits are saved and ready at the start of the next session
path = os.path.join(tempfile.mkdtemp(), 'tracks.json')
T = tm.Timing(path)
s = Sim(T)
s.laps(2)
T2 = tm.Timing(path)
T2.reset(('c', 't', 1), 3, TRACK)
check('splits saved and loaded', T2.mini_bounds is not None and all(abs(a - b) < 1e-5 for a, b in zip(T2.bounds, T.bounds)), T2.bounds)

# 11. Another driver takes the slot (online): the old driver's laps are forgotten
T = tm.Timing(cached())
T.reset(KEY, 3, TRACK)
s = Sim(T, name='Old Driver')
s.laps(2); s.settle(5)
s.name = 'New Driver'
s.frame()
check('driver change: old laps forgotten', T.laps == [] and T.fastest is None)

# 12. Grids
pts = [(i / 100, i * 600) for i in range(1, 100) if not 40 < i < 56]
check('lap with a 15% hole rejected', tm.build_grid(pts, 60000)[0] is None)
g, _ = tm.build_grid([(-0.01 + i / 100, 600 + (-0.01 + i / 100) * 60000) for i in range(0, 101)], 60600)
check('grid start: clock at the line interpolated', g is not None and abs(g[0] - 600) <= 1 and g[-1] == 60600, g and (g[0], g[-1]))
check('classify purple/green/yellow', [tm.classify(9, 10, 12), tm.classify(11, 10, 12), tm.classify(13, 10, 12),
                                       tm.classify(5, None, None), tm.classify(11, 10, None)]
      == ['purple', 'green', 'yellow', 'purple', 'green'])

print('\nALL PASS' if not fails else f'\n{fails} FAILED')
sys.exit(1 if fails else 0)
