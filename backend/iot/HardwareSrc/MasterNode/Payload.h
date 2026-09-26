#ifndef PAYLOAD_H
#define PAYLOAD_H

#include 

enum PacketType : uint8_t {
  TYPE_READING = 1,
  TYPE_ACTION  = 2,
  TYPE_COMMAND = 3
};

struct __attribute__((packed)) RadioPacket {
  uint8_t msg_type;     // 1=reading, 2=action, 3=command
  char field_id[3];     // e.g., "F1"
  char module_id[3];    // e.g., "S1" or "A1"
  char device_id[8];    // e.g., "dht11", "soil", "relay1"
  char kind[12];        // e.g., "temp_air", "soil_moisture", "pump"
  float value;          // Reading value, status flag, or duration (sec)
};

#endif