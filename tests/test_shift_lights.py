"""Shift lights: manual vs automatic gearbox, and learning where the automatic gearbox changes up.

Set AC_ROOT to your Assetto Corsa folder to also check a real car's data (optional).
"""
import collections
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
import ac_dash as ad
import carinfo

fails = 0


def check(name, ok, detail=''):
    global fails
    fails += not ok
    print(('PASS ' if ok else 'FAIL ') + name + (('  ' + str(detail)) if detail else ''))


# An F1 car: 12,500 rpm limiter, best manual change-up 12,312, automatic gearbox changes up at 10,800
PROFILE = {'limiter': 12500, 'gears': 8, 'shift': [12312] * 8, 'auto_up': 10800}

root = os.environ.get('AC_ROOT')
if root and os.path.isfile(os.path.join(root, 'content', 'cars', 'gp_2026_w17', 'data.acd')):
    w17 = carinfo.load_profile(root, 'gp_2026_w17', None)
    check('real car data (gp_2026_w17) matches the profile used below', w17 == PROFILE, w17)
else:
    print('SKIP real car data check (set AC_ROOT to an Assetto Corsa folder with gp_2026_w17)')

curve = [(0, 90), (6000, 116), (8500, 128), (10000, 125), (12500, 114), (13000, 89)]
check('best change-up from a torque curve stays under the limiter',
      all(r <= 12312 for r in carinfo.shift_points(curve, [16.5, 13.5, 11.0, 9.3], 12500)))

L = ad.ShiftLights()
check('manual, 3rd gear: flash at the best change-up point',
      L.target(3, False, PROFILE) == {'from': 10834, 'at': 12312, 'flash': True}, L.target(3, False, PROFILE))
check('top gear: no flash', L.target(8, False, PROFILE)['flash'] is False)
check('neutral / reverse: no lights', L.target(0, True, PROFILE) is None and L.target(-1, True, PROFILE) is None)
auto = L.target(3, True, PROFILE)
check('automatic, before learning: flash just before 10800', auto['at'] == 10530 and auto['flash'], auto)
check('automatic: lights start well before the gearbox changes up', auto['from'] < 10800, auto)

# A demo race with the automatic gearbox: the lights learn where it really changes up
clock = [1000.0]
ad.time.monotonic = lambda: clock[0]
demo = ad.DemoSource(seed=1)
L = ad.ShiftLights()
lit, flashed = set(), set()
for _ in range(60 * 40):
    p, g, s, app = demo.read()
    L.update('demo_car', p, clock[0], True, demo.PROFILE)
    gear = p.gear - 1
    t = L.target(gear, True, demo.PROFILE)
    if t and gear < 8:
        if p.rpms > t['from']:
            lit.add(gear)
        if t['flash'] and p.rpms >= t['at']:
            flashed.add(gear)
    clock[0] += 1 / 60
learned = {gear: sorted(v)[len(v) // 2] for gear, v in L.learned.items()}
check('learned the automatic change-up in gears 1-7', set(learned) == set(range(1, 8)), learned)
check('learned values match the real change-up (within 250 rpm of 10800)',
      all(10550 <= v <= 10800 for v in learned.values()), learned)
check('lights come on in every gear before the change', lit == set(range(1, 8)), lit)
check('lights flash in every gear before the change', flashed == set(range(1, 8)), flashed)

# A car without readable data: lights appear once the gearbox has been seen changing up
L2 = ad.ShiftLights()
check('no car data, nothing learned yet: no lights', L2.target(3, True, None) is None)
L2.learned[3] = collections.deque([10700, 10720])
check('no car data: uses what it learned', L2.target(3, True, None)['at'] == int(10720 * 0.975), L2.target(3, True, None))

print('\nALL PASS' if not fails else f'\n{fails} FAILED')
sys.exit(1 if fails else 0)
