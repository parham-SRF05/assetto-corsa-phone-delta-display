"""
Race engineer: measures what the car actually does, so the strategy is arithmetic and not opinion.

Run it, drive laps, and it prints one line per lap: lap time, fuel burned, tyre wear.
Stop it with Ctrl+C and it tells you how much fuel a race needs and how long each tyre lasts.

    python race_engineer.py --laps 53
    python race_engineer.py --laps 53 --track-length 5901

Everything comes from AC's shared memory, the same source the dashboard uses.
"""
import argparse
import csv
import ctypes
import datetime as dt
import mmap
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ac_dash import Graphics, Physics, Static  # noqa: E402  (same struct definitions, one source of truth)

MAPS = (('Local\\acpmf_physics', Physics), ('Local\\acpmf_graphics', Graphics), ('Local\\acpmf_static', Static))
POLL_HZ = 10


def attach():
    """Returns (physics, graphics, static) readers, or None until the game is running."""
    out = []
    for name, struct_type in MAPS:
        try:
            handle = mmap.mmap(-1, ctypes.sizeof(struct_type), name)
        except OSError:
            for h in out:
                h[0].close()
            return None
        out.append((handle, struct_type))
    return out


def read(handle, struct_type):
    handle.seek(0)
    return struct_type.from_buffer_copy(handle.read(ctypes.sizeof(struct_type)))


def ms(millis):
    if millis <= 0 or millis > 9_000_000:
        return '   --.---'
    return f'{millis // 60000}:{(millis % 60000) / 1000:06.3f}'


class Lap:
    __slots__ = ('number', 'time_ms', 'fuel_used', 'wear', 'temps', 'compound', 'in_pit', 'pressures')

    def __init__(self, number, time_ms, fuel_used, wear, temps, compound, in_pit, pressures=None):
        self.number = number
        self.time_ms = time_ms
        self.fuel_used = fuel_used
        self.wear = wear          # wear lost this lap, per corner (percentage points)
        self.temps = temps
        self.compound = compound
        self.in_pit = in_pit
        self.pressures = pressures or [0, 0, 0, 0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--laps', type=int, default=53, help='race distance, for the fuel sum')
    parser.add_argument('--track-length', type=float, default=5.901, help='km per lap')
    parser.add_argument('--reserve', type=float, default=1.5, help='spare litres to finish on')
    parser.add_argument('--csv', default=None, help='where to write the lap log')
    args = parser.parse_args()

    path = args.csv or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), 'logs',
        'stint-' + dt.datetime.now().strftime('%Y%m%d-%H%M') + '.csv')
    os.makedirs(os.path.dirname(path), exist_ok=True)

    print('Waiting for Assetto Corsa...', flush=True)
    handles = None
    while handles is None:
        handles = attach()
        if handles is None:
            time.sleep(1.0)
    (ph, pt), (gh, gt), (sh, st) = handles
    static = read(sh, st)
    print(f'Connected: {static.carModel} at {static.track}\n', flush=True)
    print(f'{"lap":>4} {"time":>10} {"fuel":>7} {"L/lap":>7} {"wear FL/FR/RL/RR":>26} {"temps":>22}  tyre')
    print('-' * 96, flush=True)

    laps = []
    last_completed = None
    fuel_at_lap_start = None
    wear_at_lap_start = None
    saw_pit = False
    log = open(path, 'w', newline='', encoding='utf-8')
    writer = csv.writer(log)
    writer.writerow(['lap', 'time_ms', 'fuel_left', 'fuel_used', 'compound',
                     'wear_fl', 'wear_fr', 'wear_rl', 'wear_rr',
                     'temp_fl', 'temp_fr', 'temp_rl', 'temp_rr',
                     'psi_fl', 'psi_fr', 'psi_rl', 'psi_rr', 'in_pit'])

    try:
        while True:
            physics = read(ph, pt)
            graphics = read(gh, gt)

            if graphics.isInPit:
                saw_pit = True

            if last_completed is None:
                last_completed = graphics.completedLaps
                fuel_at_lap_start = physics.fuel
                wear_at_lap_start = list(physics.tyreWear)

            if graphics.completedLaps != last_completed:
                used = (fuel_at_lap_start or 0) - physics.fuel
                wear_now = list(physics.tyreWear)
                lost = [round((wear_at_lap_start[i] - wear_now[i]), 3) for i in range(4)]
                temps = [round(t, 1) for t in physics.tyreCoreTemperature]
                press = [round(x, 1) for x in physics.wheelsPressure]
                lap = Lap(graphics.completedLaps, graphics.iLastTime, used, lost, temps,
                          graphics.tyreCompound.strip(), saw_pit, press)
                laps.append(lap)

                flag = ' (in/out lap)' if saw_pit else ''
                print(f'{lap.number:>4} {ms(lap.time_ms):>10} {physics.fuel:>6.1f}L '
                      f'{used:>6.2f}L  {"/".join(f"{w:5.2f}" for w in lost)}  '
                      f'{"/".join(f"{t:5.1f}" for t in temps)}  {lap.compound}{flag}', flush=True)
                writer.writerow([lap.number, lap.time_ms, round(physics.fuel, 2), round(used, 3), lap.compound,
                                 *lost, *temps, *press, int(saw_pit)])
                log.flush()

                last_completed = graphics.completedLaps
                fuel_at_lap_start = physics.fuel
                wear_at_lap_start = wear_now
                saw_pit = False

            time.sleep(1.0 / POLL_HZ)

    except KeyboardInterrupt:
        pass
    finally:
        log.close()
        summarise(laps, args)
        print(f'\nlap log: {path}')


