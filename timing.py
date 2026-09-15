"""Lap timing for every car in the session.

For each car Assetto Corsa gives its lap count, lap clock and position along the track
(0..1). Recording lap clock against track position gives:
  - sector and mini-sector times for every car, so purple means fastest of anyone,
  - a live delta to your best lap and to the fastest lap of the session.

Handled: race starts from the grid (lap 1 is timed from the start, like AC does), the
dashboard starting mid-lap, cars jumping (pit lane, back to the pits), a timing line that
isn't at track position 0, and tracks never seen before (sector splits are learned while
driving and saved). Car 0 is always the player.
"""
import array
import bisect
import json
import os

GRID = 1000               # a lap trace stores the lap clock at every 0.1% of the lap
MINIS_PER_SECTOR = 8
HOLD_PREVIOUS = 4.0       # seconds a finished lap's sectors stay on screen
LINE_TOLERANCE = 5e-4     # re-place the sectors if the timing line estimate moves more than this
MAX_GAP = 0.10            # a lap missing a longer stretch of data (as a lap fraction) isn't recorded
TIMING_GAP = 0.03         # a lap missing more than this still counts, but its sector times can't set bests
PURPLE, GREEN, YELLOW, LIVE = 'purple', 'green', 'yellow', 'live'


def classify(value, session_best, personal_best):
    """F1 colours: purple = fastest of anyone so far, green = your best so far, yellow = neither."""
    if session_best is None or value < session_best:
        return PURPLE
    if personal_best is None or value < personal_best:
        return GREEN
    return YELLOW


def wrap_half(x):
    """Folded into [-0.5, 0.5)."""
    return (x + 0.5) % 1.0 - 0.5


def build_grid(points, lap_time):
    """Lap clock (ms) at every 1/GRID of the lap, from (lap fraction, lap clock) samples in driving
    order. Samples before the line (a race start from the grid) give the clock at the line.
    Returns (grid, longest stretch without samples); grid is None if the lap isn't covered."""
    clean, before = [], None
    for q, t in points:
        if q <= 0.0:
            before = (q, t)
            continue
        if q >= 1.0:
            break
        if not clean:
            start = 0.0
            if before is not None:
                start = before[1] + (t - before[1]) * (0.0 - before[0]) / (q - before[0])
            clean.append((0.0, start))
        if q > clean[-1][0] and clean[-1][1] <= t < lap_time:
            clean.append((q, float(t)))
    if not clean:
        return None, 1.0
    clean.append((1.0, float(lap_time)))
    gap = max(b[0] - a[0] for a, b in zip(clean, clean[1:]))
    if len(clean) < 20 or gap > MAX_GAP:
        return None, gap
    grid = array.array('i')
    j = 0
    for k in range(GRID + 1):
        x = k / GRID
        while clean[j + 1][0] < x:
            j += 1
        (q0, t0), (q1, t1) = clean[j], clean[j + 1]
        grid.append(int(round(t0 + (t1 - t0) * (x - q0) / (q1 - q0))))
    return grid, gap


def time_at(grid, q):
    if q <= 0.0:
        return float(grid[0])
    x = q * GRID
    i = int(x)
    if i >= GRID:
        return float(grid[GRID])
    return grid[i] + (grid[i + 1] - grid[i]) * (x - i)


class Lap:
    __slots__ = ('car', 'time', 'grid', 'valid', 'line', 'gappy', 'minis', 'sectors')

    def __init__(self, car, lap_time, grid, valid, line, gappy=False):
        self.car, self.time, self.grid, self.valid, self.line = car, lap_time, grid, valid, line
        self.gappy = gappy
        self.minis = self.sectors = None


