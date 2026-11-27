/*  Valve Cart and Feed System: Controls - Valve Controls - Rework V 1.1
   Author(s):
   Matthew Ebert
   Logan Sewell
   JJ
   Joshua Piuti
   Organization: UVic Rocketry
*/
#include "Valve.h"
#include <Servo.h>

//*********Limit Pins*********
#define OpenLimitMEV 30
#define CloseLimitMEV 27
#define OpenLimitN2OV 41
#define CloseLimitN2OV 40
#define OpenLimitRTV 39
#define CloseLimitRTV 38
#define OpenLimitERV 34
#define CloseLimitERV 35
#define OpenLimitN2OF 33
#define CloseLimitN2OF 32
#define OpenLimitNCV 28
#define CloseLimitNCV 29
#define OpenLimitDUMP 22
#define CloseLimitDUMP 23
#define OpenLimitVENT 24
#define CloseLimitVENT 25

//VENT servo signal pin (PWM, driven via the Servo library)
#define VentServoPin 8
#define VentServoOpen 0
#define VentServoClose 90
//****************************

//******Enable Pins **********
#define N2OFEnab 46
#define N2OVEnab 47
#define ERVEnab 50
#define MEVEnab 51

//*********Driver Pins********
#define StepMEV 19
#define DirMEV  20
#define StepN2OV 4
#define DirN2OV  3
#define StepERV 15
#define DirERV  14
#define StepN2OF 17
#define DirN2OF  16
//****************************

//***Solenoid & Igniter Pins***
#define Igniter 45
#define Solenoid 48
#define RTVEnab 37
//****************************

//*********Safety Pins********
#define KeySwitch 5
#define EStop 6
//****************************


int IgniterState = 0;
int TargetState [10];

void MoveToTarget();
void setTarget (String valveCommands);
String readCSV(String * rxBuf);
void sendState (void);

bool ABORT = false;
//Assining Valves
//Stepper Valves
Valve N2OV = Valve(OpenLimitN2OV, CloseLimitN2OV, StepN2OV, DirN2OV, 4);
Valve N2OF  = Valve(OpenLimitN2OF, CloseLimitN2OF, StepN2OF, DirN2OF, 4);
Valve ERV  = Valve(OpenLimitERV, CloseLimitERV, StepERV, DirERV, 2);
Valve MEV  = Valve(OpenLimitMEV, CloseLimitMEV, StepMEV, DirMEV, 4);

Valve DUMP = Valve(OpenLimitDUMP, CloseLimitDUMP);
Valve VENT = Valve(OpenLimitVENT, CloseLimitVENT);
Servo ventServo;;

//Other Valves
Valve NCV  = Valve(OpenLimitNCV, CloseLimitNCV);
Valve RTV  = Valve(OpenLimitRTV, CloseLimitRTV, RTVEnab);

void setup() {
  // Sets Limit Pins
  //enable set up
  pinMode(N2OFEnab, OUTPUT);
  pinMode(N2OVEnab, OUTPUT);
  pinMode(ERVEnab, OUTPUT);
  pinMode(MEVEnab, OUTPUT);

  //Currently does not effect motors??
  digitalWrite(N2OFEnab, HIGH);
  digitalWrite(N2OVEnab, HIGH);
  digitalWrite(ERVEnab, HIGH);
  digitalWrite(MEVEnab, HIGH);

  //Unsure how these operate
  pinMode(Solenoid, OUTPUT);
  digitalWrite(Solenoid, LOW);
  pinMode(Igniter, OUTPUT);
  digitalWrite(Igniter, LOW);
  //********************

  //Sets up EStop and Keyswitch
  pinMode(EStop, INPUT_PULLUP);
  pinMode(KeySwitch, INPUT_PULLUP);


  //Serial_Startup
  Serial.begin(115200);
  //time in ms the serial blocks waiting for data
  Serial.setTimeout(10);
  //*********************************************

  Serial.println("VC,STATUS,STARTUP,SUCCESS");
  Serial.println("VC,CF,Initalizing_to_safe_state:(ALL Closed)");
  ventServo.attach(VentServoPin);
  ventServo.write(VentServoClose);  // safe state: closed
  sendState();
  delay(1000);
  //Set all target states to closed
  for(int i = 0; i<10; i++)
  {
    TargetState[i] = 1;
  }
}


void loop() {
  
  if (Serial.available() || digitalRead(EStop) == HIGH)
  {//if a message has been recieved
    //read all serial data
    String rxBuffer = Serial.readString();
    /*Should the buffer be cleared before entire message is sent/recieved
    a fragment will be created and not parsed. An end flag should be checked
    to ensure this case does not occur.*/
    String Command = "";
    String TYPE = "";
    String SOURCE = "";
    //read source of data
    SOURCE = readCSV(&rxBuffer);
    //read type of data
    TYPE = readCSV(&rxBuffer);
    //Send acknolodgement
    //CCSerial.println("VC,ACK,");
    /* Currently there is no error correcting or proper acknoledgement handling
    This ACK is for debugging only. Proper protocols should be developed to ensure
    a message is handled properly*/
    if (TYPE == "UNABORT" && ABORT)
    {
      Serial.println("VC,STATUS,CANCELLED ABORT");
      ABORT = false;
    } else if (TYPE == "ABORT" || ABORT == true || digitalRead(EStop) == HIGH)
    {
      ABORT = true;
      //Serial.println(TYPE);
      Serial.println("VC,STATUS,ABORTED");
      digitalWrite(Igniter, LOW);
      digitalWrite(Solenoid, LOW);

      //implement abort code
      TargetState[0] = 1;  //Close N2OF
      TargetState[1] = -1; //Open N2OV
      TargetState[2] = -1; //Open ERV
      TargetState[3] = 1;  //Close MEV
      TargetState[4] = 1;  //Close RTV
      TargetState[5] = 1;  //Close Igniter
      TargetState[6] = 1;  //Close NCV
      TargetState[7] = -1; //Open DUMP
      TargetState[8] = -1; //Open VENT
      
    } else if (TYPE == "CTRL")
    {
      //Serial.println(TYPE);
      setTarget(rxBuffer);
    } else if (TYPE == "CONNECT")
    {
      Serial.println("VC,STATUS,ESTABLISH");
      sendState();
    } else if (TYPE == "SUMMARY")
    { 
      //Serial.println(TYPE);
      sendState();
    } else
    {
      Serial.println("VC,ERROR,UNKNOWNCOMMAND," + SOURCE + "," + TYPE + "," + Command);
    }
  }

  //called every loop to step motors if necessary
  //disabled by keyswitch lockout on VC unless being aborted
  if(digitalRead(KeySwitch) == LOW || ABORT)  {
    MoveToTarget();
  }
}


