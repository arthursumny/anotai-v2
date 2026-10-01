/**
 * AnotAI Lapela — firmware para Seeed Studio XIAO BLE Sense (nRF52840)
 *
 * GATT Server BLE com serviço customizado de áudio:
 *  - AUDIO_DATA (NOTIFY): chunks PCM16LE mono 16 kHz fragmentados.
 *  - STATE     (READ/NOTIFY): 0=IDLE, 1=RECORDING, 2=SENDING.
 *
 * Controle por botão físico (ativo em LOW, pull-up interno):
 *  - 1 clique ......... inicia a gravação (LED aceso).
 *  - novo clique ...... encerra, transmite o pacote END e volta a IDLE.
 *
 * Formato do pacote AUDIO_DATA (little-endian):
 *  [0..1] seq uint16  — numeração sequencial (detecta perda/fora de ordem)
 *  [2]    flags uint8 — bit0 LAST (1 = último pacote da gravação)
 *  [3]    reservado
 *  [4..N] payload PCM16LE (até PACKET_PAYLOAD bytes)
 *
 * Pinos (núcleo Arduino Seeed nRF52):
 *  - Botão externo .... D2 (GND quando pressionado). Sem botão na placa!
 *  - LED .............. LED_BUILTIN (padrões de blink, ver setLed()).
 */

#include <Arduino.h>
#include <ArduinoBLE.h>
#include <PDM.h>

// ---------------------------------------------------------------- UUIDs ---
static const char *ANOTAI_AUDIO_SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b";
static const char *AUDIO_DATA_CHAR_UUID      = "4fafc202-1fb5-459e-8fcc-c5c9c331914b";
static const char *STATE_CHAR_UUID           = "4fafc203-1fb5-459e-8fcc-c5c9c331914b";
static const char *DEVICE_NAME               = "AnotAI-Lapela";

// ------------------------------------------------------------- hardware ---
constexpr uint8_t  BUTTON_PIN      = 2;     // D2, botão externo ativo em LOW
constexpr uint32_t DEBOUNCE_MS     = 50;
constexpr uint32_t DOUBLE_CLICK_MS = 400;

constexpr int      SAMPLE_RATE_HZ  = 16000; // 16 kHz, 16-bit PCM mono
constexpr int      PDM_GAIN        = 20;
constexpr size_t   PACKET_PAYLOAD  = 200;   // bytes de PCM por pacote
constexpr size_t   PACKET_HEADER   = 4;
constexpr size_t   PACKET_MAX      = PACKET_HEADER + PACKET_PAYLOAD;

// Estado anunciado na característica STATE
enum class LapelState : uint8_t { IDLE = 0, RECORDING = 1, SENDING = 2 };

// ------------------------------------------------------------ BLE GATT ---
BLEService        audioService(ANOTAI_AUDIO_SERVICE_UUID);
BLECharacteristic audioDataChar(AUDIO_DATA_CHAR_UUID, BLENotify, PACKET_MAX);
BLECharacteristic stateChar(STATE_CHAR_UUID, BLERead | BLENotify, 1);

// -------------------------------------------------------------- globals ---
static volatile bool     gPdmReady    = false;
static int16_t           gPdmBuffer[2048];
static volatile int      gPdmSamples  = 0;

static LapelState        gState       = LapelState::IDLE;
static uint16_t          gSeq         = 0;
static uint8_t           gPacket[PACKET_MAX];

static bool              gLastBtn     = HIGH;
static uint32_t          gLastChange  = 0;
static uint32_t          gLastClick   = 0;

// ---------------------------------------------------------- PDM callback ---
void onPDMdata() {
  int available = PDM.available();
  if (available <= 0 || available > (int)sizeof(gPdmBuffer)) return;
  PDM.read(gPdmBuffer, available);
  gPdmSamples = available / (int)sizeof(int16_t);
  gPdmReady = true;
}

// ------------------------------------------------------------------ LED ---
// LED_BUILTIN no XIAO BLE Sense. Padrões: IDLE = blink lento,
// RECORDING = aceso, SENDING = blink rápido.
void setLed(LapelState st) {
  static uint32_t last = 0;
  static bool on = false;
  uint32_t now = millis();
  switch (st) {
    case LapelState::RECORDING:
      digitalWrite(LED_BUILTIN, HIGH);   // ajuste a polaridade se necessário
      break;
    case LapelState::SENDING:
      if (now - last > 120) { last = now; on = !on; digitalWrite(LED_BUILTIN, on); }
      break;
    case LapelState::IDLE:
    default:
      if (now - last > 1000) { last = now; on = !on; digitalWrite(LED_BUILTIN, on); }
      break;
  }
  // Placas com RGB (variante): descomente e ajuste os pinos.
  // digitalWrite(LED_RED,   st == LapelState::RECORDING ? HIGH : LOW);
  // digitalWrite(LED_GREEN, st == LapelState::IDLE ? HIGH : LOW);
  // digitalWrite(LED_BLUE,  st == LapelState::SENDING ? HIGH : LOW);
}

