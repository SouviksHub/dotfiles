// Cash-drawer reed switch + hidden panic button -> shopwatch sensor hook.
// Hardware (~₹400): ESP32 dev board, NC/NO magnetic reed switch (door sensor), push button.
//   reed switch: GPIO 27 <-> GND, magnet on the drawer, switch on the cabinet
//   panic button: GPIO 26 <-> GND, mounted where only the owner/cashier can reach it
// Power it from the counter's USB charger. Arduino IDE → board "ESP32 Dev Module".
#include <WiFi.h>
#include <HTTPClient.h>

const char* SSID   = "your-wifi";
const char* PASS   = "your-wifi-password";
const char* HOST   = "http://192.168.1.20:8090";   // phone IP : [sensors].port
const char* TOKEN  = "same-as-sensors.token";
const char* CAMERA = "counter";                    // [[camera]] name with the counter rules

const int REED = 27, PANIC = 26;

bool post(const String& path) {
  if (WiFi.status() != WL_CONNECTED) WiFi.reconnect();
  for (int i = 0; i < 3; i++) {
    HTTPClient http;
    http.begin(String(HOST) + path);
    http.addHeader("X-Token", TOKEN);
    int code = http.POST("");
    http.end();
    if (code == 200) return true;
    delay(300);
  }
  return false;
}

void setup() {
  pinMode(REED, INPUT_PULLUP);
  pinMode(PANIC, INPUT_PULLUP);
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false);
  WiFi.begin(SSID, PASS);
  while (WiFi.status() != WL_CONNECTED) delay(250);
}

int lastDrawer = -1;
unsigned long panicSince = 0;

void loop() {
  // Magnet present (drawer shut) pulls the input LOW with a normally-open switch.
  int open = digitalRead(REED) == HIGH;
  if (open != lastDrawer) {
    delay(40);                                   // debounce
    if ((digitalRead(REED) == HIGH) == open) {
      lastDrawer = open;
      post(String("/drawer/") + CAMERA + (open ? "/open" : "/closed"));
    }
  }
  // Panic needs a 1 s hold so a bump doesn't trigger it.
  if (digitalRead(PANIC) == LOW) {
    if (!panicSince) panicSince = millis();
    if (panicSince != 1 && millis() - panicSince > 1000) { post("/panic"); panicSince = 1; }
  } else {
    panicSince = 0;
  }
  delay(20);
}
