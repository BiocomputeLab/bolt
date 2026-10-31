/*
  B.O.L.T. - Budget Optogenetic Light Tool
  Arduino Uno firmware

  Drives 4 LED strip channels, each on its own pin (default D3, D9, D10,
  D11). Each channel can be:
    - on all the time ("continuous"), or
    - flashing on and off repeatedly ("pulsed")
  at a chosen brightness (0-255), for a chosen length of time.

  The laptop GUI sends simple text commands over USB to configure and
  start/stop the experiment. All the actual timing (when to flash, when
  to stop) happens on the Arduino itself, using its own clock (millis()),
  so it keeps running correctly even if the USB connection is slow or
  briefly interrupted.

  ---------------------------------------------------------------------------
  COMMANDS THE GUI SENDS (one per line, comma-separated):

    PIN,<channel>,<pin>
        Which Arduino pin a channel uses. channel = 1-4.

    CFG,<channel>,<on?>,<brightness>,<mode>,<onSeconds>,<offSeconds>
        Sets up one channel.
        on?         = 1 to use this channel, 0 to leave it off
        brightness  = 0-255 (how bright it is while lit)
        mode        = 0 for continuous, 1 for pulsed
        onSeconds   = how long each flash lasts (pulsed mode only)
        offSeconds  = how long the dark gap between flashes lasts (pulsed
                      mode only)

    DUR,<sameForAll?>,<seconds>
        How long the experiment runs.
        sameForAll? = 1: every channel uses <seconds>.
        sameForAll? = 0: each channel's own duration is set separately
                         with CHDUR instead.
        seconds = 0 means "keep going until STOP is sent."

    CHDUR,<channel>,<seconds>
        One channel's own duration (only used when DUR's sameForAll? = 0).

    DELAY,<on?>,<seconds>
        on? = 1: wait <seconds> after RUN before any light turns on, so
        every channel starts in sync.

    RUN
        Starts the experiment using whatever was last configured.

    STOP
        Turns every channel off immediately and ends the experiment.

  WHAT THE ARDUINO SENDS BACK:
    OK       - the last command was understood and applied
    ERR,...  - the last command could not be understood
    STARTED  - the experiment has begun (lights are now on)
    DONE     - every channel has finished its duration on its own
    STOPPED  - STOP was received and everything has turned off
  ---------------------------------------------------------------------------
*/

#define NUM_CHANNELS 4
uint8_t pwmPins[NUM_CHANNELS] = {3, 9, 10, 11};   // can be changed by the PIN command

// One of these per LED channel -- everything needed to run it.
struct Channel {
  bool enabled = false;
  uint8_t intensity = 255;      // brightness, 0-255
  uint8_t mode = 0;              // 0 = continuous, 1 = pulsed
  unsigned long onTime = 0;      // flash length, in milliseconds
  unsigned long offTime = 0;     // dark gap length, in milliseconds
  unsigned long duration = 0;    // how long this channel runs for, in ms (0 = forever)
  bool isOn = false;             // is it lit right now?
  unsigned long phaseStart = 0;  // when the current on/off phase began (ms)
  bool finished = false;         // has this channel's duration run out?
};

Channel channels[NUM_CHANNELS];

bool useGlobalDuration = true;     // true: all channels share globalDuration
unsigned long globalDuration = 0;  // shared duration, in ms

bool delayEnabled = false;
unsigned long startDelayMs = 0;

bool running = false;             // is an experiment currently in progress?
bool waitingForDelay = false;     // true during the countdown before lights turn on
unsigned long runReceivedAt = 0;  // when RUN was received (used to time the delay)
unsigned long experimentStart = 0; // when the lights actually turned on

String inputLine = "";  // text received over serial so far, until the next newline

void setup() {
  Serial.begin(9600);
  for (uint8_t i = 0; i < NUM_CHANNELS; i++) {
    pinMode(pwmPins[i], OUTPUT);
    analogWrite(pwmPins[i], 0);   // make sure every LED starts off
  }
}

void loop() {
  readSerial();

  // Counting down a delayed start?
  if (waitingForDelay) {
    if (millis() - runReceivedAt >= startDelayMs) {
      beginExperiment();
    }
  }

  // Experiment in progress -- keep every channel's flashing up to date.
  if (running) {
    updateChannels();
    checkAllFinished();
  }
}

// Reads whatever has arrived over USB, one line at a time, and hands
// each complete line to handleCommand().
void readSerial() {
  while (Serial.available() > 0) {
    char c = Serial.read();
    if (c == '\n') {
      inputLine.trim();
      if (inputLine.length() > 0) {
        handleCommand(inputLine);
      }
      inputLine = "";
    } else if (c != '\r') {
      inputLine += c;
    }
  }
}

// Breaks "a,b,c" into ["a", "b", "c"].
int splitFields(const String &line, String fields[], int maxFields) {
  int count = 0;
  int start = 0;
  while (count < maxFields) {
    int idx = line.indexOf(',', start);
    if (idx == -1) {
      fields[count++] = line.substring(start);
      break;
    } else {
      fields[count++] = line.substring(start, idx);
      start = idx + 1;
    }
  }
  return count;
}

// Turns a 1-4 channel number from the GUI into a valid array index, or
// -1 if it's out of range.
int parseChannel(const String &field) {
  int ch = field.toInt() - 1;
  return (ch < 0 || ch >= NUM_CHANNELS) ? -1 : ch;
}

