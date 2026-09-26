#include "Payload.h"

#define FIELD_ID   "F1"
#define MODULE_ID  "A1"

#define RELAY1_PUMP_PIN 3
#define RELAY2_FAN_PIN  4
#define RF24_CE_PIN     9
#define RF24_CSN_PIN    10

#define HARD_MAX_RUN_TIME 300000UL // 300 seconds safety hard limit
#define WATCHDOG_TIMEOUT  600000UL // 10 minutes without signal shuts down hardware

RF24 radio(RF24_CE_PIN, RF24_CSN_PIN);
const uint64_t rxPipeAddress = 0xE8E8F0F0E2LL;
const uint64_t txPipeAddress = 0xE8E8F0F0E1LL;

struct RelayState {
  uint8_t pin;
  bool active;
  unsigned long timerStart;
  unsigned long durationMs;
  char deviceId[8];
  char kind[12];
};

RelayState relays[2] = {
  {RELAY1_PUMP_PIN, false, 0, 0, "relay1", "pump"},
  {RELAY2_FAN_PIN,  false, 0, 0, "relay2", "fan"}
};

unsigned long lastMasterSignalTime = 0;

void sendActionLog(const char* device, const char* kind, bool active) {
  radio.stopListening();
  radio.openWritingPipe(txPipeAddress);

  RadioPacket pkt;
  pkt.msg_type = TYPE_ACTION;
  strncpy(pkt.field_id, FIELD_ID, sizeof(pkt.field_id));
  strncpy(pkt.module_id, MODULE_ID, sizeof(pkt.module_id));
  strncpy(pkt.device_id, device, sizeof(pkt.device_id));
  strncpy(pkt.kind, kind, sizeof(pkt.kind));
  pkt.value = active ? 1.0f : 0.0f; // 1 = activated, 0 = deactivated

  radio.write(&pkt, sizeof(RadioPacket));
  radio.openReadingPipe(1, rxPipeAddress);
  radio.startListening();
}

void setRelay(int index, bool turnOn, unsigned long durationSec = 0) {
  RelayState &r = relays[index];
  if (turnOn) {
    unsigned long reqMs = durationSec * 1000UL;
    r.durationMs = (reqMs > 0 && reqMs <= HARD_MAX_RUN_TIME) ? reqMs : HARD_MAX_RUN_TIME;
    r.timerStart = millis();
    r.active = true;
    digitalWrite(r.pin, HIGH);
    sendActionLog(r.deviceId, r.kind, true);
  } else {
    r.active = false;
    digitalWrite(r.pin, LOW);
    sendActionLog(r.deviceId, r.kind, false);
  }
}

void setup() {
  Serial.begin(115200);
  for (int i = 0; i < 2; i++) {
    pinMode(relays[i].pin, OUTPUT);
    digitalWrite(relays[i].pin, LOW);
  }

  if (!radio.begin()) {
    Serial.println(F("nRF24L01 initialization failed!"));
    while (1);
  }
  radio.setPALevel(RF24_PA_LOW);
  radio.openReadingPipe(1, rxPipeAddress);
  radio.startListening();
  lastMasterSignalTime = millis();
}

void loop() {
  // 1. Process incoming commands from Master Node
  if (radio.available()) {
    RadioPacket cmdPkt;
    radio.read(&cmdPkt, sizeof(RadioPacket));
    lastMasterSignalTime = millis();

    if (cmdPkt.msg_type == TYPE_COMMAND) {
      for (int i = 0; i < 2; i++) {
        if (strcmp(relays[i].deviceId, cmdPkt.device_id) == 0) {
          if (cmdPkt.value > 0) {
            setRelay(i, true, (unsigned long)cmdPkt.value);
          } else {
            setRelay(i, false);
          }
        }
      }
    }
  }

  // 2. Check individual relay run-time durations
  for (int i = 0; i < 2; i++) {
    if (relays[i].active && (millis() - relays[i].timerStart >= relays[i].durationMs)) {
      setRelay(i, false);
    }
  }

  // 3. Safety Watchdog: force shut down if communication with Master is lost
  if (millis() - lastMasterSignalTime > WATCHDOG_TIMEOUT) {
    for (int i = 0; i < 2; i++) {
      if (relays[i].active) setRelay(i, false);
    }
  }
}