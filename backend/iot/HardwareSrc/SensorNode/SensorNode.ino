#include "Payload.h"

// Configuration
#define FIELD_ID   "F1"
#define MODULE_ID  "S1"

// Pin Definitions per Hardware Schematic
#define DHT_PIN       2
#define DHT_TYPE      DHT11
#define PIN_MQ135     A0
#define PIN_WATER     A1
#define PIN_SOIL      A2
#define RF24_CE_PIN   9
#define RF24_CSN_PIN  10

// Objects
RF24 radio(RF24_CE_PIN, RF24_CSN_PIN);
DHT dht(DHT_PIN, DHT_TYPE);
BH1750 lightMeter;

const uint64_t pipeAddress = 0xE8E8F0F0E1LL;
unsigned long lastSampleTime = 0;
const unsigned long SAMPLE_INTERVAL = 10000; // 10 seconds

void setup() {
  Serial.begin(115200);
  dht.begin();
  Wire.begin();
  lightMeter.begin();

  if (!radio.begin()) {
    Serial.println(F("nRF24L01 initialization failed!"));
    while (1);
  }
  radio.setPALevel(RF24_PA_LOW);
  radio.openWritingPipe(pipeAddress);
  radio.stopListening();
}

void sendPacket(const char* device, const char* kind, float val) {
  RadioPacket pkt;
  pkt.msg_type = TYPE_READING;
  strncpy(pkt.field_id, FIELD_ID, sizeof(pkt.field_id));
  strncpy(pkt.module_id, MODULE_ID, sizeof(pkt.module_id));
  strncpy(pkt.device_id, device, sizeof(pkt.device_id));
  strncpy(pkt.kind, kind, sizeof(pkt.kind));
  pkt.value = val;

  radio.write(&pkt, sizeof(RadioPacket));
  delay(20); // Small interval between packet transmissions
}

void loop() {
  if (millis() - lastSampleTime >= SAMPLE_INTERVAL) {
    lastSampleTime = millis();

    // 1. Air Temperature & Humidity
    float temp = dht.readTemperature();
    float hum = dht.readHumidity();
    if (!isnan(temp)) sendPacket("dht11", "temp_air", temp);
    if (!isnan(hum))  sendPacket("dht11", "humidity", hum);

    // 2. Soil Moisture (Calibrated mapping 0-100%)
    int rawSoil = analogRead(PIN_SOIL);
    float soilMoisture = map(rawSoil, 1023, 300, 0, 100); 
    sendPacket("soil", "soil_moisture", constrain(soilMoisture, 0, 100));

    // 3. Water Level (%)
    int rawWater = analogRead(PIN_WATER);
    float waterLevel = map(rawWater, 0, 700, 0, 100);
    sendPacket("level", "level", constrain(waterLevel, 0, 100));

    // 4. Air Quality (MQ-135 analog raw reading)
    float rawAQ = analogRead(PIN_MQ135);
    sendPacket("mq135", "air_quality", rawAQ);

    // 5. Light Intensity (Lux)
    float lux = lightMeter.readLightLevel();
    if (lux >= 0) sendPacket("light", "light", lux);
  }
}