##############################################################
# AC Dash - in-game helper for the phone dashboard (ac_dash.py).
#
# Every frame it copies each car's live timing (track position, lap clock, lap count,
# race position) into shared memory named "Local\acdash_live", which ac_dash.py reads.
# Runs inside Assetto Corsa's built-in Python 3.3, so the syntax is kept old-fashioned.
##############################################################
import mmap
import os
import struct

import ac
import acsys

MAP_NAME = 'Local\\acdash_live'
VERSION = 1
MAX_CARS = 64
# magic, version, write counter (odd while writing), car count, AC folder, track, layout
HEADER = struct.Struct('<4sIII260s64s64s')
# spline position, lap clock, last lap, best lap, laps done, race position,
# in pit lane, connected, lap invalidated, (padding), driver name
CAR = struct.Struct('<fiiiiiBBBx32s')
SIZE = HEADER.size + MAX_CARS * CAR.size
WRITE_EVERY = 1.0 / 60
NAMES_EVERY = 2.0

app_window = None
status_label = None
memory = None
counter = 0
write_timer = 0.0
names_timer = 0.0
names = {}
errors = 0


def encode(value, size):
    if value is None:
        value = ''
    return str(value).encode('utf-8')[:size - 1]


def set_status(text):
    if status_label is not None:
        ac.setText(status_label, text)


def log_error(where, error):
    global errors
    errors += 1
    if errors <= 5:
        ac.log('AC Dash ' + where + ': ' + repr(error))
    set_status('Phone dashboard: error (see log)')


def acMain(ac_version):
    global app_window, status_label, memory
    app_window = ac.newApp('AC Dash')
    ac.setSize(app_window, 230, 60)
    status_label = ac.addLabel(app_window, 'Phone dashboard: starting')
    ac.setPosition(status_label, 10, 30)
    try:
        memory = mmap.mmap(-1, SIZE, MAP_NAME)
        HEADER.pack_into(memory, 0, b'ACDH', VERSION, 0, 0,
                         encode(os.getcwd(), 260),
                         encode(ac.getTrackName(0), 64),
                         encode(ac.getTrackConfiguration(0), 64))
        set_status('Phone dashboard: sending')
    except Exception as e:
        memory = None
        log_error('start', e)
    return 'AC Dash'


def acUpdate(deltaT):
    global counter, write_timer, names_timer
    if memory is None:
        return
    write_timer += deltaT
    names_timer -= deltaT
    if write_timer < WRITE_EVERY:
        return
    write_timer = 0.0
    try:
        count = min(ac.getCarsCount(), MAX_CARS)
        if names_timer <= 0.0:
            names_timer = NAMES_EVERY
            for i in range(count):
                names[i] = encode(ac.getDriverName(i), 32)
        counter = (counter + 1) & 0xFFFFFFFF
        struct.pack_into('<I', memory, 8, counter)  # odd: a write is in progress
        struct.pack_into('<I', memory, 12, count)
        state = ac.getCarState
        for i in range(count):
            CAR.pack_into(memory, HEADER.size + i * CAR.size,
                          float(state(i, acsys.CS.NormalizedSplinePosition)),
                          int(state(i, acsys.CS.LapTime)),
                          int(state(i, acsys.CS.LastLap)),
                          int(state(i, acsys.CS.BestLap)),
                          int(state(i, acsys.CS.LapCount)),
                          int(ac.getCarRealTimeLeaderboardPosition(i)),
                          1 if ac.isCarInPitline(i) else 0,
                          1 if ac.isConnected(i) else 0,
                          1 if state(i, acsys.CS.LapInvalidated) else 0,
                          names.get(i, b''))
    except Exception as e:
        log_error('update', e)
    finally:
        if counter & 1:
            counter = (counter + 1) & 0xFFFFFFFF
            struct.pack_into('<I', memory, 8, counter)  # even: write finished


def acShutdown():
    global memory
    if memory is not None:
        memory.close()
        memory = None
