"""
The pit plan, and the call that comes off it.

The plan lives in strategy.json so it can be changed between races without touching code.
If that file is missing or broken the dashboard simply shows no pit call: the rest of the
screen keeps working exactly as before.
"""
import json
import os
import re
import threading

PLAN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'strategy.json')

# how many laps before the stop the "get ready" call appears
DEFAULT_WARN_BEFORE = 1


class Plan:
    """A list of stints. Each one names the tyre and the lap you come in at the end of."""

    def __init__(self, data):
        self.name = str(data.get('name') or '')
        self.track = str(data.get('track') or '')
        self.laps = int(data.get('laps') or 0)
        self.warn_before = max(0, int(data.get('warnLapsBefore', DEFAULT_WARN_BEFORE)))
        self.stints = []
        for raw in data.get('stints') or []:
            box = raw.get('boxOnLap')
            self.stints.append({
                'compound': str(raw.get('compound') or '').upper(),
                'box': int(box) if box else None,
            })

    @property
    def stops(self):
        """The laps you plan to come in on, in order."""
        return [s['box'] for s in self.stints if s['box']]

    def fits(self, track, race_laps):
        """A plan for another track or a different distance should not fire."""
        if self.track and track and self.track.lower() not in track.lower():
            return False
        if self.laps and race_laps and abs(self.laps - race_laps) > 0:
            return False
        return bool(self.stints)


MAX_STINTS = 8
MAX_LAPS = 200

# Shown in the tyre picker when we cannot read the car's own list.
FALLBACK_COMPOUNDS = [{'short': 'S', 'name': 'Soft'}, {'short': 'M', 'name': 'Medium'},
                      {'short': 'H', 'name': 'Hard'}, {'short': 'I', 'name': 'Intermediate'},
                      {'short': 'W', 'name': 'Wet'}]

_compound_cache = {}
_compound_lock = threading.Lock()


def compounds_for(ac_root, car):
    """
    The tyres this car actually has, straight out of its tyres.ini.

    Falls back to a generic soft/medium/hard list for cars we cannot read, so the picker
    is never empty.
    """
    if not ac_root or not car:
        return list(FALLBACK_COMPOUNDS)
    with _compound_lock:
        if car in _compound_cache:
            return _compound_cache[car]
    found = []
    try:
        import carinfo
        files, _ = carinfo.read_car_files(os.path.join(ac_root, 'content', 'cars', car))
        text = files.get('tyres.ini', '')
        if isinstance(text, bytes):
            text = text.decode('utf-8', 'ignore')
        seen = set()
        for block in re.split(r'\n(?=\[)', text):
            head = block.split(']')[0].lstrip('[').strip().upper()
            if not re.match(r'^FRONT(_\d+)?$', head):      # one entry per compound, front axle
                continue
            name = re.search(r'^NAME\s*=\s*([^;\r\n]+)', block, re.M)
            short = re.search(r'^SHORT_NAME\s*=\s*([^;\r\n]+)', block, re.M)
            label = (name.group(1).strip() if name else '') or (short.group(1).strip() if short else '')
            tag = (short.group(1).strip() if short else label[:2]).upper()
            if label and tag not in seen:
                seen.add(tag)
                found.append({'short': tag, 'name': label})
    except Exception:
        found = []
    result = found or list(FALLBACK_COMPOUNDS)
    with _compound_lock:
        _compound_cache[car] = result
    return result


def validate(data):
    """Cleans a plan coming from the settings page. Returns (clean plan dict, problems)."""
    problems = []
    laps = data.get('laps')
    try:
        laps = int(laps)
    except (TypeError, ValueError):
        laps = 0
    if not 1 <= laps <= MAX_LAPS:
        problems.append(f'Race distance must be between 1 and {MAX_LAPS} laps.')
        laps = max(1, min(laps, MAX_LAPS))

    raw = data.get('stints') or []
    if not raw:
        problems.append('A plan needs at least one stint.')
    if len(raw) > MAX_STINTS:
        problems.append(f'That is more than {MAX_STINTS} stints.')
        raw = raw[:MAX_STINTS]

    stints, last_lap = [], 0
    for i, item in enumerate(raw):
        compound = re.sub(r'[^A-Za-z0-9 +-]', '', str(item.get('compound') or ''))[:12].strip().upper()
        if not compound:
            problems.append(f'Stint {i + 1} has no tyre.')
            compound = 'HARD'
        final = i == len(raw) - 1
        box = None
        if not final:
            try:
                box = int(item.get('boxOnLap'))
            except (TypeError, ValueError):
                box = 0
            if not 1 <= box < laps:
                problems.append(f'Stop {i + 1} must be on a lap between 1 and {laps - 1}.')
                box = max(1, min(box, max(1, laps - 1)))
            if box <= last_lap:
                problems.append(f'Stop {i + 1} has to come after stop {i}.')
                box = min(last_lap + 1, max(1, laps - 1))
            last_lap = box
        stints.append({'compound': compound, 'boxOnLap': box})

    warn = data.get('warnLapsBefore', DEFAULT_WARN_BEFORE)
    try:
        warn = int(warn)
    except (TypeError, ValueError):
        warn = DEFAULT_WARN_BEFORE
    warn = max(0, min(warn, 5))

    clean = {
        'name': str(data.get('name') or '')[:80],
        'track': re.sub(r'[^A-Za-z0-9_\- |]', '', str(data.get('track') or ''))[:60],
        'laps': laps,
        'warnLapsBefore': warn,
        'stints': stints,
    }
    return clean, problems