/***********FUNCTIONS*******************/


void sendState (void)
{
  Serial.print("VC,SUMMARY");
  Serial.print(",N2OF," +  N2OF.strState());
  Serial.print(",N2OV," +  N2OV.strState());
  Serial.print(",RTV," +  RTV.solStrState());
  Serial.print(",ERV," +  ERV.strState());
  Serial.print(",MEV," +  MEV.strState());
  Serial.print(",NCV," +  NCV.strState());
  Serial.print(",DUMP," +  DUMP.strState());
  Serial.print(",VENT," +  VENT.strState());
  Serial.println("");
}


String readCSV (String * rxBuf)
{
  String Command = "";
  char c;
  c = (*rxBuf)[0];
  while (c != '\n' && c !=  ',' && (*rxBuf).length() > 1) {
    Command += c;
    (*rxBuf).remove(0, 1);
    c = (*rxBuf)[0];
  }
  (*rxBuf).remove(0, 1);
  return Command;
}


void setTarget (String valveCommands)
{
  String Label = "";
  String Value = "";
  int index = 0;
  while (valveCommands.length() > 1)
  {
    Label = readCSV(&valveCommands);
    Value = readCSV(&valveCommands);


    if (Label == "N2OF")
    {
    } else   if (Label ==  "N2OV")
    {
      index = 1;
    } else if (Label ==  "ERV")
    {
      index = 2;
    } else if (Label ==  "MEV")
    {
      index = 3;
    } else if (Label ==  "RTV")
    {
      index = 4;
    } else if (Label ==  "IGPRIME")
    {
      index = 5;
    } else if (Label == "NCV")
    {
      index = 6;
    } else if (Label == "DUMP")
    {
      index = 7;
    } else if (Label == "VENT")
    {
      index = 8;
    } else
    {
      index = 9;
      Serial.println("VC,ERROR,UNKOWNVALVE");
    }

    if (Value == "OPEN")
    {
      TargetState[index] = -1;
    }
    else if (Value == "CLOSE")
    {
      TargetState[index] = 1;
    } else
    {
      TargetState[index] = 0;
    }

  }
}





void MoveToTarget()
{

  if ( N2OF.state() != TargetState[0] && TargetState[0]!=0)
  {    
    N2OF.moveStep(TargetState[0]);
  }
  if ( N2OF.getChange())
  {
    sendState();
  }

  if ( N2OV.state() != TargetState[1] && TargetState[1]!=0)
  {
   N2OV.moveStep(-TargetState[1]);
  }
  if ( N2OV.getChange())
  {
    sendState();
  }

  if ( ERV.state() != TargetState[2] && TargetState[2]!=0)
  {
    ERV.moveStep(-TargetState[2]);
  }
  if ( ERV.getChange())
  {
    sendState();
  }

  if ( MEV.state() != TargetState[3] && TargetState[3]!=0)
  {
    MEV.moveStep(-TargetState[3]);
  }
  if ( MEV.getChange())
  {
    sendState();
  }

  if (RTV.solState() != TargetState[4] && TargetState[4]!=0)
  {
    RTV.moveSol(TargetState[4]);
    sendState();
  }

  if (IgniterState != TargetState[5] && TargetState[5]!=0)
  {
    if (TargetState[5] == 1)
    {
      digitalWrite(Igniter, LOW);
      IgniterState = 1;
    } else if (TargetState[5] == -1) {
      digitalWrite(Igniter, HIGH);
      IgniterState = -1;
    }
  }

  if (NCV.state() != TargetState[6] && TargetState[6]!=0)
  {
    if (TargetState[6] == 1)
    {
      digitalWrite(Solenoid, LOW);
    } else if (TargetState[6] == -1)
    {
      digitalWrite(Solenoid, HIGH);
    }
  }
  if (NCV.getChange())
  {
    sendState();
  }

  if (DUMP.state() != TargetState[7] && TargetState[7]!=0)
  {
    DUMP.moveStep(-TargetState[7]);
  }
  if (DUMP.getChange())
  {
    sendState();
  }

  if (VENT.state() != TargetState[8] && TargetState[8]!=0)
  {
    // VENT is a PWM servo, not a stepper -- command the angle directly.
    if (TargetState[8] == 1)  ventServo.write(VentServoClose);
    if (TargetState[8] == -1) ventServo.write(VentServoOpen);
  }
  if (VENT.getChange())
  {
    sendState();
  }
}