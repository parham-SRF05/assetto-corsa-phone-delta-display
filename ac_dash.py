"""Assetto Corsa -> phone dashboard.

Reads Assetto Corsa's shared memory (your car) plus the "AC Dash" in-game app's data (every
car's live timing), and serves a page that a phone on the same Wi-Fi opens in its browser.
Everything that happens is also written to dashboard.log next to this file.

    python ac_dash.py           normal use
    python ac_dash.py --demo    fake race data, to test the phone page without AC
    python ac_dash.py --debug   print raw values once per second
"""
import argparse
import collections
import ctypes
import ctypes.wintypes as wt
import json
import math
import os
import random
import socket
import struct
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import strategy as strategy_module
from carinfo import CarProfiles
from timing import Timing

PORT = 8765
POLL_HZ = 60   # how often AC's data is read; sets timing accuracy
PUSH_HZ = 30   # how often the phone gets an update (fast enough for shift lights)
HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.path.join(HERE, 'cache')
LOG_FILE = os.path.join(HERE, 'dashboard.log')

AC_OFF, AC_REPLAY, AC_LIVE, AC_PAUSE = 0, 1, 2, 3
AC_RACE = 2
SESSION_NAMES = {0: 'practice', 1: 'qualifying', 2: 'race', 3: 'hotlap', 4: 'time attack', 5: 'drift', 6: 'drag'}


# --- Log ----------------------------------------------------------------------------------------

_log_lock = threading.Lock()


def log(message):
    line = time.strftime('%Y-%m-%d %H:%M:%S  ') + message
    with _log_lock:
        print('\n' + line, flush=True)
        try:
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError:
            pass


def rotate_log():
    try:
        if os.path.getsize(LOG_FILE) > 2_000_000:
            os.replace(LOG_FILE, LOG_FILE + '.old')
    except OSError:
        pass


# --- AC shared memory layout (Kunos SharedFileOut.h), leading fields only -----------------

class Physics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('packetId', ctypes.c_int), ('gas', ctypes.c_float), ('brake', ctypes.c_float),
        ('fuel', ctypes.c_float), ('gear', ctypes.c_int), ('rpms', ctypes.c_int),
        ('steerAngle', ctypes.c_float), ('speedKmh', ctypes.c_float),
        ('velocity', ctypes.c_float * 3), ('accG', ctypes.c_float * 3),
        ('wheelSlip', ctypes.c_float * 4), ('wheelLoad', ctypes.c_float * 4),
        ('wheelsPressure', ctypes.c_float * 4), ('wheelAngularSpeed', ctypes.c_float * 4),
        ('tyreWear', ctypes.c_float * 4), ('tyreDirtyLevel', ctypes.c_float * 4),
        ('tyreCoreTemperature', ctypes.c_float * 4), ('camberRAD', ctypes.c_float * 4),
        ('suspensionTravel', ctypes.c_float * 4), ('drs', ctypes.c_float), ('tc', ctypes.c_float),
        ('heading', ctypes.c_float), ('pitch', ctypes.c_float), ('roll', ctypes.c_float),
        ('cgHeight', ctypes.c_float), ('carDamage', ctypes.c_float * 5),
        ('numberOfTyresOut', ctypes.c_int), ('pitLimiterOn', ctypes.c_int), ('abs', ctypes.c_float),
        ('kersCharge', ctypes.c_float), ('kersInput', ctypes.c_float), ('autoShifterOn', ctypes.c_int),
    ]


class Graphics(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('packetId', ctypes.c_int), ('status', ctypes.c_int), ('session', ctypes.c_int),
        ('currentTime', ctypes.c_wchar * 15), ('lastTime', ctypes.c_wchar * 15),
        ('bestTime', ctypes.c_wchar * 15), ('split', ctypes.c_wchar * 15),
        ('completedLaps', ctypes.c_int), ('position', ctypes.c_int),
        ('iCurrentTime', ctypes.c_int), ('iLastTime', ctypes.c_int), ('iBestTime', ctypes.c_int),
        ('sessionTimeLeft', ctypes.c_float), ('distanceTraveled', ctypes.c_float),
        ('isInPit', ctypes.c_int), ('currentSectorIndex', ctypes.c_int),
        ('lastSectorTime', ctypes.c_int), ('numberOfLaps', ctypes.c_int),
        ('tyreCompound', ctypes.c_wchar * 33), ('replayTimeMultiplier', ctypes.c_float),
        ('normalizedCarPosition', ctypes.c_float),
    ]


