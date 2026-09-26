
# Co-Farmer IoT Subsystem

The IoT subsystem is the field-level hardware layer of the Co-Farmer platform[cite: 1]. It operates autonomously to sample environmental variables, log telemetry locally and remotely, and enforce safe operating ranges for connected field hardware.

If the farm computer or internet connection drops, the Master Node continues enforcing saved range thresholds and writing all readings to its local SD card[cite: 1].



---

## 1. Directory Structure

The IoT files are organized under `backend/IOT/` as follows:

```text
co-farmer/
└── backend/
    └── IOT/
        ├── HardwareSrc/
        │   ├── ActuatorNode/
        │   │   ├── ActuatorNode.ino
        │   │   └── Payload.h
        │   ├── MasterNode/
        │   │   ├── MasterNode.ino
        │   │   └── Payload.h
        │   └── SensorNode/
        │       ├── SensorNode.ino
        │       └── Payload.h
        ├── CircuitSchematic.pdf
        └── README_IOT.md

```

> **Note for Arduino IDE:** Each `.ino` firmware sketch lives inside a dedicated folder with the exact same name inside `HardwareSrc/`. `Payload.h` is present in each node folder so that all nodes share identical packet structures upon compilation.
> 
> 

---

## 2. Hardware Architecture & Pinouts

### Hardware Specifications

| Node | Microcontroller | Key Peripherals | Interface | Role |
| --- | --- | --- | --- | --- |
| **Sensor Node** | Arduino Nano Every

 | DHT11, BH1750, Soil Moisture, Water Level, MQ-135

 | nRF24L01 Transceiver

 | Reads sensors every 10 seconds and radios binary data.

 |
| **Actuator Node** | Arduino Nano Every

 | 4-Relay Module (Water Pump, Fan)

 | nRF24L01 Transceiver

 | Controls high-power loads and returns activation logs.

 |
| **Master Node** | ESP32-WROOM-32

 | MicroSD Card Module

 | nRF24L01 + WiFi

 | Stamps time (NTP), logs CSV, posts to backend, enforces ranges.

 |

### Pin Mapping (per `CircuitSchematic.pdf` Rev 4)

* **Sensor Node (Arduino Nano Every)**:


* Air Temp & Humidity (DHT11): `D2`

* Air Quality (MQ-135): `A0`

* Water Level Sensor: `A1`

* Soil Moisture Sensor: `A2`

* Light Sensor (BH1750): `I2C` (`A4` / `A5`)


* Radio (nRF24L01): `CE = D9`, `CSN = D10`



* **Actuator Node (Arduino Nano Every)**:


* Relay 1 (Pump): `D3`

* Relay 2 (Fan): `D4`

* Relays 3 & 4: `D5`, `D6`

* Radio (nRF24L01): `CE = D9`, `CSN = D10`



* **Master Node (ESP32)**:


* Radio (nRF24L01): `CE = IO4`, `CSN = IO5`

* SD Card Reader: `CS = IO13`

* Shared SPI Bus: `SCK = IO18`, `MISO = IO19`, `MOSI = IO23`




---

## 3. Communication & Data Protocol

### Binary Payload (`Payload.h`)

All nodes transmit raw 31-byte packed C-structures to stay within the 32-byte maximum limit of the nRF24L01 module:

```cpp
struct __attribute__((packed)) RadioPacket {
  uint8_t msg_type;     // 1 = reading, 2 = action, 3 = command
  char field_id[3];     // e.g., "F1"
  char module_id[3];    // e.g., "S1" or "A1"
  char device_id[8];    // e.g., "dht11", "soil", "relay1"
  char kind[12];        // e.g., "temp_air", "soil_moisture", "pump"
  float value;          // Reading value or duration (seconds)
};

```

### Log Schema (CSV & Database API)

The Master Node formats received packets into standardized log rows for SD card storage and HTTP POST logging to `http://:8000/log`:

`field_id, module_id, device_id, type, kind, value, unit, timestamp`

// Configuration (To change, change in /co-farmer/backend/IOT/HardwareSrc/MasterNode/MasterNode.ino)
const char* WIFI_SSID     = "FarmHotspot";
const char* WIFI_PASS     = "farm123456";
const char* API_BASE_URL  = "http://192.168.1.100:8000"; // Farm Computer IP

**Example Rows:**

* `F1, S1, dht11, reading, temp_air, 30.4, C, 2026-09-25T14:00:10+03:00`

* `F1, S1, soil, reading, soil_moisture, 29.5, %, 2026-09-25T14:00:10+03:00`

* `F1, A1, relay1, action, pump, activated, , 2026-09-25T14:00:11+03:00`

* `F1, A1, relay1, action, pump, deactivated, , 2026-09-25T14:02:11+03:00`


---

## 4. Safety Controls

* **Hard Run-Time Limit:** Any relay activation command explicitly defines a duration in seconds, strictly capped at a 300-second maximum.


* **Signal Loss Watchdog:** The Actuator Node automatically turns off all active relays if no signal is received from the Master Node for longer than 10 minutes.


* **SD Card Backup:** Log data is always preserved on the Master Node's local SD card, ensuring zero data loss during server or network outages.



---

## 5. Software Dependencies & Upload Instructions

### 1. Arduino IDE Setup

* Install Board Package: **Arduino megaAVR Boards** (for Nano Every)


* Install Board Package: **esp32** by Espressif Systems



### 2. Required Libraries

Install the following via **Tools > Manage Libraries...**:

* `RF24` by TMRh20


* `DHT sensor library` by Adafruit


* `BH1750` by Christopher Laws


* `ArduinoJson` by Benoit Blanchon



### 3. Flashing Nodes

1. **Sensor Node:** Open `HardwareSrc/SensorNode/SensorNode.ino`, select **Arduino Nano Every**, select port, and upload.


2. **Actuator Node:** Open `HardwareSrc/ActuatorNode/ActuatorNode.ino`, select **Arduino Nano Every**, select port, and upload.


3. **Master Node:** Open `HardwareSrc/MasterNode/MasterNode.ino`, update `WIFI_SSID` and `API_BASE_URL`, select **ESP32 Dev Module**, select port, and upload.



```

```