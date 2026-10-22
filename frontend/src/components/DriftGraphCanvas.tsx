/**
 * DriftGraphCanvas — дрейфующий граф-фон (ЭТАП 9, коммит 2).
 * nodeCount нод плавают по canvas; линии между нодами ближе lineDistance px.
 * Пауза при document.hidden; статика при prefers-reduced-motion.
 */
import { useEffect, useRef, type ReactElement } from 'react';

interface Node { x: number; y: number; vx: number; vy: number; }

export interface DriftGraphCanvasProps {
  className?: string;
  nodeCount?: number;
  lineDistance?: number;
}

function readVar(name: string, fallback: string): string {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v.length > 0 ? v : fallback;
}

export function DriftGraphCanvas({
  className,
  nodeCount = 20,
  lineDistance = 120,
}: DriftGraphCanvasProps): ReactElement {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    let shader = '#a855f7'; /* коммит12: дефолт = violet */
    const syncColors = () => { shader = readVar('--green', '#a855f7'); };
    syncColors();
    const themeObserver = new MutationObserver(syncColors);
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class'] });

    let W = 0, H = 0, dpr = Math.max(1, window.devicePixelRatio || 1);
    const resize = () => {
      const rect = wrap.getBoundingClientRect();
      W = Math.max(1, Math.floor(rect.width));
      H = Math.max(1, Math.floor(rect.height));
      canvas.width = W * dpr;
      canvas.height = H * dpr;
      canvas.style.width = `${W}px`;
      canvas.style.height = `${H}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(wrap);

    // ноды: случайные позиции, скорость drift 0.2–0.5 px/frame
    const nodes: Node[] = Array.from({ length: nodeCount }, () => {
      const ang = Math.random() * Math.PI * 2;
      const sp = 0.2 + Math.random() * 0.3;
      return { x: Math.random() * W, y: Math.random() * H, vx: Math.cos(ang) * sp, vy: Math.sin(ang) * sp };
    });

    const draw = () => {
      ctx.clearRect(0, 0, W, H);
      // линии между ближайшими
      for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
          const a = nodes[i], b = nodes[j];
          const dist = Math.hypot(a.x - b.x, a.y - b.y);
          if (dist < lineDistance) {
            ctx.strokeStyle = shader;
            ctx.globalAlpha = (1 - dist / lineDistance) * 0.35; /* ЭТАП9 коммит7: линии тоньше, не перетягивают hero */
            ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(a.x, a.y);
            ctx.lineTo(b.x, b.y);
            ctx.stroke();
          }
        }
      }
      // ноды: круги r=3
      ctx.globalAlpha = 0.9;
      ctx.fillStyle = shader;
      for (const n of nodes) {
        ctx.beginPath();
        ctx.arc(n.x, n.y, 3, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    };

    const step = () => {
      for (const n of nodes) {
        n.x += n.vx;
        n.y += n.vy;
        // wrap-around по краям
        if (n.x < -5) n.x = W + 5; else if (n.x > W + 5) n.x = -5;
        if (n.y < -5) n.y = H + 5; else if (n.y > H + 5) n.y = -5;
      }
    };

    if (reduced) {
      draw();
    } else {
      let raf = 0;
      let running = true;
      const loop = () => {
        if (!running) return;
        step();
        draw();
        raf = requestAnimationFrame(loop);
      };
      const onVis = () => {
        if (document.hidden) { running = false; cancelAnimationFrame(raf); }
        else if (!running) { running = true; raf = requestAnimationFrame(loop); }
      };
      document.addEventListener('visibilitychange', onVis);
      raf = requestAnimationFrame(loop);
      return () => {
        running = false;
        cancelAnimationFrame(raf);
        document.removeEventListener('visibilitychange', onVis);
        ro.disconnect();
        themeObserver.disconnect();
      };
    }

    return () => { ro.disconnect(); themeObserver.disconnect(); };
  }, [nodeCount, lineDistance]);

  return (
    <div ref={wrapRef} className={`drift-wrap${className ? ' ' + className : ''}`}>
      <canvas ref={canvasRef} className="drift-canvas" aria-hidden="true" />
    </div>
  );
}
