/*  Test.ino - Manual serial command tester for Controls.ino protocol
 *
 *  PURPOSE:
 *    Lets a developer test individual valves by typing commands into the
 *    Arduino Serial Monitor. It speaks the same MCC/VC protocol as the
 *    Python server (serial_controller.py), so responses here should match
 *    exactly what the server would see on live hardware.
 *
 *  HOW TO USE:
 *    1. Flash THIS file (not Controls.ino) to the Arduino Mega.
 *    2. Open Serial Monitor: Tools -> Serial Monitor, 115200 baud, Newline.
 *    3. Type a command and press Enter. The board responds immediately.
 *
 *  SUPPORTED COMMANDS:
 *    MCC,CTRL,<VALVE>,<ACTION>   -- move one valve
 *    MCC,SUMMARY                 -- read and print all limit switch states
 *
 *  VALVE IDs:  N2OV, N2OF, ERV, MEV, RTV, NCV, IGPRIME
 *  ACTIONS:    OPEN, CLOSE, FILL (RTV only), VENT (RTV only)
 *
 *  SAFETY NOTE:
 *    IGPRIME fires the igniter output. Only test with the igniter
 *    physically disconnected or replaced with a LED/resistor load.
 *
 *  Authors: UVic Rocketry
 */

// ---------------------------------------------------------------------------
// Valve.h defines the Valve class: constructors, moveStep(), moveSol(),
// strState(), solStrState(), getChange(), state(), solState().
// Including it here makes all of those available in this file.
// ---------------------------------------------------------------------------
#include "Valve.h"


// ===========================================================================
// PIN DEFINITIONS
// These #defines are identical to Controls.ino so both files stay in sync.
// Changing a physical wire means updating the number here AND in Controls.ino.
// #define does no memory allocation -- the preprocessor substitutes the number
// everywhere the name appears before the compiler ever sees the file.
// ===========================================================================

// --- Limit switch input pins (active-low INPUT_PULLUP on each) ---
// Each stepper valve has two limit switches: one at the open end, one closed.
#define OpenLimitN2OV  41
#define CloseLimitN2OV 40
#define OpenLimitRTV   39   // RTV is a solenoid -- limit pins kept for Valve()
#define CloseLimitRTV  38
#define OpenLimitERV   34
#define CloseLimitERV  35
#define OpenLimitN2OF  33
#define CloseLimitN2OF 32
#define OpenLimitMEV   30
#define CloseLimitMEV  27
#define OpenLimitNCV   28
#define CloseLimitNCV  29

// --- Stepper driver enable pins ---
// Setting these HIGH disables the driver (motors de-energised).
// Setting LOW enables it. Currently driven HIGH in Controls.ino setup()
// but noted as "does not effect motors" -- may not be wired to drivers.
#define N2OFEnab 46
#define N2OVEnab 47
#define ERVEnab  50
#define MEVEnab  51

// --- Stepper driver Step/Dir pins ---
// Step: a rising edge on this pin advances the motor one microstep.
// Dir:  HIGH vs LOW sets the rotation direction (open vs close).
#define StepMEV  19
#define DirMEV   20
#define StepN2OV  4
#define DirN2OV   3
#define StepERV  15
#define DirERV   14
#define StepN2OF 17
#define DirN2OF  16

// --- Solenoid and igniter digital output pins ---
#define Igniter  45   // HIGH = igniter on (DANGER -- disconnect before testing)
#define Solenoid 48   // NCV is driven by this pin
#define RTVEnab  37   // RTV 3-way solenoid enable


// ===========================================================================
// VALVE OBJECTS
// Each Valve is an instance of the Valve class defined in Valve.h.
// The constructor arguments tell the object which pins it owns.
//
// Stepper valve constructor: Valve(openPin, closePin, stepPin, dirPin, rate)
//   rate = step rate multiplier used by moveStep() inside Valve.cpp.
//
// Limit-switch-only constructor: Valve(openPin, closePin)
//   NCV has no motor -- the Solenoid pin is driven directly below.
//
// Solenoid constructor: Valve(openPin, closePin, enablePin)
//   RTV is a 3-way solenoid tracked in software by moveSol().
// ===========================================================================
Valve N2OV = Valve(OpenLimitN2OV, CloseLimitN2OV, StepN2OV, DirN2OV, 4);
Valve N2OF = Valve(OpenLimitN2OF, CloseLimitN2OF, StepN2OF, DirN2OF, 4);
Valve ERV  = Valve(OpenLimitERV,  CloseLimitERV,  StepERV,  DirERV,  2);
Valve MEV  = Valve(OpenLimitMEV,  CloseLimitMEV,  StepMEV,  DirMEV,  4);
Valve NCV  = Valve(OpenLimitNCV,  CloseLimitNCV);            // limit-switch only
Valve RTV  = Valve(OpenLimitRTV,  CloseLimitRTV,  RTVEnab);  // solenoid


