/**
 * AnotAI BLE — recepção e remontagem do áudio da lapela.
 *
 * - Escuta AUDIO_DATA (NOTIFY): guarda payloads por `seq` (tolera fora de ordem)
 * - Escuta STATE: RECORDING inicia, SENDING finaliza, IDLE aborta parcial
 * - Na finalização: monta WAV PCM 16 kHz mono, salva com expo-file-system,
 *   faz upload (`POST /api/v1/audio/process`) e devolve o note_id
 */

import type { Device, Subscription } from "react-native-ble-plx";
import * as FileSystem from "expo-file-system";

import { uploadAudio } from "../services/api";
import { base64ToBytes, bytesToBase64 } from "./base64";
import {
  ANOTAI_AUDIO_SERVICE_UUID,
  AUDIO_DATA_CHAR_UUID,
  BITS_PER_SAMPLE,
  FLAG_LAST,
  LapelState,
  NUM_CHANNELS,
  PACKET_HEADER_SIZE,
  SAMPLE_RATE_HZ,
  STATE_CHAR_UUID,
} from "./constants";

export interface LapelRecordingCallbacks {
  onHardwareRecordingStarted: () => void;
  onHardwareRecordingFinished: (noteId: string) => void;
  onError: (message: string) => void;
}

function buildWav(pcm: Uint8Array): Uint8Array {
  const header = new ArrayBuffer(44);
  const v = new DataView(header);
  const writeStr = (off: number, s: string): void => {
    for (let i = 0; i < s.length; i++) v.setUint8(off + i, s.charCodeAt(i));
  };
  const dataLen = pcm.length;
  writeStr(0, "RIFF");
  v.setUint32(4, 36 + dataLen, true);
  writeStr(8, "WAVE");
  writeStr(12, "fmt ");
  v.setUint32(16, 16, true);
  v.setUint16(20, 1, true); // PCM
  v.setUint16(22, NUM_CHANNELS, true);
  v.setUint32(24, SAMPLE_RATE_HZ, true);
  v.setUint32(28, (SAMPLE_RATE_HZ * NUM_CHANNELS * BITS_PER_SAMPLE) / 8, true);
  v.setUint16(32, (NUM_CHANNELS * BITS_PER_SAMPLE) / 8, true);
  v.setUint16(34, BITS_PER_SAMPLE, true);
  writeStr(36, "data");
  v.setUint32(40, dataLen, true);
  const out = new Uint8Array(44 + dataLen);
  out.set(new Uint8Array(header), 0);
  out.set(pcm, 44);
  return out;
}

export class LapelAudioReceiver {
  private packets = new Map<number, Uint8Array>();
  private receiving = false;
  private finalizing = false;
  private subs: Subscription[] = [];

  /** Anexa aos monitors do device. Retorna função de detach. */
  async attach(device: Device, cb: LapelRecordingCallbacks): Promise<() => void> {
    this.detach();

    this.subs.push(
      device.monitorCharacteristicForService(ANOTAI_AUDIO_SERVICE_UUID, STATE_CHAR_UUID, (error, char) => {
        if (error || !char?.value) return;
        try {
          const state = base64ToBytes(char.value)[0];
          if (state === LapelState.RECORDING) this.beginReception(cb);
          else if (state === LapelState.SENDING) void this.finalize(cb);
          else if (state === LapelState.IDLE) this.abort();
        } catch (e) {
          cb.onError(e instanceof Error ? e.message : "Falha ao ler estado da lapela");
        }
      }),
      device.monitorCharacteristicForService(ANOTAI_AUDIO_SERVICE_UUID, AUDIO_DATA_CHAR_UUID, (error, char) => {
        if (error || !char?.value) return;
        try {
          this.ingest(char.value);
        } catch (e) {
          cb.onError(e instanceof Error ? e.message : "Pacote de áudio inválido");
        }
      }),
    );

    // Estado atual (caso a gravação já estivesse em curso ao anexar):
    try {
      const current = await device.readCharacteristicForService(ANOTAI_AUDIO_SERVICE_UUID, STATE_CHAR_UUID);
      if (current.value && base64ToBytes(current.value)[0] === LapelState.RECORDING) {
        this.beginReception(cb);
      }
    } catch {
      // leitura inicial é best-effort
    }

    return () => this.detach();
  }

  detach(): void {
    for (const s of this.subs) {
      try {
        s.remove();
      } catch {
        // ignore
      }
    }
    this.subs = [];
  }

  /** Descarta buffer parcial (queda de conexão no meio da gravação). */
  abort(): void {
    this.packets.clear();
    this.receiving = false;
    this.finalizing = false;
  }

  private beginReception(cb: LapelRecordingCallbacks): void {
    if (this.receiving) return;
    this.packets.clear();
    this.receiving = true;
    this.finalizing = false;
    cb.onHardwareRecordingStarted();
  }

  private ingest(b64: string): void {
    const raw = base64ToBytes(b64);
    if (raw.length < PACKET_HEADER_SIZE) return;
    const seq = raw[0] | (raw[1] << 8);
    const flags = raw[2];
    const payload = raw.slice(PACKET_HEADER_SIZE);
    this.receiving = true;
    if (payload.length > 0 && !this.packets.has(seq)) {
      this.packets.set(seq, payload);
    }
    if (flags & FLAG_LAST) {
      // Pacote END: os dados já estão no mapa; finalização via STATE=SENDING.
    }
  }

  private async finalize(cb: LapelRecordingCallbacks): Promise<void> {
    if (!this.receiving || this.finalizing) return;
    this.finalizing = true;
    try {
      const seqs = [...this.packets.keys()].sort((a, b) => a - b);
      if (seqs.length === 0) {
        this.abort();
        return;
      }
      const maxSeq = seqs[seqs.length - 1];
      let chunkLen = 0;
      for (const p of this.packets.values()) chunkLen = Math.max(chunkLen, p.length);
      const total = (maxSeq + 1) * chunkLen;
      const pcm = new Uint8Array(total);
      for (let s = 0; s <= maxSeq; s++) {
        const chunk = this.packets.get(s);
        if (chunk) pcm.set(chunk.subarray(0, Math.min(chunk.length, chunkLen)), s * chunkLen);
        // seq ausente = silêncio (zeros) — perda tolerada sem quebrar o WAV
      }
      const wav = buildWav(pcm);
      const uri = `${FileSystem.documentDirectory}lapel-${Date.now()}.wav`;
      await FileSystem.writeAsStringAsync(uri, bytesToBase64(wav), {
        encoding: FileSystem.EncodingType.Base64,
      });
      const accepted = await uploadAudio(uri, "audio/wav");
      cb.onHardwareRecordingFinished(accepted.note_id);
    } catch (e) {
      cb.onError(e instanceof Error ? e.message : "Falha ao salvar/enviar áudio da lapela");
    } finally {
      this.abort();
    }
  }
}
