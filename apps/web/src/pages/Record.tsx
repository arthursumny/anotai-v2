import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { apiError, getNote, uploadAudio } from "../api";
import type { Note } from "../types";
import Icon from "../components/Icon";

type Phase = "idle" | "recording" | "uploading" | "transcribing" | "done" | "attention";

function pickMime(): string {
  if (typeof MediaRecorder !== "undefined" && MediaRecorder.isTypeSupported("audio/webm")) return "audio/webm";
  return "";
}

const STEPS: { key: Phase; label: string; desc: string }[] = [
  { key: "uploading", label: "Enviando gravação", desc: "Só um instante…" },
  { key: "transcribing", label: "Preparando sua nota", desc: "Estamos organizando o conteúdo…" },
  { key: "done", label: "Nota pronta", desc: "Você já pode conferir." },
];
const RANK: Record<Phase, number> = { idle: 0, recording: 0, uploading: 1, transcribing: 2, done: 3, attention: 2 };

export default function Record() {
  const [phase, setPhase] = useState<Phase>("idle");
  const [elapsed, setElapsed] = useState(0);
  const [note, setNote] = useState<Note | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [level, setLevel] = useState(0);

  const recRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const timerRef = useRef<number | null>(null);
  const pollRef = useRef<number | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const rafRef = useRef<number | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const levelRef = useRef(0);

  const stopTimers = () => {
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
  };

  const stopAudioGraph = () => {
    if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = null; }
    try { audioCtxRef.current?.close(); } catch { /* noop */ }
    audioCtxRef.current = null;
    analyserRef.current = null;
    streamRef.current?.getTracks().forEach((t) => t.stop());
    streamRef.current = null;
    setLevel(0);
  };

  useEffect(() => () => { stopTimers(); stopAudioGraph(); }, []);

  /* Desenha a waveform viva no canvas */
  useEffect(() => {
    if (phase !== "recording") return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const draw = () => {
      const W = canvas.width;
      const H = canvas.height;
      ctx.clearRect(0, 0, W, H);
      const analyser = analyserRef.current;

      let data: Uint8Array | null = null;
      if (analyser) {
        data = new Uint8Array(analyser.fftSize);
        analyser.getByteTimeDomainData(data);
        let sum = 0;
        for (let i = 0; i < data.length; i += 4) {
          const v = (data[i] - 128) / 128;
          sum += v * v;
        }
        const rms = Math.sqrt(sum / (data.length / 4));
        levelRef.current = Math.min(1, rms * 3.2);
        setLevel(levelRef.current);
      }

      const bars = 56;
      const gap = 5;
      const bw = (W - gap * (bars - 1)) / bars;
      const t = performance.now() / 1000;
      for (let i = 0; i < bars; i++) {
        let amp: number;
        if (data) {
          const idx = Math.floor((i / bars) * data.length);
          const v = Math.abs((data[idx] - 128) / 128);
          amp = 0.12 + Math.min(1, v * 3.4);
        } else {
          amp = 0.25 + 0.2 * Math.abs(Math.sin(t * 2.2 + i * 0.45));
        }
        const h = Math.max(4, amp * (H - 8));
        const x = i * (bw + gap);
        const y = (H - h) / 2;
        ctx.fillStyle = "#537966";
        const r = Math.min(bw / 2, 3);
        ctx.beginPath();
        ctx.roundRect(x, y, bw, h, r);
        ctx.fill();
      }
      rafRef.current = requestAnimationFrame(draw);
    };
    rafRef.current = requestAnimationFrame(draw);
    return () => { if (rafRef.current) cancelAnimationFrame(rafRef.current); };
  }, [phase]);

  const terminalError = (n: Note): string | null => {
    const failed = (n.logs ?? []).find(
      (l) => l.status === "failed" && (l.action_type === "pipeline" || l.action_type === "extraction"),
    );
    return failed ? failed.error_message || "Falha no processamento." : null;
  };

  const hasSound = async (blob: Blob): Promise<boolean> => {
    try {
      const AC = window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      const ctx = new AC();
      try {
        const buf = await blob.arrayBuffer();
        const audio = await ctx.decodeAudioData(buf);
        for (let ch = 0; ch < audio.numberOfChannels; ch++) {
          const data = audio.getChannelData(ch);
          for (let i = 0; i < data.length; i += 10) {
            if (Math.abs(data[i]) >= 0.02) return true;
          }
        }
        return false;
      } finally {
        await ctx.close();
      }
    } catch {
      return true;
    }
  };

  const pollUntilDone = (noteId: string) => {
    setPhase("transcribing");
    let attempts = 0;
    pollRef.current = window.setInterval(async () => {
      attempts += 1;
      try {
        const fresh = await getNote(noteId);
        setNote(fresh);
        const fatal = terminalError(fresh);
        if (fresh.status === "processed" || fresh.status === "no_match") {
          stopTimers();
          setPhase("done");
        } else if (fatal) {
          stopTimers();
          setError(fatal);
          setPhase("attention");
        } else if (attempts >= 60) {
          stopTimers();
          setPhase("attention");
        }
      } catch (e) {
        stopTimers();
        const msg = apiError(e);
        setError(msg === "Erro 404" ? "Áudio sem conteúdo aproveitável — grave novamente." : msg);
        setPhase("attention");
      }
    }, 2000);
  };

  const start = async () => {
    setError(null);
    setNote(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      streamRef.current = stream;
      try {
        const AC = window.AudioContext ?? (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
        const actx = new AC();
        audioCtxRef.current = actx;
        const src = actx.createMediaStreamSource(stream);
        const analyser = actx.createAnalyser();
        analyser.fftSize = 2048;
        src.connect(analyser);
        analyserRef.current = analyser;
      } catch { /* waveform cai p/ modo simulado */ }

      const mime = pickMime();
      const rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
      chunksRef.current = [];
      rec.ondataavailable = (e) => { if (e.data.size > 0) chunksRef.current.push(e.data); };
      rec.onstop = async () => {
        if (rafRef.current) { cancelAnimationFrame(rafRef.current); rafRef.current = null; }
        try { audioCtxRef.current?.close(); } catch { /* noop */ }
        audioCtxRef.current = null;
        analyserRef.current = null;
        stream.getTracks().forEach((t) => t.stop());
        streamRef.current = null;

        const type = rec.mimeType || "audio/webm";
        const blob = new Blob(chunksRef.current, { type });
        if (blob.size < 2048 || !(await hasSound(blob))) {
          setError("Nenhum som captado — confira se o microfone está ativo e com volume, e grave de novo.");
          setPhase("idle");
          return;
        }
        setPhase("uploading");
        try {
          const ext = type.includes("webm") ? "webm" : "bin";
          const accepted = await uploadAudio(blob, `gravacao-${Date.now()}.${ext}`);
          pollUntilDone(accepted.note_id);
        } catch (e) {
          setError(apiError(e));
          setPhase("idle");
        }
      };
      rec.start();
      recRef.current = rec;
      setElapsed(0);
      setPhase("recording");
      timerRef.current = window.setInterval(() => setElapsed((s) => s + 1), 1000);
    } catch {
      setError("Permissão de microfone negada ou indisponível (use http://localhost ou HTTPS).");
    }
  };

  const stop = () => {
    if (timerRef.current) { clearInterval(timerRef.current); timerRef.current = null; }
    recRef.current?.stop();
    recRef.current = null;
  };

  const reset = () => {
    stopTimers();
    setNote(null);
    setError(null);
    setElapsed(0);
    setPhase("idle");
  };

  const mm = String(Math.floor(elapsed / 60)).padStart(2, "0");
  const ss = String(elapsed % 60).padStart(2, "0");
  const isRec = phase === "recording";
  const busyPhase = phase === "uploading" || phase === "transcribing";
  const progress = phase === "uploading" ? 32 : phase === "transcribing" ? 72 : phase === "done" ? 100 : 0;

  const headline =
    phase === "recording" ? "Ouvindo você…" :
    phase === "uploading" ? "Enviando…" :
    phase === "transcribing" ? "Preparando sua nota…" :
    phase === "done" ? "Sua nota está pronta." :
    phase === "attention" ? "Não foi possível concluir." : "Toque para registrar algo que você quer lembrar.";

  return (
    <div>
      <div className="page-head">
        <div>
          <h1>Registre uma ideia</h1>
          <p className="muted">Registre o que não quer esquecer. Depois, confira a nota e escolha o que fazer com ela.</p>
        </div>
        {(phase === "done" || phase === "attention") && (
          <button className="btn-secondary" onClick={reset}>＋ Nova gravação</button>
        )}
      </div>

      <div className={`rec-stage ${isRec ? "is-recording" : ""}`}>
        <div className="rec-control">
          <button
            className={`recbtn ${isRec ? "recording" : ""}`}
            onClick={isRec ? stop : start}
            disabled={busyPhase}
            aria-label={isRec ? "Encerrar gravação" : busyPhase ? "Aguarde o processamento terminar" : phase === "idle" ? "Iniciar gravação" : "Começar outra gravação"}
          >
            <span className="recbtn-icon"><Icon name={isRec ? "stop" : "mic"} size={22} /></span>
            <span>{isRec ? "Encerrar" : busyPhase ? "Aguarde" : phase === "idle" ? "Começar a gravar" : "Gravar novamente"}</span>
          </button>
        </div>

        <div className="rec-timer">
          {isRec && <span className="live-dot" />}
          {isRec || elapsed > 0 ? `${mm}:${ss}` : ""}
        </div>
        <p className="muted rec-hint" role="status" aria-live="polite" style={{ marginBottom: 0 }}>
          {isRec
            ? level > 0.08 ? "Gravando. Toque no botão quando terminar." : "Gravando. Se estiver silencioso, aproxime-se do microfone."
            : headline}
        </p>

        {(isRec || elapsed > 0) && phase !== "idle" && (
          <canvas ref={canvasRef} className="wave-canvas" width={520} height={84} />
        )}
      </div>

      {error && <p className="error">{error}</p>}

      {(busyPhase || phase === "done" || phase === "attention") && (
        <div className="steps">
          {STEPS.map((s, i) => {
            const on = RANK[phase] >= RANK[s.key];
            const active = (phase === "uploading" && s.key === "uploading") || (phase === "transcribing" && s.key === "transcribing");
            return (
              <div key={s.key} className={`step ${on ? "on" : ""}`} style={{ animationDelay: `${i * 80}ms` }}>
                <span className={`dot ${on ? "on" : ""} ${active ? "spin" : ""}`} />
                <span>
                  {active ? <><b>{s.label}</b> — {s.desc}</> : s.label}
                </span>
              </div>
            );
          })}
          <div className="progress-track"><div className="progress-fill" style={{ width: `${progress}%` }} /></div>

          {phase === "attention" && (
            <p className="muted" style={{ marginBottom: 0 }}>Ainda processando ou falha no acompanhamento — abra a nota ou grave de novo.</p>
          )}

          {note && (phase === "done" || phase === "attention") && (
            <div className={`card ${phase === "done" ? "result-card" : ""}`} style={{ margin: "14px 0 0" }}>
              <span className={`badge ${note.template_id ? "processed" : "general"}`}>
                {note.template_id ? "Organizada" : "Nota geral"}
              </span>
              <div style={{ fontWeight: 800, marginTop: 8, fontSize: 15 }}>
                {note.title || "Nota processada com sucesso"}
              </div>
              {note.raw_transcription && (
                <div className="muted" style={{ marginTop: 6 }}>{note.raw_transcription.slice(0, 180)}{note.raw_transcription.length > 180 ? "…" : ""}</div>
              )}
              <div className="row" style={{ marginTop: 12 }}>
                {note.raw_transcription.trim() !== "" && (
                  <Link to={`/notas/${note.id}`} className="btn btn-blue">Ver nota <Icon name="arrowRight" size={16} /></Link>
                )}
                <button className="btn-secondary" onClick={reset}>Gravar outra</button>
              </div>
            </div>
          )}
        </div>
      )}

    </div>
  );
}
