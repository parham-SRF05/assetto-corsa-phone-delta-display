"""Shift points for the shift lights, worked out from each car's own engine and gearbox files.

For every gear it finds the rpm where changing up gives more pull at the wheels than
staying in the gear (torque x gear ratio), capped just below the rev limiter.
"""
import collections
import json
import os
import struct
import threading

PRINTABLE = bytes(sorted(set(range(32, 127)) | {9, 10, 13}))
RPM_STEP = 25
PROFILE_VERSION = 2  # bump when the profile contents change, so old cache files are rebuilt


# --- Reading data.acd -----------------------------------------------------------------------

def _acd_entries(raw):
    off = 8 if struct.unpack_from('<i', raw, 0)[0] == -1111 else 0
    entries = {}
    while off + 4 <= len(raw):
        (name_len,) = struct.unpack_from('<i', raw, off)
        off += 4
        if name_len <= 0 or off + name_len + 4 > len(raw):
            raise ValueError('data.acd looks damaged')
        name = raw[off:off + name_len].decode('utf-8', 'replace')
        off += name_len
        (size,) = struct.unpack_from('<i', raw, off)
        off += 4
        entries[name.lower()] = raw[off:off + size * 4:4]  # each byte is stored as an int32
        off += size * 4
    return entries


def _acd_key(blobs):
    """The key depends on the car; recover it from the fact that every packed file is text."""
    total = sum(len(b) for b in blobs)
    if not total:
        raise ValueError('data.acd is empty')

    def best_byte(length, pos):
        counts = collections.Counter()
        for blob in blobs:
            counts.update(blob[pos::length])
        best_k, best_score = 0, -1
        for k in range(256):
            score = sum(counts[(c + k) & 255] for c in PRINTABLE)
            if score > best_score:
                best_k, best_score = k, score
        return best_k, best_score, sum(counts.values())

    for length in range(1, 65):
        k0, score, seen = best_byte(length, 0)
        if not seen or score < 0.995 * seen:
            continue  # wrong key length: the first key byte already fails to decode to text
        key, good = [k0], score
        for pos in range(1, length):
            k, score, _ = best_byte(length, pos)
            key.append(k)
            good += score
        if good >= 0.9999 * total:
            return key
    raise ValueError('could not decode data.acd')


def _decrypt(blob, key):
    n = len(key)
    return bytes((c - key[i % n]) & 255 for i, c in enumerate(blob)).decode('latin-1')


def read_car_files(car_dir):
    """All data files of a car as {lowercase name: text}, and the path they came from."""
    acd = os.path.join(car_dir, 'data.acd')
    if os.path.isfile(acd):
        with open(acd, 'rb') as f:
            entries = _acd_entries(f.read())
        key = _acd_key(list(entries.values()))
        return {name: _decrypt(blob, key) for name, blob in entries.items()}, acd
    folder = os.path.join(car_dir, 'data')
    if os.path.isdir(folder):
        files = {}
        for name in os.listdir(folder):
            path = os.path.join(folder, name)
            if os.path.isfile(path) and name.lower().endswith(('.ini', '.lut')):
                with open(path, 'rb') as f:
                    files[name.lower()] = f.read().decode('latin-1')
        return files, folder
    raise FileNotFoundError('no data.acd or data folder in ' + car_dir)


# --- Parsing ------------------------------------------------------------------------------

def _strip_comment(line):
    return line.split(';', 1)[0].split('//', 1)[0].strip()


def parse_ini(text):
    sections, current = {}, None
    for raw in text.splitlines():
        line = _strip_comment(raw)
        if line.startswith('[') and ']' in line:
            current = sections.setdefault(line[1:line.index(']')].strip().upper(), {})
        elif current is not None and '=' in line:
            key, value = line.split('=', 1)
            current[key.strip().upper()] = value.strip()
    return sections


def parse_lut(text):
    points = []
    if text.strip().startswith('('):  # inline form: (0=100|1000=120|...)
        rows = [pair.replace('=', '|', 1) for pair in text.strip().strip('()').split('|')]
    else:
        rows = [_strip_comment(line) for line in text.splitlines()]
    for row in rows:
        if '|' not in row:
            continue
        x, y = row.split('|', 1)
        try:
            points.append((float(x), float(y)))
        except ValueError:
            continue
    return sorted(points)


def _interp(points, x):
    if x <= points[0][0]:
        return points[0][1]
    for (x0, y0), (x1, y1) in zip(points, points[1:]):
        if x <= x1:
            return y0 if x1 == x0 else y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return points[-1][1]