// ===========================================================================
// TARGET STATE VARIABLES
// driveValves() compares each valve's current hardware state to its target
// and steps it toward the target on every loop() call.
//
// Values:  0 = idle (do nothing)
//          1 = move toward CLOSED / FILL
//         -1 = move toward OPEN / VENT
//
// Using separate variables per valve (instead of an array) makes the code
// easier to read while testing -- you can see exactly which valve is targeted.
// Controls.ino uses TargetState[8] for the same purpose.
// ===========================================================================
int TargetN2OV = 0;
int TargetN2OF = 0;
int TargetERV  = 0;
int TargetMEV  = 0;
int TargetRTV  = 0;   // RTV: 1 = FILL position, -1 = VENT position
int TargetNCV  = 0;   // NCV solenoid: 1 = off (closed), -1 = on (open)
int TargetIG   = 0;   // Igniter: 1 = off (safe), -1 = on (FIRE -- careful!)


// ===========================================================================
// FUNCTION DECLARATIONS (prototypes)
// C++ requires that a function is declared before it is called. Putting the
// full definitions after loop() is cleaner, but we need to tell the compiler
// the function exists. These one-liners do that.
// ===========================================================================
void handleSerial();      // read and parse one line from Serial
void setOneTarget(String body);  // parse "VALVE,ACTION" and set its target
void driveValves();       // step any valve that hasn't reached its target
void reportState();       // print VC,SUMMARY with all current limit-switch states


// ===========================================================================
// setup() -- runs ONCE when the board powers on or resets
// ===========================================================================
void setup() {
  // Open the USB serial port at 115200 baud (must match the Serial Monitor).
  Serial.begin(115200);

  // If readStringUntil() is waiting for a newline and none arrives, give up
  // after 100 ms so loop() doesn't freeze waiting for more bytes.
  Serial.setTimeout(100);

  // Print the current valve states immediately so you know the starting
  // condition before you send any commands.
  reportState();

  // Remind the developer what commands are available.
  Serial.println("TEST,READY,commands: MCC,CTRL,<VALVE>,<OPEN|CLOSE|FILL|VENT>  MCC,SUMMARY");
}


// ===========================================================================
// loop() -- called repeatedly by the Arduino runtime, forever
// Split into two helper functions so each one has a single responsibility:
//   handleSerial() -- input / command parsing
//   driveValves()  -- output / motor control
// ===========================================================================
void loop() {
  handleSerial();  // check for and process any new serial command
  driveValves();   // move any valve that still hasn't reached its target
}


