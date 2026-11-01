#include <IRremote.h>
#include <Servo.h>

#define ECHO_PIN A4
#define TRIG_PIN A5

#define ENA 5
#define ENB 6
#define IN1 7
#define IN2 8
#define IN3 9
#define IN4 11

#define Receiver 12
#define LED_Pin 13

#define LineTeacking_Pin_Right 10
#define LineTeacking_Pin_Middle 4
#define LineTeacking_Pin_Left 2

#define LineTeacking_Read_Right  !digitalRead(LineTeacking_Pin_Right)
#define LineTeacking_Read_Middle !digitalRead(LineTeacking_Pin_Middle)
#define LineTeacking_Read_Left   !digitalRead(LineTeacking_Pin_Left)

#define carSpeed 200

// Every AI movement stops automatically. The laptop can later send another
// pulse after checking a fresh camera frame.
const unsigned long AI_MOVE_PULSE_MS = 350;
const unsigned long AI_TEST_TURN_PULSE_MS = 120;
const int AI_FORWARD_CLEARANCE_CM = 40;
const unsigned long AI_SENSOR_SETTLE_MS = 350;
const unsigned long AI_DISTANCE_TIMEOUT_US = 25000;
const unsigned long AI_DISTANCE_POLL_MS = 80;
const unsigned long AI_SCAN_SETTLE_MS = 500;

enum FUNCTIONMODE {
  IDLE,
  LineTeacking,
  ObstaclesAvoidance,
  Bluetooth,
  IRremote,
  AIRecovery
};

enum MOTIONMODE {
  STOP,
  FORWARD,
  BACK,
  LEFT,
  RIGHT
};

Servo servo;

FUNCTIONMODE func_mode = IDLE;
FUNCTIONMODE previous_func_mode = IDLE;
MOTIONMODE mov_mode = STOP;

unsigned long LT_PreMillis = 0;
unsigned long aiMoveStopAt = 0;
unsigned long aiSensorReadyAt = 0;
unsigned long aiLastDistanceCheckAt = 0;
bool aiMoveActive = false;
bool emergencyLatched = false;
bool presentationActive = false;
bool routeMoveActive = false;
bool manualMoveActive = false;
bool manualPacket = false;
bool routeCollecting = false;
char routeBuffer[8];
byte routeLength = 0;
unsigned long routeStartedAt = 0;
const int ROUTE_STOP_CM = 20;

int rightDistance = 0;
int leftDistance = 0;
int middleDistance = 0;


// Forward declarations used by responsive waits.
void getBTData();
void getIRData();
void ai_recovery_mode();
int getAiDistanceCm();


// =========================
// MOTOR FUNCTIONS
// =========================

void forward(bool debug = false) {
  analogWrite(ENA, carSpeed);
  analogWrite(ENB, carSpeed);
  digitalWrite(IN1, HIGH);
  digitalWrite(IN2, LOW);
  digitalWrite(IN3, LOW);
  digitalWrite(IN4, HIGH);
  if (debug) Serial.println("Go forward!");
}

void back(bool debug = false) {
  analogWrite(ENA, carSpeed);
  analogWrite(ENB, carSpeed);
  digitalWrite(IN1, LOW);
  digitalWrite(IN2, HIGH);
  digitalWrite(IN3, HIGH);
  digitalWrite(IN4, LOW);
  if (debug) Serial.println("Go back!");
}

void left(bool debug = false) {
  analogWrite(ENA, carSpeed);
  analogWrite(ENB, carSpeed);
  digitalWrite(IN1, LOW);
  digitalWrite(IN2, HIGH);
  digitalWrite(IN3, LOW);
  digitalWrite(IN4, HIGH);
  if (debug) Serial.println("Go left!");
}

void right(bool debug = false) {
  analogWrite(ENA, carSpeed);
  analogWrite(ENB, carSpeed);
  digitalWrite(IN1, HIGH);
  digitalWrite(IN2, LOW);
  digitalWrite(IN3, HIGH);
  digitalWrite(IN4, LOW);
  if (debug) Serial.println("Go right!");
}