class Static(ctypes.Structure):
    _pack_ = 4
    _fields_ = [
        ('smVersion', ctypes.c_wchar * 15), ('acVersion', ctypes.c_wchar * 15),
        ('numberOfSessions', ctypes.c_int), ('numCars', ctypes.c_int),
        ('carModel', ctypes.c_wchar * 33), ('track', ctypes.c_wchar * 33),
        ('playerName', ctypes.c_wchar * 33), ('playerSurname', ctypes.c_wchar * 33),
        ('playerNick', ctypes.c_wchar * 33), ('sectorCount', ctypes.c_int),
    ]


# These offsets are fixed by AC's format; refuse to run if the layout above is off.
for _struct, _field, _offset in [
        (Physics, 'speedKmh', 28), (Physics, 'numberOfTyresOut', 244), (Physics, 'kersCharge', 256),
        (Physics, 'autoShifterOn', 264),
        (Graphics, 'completedLaps', 132), (Graphics, 'position', 136),
        (Graphics, 'numberOfLaps', 172), (Graphics, 'normalizedCarPosition', 248),
        (Static, 'numCars', 64), (Static, 'sectorCount', 400)]:
    if getattr(_struct, _field).offset != _offset:
        raise SystemExit(f'Layout error: {_struct.__name__}.{_field} is not at offset {_offset}')


# --- The in-game app's data (must match acdash_app/acdash.py) --------------------------------

APP_MAP = 'Local\\acdash_live'
APP_VERSION = 1
APP_HEADER = struct.Struct('<4sIII260s64s64s')
APP_CAR = struct.Struct('<fiiiiiBBBx32s')
APP_MAX_CARS = 64
APP_SIZE = APP_HEADER.size + APP_MAX_CARS * APP_CAR.size

CarData = collections.namedtuple(
    'CarData', 'spline lap_time last_lap best_lap lap_count race_pos in_pit connected invalid name')
AppData = collections.namedtuple('AppData', 'root track layout cars')


def _text(raw):
    return raw.split(b'\0', 1)[0].decode('utf-8', 'replace')


def parse_app(raw):
    magic, version, _, count, root, track, layout = APP_HEADER.unpack_from(raw, 0)
    if magic != b'ACDH' or version != APP_VERSION:
        return None
    cars = []
    for i in range(min(count, APP_MAX_CARS)):
        spline, lap_time, last, best, laps, pos, pit, connected, invalid, name = \
            APP_CAR.unpack_from(raw, APP_HEADER.size + i * APP_CAR.size)
        if not math.isfinite(spline):
            spline, connected, lap_time, laps = 0.0, 0, 0, 0  # no position: treat the car as absent
        # Offline AI may not count as "connected"; a car whose clock runs is clearly there.
        present = bool(connected) or lap_time > 0 or laps > 0
        cars.append(CarData(spline % 1.0, lap_time, last, best, laps, pos, bool(pit), present, bool(invalid),
                            _text(name)))
    return AppData(_text(root), _text(track), _text(layout), cars)


# --- Reading shared memory ----------------------------------------------------------------------

FILE_MAP_READ = 0x0004
_k32 = ctypes.WinDLL('kernel32', use_last_error=True)
_k32.OpenFileMappingW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]
_k32.OpenFileMappingW.restype = wt.HANDLE
_k32.MapViewOfFile.argtypes = [wt.HANDLE, wt.DWORD, wt.DWORD, wt.DWORD, ctypes.c_size_t]
_k32.MapViewOfFile.restype = ctypes.c_void_p
_k32.UnmapViewOfFile.argtypes = [ctypes.c_void_p]
_k32.CloseHandle.argtypes = [wt.HANDLE]


def _open_map(name):
    handle = _k32.OpenFileMappingW(FILE_MAP_READ, False, name)
    if not handle:
        return None
    addr = _k32.MapViewOfFile(handle, FILE_MAP_READ, 0, 0, 0)
    if not addr:
        _k32.CloseHandle(handle)
        return None
    return handle, addr


