/**
 * AnotAI BLE — constantes espelhando `firmware/src/main.cpp`.
 * NÃO altere sem atualizar o firmware junto.
 */

/** Serviço GATT customizado anunciado como "AnotAI-Lapela". */
export const ANOTAI_AUDIO_SERVICE_UUID = "4fafc201-1fb5-459e-8fcc-c5c9c331914b";
/** NOTIFY: chunks de áudio PCM16LE com header de 4 bytes. */
export const AUDIO_DATA_CHAR_UUID = "4fafc202-1fb5-459e-8fcc-c5c9c331914b";
/** READ/NOTIFY: 0=IDLE, 1=RECORDING, 2=SENDING. */
export const STATE_CHAR_UUID = "4fafc203-1fb5-459e-8fcc-c5c9c331914b";

export const LAPEL_DEVICE_NAME = "AnotAI-Lapela";

export const LapelState = {
  IDLE: 0,
  RECORDING: 1,
  SENDING: 2,
} as const;
export type LapelStateValue = (typeof LapelState)[keyof typeof LapelState];

/** Formato do pacote AUDIO_DATA (little-endian). */
export const PACKET_HEADER_SIZE = 4;
export const FLAG_LAST = 0x01;
export const MAX_PACKET_BYTES = 204;

/** Áudio capturado: 16 kHz, 16-bit, mono. */
export const SAMPLE_RATE_HZ = 16000;
export const BITS_PER_SAMPLE = 16;
export const NUM_CHANNELS = 1;

/** Persistência do id do último dispositivo pareado. */
export const LAPEL_DEVICE_ID_KEY = "anotai_lapel_device_id";
