#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <esp_task_wdt.h>
#include <esp_sleep.h>
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

// ---------- MPU6050 interrupt ----------
const gpio_num_t MPU_INT_PIN = GPIO_NUM_2;  // XIAO D2 / A2 / GPIO2

// ---------- Shot detection ----------
const float SHOT_THRESHOLD = 80.0;
const unsigned long MIN_SHOT_INTERVAL_MS = 1000;

// ---------- Sleep ----------
// Use this for production:
const unsigned long SLEEP_AFTER_NO_TRIGGER_MS = 10UL * 60UL * 1000UL;

// Use this for testing:
// const unsigned long SLEEP_AFTER_NO_TRIGGER_MS = 1UL * 60UL * 1000UL;

// ---------- Timing ----------
const unsigned long SAMPLE_INTERVAL_MS = 10;

// ---------- Health ----------
const int MAX_MPU_FAILURES = 5;

unsigned long lastShotTime = 0;
unsigned long lastTriggerTime = 0;
unsigned long lastSampleTime = 0;
int mpuFailures = 0;

// ---------- Watchdog ----------
void feedWatchdog() {
  esp_task_wdt_reset();
}

void setupWatchdog() {
  esp_err_t result = esp_task_wdt_add(NULL);

  if (result == ESP_OK) {
    Serial.println("Watchdog added to loop task.");
  } else {
    Serial.printf("Watchdog add returned: %d\n", result);
  }
}

// ---------- MPU low-level access ----------
void writeMPURegister(uint8_t reg, uint8_t value) {
  Wire.beginTransmission(MPU_ADDRESS);
  Wire.write(reg);
  Wire.write(value);
  Wire.endTransmission();
}

uint8_t readMPURegister(uint8_t reg) {
  Wire.beginTransmission(MPU_ADDRESS);
  Wire.write(reg);
  Wire.endTransmission(false);

  Wire.requestFrom(MPU_ADDRESS, (uint8_t)1);
  return Wire.available() ? Wire.read() : 0;
}

bool isMPUConnected() {
  Wire.beginTransmission(MPU_ADDRESS);
  return Wire.endTransmission() == 0;
}

// ---------- MPU interrupt config ----------
void configureMPUMotionInterrupt() {
  // Wake MPU6050
  writeMPURegister(0x6B, 0x00); // PWR_MGMT_1
  delay(100);

  // Disable all interrupts and clear old status
  writeMPURegister(0x38, 0x00); // INT_ENABLE
  readMPURegister(0x3A);        // INT_STATUS

  // Accel ±2g for sensitive motion wake
  writeMPURegister(0x1C, 0x00); // ACCEL_CONFIG

  // Motion threshold.
  // Approx 2 mg per LSB. 10 ≈ 20 mg.
  // Increase if it wakes too easily.
  writeMPURegister(0x1F, 5);   // MOT_THR

  // Motion duration in ms
  writeMPURegister(0x20, 1);   // MOT_DUR

  // Motion detection control
  writeMPURegister(0x69, 0x15); // MOT_DETECT_CTRL

  // INT pin:
  // active high, push-pull, latched until INT_STATUS is read
  writeMPURegister(0x37, 0x20); // INT_PIN_CFG

  // Enable motion interrupt only
  writeMPURegister(0x38, 0x40); // INT_ENABLE

  delay(50);
  readMPURegister(0x3A);        // Clear old interrupt
}

void disableMPUInterruptsForNormalMode() {
  writeMPURegister(0x38, 0x00); // Disable MPU interrupts
  readMPURegister(0x3A);        // Clear status
}

// ---------- WiFi events ----------
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
      break;
    }

    default:
      break;
  }
}

// ---------- WiFi ----------
void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }

  Serial.print("Connecting to WiFi: ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.persistent(false);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long startAttempt = millis();

  while (WiFi.status() != WL_CONNECTED &&
         millis() - startAttempt < 3000) {
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

  WiFi.disconnect(false);
  delay(100);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

// ---------- Trigger ----------
bool sendTrigger() {
  if (WiFi.status() != WL_CONNECTED) {
    connectWiFi();
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Cannot send trigger: WiFi not connected.");
    return false;
  }

  esp_task_wdt_delete(NULL);

  HTTPClient http;
  http.setConnectTimeout(1000);
  http.setTimeout(1000);

  http.begin(TRIGGER_URL);
  http.addHeader("Content-Type", "application/json");

  int httpCode = http.POST("{}");

  Serial.print("POST response code: ");
  Serial.println(httpCode);

  http.end();

  esp_task_wdt_add(NULL);

  return httpCode > 0 && httpCode < 400;
}

// ---------- Power save ----------
void enterPowerSaveMode() {
  Serial.println("Entering deep sleep mode...");

  configureMPUMotionInterrupt();

  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);

  delay(100);

  pinMode((int)MPU_INT_PIN, INPUT_PULLDOWN);

  esp_sleep_enable_ext1_wakeup_io(
      1ULL << MPU_INT_PIN,
      ESP_EXT1_WAKEUP_ANY_HIGH
  );

  Serial.println("Deep sleep armed. Wake on GPIO2 HIGH / MPU6050 INT.");
  Serial.flush();

  esp_task_wdt_delete(NULL);
  esp_deep_sleep_start();
}

// ---------- MPU setup ----------
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

      disableMPUInterruptsForNormalMode();

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

// ---------- Logging ----------
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

// ---------- Setup ----------
void setup() {
  Serial.begin(115200);
  delay(2000);

  esp_sleep_wakeup_cause_t wakeReason = esp_sleep_get_wakeup_cause();

  if (wakeReason == ESP_SLEEP_WAKEUP_EXT1) {
    Serial.println("Woke from deep sleep by GPIO / MPU interrupt.");
  } else {
    Serial.println("Normal boot.");
  }

  WiFi.onEvent(WiFiEvent);

  setupWatchdog();

  Serial.println();
  Serial.println("Archery shot detector starting...");

  setupMPU6050();

  lastTriggerTime = millis();
  lastSampleTime = millis();

  connectWiFi();

  Serial.println("Ready.");
}

// ---------- Main loop ----------
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
    lastTriggerTime = now;

    Serial.println("SHOT DETECTED!");
    logAccelValues(x, y, z, impactValue);

    bool ok = sendTrigger();

    if (ok) {
      Serial.println("Trigger sent successfully.");
    } else {
      Serial.println("Trigger failed.");
    }
  }

  if (now - lastTriggerTime > SLEEP_AFTER_NO_TRIGGER_MS) {
    enterPowerSaveMode();
  }
}