def _close_map(view):
    if view:
        _k32.UnmapViewOfFile(view[1])
        _k32.CloseHandle(view[0])


class StaleWatch:
    """Data that stops changing means its writer is gone (or paused). Let go of the memory once so
    a closed game's memory is freed, and keep treating that same frozen data as stale."""

    def __init__(self, stale_after):
        self.stale_after = stale_after
        self.last_id = None
        self.last_change = 0.0
        self.stale_id = None

    def restart(self, now):
        self.last_id, self.last_change = None, now

    def check(self, packet_id, now):
        """Returns 'fresh', 'stale' (just went stale: close the memory) or 'still_stale'."""
        if packet_id != self.last_id:
            self.last_id, self.last_change = packet_id, now
        if packet_id != self.stale_id and now - self.last_change <= self.stale_after:
            self.stale_id = None
            return 'fresh'
        if self.stale_id is None:
            self.stale_id = packet_id
            return 'stale'
        return 'still_stale'


class ACSource:
    """AC's own shared memory, opened read-only (never created, so AC is unaffected)."""

    MAPS = (('Local\\acpmf_physics', Physics), ('Local\\acpmf_graphics', Graphics),
            ('Local\\acpmf_static', Static))

    def __init__(self):
        self.views = None
        self.watch = StaleWatch(3.0)

    def _close(self):
        for view in self.views or []:
            _close_map(view)
        self.views = None

    def read(self):
        """(physics, graphics, static), or None when AC isn't running."""
        now = time.monotonic()
        if self.views is None:
            views = [_open_map(name) for name, _ in self.MAPS]
            if not all(views):
                for view in views:
                    _close_map(view)
                return None
            self.views = views
            self.watch.restart(now)
        phys, graph, stat = (struct_.from_buffer_copy(ctypes.string_at(view[1], ctypes.sizeof(struct_)))
                             for view, (_, struct_) in zip(self.views, self.MAPS))
        verdict = self.watch.check(phys.packetId, now)
        if verdict == 'fresh':
            return phys, graph, stat
        if verdict == 'stale':
            self._close()
        return (phys, graph, stat) if graph.status == AC_PAUSE else None


class AppSource:
    """Every car's live timing, written by the AC Dash in-game app."""

    def __init__(self):
        self.view = None
        self.watch = StaleWatch(2.0)
        self.last = None

    def read(self):
        now = time.monotonic()
        if self.view is None:
            self.view = _open_map(APP_MAP)
            if self.view is None:
                return None
            self.watch.restart(now)
        addr = self.view[1]
        for _ in range(5):
            before = struct.unpack('<I', ctypes.string_at(addr + 8, 4))[0]
            if before & 1:  # the app is mid-write
                time.sleep(0.001)
                continue
            raw = ctypes.string_at(addr, APP_SIZE)
            if struct.unpack_from('<I', raw, 8)[0] == before:
                break
        else:
            return self.last
        verdict = self.watch.check(before, now)
        if verdict != 'fresh':
            if verdict == 'stale':
                _close_map(self.view)
                self.view = None
            return None
        self.last = parse_app(raw)
        return self.last


class LiveSource:
    def __init__(self):
        self.ac = ACSource()
        self.app = AppSource()
        self.profiles = CarProfiles(CACHE_DIR, log)

    def read(self):
        snap = self.ac.read()
        if snap is None:
            return None
        p, g, s = snap
        app = self.app.read()
        if app is None and g.status == AC_PAUSE:
            app = self.app.last  # the app may not run while paused
        return p, g, s, app

    def profile(self, app, car):
        return self.profiles.get(app.root, car)


# --- Shift lights -----------------------------------------------------------------------------

