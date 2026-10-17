import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';

/* ---------- Modal ---------- */
export function Modal({
  title, sub, onClose, children, wide,
}: {
  title: string; sub?: string; onClose: () => void; children: React.ReactNode; wide?: boolean;
}) {
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose(); };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [onClose]);

  return createPortal(
    <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <div className="modal" style={wide ? { width: 'min(720px, 100%)' } : undefined} role="dialog" aria-label={title}>
        <h2>{title}</h2>
        {sub && <div className="sub">{sub}</div>}
        {children}
      </div>
    </div>,
    document.body,
  );
}

/* ---------- Toast ---------- */
export function useToast() {
  const [msg, setMsg] = useState<string | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const show = (m: string) => {
    setMsg(m);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setMsg(null), 2400);
  };
  const node = msg ? <div className="toast">{msg}</div> : null;
  return { show, node };
}

/* ---------- Confirm ---------- */
export function useConfirm() {
  const [state, setState] = useState<{ text: string; resolve: (v: boolean) => void } | null>(null);

  const ask = (text: string) =>
    new Promise<boolean>((resolve) => setState({ text, resolve }));

  const node = state
    ? createPortal(
        <div className="modal-backdrop" onMouseDown={(e) => { if (e.target === e.currentTarget) { state.resolve(false); setState(null); } }}>
          <div className="modal" style={{ width: 'min(420px, 100%)' }}>
            <h2>Подтверди</h2>
            <p className="dim" style={{ margin: '10px 0 0' }}>{state.text}</p>
            <div className="actions">
              <button className="btn" onClick={() => { state.resolve(false); setState(null); }}>Отмена</button>
              <button className="btn primary" onClick={() => { state.resolve(true); setState(null); }}>Да, сделать</button>
            </div>
          </div>
        </div>,
        document.body,
      )
    : null;

  return { ask, node };
}

/* ---------- Kind picker ---------- */
import { KINDS, KIND_LIST, type NodeKind } from './types';

export function KindPicker({ onPick }: { onPick: (k: NodeKind) => void }) {
  return (
    <div className="kind-picker">
      {KIND_LIST.map((k) => (
        <button key={k} className="kind-opt" onClick={() => onPick(k)}>
          <span className="g">{KINDS[k].glyph}</span>
          <span>
            <span className="n">{KINDS[k].label}</span>
            <br />
            <span className="h">{KINDS[k].hint}</span>
          </span>
        </button>
      ))}
    </div>
  );
}

/* ---------- Segmented control ---------- */
export function Seg<T extends string>({
  value, options, onChange, alt,
}: {
  value: T;
  options: { v: T; label: string }[];
  onChange: (v: T) => void;
  alt?: boolean;
}) {
  return (
    <div className="seg">
      {options.map((o) => (
        <button
          key={o.v}
          className={o.v === value ? (alt ? 'on alt' : 'on') : ''}
          onClick={() => onChange(o.v)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

/* ---------- time helpers ---------- */
export function fmtDate(ts: number): string {
  return new Date(ts).toLocaleDateString('ru-RU', { day: '2-digit', month: 'short', year: 'numeric' });
}
export function fmtDateTime(ts: number): string {
  return new Date(ts).toLocaleString('ru-RU', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' });
}
export function relTime(ts: number): string {
  const d = Date.now() - ts;
  const m = Math.floor(d / 60000);
  if (m < 1) return 'только что';
  if (m < 60) return `${m} мин назад`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h} ч назад`;
  const dd = Math.floor(h / 24);
  if (dd < 30) return `${dd} дн назад`;
  return fmtDate(ts);
}