def save(data, path=PLAN_FILE):
    """Validates and writes the plan. Returns (plan, problems)."""
    clean, problems = validate(data)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(clean, f, indent=2)
        f.write(os.linesep)
    os.replace(tmp, path)
    return Plan(clean), problems


def load(path=PLAN_FILE):
    try:
        with open(path, encoding='utf-8') as f:
            return Plan(json.load(f))
    except (OSError, ValueError, TypeError, KeyError):
        return None


class Engineer:
    """
    Follows the plan against what you actually do.

    Stops are counted from real pit entries, so if you box early, box late, or take an
    extra stop, the next call still points at the next tyre in the plan.
    """

    def __init__(self, plan):
        self.plan = plan
        self.stops_done = 0
        self._in_pit = False
        self._last_lap = 0

    def set_plan(self, plan):
        """Swap in a plan edited on the phone, without restarting the dashboard."""
        self.plan = plan
        self.reset()

    def reset(self):
        self.stops_done = 0
        self._in_pit = False
        self._last_lap = 0

    def update(self, completed_laps, in_pit):
        """Call every frame. Counts a stop on each pit entry, and a restart on lap 0."""
        if completed_laps < self._last_lap:      # new session
            self.reset()
        self._last_lap = completed_laps
        if in_pit and not self._in_pit and completed_laps > 0:
            self.stops_done += 1
        self._in_pit = bool(in_pit)

    def screen(self, in_race, track, race_laps, completed_laps, in_pit):
        """
        What the dashboard shows, in every session.

        When the plan fits the race you are in, that is the live pit call. Otherwise it is a
        way into the settings page - the strip is the only door to it, so it must never
        disappear just because there is no plan yet.
        """
        plan = self.plan
        if plan is None or not plan.stints:
            return {'state': 'setup', 'text': 'PLAN THIS RACE', 'sub': 'TAP TO SET UP'}

        if not in_race or not plan.fits(track, race_laps):
            stops = len(plan.stops)
            summary = ' · '.join(x for x in (f'{plan.laps} LAPS' if plan.laps else '',
                                             (plan.track or '').upper()) if x)
            reason = 'TAP TO EDIT'
            if in_race and race_laps and plan.laps != race_laps:
                reason = f'THIS RACE IS {race_laps} · TAP TO EDIT'
            elif in_race and plan.track and track and plan.track.lower() not in track.lower():
                reason = 'ANOTHER TRACK · TAP TO EDIT'
            return {'state': 'setup', 'text': summary or 'PLAN READY',
                    'sub': f'{stops} STOP{"S" if stops != 1 else ""} · {reason}' if stops else reason}

        self.update(completed_laps, in_pit)
        return self.call(completed_laps, in_pit, race_laps)

    def call(self, completed_laps, in_pit, race_laps=0):
        """
        What the dashboard should show right now, or None.

        state is one of:
          box   - come in at the end of this lap
          warn  - one more lap, then box
          pit   - you are in the pit lane; here is the tyre to fit
          info  - nothing to do yet, just the next stop
        """
        if not self.plan or not self.plan.stints:
            return None

        current_lap = completed_laps + 1
        index = min(self.stops_done, len(self.plan.stints) - 1)
        stint = self.plan.stints[index]
        next_tyre = self.plan.stints[index + 1]['compound'] if index + 1 < len(self.plan.stints) else None

        # In the pit box the tyre you want is the one starting this stint, which the pit
        # entry already advanced us onto - not the one after it.
        if in_pit and self.stops_done and stint['compound']:
            return {'state': 'pit', 'text': 'FIT ' + stint['compound'],
                    'sub': f'STOP {self.stops_done} OF {len(self.plan.stops)}'}

        box_lap = stint['box']
        if not box_lap:
            left = (race_laps - completed_laps) if race_laps else 0
            sub = f'{left} LAPS TO THE FLAG' if left > 0 else 'RUN TO THE FLAG'
            return {'state': 'info', 'text': 'NO MORE STOPS', 'sub': sub, 'tyre': stint['compound']}

        if current_lap >= box_lap:
            overdue = current_lap - box_lap
            sub = 'THIS LAP' if not overdue else f'{overdue} LAP{"S" if overdue > 1 else ""} LATE'
            return {'state': 'box', 'text': 'BOX BOX BOX',
                    'sub': sub + (' · ' + next_tyre if next_tyre else ''), 'tyre': stint['compound']}

        if current_lap >= box_lap - self.plan.warn_before:
            return {'state': 'warn', 'text': 'BOX NEXT LAP',
                    'sub': (next_tyre or '') + f' · END OF L{box_lap}', 'tyre': stint['compound']}

        return {'state': 'info', 'text': f'NEXT STOP L{box_lap}',
                'sub': f'{box_lap - current_lap} LAPS' + (' · ' + next_tyre if next_tyre else ''),
                'tyre': stint['compound']}
