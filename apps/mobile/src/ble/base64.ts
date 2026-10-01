/**
 * AnotAI BLE — base64 <-> bytes sem dependências extras
 * (o ble-plx entrega notificações em base64).
 */

const ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
const REVERSE = new Map<string, number>([...ALPHABET].map((c, i) => [c, i]));

/** Decodifica base64 para bytes. Lança Error se inválido. */
export function base64ToBytes(b64: string): Uint8Array {
  const clean = b64.replace(/\s+/g, "");
  if (clean.length % 4 !== 0) throw new Error("base64 inválido");
  const pad = clean.endsWith("==") ? 2 : clean.endsWith("=") ? 1 : 0;
  const out = new Uint8Array((clean.length / 4) * 3 - pad);
  let o = 0;
  for (let i = 0; i < clean.length; i += 4) {
    const a = REVERSE.get(clean[i]);
    const b = REVERSE.get(clean[i + 1]);
    const c = clean[i + 2] === "=" ? 0 : REVERSE.get(clean[i + 2]);
    const d = clean[i + 3] === "=" ? 0 : REVERSE.get(clean[i + 3]);
    if (a === undefined || b === undefined || c === undefined || d === undefined) {
      throw new Error("base64 inválido");
    }
    const n = (a << 18) | (b << 12) | (c << 6) | d;
    out[o++] = (n >> 16) & 0xff;
    if (o < out.length) out[o++] = (n >> 8) & 0xff;
    if (o < out.length) out[o++] = n & 0xff;
  }
  return out;
}

/** Codifica bytes para base64 (usado ao salvar o WAV). */
export function bytesToBase64(bytes: Uint8Array): string {
  let out = "";
  for (let i = 0; i < bytes.length; i += 3) {
    const a = bytes[i];
    const b = i + 1 < bytes.length ? bytes[i + 1] : 0;
    const c = i + 2 < bytes.length ? bytes[i + 2] : 0;
    const n = (a << 16) | (b << 8) | c;
    out += ALPHABET[(n >> 18) & 0x3f] + ALPHABET[(n >> 12) & 0x3f];
    out += i + 1 < bytes.length ? ALPHABET[(n >> 6) & 0x3f] : "=";
    out += i + 2 < bytes.length ? ALPHABET[n & 0x3f] : "=";
  }
  return out;
}