void stop(bool debug = false) {
  digitalWrite(ENA, LOW);
  digitalWrite(ENB, LOW);
  if (debug) Serial.println("Stop!");
}


// =========================
// MODE / SAFETY HELPERS
// =========================

const char *modeName(FUNCTIONMODE mode) {
  switch (mode) {
    case IDLE: return "IDLE";
    case LineTeacking: return "LINE";
    case ObstaclesAvoidance: return "OBSTACLE";
    case Bluetooth: return "BLUETOOTH";
    case IRremote: return "IR";
    case AIRecovery: return presentationActive ? "PRESENTATION" : "AI_RECOVERY";
    default: return "UNKNOWN";
  }
}

void clearAiMovement() {
  routeMoveActive = false;
  manualMoveActive = false;
  aiMoveActive = false;
  aiMoveStopAt = 0;
  mov_mode = STOP;
}

void enterNormalMode(FUNCTIONMODE newMode, MOTIONMODE newMotion = STOP) {
  presentationActive = false;
  if (func_mode == AIRecovery) {
    stop();
  }
  clearAiMovement();
  func_mode = newMode;
  mov_mode = newMotion;
}

void printStatus() {
  Serial.print("STATUS:MODE=");
  Serial.print(modeName(func_mode));
  Serial.print(",MOTION=");
  Serial.print((int)mov_mode);
  Serial.print(",EMERGENCY=");
  Serial.println(emergencyLatched ? "1" : "0");
}

void enterAiRecovery(const char *reason) {
  if (emergencyLatched) {
    Serial.println("AI:ERROR:EMERGENCY_LATCHED");
    return;
  }
  if (func_mode != AIRecovery) {
    previous_func_mode = func_mode;
  }
  stop();
  clearAiMovement();
  func_mode = AIRecovery;
  presentationActive = false;
  servo.write(90);
  aiSensorReadyAt = millis() + AI_SENSOR_SETTLE_MS;

  Serial.print("STUCK:");
  Serial.println(reason);
  Serial.print("AI:READY:PREVIOUS=");
  Serial.println(modeName(previous_func_mode));
}

void enterPresentation() {
  if (emergencyLatched) {
    Serial.println(F("AI:ERROR:EMERGENCY_LATCHED"));
    return;
  }
  enterAiRecovery("PRESENTATION");
  presentationActive = true;
  Serial.println(F("PRESENTATION:READY"));
}

void resumePreviousMode() {
  presentationActive = false;
  if (func_mode != AIRecovery) {
    Serial.println("AI:ERROR:NOT_ACTIVE");
    return;
  }

  stop();
  clearAiMovement();
  func_mode = previous_func_mode;
  Serial.print("AI:RESUMED:");
  Serial.println(modeName(func_mode));
}

void emergencyStop() {
  presentationActive = false;
  stop();
  clearAiMovement();
  func_mode = IDLE;
  emergencyLatched = true;
  Serial.println("EMERGENCY:STOPPED");
}

void clearEmergency() {
  presentationActive = false;
  stop();
  clearAiMovement();
  func_mode = IDLE;
  emergencyLatched = false;
  Serial.println("EMERGENCY:CLEARED");
}

void startAiMovement(MOTIONMODE motion, char command,
                     unsigned long pulseMs = AI_MOVE_PULSE_MS, bool route = false, bool manual = false) {
  if (func_mode != AIRecovery) {
    Serial.println("AI:ERROR:NOT_ACTIVE");
    return;
  }

  if ((route || manual) && (!presentationActive || aiMoveActive || pulseMs < 1 || pulseMs > 350)) {
    Serial.println(F("AI:ERROR:ROUTE_NOT_READY"));
    return;
  }
  if (!manual && (motion == FORWARD || route)) {
    int distanceCm = getAiDistanceCm();
    if (distanceCm == -2) {
      stop();
      clearAiMovement();
      Serial.println(F("AI:BLOCKED:SENSOR_NOT_READY"));
      return;
    }
    if (distanceCm < 0) {
      stop();
      clearAiMovement();
      Serial.println(F("AI:BLOCKED:SENSOR_INVALID"));
      return;
    }
    if (distanceCm <= (route ? ROUTE_STOP_CM : AI_FORWARD_CLEARANCE_CM)) {
      stop();
      clearAiMovement();
      Serial.print(F("AI:BLOCKED:TOO_CLOSE_CM="));
      Serial.println(distanceCm);
      return;
    }
    aiLastDistanceCheckAt = millis();
  }

  mov_mode = motion;
  routeMoveActive = route;
  manualMoveActive = manual;
  switch (motion) {
    case FORWARD: forward(); break;
    case BACK: back(); break;
    case LEFT: left(); break;
    case RIGHT: right(); break;
    case STOP: stop(); break;
  }

  if (motion == STOP) {
    clearAiMovement();
  } else {
    aiMoveActive = true;
    aiMoveStopAt = millis() + pulseMs;
  }

  Serial.print("AI:ACK:");
  Serial.println(command);
}

