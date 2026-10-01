type BrandMarkProps = { className?: string };
type BrandProps = { compact?: boolean; tagline?: boolean };

export function BrandMark({ className = "" }: BrandMarkProps) {
  return (
    <span className={`brand-mark ${className}`.trim()} aria-hidden="true">
      <svg viewBox="0 0 24 24" fill="none" role="presentation">
        <rect x="8" y="3.5" width="8" height="12" rx="4" fill="currentColor" />
        <path d="M5.5 11.5a6.5 6.5 0 0 0 13 0M12 18v3M9 21h6" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" />
      </svg>
    </span>
  );
}

export default function Brand({ compact = false, tagline = true }: BrandProps) {
  return (
    <span className={`brand${compact ? " brand--compact" : ""}`}>
      <BrandMark />
      <span className="brand-copy">
        <span className="brand-name">Anot<span>AI</span></span>
        {tagline && <span className="brand-sub">ideias em ordem</span>}
      </span>
    </span>
  );
}
