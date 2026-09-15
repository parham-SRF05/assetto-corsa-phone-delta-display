"""Full simulated race (grid start, 8 cars, 10 laps) checked against the exact times the demo drove."""
import copy
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


def true_time(entry, q):
    pre, chunks = entry
    x = min(max(q, 0.0), 1.0) * ad.DemoSource.CHUNKS
    j = int(x)
    return pre + sum(chunks[:j]) + (chunks[j] * (x - j) if j < len(chunks) else 0.0)


def race(cache_file, seconds):
    clock = [1000.0]
    ad.time.monotonic = lambda: clock[0]
    demo = ad.DemoSource(seed=3)
    T = tm.Timing(cache_file)
    KEY = ('demo_car', 'demo_track', ad.AC_RACE)
    r = {'demo': demo, 'T': T, 'pos_bad': 0, 'pos_lag': 0, 'color_checks': 0, 'color_errors': 0, 'deltas': [],
         'colors': set(), 'states': set(), 'first_display': None, 'ready_at_start': None, 'max_json': 0}
    for f in range(int(60 * seconds)):
        p, g, s, app = demo.read()
        live_before = T.live
        n_mini = len(live_before['colors']) if live_before else 0
        n_sec = len(live_before['sectors']) if live_before else 0
        bests = copy.deepcopy((T.best_sectors, T.pb_sectors, T.best_minis, T.pb_minis))
        T.update(clock[0], KEY, 'demo_track', 3, app.cars, g.currentSectorIndex)
        view = ad.build_view(p, g, s, app, T, None, True, clock[0])
        r['max_json'] = max(r['max_json'], len(json.dumps(view, allow_nan=False)))
        if r['ready_at_start'] is None:
            r['ready_at_start'] = view['sectorsReady']
        rank = app.cars[0].race_pos + 1
        r['pos_bad'] += view['position'] != rank
        r['pos_lag'] += g.position != rank
        if T.live is live_before and T.live is not None and bests[0] is not None:
            live = T.live
            for k in range(n_sec, len(live['sectors'])):
                sec = live['sectors'][k]
                if sec['time'] is None:
                    continue
                r['color_checks'] += 1
                exp = tm.classify(sec['time'], bests[0][k], bests[1][k])
                near = any(b is not None and abs(sec['time'] - b) <= 1 for b in (bests[0][k], bests[1][k]))
                r['color_errors'] += exp != sec['color'] and not near
            for j in range(n_mini, len(live['colors'])):
                a, b = live['cross'][j], live['cross'][j + 1]
                if a is None or b is None:
                    continue
                r['color_checks'] += 1
                r['color_errors'] += tm.classify(b - a, bests[2][j], bests[3][j]) != live['colors'][j]
        if T.previous and r['first_display'] is None:
            r['first_display'] = copy.deepcopy(T.previous[0]['colors']), copy.deepcopy(T.previous[0]['sectors'])
        me, best = demo.cars[0], T.player_best()
        if best and view['deltaBest'] is not None and f % 97 == 0:
            elapsed = (clock[0] - me['start']) * 1000
            pre = demo.pre_line(me)
            if elapsed > pre:
                q_true = demo.progress(me['chunks'], elapsed - pre)
                hist = [h for h in me['history'] if int(round(h[0] + sum(h[1]))) == best.time]
                if len(hist) == 1 and 0.05 < q_true < 0.95:
                    # our position scale is shifted by (true line - our line estimate)
                    q_ours = q_true + ad.DemoSource.LINE - best.line
                    r['deltas'].append(view['deltaBest'] - (elapsed - true_time(hist[0], q_true)))
        for sec in view['sectors']:
            r['colors'].update(c for c in sec['minis'] if c)
            r['states'].add((sec['state'], sec['prev']))
        clock[0] += 1 / 60
    return r


path = os.path.join(tempfile.mkdtemp(), 'tracks.json')
print('--- first ever race on this track (no saved sector splits) ---')
r = race(path, 330)
demo, T = r['demo'], r['T']
print('player laps', demo.cars[0]['laps'], '| laps recorded', len(T.laps), '| biggest update', r['max_json'], 'bytes')
check('timing line found (~0.004)', abs(T.line - demo.LINE) < 0.0008, T.line)
check('sector splits learned (~0.30, 0.66)',
      T.bounds and all(abs(b + T.line - demo.LINE - s) < 0.0012 for b, s in zip(T.bounds, demo.SPLITS)), T.bounds)
per_car = {i: sum(1 for l in T.laps if l.car == i) for i in range(8)}
check('every lap of every car recorded, lap 1 from the grid included',
      all(per_car[i] == len(demo.cars[i]['history']) for i in range(8)), (per_car, [len(c['history']) for c in demo.cars]))
errs = []
for lap in T.laps:
    hist = [h for h in demo.cars[lap.car]['history'] if int(round(h[0] + sum(h[1]))) == lap.time]
    if len(hist) != 1 or lap.sectors is None:
        continue
    edges = [0.0] + [b + T.line - demo.LINE for b in T.bounds] + [1.0]
    truth = [true_time(hist[0], edges[k + 1]) - (true_time(hist[0], edges[k]) if k else 0.0) for k in range(3)]
    errs += [abs(a - b) for a, b in zip(lap.sectors, truth)]
check('sector times of all cars within 3 ms of the truth (lap 1 included)', errs and max(errs) < 3.0,
      f'{len(errs)} sectors, max error {max(errs):.2f} ms')
check('fastest lap = quickest lap of anyone', T.fastest.time == min(l.time for l in T.laps))
check('your best = your quickest lap', T.player_best().time == min(l.time for l in T.laps if l.car == 0))
check('live delta within 3 ms of the truth', r['deltas'] and max(abs(x) for x in r['deltas']) < 3.0,
      f"{len(r['deltas'])} samples, max error {max(abs(x) for x in r['deltas']):.2f} ms")
check('colours follow the F1 rules', r['color_errors'] == 0 and r['color_checks'] > 150,
      f"{r['color_checks']} checked")
check('position live on every update', r['pos_bad'] == 0, f"AC's own position was behind on {r['pos_lag']} updates")
check('purple, green and yellow all shown', {'purple', 'green', 'yellow'} <= r['colors'], r['colors'])
colors, sectors = r['first_display']
check('lap 1 of a new track: all 24 mini sectors and 3 sectors shown', len(colors) == 24 and all(colors)
      and all(s['time'] for s in sectors), [s['time'] for s in sectors])

print('--- next race on the same track (sector splits saved) ---')
r2 = race(path, 70)
check('sectors ready on the grid before the start', r2['ready_at_start'] is True)
colors, sectors = r2['first_display']
check('lap 1: all 24 mini sectors and 3 sectors shown', len(colors) == 24 and all(colors)
      and all(s['time'] for s in sectors), [s['time'] for s in sectors])

print('\nALL PASS' if not fails else f'\n{fails} FAILED')
sys.exit(1 if fails else 0)