# --- Shift points -------------------------------------------------------------------------

def shift_points(torque_curve, ratios, limiter):
    """Best upshift rpm for each gear (the last entry is for top gear: just under the limiter)."""
    top = limiter if limiter > 0 else torque_curve[-1][0]
    cap = top - min(250.0, 0.015 * top)  # stay clear of the limiter itself
    low = int(top * 0.3)
    # Changing up can only pay off above peak power, so start looking there.
    start = max(range(low, int(top) + 1, RPM_STEP), key=lambda r: _interp(torque_curve, r) * r)
    shifts = []
    for this_gear, next_gear in zip(ratios, ratios[1:]):
        shift = cap
        if this_gear > next_gear > 0:
            for rpm in range(start, int(top) + 1, RPM_STEP):
                after = rpm * next_gear / this_gear
                if _interp(torque_curve, after) * next_gear >= _interp(torque_curve, rpm) * this_gear:
                    shift = min(rpm, cap)
                    break
        shifts.append(int(round(shift)))
    shifts.append(int(round(cap)))
    return shifts


def build_profile(files):
    engine = parse_ini(files['engine.ini'])
    drivetrain = parse_ini(files['drivetrain.ini'])
    curve_name = engine.get('HEADER', {}).get('POWER_CURVE', 'power.lut')
    curve_text = curve_name if curve_name.startswith('(') else files[curve_name.lower()]
    curve = parse_lut(curve_text)
    if len(curve) < 2:
        raise ValueError('torque curve not found')
    limiter = float(engine.get('ENGINE_DATA', {}).get('LIMITER', 0) or 0)
    gears = drivetrain.get('GEARS', {})
    count = int(float(gears['COUNT']))
    ratios = [float(gears['GEAR_%d' % g]) for g in range(1, count + 1)]
    shifts = shift_points(curve, ratios, limiter)
    auto_up = float(drivetrain.get('AUTO_SHIFTER', {}).get('UP', 0) or 0)  # where AC's automatic gearbox changes up
    return {'limiter': int(limiter or curve[-1][0]), 'gears': count, 'shift': shifts,
            'auto_up': int(auto_up) if auto_up > 0 else None}


def load_profile(ac_root, car, cache_dir=None):
    car_dir = os.path.join(ac_root, 'content', 'cars', car)
    source = os.path.join(car_dir, 'data.acd')
    if not os.path.isfile(source):
        source = os.path.join(car_dir, 'data')
    stamp = [PROFILE_VERSION, source, os.path.getmtime(source)] if os.path.exists(source) else None
    cache_file = os.path.join(cache_dir, 'car_' + car + '.json') if cache_dir else None
    if cache_file and stamp and os.path.isfile(cache_file):
        try:
            with open(cache_file, encoding='utf-8') as f:
                cached = json.load(f)
            if cached.get('stamp') == stamp:
                return cached['profile']
        except (OSError, ValueError, KeyError):
            pass
    files, _ = read_car_files(car_dir)
    profile = build_profile(files)
    if cache_file and stamp:
        os.makedirs(cache_dir, exist_ok=True)
        with open(cache_file, 'w', encoding='utf-8') as f:
            json.dump({'stamp': stamp, 'profile': profile}, f)
    return profile


class CarProfiles:
    """Loads shift points in the background (unpacking a car takes about a second)."""

    def __init__(self, cache_dir, log=print):
        self.cache_dir = cache_dir
        self.log = log
        self._lock = threading.Lock()
        self._done = {}
        self._loading = set()

    def get(self, ac_root, car):
        if not ac_root or not car:
            return None
        with self._lock:
            if car in self._done:
                return self._done[car]
            if car in self._loading:
                return None
            self._loading.add(car)
        threading.Thread(target=self._load, args=(ac_root, car), daemon=True).start()
        return None

    def _load(self, ac_root, car):
        try:
            profile = load_profile(ac_root, car, self.cache_dir)
            self.log('Shift lights for %s: manual change-up at %s rpm, automatic gearbox at %s rpm, limiter %d'
                     % (car, ', '.join(str(r) for r in profile['shift'][:-1]), profile['auto_up'],
                        profile['limiter']))
        except Exception as e:  # a car with unusual files still gets lights learned from the gearbox
            profile = None
            self.log('No car data for %s (%s): shift lights will learn from the automatic gearbox only'
                     % (car, e))
        with self._lock:
            self._done[car] = profile
            self._loading.discard(car)