void ai_recovery_mode() {
  if (func_mode != AIRecovery || !aiMoveActive) {
    return;
  }

  if ((long)(millis() - aiMoveStopAt) >= 0) {
    stop();
    clearAiMovement();
    Serial.println("AI:MOVE_DONE");
    return;
  }

  if (!manualMoveActive && (mov_mode == FORWARD || routeMoveActive) && millis() - aiLastDistanceCheckAt >= AI_DISTANCE_POLL_MS) {
    aiLastDistanceCheckAt = millis();
    int distanceCm = getAiDistanceCm();
    if (distanceCm < 0 || distanceCm <= (routeMoveActive ? ROUTE_STOP_CM : AI_FORWARD_CLEARANCE_CM)) {
      stop();
      clearAiMovement();
      if (distanceCm < 0) {
        Serial.println(F("AI:BLOCKED:SENSOR_INVALID"));
      } else {
        Serial.print(F("AI:BLOCKED:TOO_CLOSE_CM="));
        Serial.println(distanceCm);
      }
    }
  }
}

// Wait while continuing to service Serial and IR. Returning false tells the
// caller that another command changed the active mode, so it must stop its old
// routine immediately.
bool responsiveDelay(unsigned long durationMs, FUNCTIONMODE expectedMode) {
  unsigned long startedAt = millis();
  while (millis() - startedAt < durationMs) {
    getBTData();
    getIRData();
    ai_recovery_mode();
    if (func_mode != expectedMode) {
      return false;
    }
    delay(1);
  }
  return true;
}


// =========================
// ULTRASONIC DISTANCE
// =========================

int getDistance() {
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  return (int)pulseIn(ECHO_PIN, HIGH) / 58;
}

int getAiDistanceCm() {
  if ((long)(millis() - aiSensorReadyAt) < 0) {
    return -2;  // Servo must face forward before a safety reading is used.
  }
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  unsigned long echoUs = pulseIn(ECHO_PIN, HIGH, AI_DISTANCE_TIMEOUT_US);
  if (echoUs == 0) {
    return -1;  // Missing echo is never treated as a clear path.
  }
  int distanceCm = (int)(echoUs / 58);
  if (distanceCm < 3 || distanceCm > 300) {
    return -1;
  }
  return distanceCm;
}

void printAiDistance() {
  if (func_mode != AIRecovery) {
    Serial.println(F("AI:DIST:NOT_ACTIVE"));
    return;
  }
  int distanceCm = getAiDistanceCm();
  if (distanceCm == -2) {
    Serial.println(F("AI:DIST:NOT_READY"));
  } else if (distanceCm < 0) {
    Serial.println(F("AI:DIST:INVALID"));
  } else {
    Serial.print(F("AI:DIST_CM="));
    Serial.println(distanceCm);
  }
}