def summarise(laps, args):
    """Only clean laps count: no pit lap, no lap slower than 115% of the best."""
    if not laps:
        print('\nNo complete laps recorded.')
        return
    timed = [lap for lap in laps if lap.time_ms > 0 and not lap.in_pit]
    if not timed:
        print('\nNo clean laps recorded.')
        return
    best = min(lap.time_ms for lap in timed)
    clean = [lap for lap in timed if lap.time_ms <= best * 1.15]

    print('\n' + '=' * 60)
    print(f'clean laps          {len(clean)}   best {ms(best)}')

    fuel = [lap.fuel_used for lap in clean if lap.fuel_used > 0]
    if fuel:
        per_lap = sum(fuel) / len(fuel)
        worst = max(fuel)
        need = per_lap * args.laps + args.reserve
        print(f'fuel                {per_lap:.2f} L/lap average, {worst:.2f} L worst')
        print(f'{args.laps} laps            {per_lap * args.laps:.1f} L + {args.reserve:.1f} L reserve '
              f'= {need:.1f} L to start with')
        print(f'                    ({need / args.track_length / args.laps * 100:.1f} L per 100 km)')

    hot = [lap.pressures for lap in clean if any(lap.pressures)]
    if hot:
        avg = [sum(p[i] for p in hot) / len(hot) for i in range(4)]
        print(f'hot pressures       {"/".join(f"{p:.1f}" for p in avg)} psi   '
              f'(this car wants about 28-30 psi hot)')

    by_compound = {}
    for lap in clean:
        by_compound.setdefault(lap.compound or '?', []).append(lap)
    print()
    for compound, group in by_compound.items():
        wear = [max(lap.wear) for lap in group if max(lap.wear) > 0]
        if not wear:
            continue
        per_lap = sum(wear) / len(wear)
        corner = ['FL', 'FR', 'RL', 'RR'][max(range(4), key=lambda i: sum(lap.wear[i] for lap in group))]
        print(f'{compound:<18} {per_lap:.2f}% per lap on the worst corner ({corner})')
        for limit in (10, 15, 20):
            print(f'                    {limit}% worn after {limit / per_lap:4.1f} laps')
    print('=' * 60)


if __name__ == '__main__':
    main()