// ===========================================================================
// handleSerial()
// Checks whether a full line has arrived over USB. If yes, parses it as a
// MCC,TYPE,[BODY] command and dispatches to the right handler.
// If the line is malformed or the source is not MCC, an error is printed.
// ===========================================================================
void handleSerial() {
  // Serial.available() returns the number of unread bytes in the receive
  // buffer. Zero means nothing to do this cycle, so return early.
  if (!Serial.available()) return;

  // Read bytes until a newline character is received (the Enter key in
  // Serial Monitor). The newline itself is NOT included in the returned String.
  String line = Serial.readStringUntil('\n');

  // trim() removes leading/trailing whitespace and carriage returns (\r).
  // Serial Monitor on Windows often appends \r before \n.
  line.trim();

  // Ignore completely empty lines (e.g. a bare Enter press).
  if (line.length() == 0) return;

  // --- Parse SOURCE token (everything before the first comma) ---
  // indexOf(',') returns the index of the first comma, or -1 if not found.
  int c1 = line.indexOf(',');
  if (c1 < 0) {
    Serial.println("TEST,ERR,no comma -- expected MCC,TYPE,...");
    return;
  }

  // substring(0, c1) copies characters from index 0 up to (not including) c1.
  // For "MCC,CTRL,N2OV,OPEN" this gives "MCC".
  if (line.substring(0, c1) != "MCC") {
    Serial.println("TEST,ERR,source must be MCC");
    return;
  }

  // --- Parse TYPE token (between the first and second comma) ---
  // rest = everything after the first comma: "CTRL,N2OV,OPEN"
  String rest = line.substring(c1 + 1);  // +1 to skip the comma itself
  int c2 = rest.indexOf(',');

  // If there is no second comma, the entire remainder is the TYPE (e.g. "SUMMARY").
  // The ternary operator (condition ? valueIfTrue : valueIfFalse) handles both cases.
  String type = (c2 < 0) ? rest : rest.substring(0, c2);
  String body = (c2 < 0) ? ""   : rest.substring(c2 + 1);

  // --- Dispatch to the correct handler ---
  if (type == "SUMMARY") {
    // No body needed -- just read and report current limit switch states.
    reportState();

  } else if (type == "CTRL") {
    // body = "VALVE,ACTION" -- pass to setOneTarget() for further parsing.
    setOneTarget(body);

  } else {
    // Anything else is an unrecognized command type.
    // Echo back the VC,ERROR format that Controls.ino would send so responses
    // look identical whether you're testing here or against the real firmware.
    Serial.println("VC,ERROR,UNKNOWNCOMMAND,MCC," + type);
  }
}


// ===========================================================================
// setOneTarget(body)
// Parses "VALVE,ACTION" from a CTRL command and sets ONLY that valve's target.
// Every other valve target stays at its current value -- nothing else moves.
//
// dir encoding:
//   OPEN or VENT  -> dir = -1  (stepper moves toward open limit switch)
//   CLOSE or FILL -> dir =  1  (stepper moves toward close limit switch)
// ===========================================================================
void setOneTarget(String body) {
  // Find the comma separating valve name from action.
  int comma = body.indexOf(',');
  if (comma < 0) {
    Serial.println("TEST,ERR,CTRL needs VALVE,ACTION e.g. N2OV,OPEN");
    return;
  }

  // Split body into its two tokens.
  String valve  = body.substring(0, comma);
  String action = body.substring(comma + 1);

  // Clean up any stray whitespace that might have crept in.
  valve.trim();
  action.trim();

  // Convert the human-readable action to the numeric direction the Valve
  // class and digitalWrite calls expect.
  int dir = 0;
  if      (action == "OPEN" || action == "VENT")  dir = -1;
  else if (action == "CLOSE" || action == "FILL") dir =  1;
  else {
    Serial.println("TEST,ERR,unknown action '" + action + "' -- use OPEN/CLOSE/FILL/VENT");
    return;
  }

  // Map the valve label to its target variable.
  // Only the matched valve is changed; every other target is untouched.
  if      (valve == "N2OV")   { TargetN2OV = dir; }
  else if (valve == "N2OF")   { TargetN2OF = dir; }
  else if (valve == "ERV")    { TargetERV  = dir; }
  else if (valve == "MEV")    { TargetMEV  = dir; }
  else if (valve == "RTV")    { TargetRTV  = dir; }
  else if (valve == "NCV")    { TargetNCV  = dir; }
  else if (valve == "IGPRIME"){ TargetIG   = dir; }
  else {
    // Mirror the exact error string Controls.ino sends so test output is
    // indistinguishable from real firmware output.
    Serial.println("VC,ERROR,UNKOWNVALVE");
    return;
  }

  // Confirm the target was accepted. This line is TEST-only output; Controls.ino
  // does not send this, it just silently updates TargetState[].
  Serial.println("TEST,OK,target set: " + valve + " -> " + action);
}