void printAiSideScan() {
  if (func_mode != AIRecovery) {
    Serial.println(F("AI:SCAN:NOT_ACTIVE"));
    return;
  }

  // This is a stationary measurement, never a turn authorization. A forward
  // command stays blocked until the sensor has centred and settled again.
  stop();
  clearAiMovement();
  servo.write(170);  // Same left/right orientation as obstacle mode.
  aiSensorReadyAt = millis() + AI_SCAN_SETTLE_MS;
  delay(AI_SCAN_SETTLE_MS + 20);
  int leftCm = getAiDistanceCm();

  servo.write(10);
  aiSensorReadyAt = millis() + AI_SCAN_SETTLE_MS + 150;
  delay(AI_SCAN_SETTLE_MS + 170);
  int rightCm = getAiDistanceCm();

  servo.write(90);
  aiSensorReadyAt = millis() + AI_SCAN_SETTLE_MS;
  delay(AI_SCAN_SETTLE_MS + 20);
  int centerCm = getAiDistanceCm();

  Serial.print(F("AI:SCAN:L="));
  Serial.print(leftCm);
  Serial.print(F(",C="));
  Serial.print(centerCm);
  Serial.print(F(",R="));
  Serial.println(rightCm);
}


// =========================
// BLUETOOTH / USB SERIAL
// =========================

void getBTData() {
  if (routeCollecting && millis() - routeStartedAt > 1000) {
    routeCollecting = false; routeLength = 0;
    stop(); clearAiMovement();
    Serial.println(F("AI:ERROR:ROUTE_PACKET_TIMEOUT"));
  }
  while (Serial.available()) {
    char command = Serial.read();

    // Dedicated bounded route packets. Stop can interrupt incomplete input.
    if (routeCollecting) {
      if (command == '!' || command == 'S' || command == 's') {
        routeCollecting = false; routeLength = 0;
        if (command == '!') emergencyStop();
        else { stop(); clearAiMovement(); Serial.println(F("AI:ACK:S")); }
        continue;
      }
      if (command == '\n') {
        routeCollecting = false;
        routeBuffer[routeLength] = '\0';
        bool valid = routeLength >= 2 && (routeBuffer[0] == 'F' || routeBuffer[0] == 'L' || routeBuffer[0] == 'R' || (manualPacket && routeBuffer[0] == 'B'));
        unsigned long duration = 0;
        for (byte i = 1; i < routeLength; i++) {
          if (routeBuffer[i] < '0' || routeBuffer[i] > '9') valid = false;
          duration = duration * 10 + (routeBuffer[i] - '0');
        }
        if (!valid || duration < 1 || duration > 350 || emergencyLatched) {
          stop(); clearAiMovement();
          if (manualPacket) Serial.println(F("AI:ERROR:INVALID_MANUAL_PACKET"));
          else Serial.println(F("AI:ERROR:INVALID_ROUTE_PACKET"));
        } else {
          char direction = routeBuffer[0];
          MOTIONMODE motion = direction == 'F' ? FORWARD : direction == 'B' ? BACK : direction == 'L' ? LEFT : RIGHT;
          startAiMovement(motion, direction, duration, !manualPacket, manualPacket);
        }
        routeLength = 0;
        continue;
      }
      if (routeLength >= sizeof(routeBuffer) - 1) {
        routeCollecting = false; routeLength = 0;
        emergencyStop();
        continue;
      }
      routeBuffer[routeLength++] = command;
      continue;
    }

    // An emergency STOP remains active even if later serial or IR movement
    // commands arrive. Clearing it leaves the car stopped in IDLE.
    if (emergencyLatched && command != 'C' && command != '!' &&
        command != '?' && command != 's' && command != 'S' &&
        command != '\r' && command != '\n') {
      Serial.println("ERROR:EMERGENCY_LATCHED");
      continue;
    }

    switch (command) {
      case '~': manualPacket = false; routeCollecting = true; routeLength = 0; routeStartedAt = millis(); break;
      case '@': manualPacket = true; routeCollecting = true; routeLength = 0; routeStartedAt = millis(); break;
      // Existing lower-case manual commands remain unchanged.
      case 'f': enterNormalMode(Bluetooth, FORWARD); break;
      case 'b': enterNormalMode(Bluetooth, BACK); break;
      case 'l': enterNormalMode(Bluetooth, LEFT); break;
      case 'r': enterNormalMode(Bluetooth, RIGHT); break;
      case 's': enterNormalMode(Bluetooth, STOP); break;
      case '1': enterNormalMode(LineTeacking); break;
      case '2': enterNormalMode(ObstaclesAvoidance); break;

      // Separate AI recovery protocol uses upper-case commands.
      case 'A': enterAiRecovery("SERIAL"); break;
      case 'P': enterPresentation(); break;
      case 'F': startAiMovement(FORWARD, 'F'); break;
      case 'B': startAiMovement(BACK, 'B'); break;
      case 'L': startAiMovement(LEFT, 'L'); break;
      case 'R': startAiMovement(RIGHT, 'R'); break;
      case 'J': startAiMovement(LEFT, 'J', AI_TEST_TURN_PULSE_MS); break;
      case 'K': startAiMovement(RIGHT, 'K', AI_TEST_TURN_PULSE_MS); break;
      case 'S': startAiMovement(STOP, 'S'); break;
      case 'X': resumePreviousMode(); break;
      case 'D': printAiDistance(); break;
      case 'G': printAiSideScan(); break;

      // Global emergency stop and read-only status query.
      case '!': emergencyStop(); break;
      case 'C': clearEmergency(); break;
      case '?': printStatus(); break;

      case '\r':
      case '\n':
        break;
      default:
        Serial.println("ERROR:UNKNOWN_COMMAND");
        break;
    }
  }
}


