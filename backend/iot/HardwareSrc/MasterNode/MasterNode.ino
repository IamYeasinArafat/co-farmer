#include "time.h"
#include "Payload.h"

// Configuration
const char* WIFI_SSID     = "FarmHotspot";
const char* WIFI_PASS     = "farm123456";
const char* API_BASE_URL  = "http://192.168.1.100:8000"; // Farm Computer IP

#define RF24_CE_PIN   4
#define RF24_CSN_PIN  5
#define SD_CS_PIN     13

RF24 radio(RF24_CE_PIN, RF24_CSN_PIN);
const uint64_t rxPipeAddress    = 0xE8E8F0F0E1LL;
const uint64_t actuatorPipeAddr = 0xE8E8F0F0E2LL;

// Range rules loaded from SD / Farm Computer
int rangeVersion = 0;
float minSoilMoisture = 30.0, maxSoilMoisture = 45.0;
float maxTempAir = 32.0;
float minWaterLevel = 20.0;
int pumpRunSeconds = 120;

unsigned long lastRangeCheck = 0;
const unsigned long RANGE_CHECK_INTERVAL = 300000; // 5 minutes in demo

String getISO8601Time() {
  struct tm timeinfo;
  if (!getLocalTime(&timeinfo)) return "1970-01-01T00:00:00+03:00";
  char strftime_buf[64];
  strftime(strftime_buf, sizeof(strftime_buf), "%Y-%m-%dT%H:%M:%S+03:00", &timeinfo);
  return String(strftime_buf);
}

void logToSD(const String& csvRow) {
  File file = SD.open("/readings.csv", FILE_APPEND);
  if (file) {
    file.println(csvRow);
    file.close();
  }
}

void sendHttpLog(const String& csvRow) {
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    http.begin(String(API_BASE_URL) + "/log");
    http.addHeader("Content-Type", "text/plain");
    http.POST(csvRow);
    http.end();
  }
}

void sendActuatorCommand(const char* device, float durationSec) {
  radio.stopListening();
  radio.openWritingPipe(actuatorPipeAddr);

  RadioPacket cmdPkt;
  cmdPkt.msg_type = TYPE_COMMAND;
  strncpy(cmdPkt.field_id, "F1", sizeof(cmdPkt.field_id));
  strncpy(cmdPkt.module_id, "A1", sizeof(cmdPkt.module_id));
  strncpy(cmdPkt.device_id, device, sizeof(cmdPkt.device_id));
  strncpy(cmdPkt.kind, "control", sizeof(cmdPkt.kind));
  cmdPkt.value = durationSec;

  radio.write(&cmdPkt, sizeof(RadioPacket));
  radio.openReadingPipe(1, rxPipeAddress);
  radio.startListening();
}

void fetchNewRanges() {
  if (WiFi.status() != WL_CONNECTED) return;
  HTTPClient http;
  http.begin(String(API_BASE_URL) + "/ranges");
  int code = http.GET();

  if (code == HTTP_CODE_OK) {
    String payload = http.getString();
    StaticJsonDocument<512> doc;
    if (deserializeJson(doc, payload) == DeserializationError::Ok) {
      JsonObject f1 = doc["F1"]["soil_moisture"];
      int ver = f1["version"];
      if (ver > rangeVersion) {
        rangeVersion = ver;
        minSoilMoisture = f1["valid_from"][0];
        maxSoilMoisture = f1["valid_from"][1];
        maxTempAir = doc["F1"]["temp_air"][1];
        pumpRunSeconds = doc["F1"]["pump_seconds"];

        // Save ranges to SD for persistence across reboot
        File f = SD.open("/ranges.json", FILE_WRITE);
        if (f) { f.print(payload); f.close(); }
      }
    }
  }
  http.end();
}

void checkRangesAndEnforce(const RadioPacket& pkt) {
  if (strcmp(pkt.kind, "soil_moisture") == 0) {
    if (pkt.value < minSoilMoisture) {
      sendActuatorCommand("relay1", pumpRunSeconds); // Pump on for configured time
    }
  } else if (strcmp(pkt.kind, "temp_air") == 0) {
    if (pkt.value > maxTempAir) {
      sendActuatorCommand("relay2", 180); // Fan on
    }
  }
}

void setup() {
  Serial.begin(115200);

  // WiFi & NTP Clock initialization
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  configTime(10800, 0, "pool.ntp.org"); // UTC+3 Qatar Time

  // SD Card Initialization
  if (!SD.begin(SD_CS_PIN)) Serial.println(F("SD Card Mount Failed!"));

  // Radio Initialization
  if (!radio.begin()) {
    Serial.println(F("nRF24L01 initialization failed!"));
    while (1);
  }
  radio.setPALevel(RF24_PA_LOW);
  radio.openReadingPipe(1, rxPipeAddress);
  radio.startListening();
}

void loop() {
  // 1. Process incoming packet from Sensor/Actuator Nodes
  if (radio.available()) {
    RadioPacket pkt;
    radio.read(&pkt, sizeof(RadioPacket));

    String timestamp = getISO8601Time();
    String typeStr = (pkt.msg_type == TYPE_ACTION) ? "action" : "reading";
    String valStr = (pkt.msg_type == TYPE_ACTION) ? (pkt.value > 0 ? "activated" : "deactivated") : String(pkt.value, 1);
    String unitStr = (strcmp(pkt.kind, "temp_air") == 0) ? "C" : ((strcmp(pkt.kind, "soil_moisture") == 0) ? "%" : "");

    // Format CSV: field_id,module_id,device_id,type,kind,value,unit,timestamp
    String csvRow = String(pkt.field_id) + "," + String(pkt.module_id) + "," + 
                    String(pkt.device_id) + "," + typeStr + "," + 
                    String(pkt.kind) + "," + valStr + "," + unitStr + "," + timestamp;

    logToSD(csvRow);
    sendHttpLog(csvRow);

    if (pkt.msg_type == TYPE_READING) {
      checkRangesAndEnforce(pkt);
    }
  }

  // 2. Fetch updated ranges periodically
  if (millis() - lastRangeCheck >= RANGE_CHECK_INTERVAL) {
    lastRangeCheck = millis();
    fetchNewRanges();
  }
}