// ---------------------------------------------------------------- state ---
void publishState(LapelState st) {
  gState = st;
  uint8_t v = static_cast<uint8_t>(st);
  stateChar.writeValue(&v, 1);
  setLed(st);
}

// --------------------------------------------------------------- stream ---
// Empacota e notifica. Retorna false se não há central inscrita.
bool sendPacket(const int16_t *pcm, size_t sampleCount, bool last) {
  size_t bytes = sampleCount * sizeof(int16_t);
  if (bytes > PACKET_PAYLOAD) bytes = PACKET_PAYLOAD;
  gPacket[0] = (uint8_t)(gSeq & 0xFF);
  gPacket[1] = (uint8_t)((gSeq >> 8) & 0xFF);
  gPacket[2] = last ? 0x01 : 0x00;
  gPacket[3] = 0x00;
  if (bytes > 0) memcpy(&gPacket[PACKET_HEADER], pcm, bytes);
  gSeq++;
  if (!audioDataChar.subscribed()) return false;  // sem central: descarta
  return audioDataChar.writeValue(gPacket, PACKET_HEADER + bytes);
}

void startRecording() {
  gSeq = 0;
  publishState(LapelState::RECORDING);
}

void stopRecording() {
  // Sinaliza fim do stream com um pacote END (payload vazio + flag LAST).
  // O app mobile remonta pelo seq e usa o flag LAST como fim da gravação.
  publishState(LapelState::SENDING);
  static int16_t empty[1] = {0};
  sendPacket(empty, 0, true);
  delay(150);  // dá tempo da notificação ser entregue
  publishState(LapelState::IDLE);
}

// --------------------------------------------------------------- button ---
// Retorna true no clique (borda de descida com debounce).
bool readClick() {
  bool level = digitalRead(BUTTON_PIN);
  uint32_t now = millis();
  bool clicked = false;
  if (level != gLastBtn && (now - gLastChange) > DEBOUNCE_MS) {
    gLastChange = now;
    gLastBtn = level;
    if (level == LOW) clicked = true;  // pressionou
  }
  return clicked;
}

// ----------------------------------------------------------------- setup ---
void setup() {
  Serial.begin(115200);
  pinMode(LED_BUILTIN, OUTPUT);
  pinMode(BUTTON_PIN, INPUT_PULLUP);

  PDM.onReceive(onPDMdata);
  PDM.setBufferSize(4096);
  PDM.setGain(PDM_GAIN);
  if (!PDM.begin(1, SAMPLE_RATE_HZ)) {
    // Microfone indisponível: pisca rápido para indicar falha fatal.
    while (true) { digitalWrite(LED_BUILTIN, HIGH); delay(100); digitalWrite(LED_BUILTIN, LOW); delay(100); }
  }

  if (!BLE.begin()) {
    while (true) { digitalWrite(LED_BUILTIN, HIGH); delay(500); digitalWrite(LED_BUILTIN, LOW); delay(500); }
  }
  BLE.setLocalName(DEVICE_NAME);
  BLE.setAdvertisedService(audioService);
  audioService.addCharacteristic(audioDataChar);
  audioService.addCharacteristic(stateChar);
  BLE.addService(audioService);
  publishState(LapelState::IDLE);
  BLE.advertise();
  Serial.println("AnotAI Lapela pronta. Aguardando central BLE...");
}

// ------------------------------------------------------------------ loop ---
void loop() {
  BLEDevice central = BLE.central();

  if (central && central.connected()) {
    if (readClick()) {
      uint32_t now = millis();
      bool doubleClick = (now - gLastClick) < DOUBLE_CLICK_MS;
      gLastClick = now;
      // Alternado: IDLE -> RECORDING; RECORDING -> stop (clique ou duplo).
      if (gState == LapelState::IDLE) {
        startRecording();
      } else if (gState == LapelState::RECORDING) {
        (void)doubleClick;  // clique simples ou duplo encerram
        stopRecording();
      }
    }

    // Stream ao vivo durante a gravação.
    if (gState == LapelState::RECORDING && gPdmReady) {
      gPdmReady = false;
      // Fragmenta o buffer PDM em pacotes de PACKET_PAYLOAD bytes.
      size_t offset = 0;
      int remaining = gPdmSamples;
      while (remaining > 0) {
        size_t chunk = (size_t)remaining * sizeof(int16_t);
        if (chunk > PACKET_PAYLOAD) chunk = PACKET_PAYLOAD;
        if (!sendPacket(&gPdmBuffer[offset], chunk / sizeof(int16_t), false)) break;
        offset += chunk / sizeof(int16_t);
        remaining -= (int)(chunk / sizeof(int16_t));
      }
    }
    setLed(gState);
  } else {
    // Sem central: garante captura parada e volta a anunciar.
    if (gState != LapelState::IDLE) publishState(LapelState::IDLE);
    gPdmReady = false;
    setLed(LapelState::IDLE);
    if (readClick()) { /* ignora cliques sem central conectada */ }
    BLE.advertise();
    delay(50);
  }
}