class ShiftLights:
    """Where the lights flash in the current gear.

    Manual gearbox: the best change-up point from the car's torque curve and gearing.
    Automatic gearbox: just before the gearbox changes up. Starts from the car's own automatic
    gearbox setting, then follows where it is seen actually changing up, so it fits any car.
    """

    WINDOW = 0.4        # seconds of rpm history used to find the rpm just before a change up
    AUTO_LEAD = 0.975   # flash a little before the automatic change so it can be seen

    def __init__(self):
        self.car = None
        self._reset()

    def _reset(self):
        self.learned = {}
        self.gear = None
        self.recent = collections.deque()

    def update(self, car, p, now, auto, profile):
        if car != self.car:
            self.car = car
            self._reset()
        gear = p.gear - 1
        if gear != self.gear:
            if auto and self.gear is not None and self.gear >= 1 and gear == self.gear + 1 and self.recent:
                peak = max(rpm for _, rpm, _ in self.recent)
                pushing = max(gas for _, _, gas in self.recent) > 0.5
                if pushing and peak > (0.5 * profile['limiter'] if profile else 3000):
                    self.learned.setdefault(self.gear, collections.deque(maxlen=5)).append(peak)
            self.gear = gear
            self.recent.clear()
        self.recent.append((now, p.rpms, p.gas))
        while now - self.recent[0][0] > self.WINDOW:
            self.recent.popleft()

    def target(self, gear, auto, profile):
        if gear < 1:
            return None
        if profile and gear >= profile['gears']:
            at = profile['shift'][-1]  # top gear: lights show the limiter coming, no flash
            return {'from': int(at * 0.88), 'at': at, 'flash': False}
        if auto:
            seen = sorted(self.learned.get(gear, ()))
            up = (seen[len(seen) // 2] if len(seen) >= 2 else
                  profile.get('auto_up') if profile and profile.get('auto_up') else
                  seen[0] if seen else None)
            if up:
                at = int(up * self.AUTO_LEAD)
                return {'from': int(at * 0.86), 'at': at, 'flash': True}
        if profile:
            at = profile['shift'][min(gear, len(profile['shift'])) - 1]
            return {'from': int(at * 0.88), 'at': at, 'flash': True}
        return None


# --- Demo -------------------------------------------------------------------------------------

class DemoSource:
    """A fake 10-lap race with 8 cars: grid start, automatic gearbox. For testing without AC."""

    RACE_LAPS = 10
    NAMES = ('You', 'Hamilton', 'Russell', 'Leclerc', 'Norris', 'Piastri', 'Verstappen', 'Alonso')
    PACE = (30250, 30000, 30120, 30400, 30550, 30700, 30850, 31000)  # average lap, ms
    SPLITS = (0.30, 0.66)   # sector ends, as a fraction of the lap
    LINE = 0.004            # track position of the timing line
    CHUNKS = 48
    GRID_GAP = 0.0025       # grid slots behind the line, as a fraction of the lap
    LIGHTS = 2.0            # seconds on the grid before the start
    RATIOS = (16.5, 13.5, 11.0, 9.3, 7.6, 6.5, 5.75, 5.1)
    AUTO_UP = 10800
    PROFILE = {'limiter': 12500, 'gears': 8, 'shift': [12312] * 8, 'auto_up': 10800}

    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.p, self.g, self.s = Physics(), Graphics(), Static()
        self.s.carModel, self.s.track = 'demo_car', 'demo_track'
        self.s.numCars, self.s.sectorCount = len(self.NAMES), 3
        self.g.status, self.g.session, self.g.numberOfLaps = AC_LIVE, AC_RACE, self.RACE_LAPS
        self.p.autoShifterOn = 1
        self.shape = [1 + 0.35 * math.sin(6 * math.pi * j / self.CHUNKS) for j in range(self.CHUNKS)]
        self._start(time.monotonic())

    def _lap(self, pace):
        mean = sum(self.shape) / self.CHUNKS
        return [pace / self.CHUNKS * w / mean * (1 + self.rng.uniform(-0.02, 0.02)) for w in self.shape]

    def _start(self, now):
        self.race_start = now + self.LIGHTS
        self.cars = [{'name': name, 'pace': pace, 'laps': 0, 'start': self.race_start,
                      'grid': -self.GRID_GAP * (i + 1), 'chunks': self._lap(pace), 'last': 0, 'best': 0,
                      'history': []}
                     for i, (name, pace) in enumerate(zip(self.NAMES, self.PACE))]
        self.g.position, self.g.completedLaps = 1, 0
        self.gear, self.rpm, self.last_now = 1, 5000.0, now

    @staticmethod
    def pre_line(car):
        """Time from the grid to the line (lap 1 only)."""
        return -car['grid'] * car['pace'] if car['laps'] == 0 else 0.0

    @classmethod
    def progress(cls, chunks, elapsed):
        done = 0.0
        for j, d in enumerate(chunks):
            if elapsed < done + d:
                return (j + (elapsed - done) / d) / cls.CHUNKS
            done += d
        return 1.0

    def read(self):
        now = time.monotonic()
        if self.cars[0]['laps'] >= self.RACE_LAPS + 2:
            self._start(now)
        states = []
        for car in self.cars:
            elapsed = (now - car['start']) * 1000
            while elapsed >= self.pre_line(car) + sum(car['chunks']):
                total = self.pre_line(car) + sum(car['chunks'])
                car['history'].append((self.pre_line(car), car['chunks']))
                car['laps'] += 1
                car['last'] = int(round(total))
                car['best'] = min(car['best'] or car['last'], car['last'])
                car['start'] += total / 1000
                car['chunks'] = self._lap(car['pace'])
                elapsed -= total
            elapsed = max(0.0, elapsed)
            pre = self.pre_line(car)
            q = car['grid'] * (1 - elapsed / pre) if elapsed < pre else self.progress(car['chunks'], elapsed - pre)
            states.append((car['laps'] + q, elapsed, q))
        order = sorted(range(len(self.cars)), key=lambda i: -states[i][0])
        cars = [CarData((q + self.LINE) % 1.0, int(elapsed), car['last'], car['best'], car['laps'],
                        order.index(i), False, True, False, car['name'])
                for i, (car, (_, elapsed, q)) in enumerate(zip(self.cars, states))]
        g, p, me, q = self.g, self.p, cars[0], states[0][2]
        if me.lap_count != g.completedLaps:
            g.position = order.index(0) + 1  # AC's own position only updates once per lap
        g.completedLaps, g.iCurrentTime, g.normalizedCarPosition = me.lap_count, me.lap_time, me.spline
        g.currentSectorIndex = sum(1 for split in self.SPLITS if q >= split)

        dt, self.last_now = max(0.0, now - self.last_now), now
        gas = 0.0
        if now < self.race_start:
            self.gear, self.rpm = 1, 5000.0
        elif (now - self.race_start) % 12.0 < 10.0:
            gas = 1.0
            self.rpm += 9000.0 / self.gear * dt
            if self.gear < 8 and self.rpm >= self.AUTO_UP:  # the automatic gearbox changes up
                self.rpm *= self.RATIOS[self.gear] / self.RATIOS[self.gear - 1]
                self.gear += 1
            self.rpm = min(self.rpm, 12400.0)
        else:
            self.gear, self.rpm = 3, 8000.0
        p.gear, p.rpms, p.gas, p.speedKmh = self.gear + 1, int(self.rpm), gas, 60 + 30 * self.gear
        p.kersCharge = 0.5 + 0.45 * math.sin((now - self.race_start) / 6)
        p.packetId += 1
        return p, g, self.s, AppData('', 'demo_track', '', cars)

    def profile(self, app, car):
        return self.PROFILE


# --- Building what the phone shows --------------------------------------------------------------

def fmt_lap(ms):
    ms = int(ms)
    return '%d:%02d.%03d' % (ms // 60000, ms // 1000 % 60, ms % 1000)


def build_view(p, g, s, app, timing, shift, auto, now, engineer=None):
    cars = app.cars
    present = [c for c in cars if c.connected]
    position = g.position
    if g.session == AC_RACE and cars and cars[0].race_pos >= 0 and present:
        base = 1 if min(c.race_pos for c in present) == 0 else 0  # 0-based or 1-based
        position = cars[0].race_pos + base
    view = {'live': True, 'paused': g.status == AC_PAUSE, 'position': position, 'cars': len(present)}

    if g.session == AC_RACE and g.numberOfLaps > 0:
        left = g.numberOfLaps - g.completedLaps
        view['lapsLabel'] = 'LAPS LEFT'
        view['lapsValue'] = str(left) if left > 0 else 'FIN'
        view['lapsSub'] = ('FINAL LAP' if left == 1 else
                           f'LAP {g.completedLaps + 1}/{g.numberOfLaps}' if left > 0 else 'FINISHED')
    elif math.isfinite(g.sessionTimeLeft) and g.sessionTimeLeft > 0:
        secs = int(g.sessionTimeLeft // 1000)
        view['lapsLabel'], view['lapsValue'] = 'TIME LEFT', f'{secs // 60}:{secs % 60:02d}'
        view['lapsSub'] = f'LAP {g.completedLaps + 1}'
    else:
        view['lapsLabel'], view['lapsValue'], view['lapsSub'] = 'LAP', str(g.completedLaps + 1), ''

    gear = p.gear - 1
    view['gear'] = 'R' if gear < 0 else 'N' if gear == 0 else str(gear)
    view['gearbox'] = 'AUTO' if auto else 'MANUAL'
    view['rpm'] = p.rpms
    view['shift'] = shift

    best, fastest = timing.player_best(), timing.fastest
    view['deltaBest'] = timing.delta_to(best)
    view['deltaFastest'] = timing.delta_to(fastest)
    view['bestTime'] = fmt_lap(best.time) if best else None
    view['fastest'] = None
    if fastest:
        name = cars[fastest.car].name if fastest.car < len(cars) else ''
        view['fastest'] = {'time': fmt_lap(fastest.time), 'you': fastest.car == 0,
                           'driver': (name or f'Car {fastest.car}').split()[-1].upper()}
    view['invalid'] = timing.lap_invalid()
    view['sectors'] = timing.sectors_view(now)
    view['sectorsReady'] = timing.mini_bounds is not None

    charge = p.kersCharge
    view['ers'] = max(0.0, min(1.0, charge)) if math.isfinite(charge) and charge > 0 else None

    # The pit call during a planned race; otherwise a way into the strategy page.
    view['pit'] = None
    if engineer is not None:
        track_id = app.track + ('|' + app.layout if app.layout else '')
        view['pit'] = engineer.screen(g.session == AC_RACE, track_id, g.numberOfLaps,
                                      g.completedLaps, bool(g.isInPit))
    return view


# --- Serving the phone ------------------------------------------------------------------------

class State:
    STALE_AFTER = 3.0  # the reader updates at least every 0.5 s; older data means it's stuck

    def __init__(self):
        self._lock = threading.Lock()
        self._json = json.dumps({'live': False, 'message': 'Starting...'})
        self._time = time.monotonic()

    def set(self, view):
        data = json.dumps(view, allow_nan=False)
        with self._lock:
            self._json, self._time = data, time.monotonic()

    def get(self):
        with self._lock:
            data, age = self._json, time.monotonic() - self._time
        if age > self.STALE_AFTER:
            return json.dumps({'live': False, 'message': 'Dashboard stopped reading the game (see dashboard.log)'})
        return data


class Strategy:
    """The plan, shared between the reader loop and the settings page."""

    def __init__(self):
        self.lock = threading.Lock()
        self.engineer = strategy_module.Engineer(strategy_module.load())
        self.car = self.track = self.ac_root = ''
        self.race_laps = 0

    def seen(self, car, track, ac_root, race_laps):
        with self.lock:
            self.car, self.track, self.ac_root = car or '', track or '', ac_root or ''
            if race_laps:
                self.race_laps = race_laps

    def as_json(self):
        with self.lock:
            plan, car, track, root, laps = (self.engineer.plan, self.car, self.track,
                                            self.ac_root, self.race_laps)
        stints = [{'compound': x['compound'], 'boxOnLap': x['box']} for x in plan.stints] if plan else []
        return {
            'plan': {'name': plan.name if plan else '', 'track': plan.track if plan else '',
                     'laps': plan.laps if plan else (laps or 0),
                     'warnLapsBefore': plan.warn_before if plan else strategy_module.DEFAULT_WARN_BEFORE,
                     'stints': stints},
            'compounds': strategy_module.compounds_for(root, car),
            'car': car, 'track': track, 'raceLaps': laps,
        }

    def replace(self, data):
        plan, problems = strategy_module.save(data)
        with self.lock:
            self.engineer.set_plan(plan)
        log(f'Pit plan saved: {plan.laps} laps, stops on ' +
            (', '.join(str(x) for x in plan.stops) or 'no stops'))
        return problems


def make_handler(state, strategy_store):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, body, kind='text/html; charset=utf-8', code=200):
            self.send_response(code)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path.split('?')[0] != '/api/strategy':
                self.send_error(404)
                return
            try:
                length = min(int(self.headers.get('Content-Length') or 0), 64 * 1024)
                data = json.loads(self.rfile.read(length).decode('utf-8'))
                problems = strategy_store.replace(data)
                body = json.dumps({'ok': True, 'problems': problems,
                                   'saved': strategy_store.as_json()}).encode()
            except (ValueError, TypeError, OSError) as e:
                body = json.dumps({'ok': False, 'problems': [f'Could not save: {e}']}).encode()
            self._send(body, 'application/json; charset=utf-8')

        def do_GET(self):
            path = self.path.split('?')[0]
            if path in ('/', '/index.html'):
                with open(os.path.join(HERE, 'dash.html'), 'rb') as f:
                    body = f.read()
                self._send(body)
            elif path in ('/strategy', '/strategy.html'):
                with open(os.path.join(HERE, 'strategy.html'), 'rb') as f:
                    body = f.read()
                self._send(body)
            elif path == '/api/strategy':
                self._send(json.dumps(strategy_store.as_json()).encode(),
                           'application/json; charset=utf-8')
            elif path == '/events':
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                client = self.client_address[0]
                log(f'Phone connected ({client})')
                try:
                    self.wfile.write(b'retry: 1000\n\n')
                    while True:
                        self.wfile.write(f'data: {state.get()}\n\n'.encode())
                        self.wfile.flush()
                        time.sleep(1 / PUSH_HZ)
                except OSError:
                    log(f'Phone disconnected ({client}): screen off, left Wi-Fi or page closed')
            else:
                self.send_error(404)

        def log_message(self, fmt, *args):
            pass

    return Handler


def reader_loop(source, state, timing, debug, strategy_store):
    lights = ShiftLights()
    engineer = strategy_store.engineer
    plan = engineer.plan
    if plan:
        log(f'Pit plan: {plan.name or "unnamed"} - stops on laps ' +
            ', '.join(str(x) for x in plan.stops))
    else:
        log('No pit plan yet: open the dashboard and tap the pit strip to make one')
    status = session = gearbox = None
    last_print = last_error_time = 0.0
    last_error = None

    def situation(new, message):
        nonlocal status
        if new != status:
            status = new
            log(message)

    while True:
        try:
            now = time.monotonic()
            snap = source.read()
            if snap is None:
                state.set({'live': False, 'message': 'Waiting for Assetto Corsa'})
                situation('waiting', 'Waiting for Assetto Corsa...')
                time.sleep(0.5)
                continue
            p, g, s, app = snap
            if g.status in (AC_OFF, AC_REPLAY):
                message = 'Replay' if g.status == AC_REPLAY else 'Waiting for session'
                state.set({'live': False, 'message': message})
                situation(('status', g.status), f'Assetto Corsa: {message.lower()}')
                time.sleep(0.2)
                continue
            if app is None or not app.cars:
                state.set({'live': False,
                           'message': 'AC Dash app is not sending. In the game, click AC Dash in the app bar'})
                situation('no_app', 'Assetto Corsa is running but the AC Dash app is not sending data. '
                                    'In the game, open the app bar on the right and click "AC Dash".')
                time.sleep(0.2)
                continue
            situation('live', 'Receiving data from Assetto Corsa')

            n = s.sectorCount if 1 <= s.sectorCount <= 10 else 3
            track_id = app.track + ('|' + app.layout if app.layout else '')
            strategy_store.seen(s.carModel, track_id, app.root, g.numberOfLaps)
            key = (s.carModel, track_id, g.session)
            timing.update(now, key, track_id, n, app.cars, g.currentSectorIndex)
            if key != session:
                session = key
                log(f'Session: {s.carModel} at {track_id}, {SESSION_NAMES.get(g.session, g.session)} '
                    f'({n} sectors, {len(app.cars)} cars)'
                    + ('' if timing.mini_bounds else '; sector splits are learned during your first lap'))

            flag = p.autoShifterOn
            auto = flag == 1
            if (s.carModel, flag) != gearbox:
                gearbox = (s.carModel, flag)
                log('Gearbox: ' + ('automatic' if auto else 'manual')
                    + ('' if flag in (0, 1) else f' (unexpected gearbox value {flag}, treated as manual)'))
            profile = source.profile(app, s.carModel)
            lights.update(s.carModel, p, now, auto, profile)
            view = build_view(p, g, s, app, timing, lights.target(p.gear - 1, auto, profile), auto, now,
                              engineer)
            state.set(view)

            if now - last_print >= (1.0 if debug else 0.5):
                last_print = now
                if debug:
                    me = app.cars[0]
                    print(f'status={g.status} session={g.session} laps={g.completedLaps}/{g.numberOfLaps} '
                          f'sector={g.currentSectorIndex} racePos={me.race_pos} lapCount={me.lap_count} '
                          f'spline={me.spline:.4f} clock={me.lap_time} last={me.last_lap} '
                          f'invalid={me.invalid} line={timing.line} bounds={timing.bounds} '
                          f'rpm={p.rpms} gear={p.gear} auto={flag} shift={view["shift"]} learned='
                          f'{ {k: list(v) for k, v in lights.learned.items()} } ers={p.kersCharge:.3f}')
                else:
                    def d(ms):
                        return '--.---' if ms is None else f'{ms / 1000:+.3f}'
                    ers = '--' if view['ers'] is None else f"{view['ers'] * 100:.0f}%"
                    line = (f"P{view['position']}/{view['cars']}  {view['lapsLabel']} {view['lapsValue']}  "
                            f"best {d(view['deltaBest'])}  fastest {d(view['deltaFastest'])}  ERS {ers}  "
                            f"| gear {view['gear']} {p.rpms} rpm")
                    print('\r' + line.ljust(100), end='', flush=True)
            time.sleep(1 / POLL_HZ)
        except Exception:
            text = traceback.format_exc()
            now = time.monotonic()
            if text != last_error or now - last_error_time > 60:
                last_error, last_error_time = text, now
                log('Error (the dashboard keeps running):\n' + text)
            time.sleep(0.5)


def lan_addresses():
    """Best guess first: phone hotspots use 192.168.x.x or 10.x.x.x; 172.16-31.x.x is usually a
    virtual adapter (VPN, WSL, Hyper-V) that the phone can't reach, so those go last."""
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(('8.8.8.8', 80))  # picks a route; sends nothing
            found.append(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            if info[4][0] not in found:
                found.append(info[4][0])
    except OSError:
        pass

    def rank(ip):
        first, second = (int(x) for x in ip.split('.')[:2])
        return 1 if first == 172 and 16 <= second <= 31 else 0
    return sorted((ip for ip in found if not ip.startswith(('127.', '169.254.'))), key=rank)


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(errors='replace')
    parser = argparse.ArgumentParser(description='Assetto Corsa phone dashboard')
    parser.add_argument('--demo', action='store_true', help='fake race data (no AC needed)')
    parser.add_argument('--debug', action='store_true', help='print raw values')
    parser.add_argument('--port', type=int, default=PORT)
    args = parser.parse_args()

    state = State()
    strategy_store = Strategy()
    try:
        server = ThreadingHTTPServer(('0.0.0.0', args.port), make_handler(state, strategy_store))
    except OSError as e:
        print(f'Could not start on port {args.port}: {e}')
        print('Is the dashboard already open in another window?')
        return 1
    server.daemon_threads = True

    rotate_log()
    print('=' * 60)
    print(' AC Phone Dashboard' + ('  (DEMO DATA)' if args.demo else ''))
    print('=' * 60)
    addresses = lan_addresses()
    if addresses:
        print(' On the phone, open Chrome and go to:')
        print(f'   http://{addresses[0]}:{args.port}')
        for ip in addresses[1:]:
            print(f'   (if that does not load, try http://{ip}:{args.port})')
    else:
        print(' No network found. Is the laptop connected to the hotspot?')
    print(' Close this window (or press Ctrl+C) to stop.')
    print('=' * 60)
    log('Dashboard started' + (' (demo)' if args.demo else '') + ' on '
        + ', '.join(f'http://{ip}:{args.port}' for ip in addresses))
    source = DemoSource() if args.demo else LiveSource()
    timing = Timing(None if args.demo else os.path.join(CACHE_DIR, 'tracks.json'))
    threading.Thread(target=reader_loop, args=(source, state, timing, args.debug, strategy_store), daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