// =========================
// IR REMOTE CONTROL
// =========================

void getIRData() {
  if (!IrReceiver.decode()) {
    return;
  }

  uint32_t rawData = IrReceiver.decodedIRData.decodedRawData;
  Serial.print("IR code: 0x");
  Serial.println(rawData, HEX);

  if (emergencyLatched) {
    if (rawData == 0xBF40FF00) {
      stop();
      Serial.println("OK");
    } else {
      Serial.println("IR:BLOCKED:EMERGENCY_LATCHED");
    }
    IrReceiver.resume();
    return;
  }

  if (rawData != 0x00000000) {
    switch (rawData) {
      case 0xB946FF00:  // UP
        Serial.println("UP");
        enterNormalMode(IRremote, FORWARD);
        break;
      case 0xEA15FF00:  // DOWN
        Serial.println("DOWN");
        enterNormalMode(IRremote, BACK);
        break;
      case 0xBB44FF00:  // LEFT
        Serial.println("LEFT");
        enterNormalMode(IRremote, LEFT);
        break;
      case 0xBC43FF00:  // RIGHT
        Serial.println("RIGHT");
        enterNormalMode(IRremote, RIGHT);
        break;
      case 0xBF40FF00:  // OK / STOP
        Serial.println("OK");
        enterNormalMode(IRremote, STOP);
        break;
      case 0xAD52FF00: Serial.println("0"); break;
      case 0xE916FF00:  // 1 = Obstacle Avoidance
        Serial.println("1");
        enterNormalMode(ObstaclesAvoidance);
        break;
      case 0xE619FF00:  // 2 = Line Tracking
        Serial.println("2");
        enterNormalMode(LineTeacking);
        break;
      case 0xF20DFF00:  // 3 = Manual AI Recovery test trigger
        Serial.println("3");
        enterAiRecovery("IR_BUTTON_3");
        break;
      case 0xF30CFF00:  // 4 = Camera + voice presentation mode
        Serial.println("4");
        enterPresentation();
        break;
      case 0xE718FF00: Serial.println("5"); break;
      case 0xA15EFF00: Serial.println("6"); break;
      case 0xF708FF00: Serial.println("7"); break;
      case 0xE31CFF00: Serial.println("8"); break;
      case 0xA55AFF00: Serial.println("9"); break;
      case 0xBD42FF00: Serial.println("*"); break;
      case 0xB54AFF00: Serial.println("#"); break;
      default: Serial.println("Other button"); break;
    }
  }

  IrReceiver.resume();
}


// =========================
// EXISTING MODES
// =========================

void bluetooth_mode() {
  if (func_mode != Bluetooth) return;
  switch (mov_mode) {
    case FORWARD: forward(); break;
    case BACK: back(); break;
    case LEFT: left(); break;
    case RIGHT: right(); break;
    case STOP: stop(); break;
  }
}

