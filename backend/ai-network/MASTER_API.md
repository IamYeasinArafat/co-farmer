# Master node ↔ farm computer: the HTTP API

This is the contract between the ESP32 master node and the farm computer (the laptop running `server.py`), as in the Hydro Monitor plan (sections 1.3, 2.3–2.5). Two small formats connect the two parts, so the firmware and the AI can be built in parallel:
- **Log rows:** the master sends these to the laptop.
- **Ranges:** the master downloads these from the laptop.

```
Sensor set (Nano) --radio--> MASTER (ESP32 + SD) --POST /log--> farm computer: SQLite, dashboard, agent network
                                   ^                                      |
                                   +-------------- GET /ranges -----------+   (every 5 min in the demo)
```

Start the laptop side with `python server.py`. It listens on port 8000 on every interface, so the ESP32 reaches it at `http://<laptop IP>:8000`. At the venue, run a hotspot from the laptop or a phone (plan 2.7).

## 1. `POST /log`: every reading and every actuator switch

Send the rows in **the plan's CSV log format**, one line per reading or switch. Several lines per request are fine; a header line is optional.

```
field_id,module_id,device_id,type,kind,value,unit,timestamp
F1,S1,dht11,reading,temp_air,30.4,C,2026-09-25T14:00:10+03:00
F1,S1,soil,reading,soil_moisture,29.5,%,2026-09-25T14:00:10+03:00
F1,A1,relay1,action,pump,activated,,2026-09-25T14:00:11+03:00
F1,A1,relay1,action,pump,deactivated,,2026-09-25T14:02:11+03:00
```

- **Request:** `Content-Type: text/csv`, with the lines as the body.
  - The answer is `{"stored": 4}`.
  - A bad row gets HTTP 400, and the whole request is rejected.
  - JSON works too: one object or a list with the same 8 keys (`Content-Type: application/json`).
- **`timestamp`** is when the master received the reading, in ISO 8601 with the offset (`+03:00`). The master gets the time over NTP at startup.
- **`type`** is `reading` or `action`.
  - For actions, `kind` is `pump` (or `fan`), and `value` is `activated` or `deactivated`.
- **The IDs are the ones in `farms/alkhor/hardware.json`:**
  - Fields `F1`, `F2`.
  - Sensor sets `S1`, `S2`: devices `dht11` (`temp_air`, `humidity`), `soil` (`soil_moisture`), `light` (`light`), and `level` (`level`, S1 only).
  - Actuator sets `A1`, `A2`: `relay1` is the drip-line pump.
- **Units:**
  - `temp_air` in C;
  - `humidity`, `soil_moisture` and `level` in %;
  - `light` in lux.
- **If the laptop can't be reached,** keep the rows on the SD card and send them when it's back. The server accepts old timestamps.

## 2. `GET /ranges/version` and `GET /ranges`: what the master enforces

Poll `GET /ranges/version` every 5 minutes in the demo (daily is enough for the product). It's tiny:

```json
{"version": 13}
```

Only when the number differs from the one on your SD card, download the full file. Use `GET /ranges?have=12`, which answers **304 Not Modified** if 12 is still the latest:

```json
{"version": 13, "valid_from": "2026-09-26T06:05:00+03:00",
 "F1": {"soil_moisture": [35, 50], "temp_air": [24, 32], "humidity": [40, 80], "level": [20, 100], "pump_seconds": 300},
 "F2": {"soil_moisture": [33, 48], "temp_air": [24, 32], "humidity": [40, 80], "pump_seconds": 120}}
```

- **Save it on the SD card,** so the ranges survive a restart. If the laptop is down, keep enforcing the last saved version.
- **Every value is already clamped inside the farm's hard limits by code** (`checker.py`), so the master never gets an unsafe range from the AI.
- **Pairs are `[min, max]`.** `level` exists only for the field with the tank sensor.

### What the master does with them (plan 2.5)

| Reading | Below the minimum | Above the maximum |
|---|---|---|
| `soil_moisture` | Pump on for `pump_seconds`, then off (log `activated` / `deactivated`) | Alert |
| `temp_air` | Alert | Fan on until back under the maximum (Al Khor has no fan: alert only) |
| `humidity` | Alert | Fan on (no fan: alert only) |
| `level` (tank) | Alert, and block the pump | |

### Safety in the hardware (plan 2.6)
- Every "on" command has a maximum run time, after which the relay switches itself off.
- If the actuator set hears nothing from the master for a set time, it switches everything off.

## 3. Other endpoints (for the laptop, not the ESP32)

| Endpoint | What |
|---|---|
| `POST /run` | Starts an agent run now; the dashboard's "Run now" button does the same. HTTP 409 if a run is already going. |
| `GET /status` | Latest version, rows received, last runs |

The agents also run on their own every 6 hours: 00:00, 06:00, 12:00 and 18:00 farm time (`python server.py --every 6`).

## Arduino sketch outline (ESP32, `HTTPClient`)

```cpp
#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
const char *SERVER = "http://192.168.137.1:8000";   // the laptop's IP on the hotspot
int rangesVersion = 0;                                // loaded from the SD card at boot

bool postRows(const String &csvLines) {               // one or more CSV lines, see section 1
  HTTPClient http;
  http.begin(String(SERVER) + "/log");
  http.addHeader("Content-Type", "text/csv");
  int code = http.POST(csvLines);
  http.end();
  return code == 200;                                 // false: keep the rows on the SD card and retry later
}

void checkRanges() {                                  // every 5 minutes in the demo
  HTTPClient http;
  http.begin(String(SERVER) + "/ranges/version");
  if (http.GET() == 200) {
    StaticJsonDocument<64> v;
    deserializeJson(v, http.getString());
    if (v["version"].as<int>() != rangesVersion) {
      http.end();
      http.begin(String(SERVER) + "/ranges?have=" + String(rangesVersion));
      if (http.GET() == 200) {
        String body = http.getString();
        // save body to the SD card (e.g. /ranges.json), then parse and apply it
        DynamicJsonDocument r(1024);
        deserializeJson(r, body);
        rangesVersion = r["version"];
        float soilMin = r["F1"]["soil_moisture"][0];
        int pumpSeconds = r["F1"]["pump_seconds"];
        // ... keep per field, use in the range checks of section 2
      }
    }
  }
  http.end();
}
```

## Testing without hardware

```powershell
python seed_demo.py --reset          # a seeded heatwave day in the database (optional)
python server.py                     # the farm computer
python fake_master.py --check 20     # a fake master: posts rows every 10 s, checks ranges every 20 s
```

Type `dry F1` in the fake master's window to "pull the soil probe": the pump switches on, and the activated and deactivated rows appear on the dashboard.