class Car:
    def __init__(self):
        self.lap_count = None
        self.samples = None       # [(track position, lap clock)] of the current lap
        self.positions = None     # the same positions, for fast lookups
        self.complete = False     # samples cover this lap from the timing line
        self.clock_ok = False     # the lap clock still counts from the timing line
        self.valid = True
        self.name = None
        self.last_raw = None      # (track position, lap clock) from the previous update
        self.last_lap_seen = 0
        self.pending = None       # crossed the line; waiting for AC's official lap time
        self.best = None


class Timing:
    def __init__(self, cache_file=None):
        self.cache_file = cache_file
        self.cache = {}
        if cache_file and os.path.isfile(cache_file):
            try:
                with open(cache_file, encoding='utf-8') as f:
                    self.cache = json.load(f)
            except (OSError, ValueError):
                self.cache = {}
        self.key = object()
        self.reset(None, 3, None)

    # --- session ---------------------------------------------------------------------------

    def reset(self, key, sector_count, track_id):
        self.key = key
        self.n = sector_count
        self.track_id = track_id
        self.cars = {}
        self.laps = []
        self.fastest = None
        self.live = self.finishing = self.previous = None
        self.prev_sector_index = None
        self.line_samples = []
        self.line_observed = False
        self.bounds = self.mini_bounds = None
        self.best_minis = self.best_sectors = self.pb_minis = self.pb_sectors = None
        self.learned = [None] * (sector_count - 1)
        entry = self.cache.get(track_id) if track_id else None
        self.line = float(entry.get('line', 0.0)) if entry else 0.0  # most tracks: position 0 is the line
        self.splits = None        # sector ends as track positions (not relative to the line)
        if sector_count == 1:
            self.splits = []
        elif entry and len(entry.get('sectors', ())) == sector_count - 1:
            self.splits = [(b + self.line) % 1.0 for b in entry['sectors']]
        if self.splits is not None:
            self._apply_splits()

    def update(self, now, key, track_id, sector_count, cars, sector_index):
        """cars: per-car data from the in-game app (index 0 = player).
        sector_index: the player's current sector from AC's shared memory."""
        player = self.cars.get(0)
        restarted = (cars and player is not None and player.lap_count is not None
                     and cars[0].connected and cars[0].lap_count < player.lap_count)
        if key != self.key or restarted:
            self.reset(key, sector_count, track_id)
        for i, c in enumerate(cars):
            self._update_car(now, i, c)
        if cars:
            self._learn_sectors(cars[0], sector_index)
            self._sync_live()

    # --- per car -----------------------------------------------------------------------------

    def _update_car(self, now, i, c):
        car = self.cars.get(i)
        if car is None:
            car = self.cars[i] = Car()
        if not c.connected:
            if car.lap_count is not None:
                self.cars[i] = Car()
            if i == 0:
                self.live = self.finishing = None
            return
        if car.name is not None and c.name and c.name != car.name:
            self._forget_car(i)  # someone else took this slot (online)
            car = self.cars[i]
        if c.name:
            car.name = c.name
        if car.pending:
            self._try_finish(now, i, car, c)
        if car.lap_count is None:
            car.lap_count = c.lap_count
            q = wrap_half(c.spline - self.line)
            # Race start (clock not running yet, on the grid) or just past the line: full lap ahead.
            self._start_trace(car, c, from_line=c.lap_time < 500 and -0.1 < q < 0.02)
        elif c.lap_count != car.lap_count:
            pending = None
            if c.lap_count == car.lap_count + 1 and car.last_raw is not None:
                pending = {'samples': car.samples if car.complete else None, 'valid': car.valid,
                           'last_before': car.last_lap_seen, 'since': now,
                           'before': car.last_raw, 'after': (c.spline, c.lap_time)}
            if i == 0:
                self.finishing = self.live if pending else None
                self.live = None
            car.lap_count = c.lap_count
            self._start_trace(car, c, from_line=0 <= c.lap_time < 1000)
            car.pending = pending
            if pending:
                self._try_finish(now, i, car, c)
        else:
            self._add_sample(car, c)
        car.last_lap_seen, car.last_raw = c.last_lap, (c.spline, c.lap_time)

    def _start_trace(self, car, c, from_line, clock_ok=True):
        q = (c.spline - self.line) % 1.0  # how far round the lap
        if q > 0.5 and (from_line or c.lap_time < 1000):
            q -= 1.0  # just short of the line: on the grid, or the lap has only just started
        start = self.line + q
        car.samples = [(start, c.lap_time)]
        car.positions = [start]
        car.complete = from_line
        car.clock_ok = clock_ok
        car.valid = True

    def _add_sample(self, car, c):
        prev_p, prev_t = car.samples[-1]
        p = c.spline + round(prev_p - c.spline)  # keep the position continuous across 1.0 -> 0.0
        t = c.lap_time
        moved = abs(p - prev_p)
        if t < prev_t - 50 or (moved > 0.05 and moved * 20000 > max(t - prev_t, 0)):
            # The lap clock went back (e.g. back to the pits) or the car jumped faster than any car
            # can drive: carry on from here, but this lap can no longer be a complete trace.
            valid = car.valid
            self._start_trace(car, c, from_line=False, clock_ok=t >= prev_t - 50 and car.clock_ok)
            car.valid = valid
            return
        if c.invalid and t > 500:
            car.valid = False
        if t > prev_t and p > prev_p:
            car.samples.append((p, t))
            car.positions.append(p)

    def _try_finish(self, now, i, car, c):
        pend = car.pending
        lap_time = c.last_lap
        plausible = 0 <= lap_time - pend['before'][1] <= 1000
        if plausible and (lap_time != pend['last_before'] or now - pend['since'] > 0.5):
            car.pending = None
            self._finish_lap(now, i, car, pend, lap_time)
        elif now - pend['since'] > 2.0:
            car.pending = None  # AC never reported a matching lap time
            if i == 0:
                self.finishing = None

    def _finish_lap(self, now, i, car, pend, lap_time):
        if i == 0:
            self._close_player_display(lap_time, now)  # colours use the bests from before this lap
        # The line lies between the last update of the old lap and the first of the new one;
        # the lap clocks say exactly how far along that step it was crossed.
        (pos_a, t_a), (pos_b, t_b) = pend['before'], pend['after']
        pos_b += round(pos_a - pos_b)
        step = (lap_time - t_a) + t_b
        if step > 0 and 0 <= t_b < 1000 and abs(pos_b - pos_a) < 0.05:
            self._observe_line(pos_a + (pos_b - pos_a) * (lap_time - t_a) / step)
        samples = pend['samples']
        if samples is None or lap_time <= 0:
            return
        grid, gap = build_grid([(p - self.line, t) for p, t in samples], lap_time)
        if grid is None:
            return
        lap = Lap(i, lap_time, grid, pend['valid'], self.line, gappy=gap > TIMING_GAP)
        self._measure(lap)
        self.laps.append(lap)
        if lap.valid:
            self._add_best(car, lap)

    def _forget_car(self, i):
        self.cars[i] = Car()
        self.laps = [lap for lap in self.laps if lap.car != i]
        if i == 0:
            self.live = self.finishing = self.previous = None
        self._rebuild_bests()

    # --- timing line and sectors -------------------------------------------------------------

    def _observe_line(self, position):
        self.line_samples.append(wrap_half(position))
        del self.line_samples[:-21]
        ordered = sorted(self.line_samples)
        estimate = ordered[len(ordered) // 2]
        moved = abs(wrap_half(estimate - self.line)) > LINE_TOLERANCE
        self.line_observed = True
        if moved:
            self.line = estimate
            if self.splits is not None and self._apply_splits():
                self._save_cache()

    def _learn_sectors(self, player, sector_index):
        prev, self.prev_sector_index = self.prev_sector_index, sector_index
        if prev is None or not player.connected or self.bounds is not None:
            return
        if sector_index == prev + 1 and 1 <= sector_index <= self.n - 1:
            self.learned[sector_index - 1] = player.spline % 1.0
            if all(s is not None for s in self.learned):
                self.splits = list(self.learned)
                if self._apply_splits():
                    self._save_cache()
                else:
                    # Doesn't fit yet (usually: timing line not found yet). Keep these splits for
                    # when it is, but learn them again meanwhile.
                    self.learned = [None] * (self.n - 1)

    def _apply_splits(self):
        bounds = [(s - self.line) % 1.0 for s in self.splits]
        if not all(a < b for a, b in zip([0.0] + bounds, bounds + [1.0])):
            self.bounds = self.mini_bounds = None
            self.live = self.finishing = None
            return False
        self.bounds = bounds
        edges = [0.0] + bounds + [1.0]
        m = MINIS_PER_SECTOR
        self.mini_bounds = [edges[k] + (edges[k + 1] - edges[k]) * j / m
                            for k in range(self.n) for j in range(m)] + [1.0]
        self.live = self.finishing = None  # rebuilt from the car's trace on the next update
        self._rebuild_bests()
        return True

    def _save_cache(self):
        if not self.cache_file or not self.track_id or self.bounds is None:
            return
        self.cache[self.track_id] = {'line': round(self.line, 6),
                                     'sectors': [round(b, 6) for b in self.bounds]}
        try:
            os.makedirs(os.path.dirname(self.cache_file), exist_ok=True)
            tmp = self.cache_file + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, indent=1)
            os.replace(tmp, self.cache_file)
        except OSError:
            pass

    # --- bests -------------------------------------------------------------------------------

    def _rebuild_bests(self):
        self.fastest = None
        for car in self.cars.values():
            car.best = None
        if self.mini_bounds is not None:
            count = self.n * MINIS_PER_SECTOR
            self.best_minis, self.pb_minis = [None] * count, [None] * count
            self.best_sectors, self.pb_sectors = [None] * self.n, [None] * self.n
        for lap in self.laps:
            self._measure(lap)
            if lap.valid and lap.car in self.cars:
                self._add_best(self.cars[lap.car], lap)

    def _measure(self, lap):
        if self.mini_bounds is None:
            return
        shift = self.line - lap.line  # in case the line estimate moved since this lap was recorded
        at = [time_at(lap.grid, b + shift) for b in self.mini_bounds]
        at[0] = 0.0  # sector 1 counts from the lap clock's start, like AC (includes a grid start)
        m = MINIS_PER_SECTOR
        lap.minis = [b - a for a, b in zip(at, at[1:])]
        lap.sectors = [at[(k + 1) * m] - at[k * m] for k in range(self.n)]

    def _add_best(self, car, lap):
        if self.fastest is None or lap.time < self.fastest.time:
            self.fastest = lap
        if car.best is None or lap.time < car.best.time:
            car.best = lap
        if lap.minis is None or lap.gappy:
            return  # sector times across a stretch of missing data are estimates: no bests from them
        targets = [(self.best_minis, lap.minis), (self.best_sectors, lap.sectors)]
        if lap.car == 0:
            targets += [(self.pb_minis, lap.minis), (self.pb_sectors, lap.sectors)]
        for best, values in targets:
            for j, v in enumerate(values):
                if best[j] is None or v < best[j]:
                    best[j] = v

    # --- the player's live lap ---------------------------------------------------------------

    def _sync_live(self):
        """Closes every mini sector the player has passed. Works from the car's trace, so it also
        catches up when sector splits are found mid-lap or the dashboard started mid-lap."""
        car = self.cars.get(0)
        if car is None or car.samples is None or not car.clock_ok or self.mini_bounds is None:
            self.live = None
            return
        live = self.live
        if live is None or live['samples'] is not car.samples:
            live = self.live = {'samples': car.samples, 'cross': [0.0], 'colors': [], 'sectors': []}
        bounds, positions, cross = self.mini_bounds, car.positions, live['cross']
        while len(cross) < len(bounds) - 1:  # the finish line is closed by the lap time
            k = len(cross)
            target = self.line + bounds[k]
            if target > positions[-1]:
                break
            cross.append(None if target < positions[0] else self._time_at_position(car, target))
            self._close_mini(live, k - 1)

    @staticmethod
    def _time_at_position(car, target):
        i = bisect.bisect_left(car.positions, target)
        if i <= 0:
            return float(car.samples[0][1])
        if i >= len(car.samples):
            return float(car.samples[-1][1])
        (p0, t0), (p1, t1) = car.samples[i - 1], car.samples[i]
        return t0 + (t1 - t0) * (target - p0) / (p1 - p0)

    def _close_player_display(self, lap_time, now):
        live, self.finishing = self.finishing, None
        if live is None or self.mini_bounds is None:
            return
        last = len(self.mini_bounds) - 1
        cross = live['cross']
        if len(cross) > last:
            return
        p_a, t_a = live['samples'][-1]
        end = self.line + 1.0
        while len(cross) < last:  # boundaries passed between the last update and the line
            k = len(cross)
            target = self.line + self.mini_bounds[k]
            value = t_a + (lap_time - t_a) * (target - p_a) / (end - p_a) if end > p_a else float(lap_time)
            cross.append(min(max(value, float(t_a)), float(lap_time)))
            self._close_mini(live, k - 1)
        cross.append(float(lap_time))
        self._close_mini(live, last - 1)
        self.previous = (live, now)

    def _close_mini(self, live, j):
        cross = live['cross']
        a, b = cross[j], cross[j + 1]
        live['colors'].append(None if a is None or b is None
                              else classify(b - a, self.best_minis[j], self.pb_minis[j]))
        m = MINIS_PER_SECTOR
        if (j + 1) % m == 0:
            k = j // m
            a = cross[j + 1 - m]
            if a is None or b is None:
                live['sectors'].append({'color': None, 'time': None, 'delta': None})  # not seen
                return
            value, best = b - a, self.best_sectors[k]
            live['sectors'].append({
                'color': classify(value, best, self.pb_sectors[k]),
                'time': int(round(value)),
                'delta': None if best is None else int(round(value - best)),
            })

    # --- what the dashboard shows ------------------------------------------------------------

    def delta_to(self, lap):
        """Live gap of the player's current lap to a reference lap, in ms (negative = ahead)."""
        car = self.cars.get(0)
        if lap is None or car is None or not car.samples or not car.clock_ok:
            return None
        p, t = car.samples[-1]
        q = p - lap.line
        if not 0.0 < q < 1.0 or t <= 0:
            return None
        return int(round(t - time_at(lap.grid, q)))

    def player_best(self):
        car = self.cars.get(0)
        return car.best if car else None

    def lap_invalid(self):
        car = self.cars.get(0)
        return bool(car and car.samples and not car.valid)

    def sectors_view(self, now):
        source, previous = self.live, False
        if self.previous and now - self.previous[1] < HOLD_PREVIOUS:
            source, previous = self.previous[0], True
        m = MINIS_PER_SECTOR
        out = []
        for k in range(self.n):
            sector = {'state': 'pending', 'color': None, 'time': None, 'delta': None,
                      'prev': previous, 'minis': [None] * m}
            if source is not None:
                colors = source['colors']
                for j in range(m):
                    index = k * m + j
                    if index < len(colors):
                        sector['minis'][j] = colors[index]
                    elif index == len(colors) and not previous:
                        sector['minis'][j] = LIVE
                if k < len(source['sectors']):
                    sector.update(source['sectors'][k])
                    sector['state'] = 'done'
                elif not previous and len(colors) >= k * m:
                    sector['state'] = 'live'
            out.append(sector)
        return out