void irremote_mode() {
  if (func_mode != IRremote) return;
  switch (mov_mode) {
    case FORWARD: forward(); break;
    case BACK: back(); break;
    case LEFT: left(); break;
    case RIGHT: right(); break;
    case STOP: stop(); break;
  }
}

void line_teacking_mode() {
  if (func_mode != LineTeacking) return;

  if (LineTeacking_Read_Middle) {
    forward();
    LT_PreMillis = millis();
  } else if (LineTeacking_Read_Right) {
    right();
    while (LineTeacking_Read_Right && func_mode == LineTeacking) {
      getBTData();
      getIRData();
      delay(1);
    }
    LT_PreMillis = millis();
  } else if (LineTeacking_Read_Left) {
    left();
    while (LineTeacking_Read_Left && func_mode == LineTeacking) {
      getBTData();
      getIRData();
      delay(1);
    }
    LT_PreMillis = millis();
  } else if (millis() - LT_PreMillis > 150) {
    stop();
  }
}

void obstacles_avoidance_mode() {
  if (func_mode != ObstaclesAvoidance) return;

  servo.write(90);
  if (!responsiveDelay(500, ObstaclesAvoidance)) return;
  middleDistance = getDistance();

  if (middleDistance > 40) {
    forward();
    return;
  }

  stop();
  if (!responsiveDelay(500, ObstaclesAvoidance)) return;

  servo.write(10);
  if (!responsiveDelay(500, ObstaclesAvoidance)) return;
  rightDistance = getDistance();
  if (!responsiveDelay(500, ObstaclesAvoidance)) return;

  servo.write(90);
  if (!responsiveDelay(500, ObstaclesAvoidance)) return;
  servo.write(170);
  if (!responsiveDelay(500, ObstaclesAvoidance)) return;
  leftDistance = getDistance();
  if (!responsiveDelay(500, ObstaclesAvoidance)) return;

  servo.write(90);
  if (!responsiveDelay(500, ObstaclesAvoidance)) return;

  if (rightDistance > leftDistance) {
    stop();
    if (!responsiveDelay(100, ObstaclesAvoidance)) return;
    back();
    if (!responsiveDelay(200, ObstaclesAvoidance)) return;
    right();
    responsiveDelay(300, ObstaclesAvoidance);
  } else if (rightDistance < leftDistance) {
    stop();
    if (!responsiveDelay(100, ObstaclesAvoidance)) return;
    back();
    if (!responsiveDelay(200, ObstaclesAvoidance)) return;
    left();
    responsiveDelay(300, ObstaclesAvoidance);
  } else if (rightDistance <= 40 || leftDistance <= 40) {
    back();
    responsiveDelay(180, ObstaclesAvoidance);
  } else {
    forward();
  }
}


// =========================
// SETUP / MAIN LOOP
// =========================

void setup() {
  Serial.begin(9600);

  servo.attach(3, 500, 2400);
  servo.write(90);
  IrReceiver.begin(Receiver, ENABLE_LED_FEEDBACK);

  pinMode(ECHO_PIN, INPUT);
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(IN1, OUTPUT);
  pinMode(IN2, OUTPUT);
  pinMode(IN3, OUTPUT);
  pinMode(IN4, OUTPUT);
  pinMode(ENA, OUTPUT);
  pinMode(ENB, OUTPUT);
  pinMode(LineTeacking_Pin_Right, INPUT);
  pinMode(LineTeacking_Pin_Middle, INPUT);
  pinMode(LineTeacking_Pin_Left, INPUT);

  stop();
  Serial.println("Smart Car Ready");
  Serial.println("CAPS:AI_RECOVERY_V2_DISTANCE");
  Serial.println("CAPS:AI_SIDE_SCAN_V1");
  Serial.println("CAPS:AI_TEST_TURN_120MS");
  Serial.println("CAPS:PRESENTATION_V1");
  Serial.println(F("CAPS:ROUTE_V1_20CM"));
  Serial.println(F("CAPS:MANUAL_ADVISORY_V1"));
}

void loop() {
  getBTData();
  getIRData();

  bluetooth_mode();
  irremote_mode();
  line_teacking_mode();
  obstacles_avoidance_mode();
  ai_recovery_mode();
}


