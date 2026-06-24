#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <esp_task_wdt.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>

// ---------- WiFi / Server ----------
const char* WIFI_SSID     = "archeryNet";
const char* WIFI_PASSWORD = "archery2026";
const char* TRIGGER_URL   = "http://192.168.60.1/api/trigger-all";
unsigned long lastWiFiReconnectAttempt = 0;
const unsigned long WIFI_RECONNECT_INTERVAL_MS = 5000;

// ---------- MPU6050 ----------
Adafruit_MPU6050 mpu;
const uint8_t MPU_ADDRESS = 0x68;

// ---------- Shot detection ----------
const float SHOT_THRESHOLD = 80.0;
const unsigned long MIN_SHOT_INTERVAL_MS = 1000;

// ---------- Timing ----------
const unsigned long SAMPLE_INTERVAL_MS = 10;

// ---------- Health ----------
const int MAX_MPU_FAILURES = 5;

unsigned long lastShotTime = 0;
unsigned long lastSampleTime = 0;
int mpuFailures = 0;

void feedWatchdog() {
  esp_task_wdt_reset();
}

void setupWatchdog() {
  // The ESP32 Arduino core often initializes the watchdog before setup().
  // We only add the Arduino loop task to the existing watchdog.
  esp_err_t result = esp_task_wdt_add(NULL);

  if (result == ESP_OK) {
    Serial.println("Watchdog added to loop task.");
  } else {
    Serial.printf("Watchdog add returned: %d\n", result);
  }
}

bool isMPUConnected() {
  Wire.beginTransmission(MPU_ADDRESS);
  return Wire.endTransmission() == 0;
}

void maintainWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }

  unsigned long now = millis();

  if (now - lastWiFiReconnectAttempt < WIFI_RECONNECT_INTERVAL_MS) {
    return;
  }

  lastWiFiReconnectAttempt = now;

  Serial.println("WiFi not connected. Forcing reconnect...");

  WiFi.disconnect(false);   // disconnect, but keep credentials
  delay(100);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void WiFiEvent(WiFiEvent_t event, arduino_event_info_t info) {

    switch (event) {

        case ARDUINO_EVENT_WIFI_STA_CONNECTED:
            Serial.println("WiFi connected to AP.");
            break;

        case ARDUINO_EVENT_WIFI_STA_GOT_IP:
            Serial.print("WiFi connected. IP address: ");
            Serial.println(WiFi.localIP());
            break;

        case ARDUINO_EVENT_WIFI_STA_DISCONNECTED: {
            wifi_err_reason_t reason =
                static_cast<wifi_err_reason_t>(info.wifi_sta_disconnected.reason);

            Serial.printf("WiFi disconnected, reason: %s (%d)\n",
                          WiFi.disconnectReasonName(reason),
                          info.wifi_sta_disconnected.reason);
            }
            break;

        default:
            break;
    }
}

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }

  Serial.print("Connecting to WiFi: ");
  Serial.println(WIFI_SSID);

  WiFi.onEvent(WiFiEvent);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(false);

  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long startAttempt = millis();

  while (WiFi.status() != WL_CONNECTED &&
         millis() - startAttempt < 000) {
    feedWatchdog();
    delay(250);
    Serial.print(".");
  }

  Serial.println();

  if (WiFi.status() == WL_CONNECTED) {
    Serial.print("WiFi connected. IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("WiFi connection failed.");
  }
}

bool sendTrigger() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Cannot send trigger: WiFi not connected.");
    return false;
  }

  esp_task_wdt_delete(NULL);   // pause watchdog monitoring for this task

  HTTPClient http;
  http.setConnectTimeout(1000);
  http.setTimeout(1000);

  http.begin(TRIGGER_URL);
  http.addHeader("Content-Type", "application/json");

  int httpCode = http.POST("{}");

  Serial.print("POST response code: ");
  Serial.println(httpCode);

  http.end();

  esp_task_wdt_add(NULL);      // re-enable watchdog monitoring

  return httpCode > 0 && httpCode < 400;
}

void setupMPU6050() {
  Wire.begin();

  const int MAX_RETRIES = 5;

  for (int attempt = 1; attempt <= MAX_RETRIES; attempt++) {
    feedWatchdog();

    Serial.printf("Initializing MPU6050 (attempt %d/%d)\n",
                  attempt, MAX_RETRIES);

    if (mpu.begin(MPU_ADDRESS, &Wire)) {
      Serial.println("MPU6050 found.");

      mpu.setAccelerometerRange(MPU6050_RANGE_16_G);
      mpu.setGyroRange(MPU6050_RANGE_500_DEG);
      mpu.setFilterBandwidth(MPU6050_BAND_94_HZ);

      return;
    }

    Serial.println("MPU6050 not found.");

    for (int i = 0; i < 10; i++) {
      feedWatchdog();
      delay(100);
    }
  }

  Serial.println("MPU6050 initialization failed. Restarting...");
  delay(500);
  ESP.restart();
}

void logAccelValues(float x, float y, float z, float impactValue) {
  Serial.print("x=");
  Serial.print(x);
  Serial.print(" y=");
  Serial.print(y);
  Serial.print(" z=");
  Serial.print(z);
  Serial.print(" sum=");
  Serial.println(impactValue);
}

void setup() {
  Serial.begin(115200);
  delay(2000);

  setupWatchdog();

  Serial.println();
  Serial.println("Archery shot detector starting...");

  setupMPU6050();
  connectWiFi();

  Serial.println("Ready.");
}

void loop() {
  feedWatchdog();

  maintainWiFi();

  unsigned long now = millis();

  if (now - lastSampleTime < SAMPLE_INTERVAL_MS) {
    delay(1);
    return;
  }

  lastSampleTime = now;

  if (!isMPUConnected()) {
    mpuFailures++;

    Serial.printf("MPU6050 communication failure %d/%d\n",
                  mpuFailures, MAX_MPU_FAILURES);

    if (mpuFailures >= MAX_MPU_FAILURES) {
      Serial.println("MPU6050 lost. Restarting...");
      delay(100);
      ESP.restart();
    }

    return;
  }

  mpuFailures = 0;

  sensors_event_t accel, gyro, temp;
  mpu.getEvent(&accel, &gyro, &temp);

  float x = accel.acceleration.x;
  float y = accel.acceleration.y;
  float z = accel.acceleration.z;

  float impactValue = fabs(x) + fabs(y) + fabs(z);

  bool overThreshold = impactValue > SHOT_THRESHOLD;
  bool cooldownPassed = now - lastShotTime >= MIN_SHOT_INTERVAL_MS;

  if (overThreshold && cooldownPassed) {
    lastShotTime = now;

    Serial.println("SHOT DETECTED!");
    logAccelValues(x, y, z, impactValue);

    bool ok = sendTrigger();

    if (ok) {
      Serial.println("Trigger sent successfully.");
    } else {
      Serial.println("Trigger failed.");
    }
  }
}