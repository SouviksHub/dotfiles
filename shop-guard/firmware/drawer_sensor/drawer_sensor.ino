// Shop Guard cash-drawer sensor for Olimex ESP32-POE (wired Ethernet + PoE power).
//
// Wiring: magnetic reed switch (normally-open type) between GPIO 4 and GND. The magnet
// is on the drawer, the switch on the cabinet, so the switch is CLOSED when the drawer
// is shut. INPUT_PULLUP means a cut or unplugged wire reads exactly like "open", so
// cutting the sensor cable raises the same alert as opening the drawer.
//
// Build: Arduino IDE / arduino-cli with the esp32 core 3.x (Espressif), board
// "OLIMEX ESP32-PoE" (its variant supplies the Ethernet PHY pins to ETH.begin()),
// library PubSubClient by Nick O'Leary.
// Fill in the broker address and the MQTT password you created with mosquitto_passwd.
// NOTE: not compile-tested yet; build and verify it on your bench board before February.

#include <ETH.h>
#include <PubSubClient.h>

const char* MQTT_HOST = "192.168.1.10";   // the mini PC's LAN IP
const uint16_t MQTT_PORT = 1884;          // device listener (password protected)
const char* MQTT_USER = "drawer";
const char* MQTT_PASS = "CHANGE_ME";

const int SENSOR_PIN = 4;
const unsigned long DEBOUNCE_MS = 50;
const unsigned long HEARTBEAT_MS = 15000;

NetworkClient net;
PubSubClient mqtt(net);
bool ethUp = false;
int lastStable = -1, lastRead = -1;
unsigned long lastChange = 0, lastBeat = 0;

void onEvent(arduino_event_id_t event) {
  if (event == ARDUINO_EVENT_ETH_GOT_IP) ethUp = true;
  if (event == ARDUINO_EVENT_ETH_DISCONNECTED || event == ARDUINO_EVENT_ETH_STOP) ethUp = false;
}

const char* stateName(int level) { return level == LOW ? "closed" : "open"; }

void publishState(int level) {
  mqtt.publish("shopguard/drawer/state", stateName(level), true);   // retained
}

void ensureMqtt() {
  if (mqtt.connected() || !ethUp) return;
  // Last will: the broker announces "offline" if this board disappears.
  if (mqtt.connect("drawer-sensor", MQTT_USER, MQTT_PASS,
                   "shopguard/drawer/status", 1, true, "offline")) {
    mqtt.publish("shopguard/drawer/status", "online", true);
    if (lastStable != -1) publishState(lastStable);
  }
}

void setup() {
  pinMode(SENSOR_PIN, INPUT_PULLUP);
  Network.onEvent(onEvent);
  ETH.begin();
  mqtt.setServer(MQTT_HOST, MQTT_PORT);
}

void loop() {
  ensureMqtt();
  mqtt.loop();

  int level = digitalRead(SENSOR_PIN);
  unsigned long now = millis();
  if (level != lastRead) { lastRead = level; lastChange = now; }
  if (now - lastChange >= DEBOUNCE_MS && level != lastStable) {
    lastStable = level;
    if (mqtt.connected()) publishState(level);
  }
  if (now - lastBeat >= HEARTBEAT_MS) {
    lastBeat = now;
    if (mqtt.connected()) mqtt.publish("shopguard/drawer/heartbeat", "1");
  }
  delay(5);
}
