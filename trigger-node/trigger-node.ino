#include <Wire.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Adafruit_MPU6050.h>
#include <Adafruit_Sensor.h>

// ---------- WiFi / Server ----------
const char* WIFI_SSID     = "archeryNet";
const char* WIFI_PASSWORD = "archery2026";
const char* TRIGGER_URL   = "http://192.168.60.1/api/trigger-all";

// ---------- MPU6050 ----------
Adafruit_MPU6050 mpu;

// ---------- Shot detection ----------
const float SHOT_THRESHOLD = 80.0;   // Tune this value
const unsigned long MIN_SHOT_INTERVAL_MS = 1000;

unsigned long lastShotTime = 0;

// ---------- Timing ----------
const unsigned long SAMPLE_INTERVAL_MS = 10; // 100 Hz sampling
unsigned long lastSampleTime = 0;

void connectWiFi() {
  if (WiFi.status() == WL_CONNECTED) {
    return;
  }

  Serial.print("Connecting to WiFi: ");
  Serial.println(WIFI_SSID);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  unsigned long startAttempt = millis();

  while (WiFi.status() != WL_CONNECTED && millis() - startAttempt < 15000) {
    delay(500);
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

  HTTPClient http;
  http.begin(TRIGGER_URL);
  http.addHeader("Content-Type", "application/json");

  int httpCode = http.POST("{}");

  Serial.print("POST response code: ");
  Serial.println(httpCode);

  http.end();

  return httpCode > 0 && httpCode < 400;
}

void setupMPU6050() {
  Wire.begin();

  if (!mpu.begin(0x68, &Wire)) {
    Serial.println("MPU6050 not found!");
    while (true) {
      delay(1000);
    }
  }

  Serial.println("MPU6050 found.");

  mpu.setAccelerometerRange(MPU6050_RANGE_16_G);
  mpu.setGyroRange(MPU6050_RANGE_500_DEG);
  mpu.setFilterBandwidth(MPU6050_BAND_94_HZ);
}

void setup() {
  Serial.begin(115200);
  delay(2000);

  Serial.println();
  Serial.println("Archery shot detector starting...");

  setupMPU6050();
  connectWiFi();

  Serial.println("Ready.");
}

void logAccelValues(const float x, const float y, const float z, const float impactValue){
  Serial.print("x=");
  Serial.print(x);
  Serial.print(" y=");
  Serial.print(y);
  Serial.print(" z=");
  Serial.print(z);
  Serial.print(" sum=");
  Serial.println(impactValue);
}

void loop() {
  unsigned long now = millis();

  if (now - lastSampleTime < SAMPLE_INTERVAL_MS) {
    return;
  }

  lastSampleTime = now;

  sensors_event_t accel;
  sensors_event_t gyro;
  sensors_event_t temp;

  mpu.getEvent(&accel, &gyro, &temp);

  float x = accel.acceleration.x;
  float y = accel.acceleration.y;
  float z = accel.acceleration.z;

  const float impactValue = abs(x) + abs(y) + abs(z);

  bool overThreshold = impactValue > SHOT_THRESHOLD;
  bool cooldownPassed = now - lastShotTime >= MIN_SHOT_INTERVAL_MS;

  if (overThreshold && cooldownPassed) {
    lastShotTime = now;

    Serial.println("SHOT DETECTED!");
    logAccelValues(x, y, z, impactValue);

    const bool ok = sendTrigger();

    if (ok) {
      Serial.println("Trigger sent su, ccessfully.");
    } else {
      Serial.println("Trigger failed.");
    }
  }
}