# Assetto Corsa Phone Delta Display

Turn an old Android phone into a live F1-style timing screen for **Assetto Corsa**, over Wi-Fi.
No app to install on the phone: it's a web page you open in Chrome.

It shows:

- **Live race position**, updated the moment you pass or get passed (AC's own position only updates once per lap)
- **Delta to your best lap** and **delta to the fastest lap of the session**, with that lap's time and driver
- **3 sectors with 8 mini sectors each**, in F1 colours:
  - **purple**: fastest of anyone in the session (AI included)
  - **green**: your personal best
  - **yellow**: slower than both
- **F1 shift lights** (5 green, 5 red, 5 blue, flashing blue = change up), for both manual and automatic gearboxes
- **ERS battery** level
- **Laps left** in a race, or time left in practice and qualifying
- **LAP INVALID** when you cut the track

---

## How it works

```
Assetto Corsa ──► AC Dash in-game app ──┐
(every car's live timing)               ├──► ac_dash.py on the PC ──Wi-Fi──► Chrome on the phone
Assetto Corsa shared memory ────────────┘    (works out sectors, deltas,
(your car: gear, rpm, ERS, laps)             colours, shift points)
```

- **The in-game app** (`acdash_app/acdash.py`) runs inside Assetto Corsa. It copies every car's track position, lap clock and race position into shared memory, 60 times a second.
- **`ac_dash.py`** reads that, plus Assetto Corsa's own shared memory for your car. It records a time-vs-distance trace of every lap of every car, which gives sector and mini-sector times for everyone.
- **The phone** gets an update 30 times a second.
- **Shift points** come from each car's own engine torque curve and gear ratios. With the automatic gearbox, the lights follow where the gearbox actually changes up.

---

## What you need

- A **Windows PC** with **Assetto Corsa**. Tested with AC 1.16, Content Manager and Custom Shaders Patch.
- **Python 3.8 or newer** ([python.org](https://www.python.org/downloads/)). No extra packages needed.
- An **Android phone** with Chrome (tested on a Galaxy S21 FE in landscape).
- The PC and the phone on the **same Wi-Fi network**. A phone hotspot works fine.

---

## Step-by-step setup

### 1. Download

Click **Code → Download ZIP** on this page and unzip it anywhere, for example `C:\ac-dash`.
Or with git:

```bash
git clone https://github.com/parham-SRF05/assetto-corsa-phone-delta-display.git
```

### 2. Install Python

1. Download Python from [python.org](https://www.python.org/downloads/).
2. In the installer, **tick "Add python.exe to PATH"** before clicking Install.
3. Check it worked: open Command Prompt and run `python --version`.

### 3. Install the in-game app

1. Open your Assetto Corsa folder. On Steam: right-click Assetto Corsa → **Manage → Browse local files**.
2. Go to `apps\python\` and create a folder named exactly **`acdash`**.
3. Copy **`acdash_app\acdash.py`** from this project into it, so you end up with:

   ```
   assettocorsa\apps\python\acdash\acdash.py
   ```

4. Turn the app on. Either:
   - **Content Manager:** Settings → Assetto Corsa → Apps, and tick **AC Dash**, or
   - **By hand:** open `Documents\Assetto Corsa\cfg\python.ini` and add these two lines at the end:

     ```ini
     [ACDASH]
     ACTIVE=1
     ```

### 4. Get the phone ready

1. **Keep the screen on.** Browsers only let a page keep the screen awake over a secure connection, which a local Wi-Fi address isn't. So the phone has to do it:
   - Settings → About phone → Software information → tap **Build number** 7 times to unlock Developer options.
   - Settings → **Developer options** → turn on **Stay awake**.
   - The screen now stays on while charging, so keep the phone plugged in while you race.
2. Connect the phone to the **same Wi-Fi as the PC**.

### 5. Start the dashboard

1. Double-click **`Start AC Dashboard.bat`** in the project folder.
2. The first time, Windows Firewall may ask about Python: allow it on **private networks**.
3. The window shows an address like `http://192.168.1.23:8765`. **Leave this window open while you race.**
4. On the phone, open **Chrome** and type that address.
5. Turn the phone to landscape and **tap the screen once** for full screen.

### 6. Race

1. Start a session from Content Manager (or AC's own launcher). The phone connects on its own.
2. If the phone says **"AC Dash app is not sending"**: in the game, open the app bar on the right side of the screen and click **AC Dash** once.
3. **First time on a new track:** the dashboard learns where the sectors split while you drive your first lap, and saves them. On every later visit the sectors show from the start of lap 1.

To stop, close the black dashboard window.

---

## Reading the screen

| Area | Meaning |
|---|---|
| Top lights | Shift lights. They fill up as you approach the shift point; **all flashing blue = change up**. In top gear they only warn of the rev limiter. |
| POSITION | Live race position / cars in the session |
| LAPS LEFT | Laps to go (race), or time left (practice, qualifying) |
| Gear box | Current gear, with **AUTO** or **MANUAL** gearbox underneath |
| vs YOUR BEST | Gap to your best lap at this point of the track. **Green** = ahead, **red** = behind |
| vs FASTEST | Gap to the fastest lap of the session by anyone. **Purple** = ahead, **red** = behind |
| S1 / S2 / S3 | Gap to the fastest time of that sector, coloured purple / green / yellow. The small bars are the 8 mini sectors; the outlined one is where you are. A finished lap stays on screen for 4 seconds. |
| ERS | Battery charge |

---

## Troubleshooting

| What you see | What to do |
|---|---|
| The phone can't open the address | Check both devices are on the same Wi-Fi. Try the second address in the dashboard window if there is one. Turn off any VPN on the PC. Allow Python through Windows Firewall. |
| **Waiting for Assetto Corsa** | Normal until you're in a session. Replays don't show data. |
| **AC Dash app is not sending** | Click **AC Dash** in the in-game app bar. If it isn't listed, check step 3. |
| **No connection to PC** | The dashboard window was closed, or the phone left the Wi-Fi. The page reconnects by itself. |
| The phone screen turns off | Turn on **Stay awake** (step 4) and keep the phone charging. |
| **Could not start on port 8765** | The dashboard is already open in another window. Close it first. |
| No shift lights | The car's data files couldn't be read. With the automatic gearbox, the lights still appear after the gearbox has changed up a couple of times. |
| Anything else | Open **`dashboard.log`** in the project folder. It records connections, sessions, gearbox mode and any errors. |

**Try it without the game:** open Command Prompt in the project folder and run `python ac_dash.py --demo`. It plays a fake 8-car race so you can check the phone page.

---

## Files

| File | What it does |
|---|---|
| `ac_dash.py` | Main program: reads the game data, serves the phone page |
| `timing.py` | Lap, sector and mini-sector timing for every car; deltas and colours |
| `carinfo.py` | Reads each car's engine and gearbox files to work out shift points |
| `dash.html` | The phone page |
| `acdash_app/acdash.py` | The in-game app (runs inside Assetto Corsa's Python 3.3) |
| `Start AC Dashboard.bat` | Double-click to start |
| `tests/` | Tests for the timing engine and shift lights |

The dashboard creates a `cache` folder (learned sector splits and car shift points) and `dashboard.log`.

---

## Tests

No game needed:

```bash
python tests/test_timing_edges.py
```

```bash
python tests/test_race_simulation.py
```

```bash
python tests/test_shift_lights.py
```

- **`test_timing_edges.py`**: race starts from the grid, starting mid-lap, pit lane jumps, back to pits, laptop freezes, invalid laps, restarts, driver swaps.
- **`test_race_simulation.py`**: a full 8-car, 10-lap race, checked against the exact times the simulation drove.
- **`test_shift_lights.py`**: manual and automatic gearbox shift points. Set `AC_ROOT` to your Assetto Corsa folder to also check real car data.

---

## Notes

- **Windows only**, because Assetto Corsa's shared memory is a Windows feature.
- **Sector split positions** are learned from your car's sector changes. They're saved per track in `cache/tracks.json`; delete that file to make them learn again.
- **Automatic gearbox detection** uses Assetto Corsa's `autoShifterOn` flag. The dashboard window and `dashboard.log` show which mode it found.

## Pit calls

The dashboard can run your race strategy and call you into the pits.

### Planning the race on your phone

Tap the **pit strip** along the bottom of the dashboard, or open `http://<your-pc>:8765/strategy`
in any browser on the same Wi-Fi.

| On the page | What it does |
|---|---|
| **Laps** | The race distance, with a stepper you can hit with a thumb. It tells you what you have built: *"2 stops, 3 stints"* |
| **The plan** | A bar drawn in tyre colours, with `BOX 24` markers under it. It redraws as you edit, so you see the shape of the race before you drive it |
| **A card per stint** | Pick the tyre from the chips, set the lap you come in at the end of. The final card says *"Runs to the flag"* instead of asking for a lap, because it cannot have a stop |
| **+ Add a stop** | Splits the remaining distance in half and gives you a sensible lap number, rather than a blank to fill in |
| **Remove** | Drops a stint; the remaining stops keep their order |
| **Tell me early** | How many laps before the stop the warning appears (0–5) |
| **Save plan** | Applies immediately — no restart, even in the middle of a session |

**The tyre buttons are your car's own tyres.** They come from the car's `tyres.ini`, so an F1 mod
with five compounds offers all five — `S`, `S1`, `M`, `M1`, `H` — with their real names, not a
generic soft/medium/hard guess. Cars whose data cannot be read fall back to a generic list, so the
picker is never empty.

**Mistakes get corrected, not rejected.** Put stop 2 before stop 1 and it reorders them and tells
you why. Ask for a stop on lap 99 of a 10-lap race and it moves it to lap 9. You never hit a
dead end with a red error and no way forward.

### What you get on track

In order, as the stop approaches:

| | On the phone | Colour |
|---|---|---|
| Most of the race | `NEXT STOP L24 · 15 LAPS` | grey, easy to ignore |
| One lap before | `BOX NEXT LAP · HARD` | amber |
| The stop lap | **`BOX BOX BOX`** | flashing red |
| In the pit lane | `FIT HARD · STOP 1 OF 3` | green |
| After the last stop | `NO MORE STOPS · 12 LAPS TO THE FLAG` | grey |

It sits between the sectors and the ERS bar, so it never covers your delta.

**It follows the race, not a script.** Stops are counted from real pit entries, so boxing early,
boxing late or taking a surprise extra stop all keep the next call pointing at the right tyre. Stay
out past your window and it tells you how far: `3 LAPS LATE`.

### The file behind it

The page writes `strategy.json`, which you can also edit by hand:

```json
{
  "name": "Silverstone 53 - soft start, hard middle, medium home",
  "track": "silverstone",
  "laps": 53,
  "warnLapsBefore": 1,
  "stints": [
    { "compound": "SOFT",   "boxOnLap": 7  },
    { "compound": "HARD",   "boxOnLap": 24 },
    { "compound": "HARD",   "boxOnLap": 41 },
    { "compound": "MEDIUM", "boxOnLap": null }
  ]
}
```

`boxOnLap` is the lap you come in at the end of; the last stint has `null` because it runs to the
flag. A plan only fires when its `track` and `laps` match the race you are actually in, so an old
plan can never call you into the pits at the wrong circuit.

### Working out what the plan should be

```bash
python race_engineer.py --laps 53
```

Start it before you go out and drive. It prints one line per lap — lap time, litres burned, wear on
each corner, hot pressures — and when you stop it with Ctrl+C it tells you how much fuel the race
needs and how many laps each compound lasts. Those are the numbers a plan should be built on.