// ===========================================================================
// driveValves()
// Called every loop(). For each valve, checks whether its current hardware
// state matches its target. If not, nudges it one step closer.
//
// moveStep(dir) is defined in Valve.cpp. It pulses the Step pin once in the
// given direction and updates the object's internal state by reading the limit
// switches. When a limit switch trips, getChange() returns true for one cycle.
//
// Direction inversion note:
//   N2OV, ERV, MEV use moveStep(-Target) instead of moveStep(Target).
//   This is because those valves are physically wired with the stepper
//   direction reversed relative to the limit-switch convention. The same
//   inversion exists in Controls.ino MoveToTarget(). If a valve runs the
//   wrong way on your bench, flip the sign for that valve here.
//
// anyChange tracks whether any valve reached a limit switch this cycle.
// If so, reportState() broadcasts a fresh VC,SUMMARY so you can see the
// transition in the Serial Monitor without having to send MCC,SUMMARY manually.
// ===========================================================================
void driveValves() {
  bool anyChange = false;  // becomes true if any valve hits a limit switch

  // --- N2OF ---
  // state() returns the current hardware state (-1 open, 1 closed, 0 in transit).
  // We only call moveStep if the valve hasn't reached its target AND a target
  // has actually been set (target != 0 means "move", 0 means "idle").
  if (N2OF.state() != TargetN2OF && TargetN2OF != 0) {
    N2OF.moveStep(TargetN2OF);
  }
  if (N2OF.getChange()) anyChange = true;  // limit switch just tripped

  // --- N2OV (direction inverted -- see note above) ---
  if (N2OV.state() != TargetN2OV && TargetN2OV != 0) {
    N2OV.moveStep(-TargetN2OV);
  }
  if (N2OV.getChange()) anyChange = true;

  // --- ERV (direction inverted) ---
  if (ERV.state() != TargetERV && TargetERV != 0) {
    ERV.moveStep(-TargetERV);
  }
  if (ERV.getChange()) anyChange = true;

  // --- MEV (direction inverted) ---
  if (MEV.state() != TargetMEV && TargetMEV != 0) {
    MEV.moveStep(-TargetMEV);
  }
  if (MEV.getChange()) anyChange = true;

  // --- RTV (solenoid, not stepper) ---
  // solState() returns the software-tracked solenoid state (1 = FILL, -1 = VENT).
  // moveSol() energises or de-energises the solenoid and updates the internal state.
  if (TargetRTV != 0 && RTV.solState() != TargetRTV) {
    RTV.moveSol(TargetRTV);
    anyChange = true;  // solenoid has no limit switch; mark change immediately
  }

  // --- NCV (driven directly via the Solenoid pin, not through Valve.moveStep) ---
  // NCV has no stepper; it's just a solenoid on pin 48.
  // HIGH energises it (open), LOW de-energises (closed).
  if (TargetNCV != 0) {
    if (TargetNCV == 1)  digitalWrite(Solenoid, LOW);   // close NCV
    if (TargetNCV == -1) digitalWrite(Solenoid, HIGH);  // open NCV
  }
  if (NCV.getChange()) anyChange = true;  // NCV limit switches still read

  // --- Igniter ---
  // !! DANGER: HIGH fires the igniter output !!
  // Only test this with the igniter physically disconnected or replaced
  // with a resistor/LED substitute.
  if (TargetIG != 0) {
    if (TargetIG == 1)  digitalWrite(Igniter, LOW);   // igniter safe/off
    if (TargetIG == -1) digitalWrite(Igniter, HIGH);  // igniter on
  }

  // If any valve reached a limit switch this cycle, print a fresh summary
  // so the state change is visible in the Serial Monitor without extra input.
  if (anyChange) reportState();
}


// ===========================================================================
// reportState()
// Reads all valve limit switches and prints a VC,SUMMARY line.
// This is identical in format to what Controls.ino sendState() emits, so the
// Python server (serial_controller.py) can parse it the same way on real hardware.
//
// Serial.print() sends text without a newline; Serial.println() adds one.
// Building the line in pieces avoids allocating one large String in memory,
// which matters on the Mega's limited 8 KB of SRAM.
// ===========================================================================
void reportState() {
  Serial.print("VC,SUMMARY");
  Serial.print(",N2OF," + N2OF.strState());     // "OPEN", "CLOSE", or "TRANSIT"
  Serial.print(",N2OV," + N2OV.strState());
  Serial.print(",RTV,"  + RTV.solStrState());   // "FILLING", "VENTING", or "TRANSIT"
  Serial.print(",ERV,"  + ERV.strState());
  Serial.print(",MEV,"  + MEV.strState());
  Serial.print(",NCV,"  + NCV.strState());
  Serial.println("");   // terminate the line
}