// Works out which command a line is, and does what it asks.
void handleCommand(String line) {
  String fields[8];
  int n = splitFields(line, fields, 8);
  String cmd = fields[0];
  cmd.toUpperCase();

  if (cmd == "PIN" && n >= 3) {
    int ch = parseChannel(fields[1]);
    if (ch < 0) { Serial.println("ERR,bad_channel"); return; }
    pwmPins[ch] = (uint8_t) fields[2].toInt();
    pinMode(pwmPins[ch], OUTPUT);
    analogWrite(pwmPins[ch], 0);
    Serial.println("OK");
  }
  else if (cmd == "CFG" && n >= 6) {
    int ch = parseChannel(fields[1]);
    if (ch < 0) { Serial.println("ERR,bad_channel"); return; }
    channels[ch].enabled   = fields[2].toInt() != 0;
    channels[ch].intensity = (uint8_t) constrain(fields[3].toInt(), 0, 255);
    channels[ch].mode      = (uint8_t) fields[4].toInt();
    // The seconds value must be multiplied by 1000 BEFORE converting to a
    // whole number, not after -- otherwise anything under 1 second (e.g.
    // 0.25s) gets rounded down to 0 before it's scaled up, and is lost.
    channels[ch].onTime    = (unsigned long) (fields[5].toFloat() * 1000.0);
    channels[ch].offTime   = (unsigned long) (fields[6].toFloat() * 1000.0);
    Serial.println("OK");
  }
  else if (cmd == "DUR" && n >= 3) {
    useGlobalDuration = fields[1].toInt() != 0;
    globalDuration = (unsigned long) (fields[2].toFloat() * 1000.0);
    Serial.println("OK");
  }
  else if (cmd == "CHDUR" && n >= 3) {
    int ch = parseChannel(fields[1]);
    if (ch < 0) { Serial.println("ERR,bad_channel"); return; }
    channels[ch].duration = (unsigned long) (fields[2].toFloat() * 1000.0);
    Serial.println("OK");
  }
  else if (cmd == "DELAY" && n >= 3) {
    delayEnabled = fields[1].toInt() != 0;
    startDelayMs = (unsigned long) (fields[2].toFloat() * 1000.0);
    Serial.println("OK");
  }
  else if (cmd == "RUN") {
    startRun();
    Serial.println("OK");
  }
  else if (cmd == "STOP") {
    stopAll();
    Serial.println("STOPPED");
  }
  else {
    Serial.println("ERR,unknown_command");
  }
}

// Gets everything ready for a new experiment, then either starts
// straight away or waits out a delayed start first.
void startRun() {
  if (useGlobalDuration) {
    for (uint8_t i = 0; i < NUM_CHANNELS; i++) {
      channels[i].duration = globalDuration;
    }
  }
  for (uint8_t i = 0; i < NUM_CHANNELS; i++) {
    channels[i].finished = false;
    channels[i].isOn = false;
    analogWrite(pwmPins[i], 0);
  }

  if (delayEnabled && startDelayMs > 0) {
    waitingForDelay = true;
    runReceivedAt = millis();
  } else {
    beginExperiment();
  }
}

// Actually turns the lights on and starts the clock.
void beginExperiment() {
  waitingForDelay = false;
  running = true;
  experimentStart = millis();
  for (uint8_t i = 0; i < NUM_CHANNELS; i++) {
    if (!channels[i].enabled) continue;
    channels[i].phaseStart = experimentStart;
    channels[i].isOn = true;
    analogWrite(pwmPins[i], channels[i].intensity);   // continuous channels just stay lit from here
  }
  Serial.println("STARTED");
}

// Called constantly while running: turns each pulsed channel's LED on or
// off right when it's due, and stops a channel once its duration is up.
void updateChannels() {
  unsigned long now = millis();
  for (uint8_t i = 0; i < NUM_CHANNELS; i++) {
    Channel &c = channels[i];
    if (!c.enabled || c.finished) continue;

    // duration == 0 means "never stop on its own"
    if (c.duration > 0 && (now - experimentStart) >= c.duration) {
      analogWrite(pwmPins[i], 0);
      c.finished = true;
      continue;
    }

    if (c.mode == 1) { // pulsed -- flip the LED on/off at the right moments
      unsigned long elapsed = now - c.phaseStart;
      if (c.isOn && elapsed >= c.onTime) {
        analogWrite(pwmPins[i], 0);
        c.isOn = false;
        c.phaseStart = now;
      } else if (!c.isOn && elapsed >= c.offTime) {
        analogWrite(pwmPins[i], c.intensity);
        c.isOn = true;
        c.phaseStart = now;
      }
    }
    // mode == 0 (continuous): nothing to do, it just stays lit
  }
}

// Ends the experiment automatically once every channel with a real
// duration has finished (channels left running forever don't count).
void checkAllFinished() {
  bool anyActive = false;
  bool anyHasDuration = false;
  for (uint8_t i = 0; i < NUM_CHANNELS; i++) {
    if (!channels[i].enabled) continue;
    if (!channels[i].finished) anyActive = true;
    if (channels[i].duration > 0) anyHasDuration = true;
  }
  if (!anyActive && anyHasDuration && running) {
    running = false;
    Serial.println("DONE");
  }
}

// Turns everything off right away (used by STOP).
void stopAll() {
  running = false;
  waitingForDelay = false;
  for (uint8_t i = 0; i < NUM_CHANNELS; i++) {
    analogWrite(pwmPins[i], 0);
    channels[i].isOn = false;
    channels[i].finished = true;
  }
}
