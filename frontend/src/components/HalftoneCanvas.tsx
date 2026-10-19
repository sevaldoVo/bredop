import { useEffect, useRef } from 'react';

/**
 * ЭТАП 9: HalftoneCanvas — фон из зелёных пикселей 4x4.
 * Плотность поля меняется «волнами» (синусоиды по времени), курсор локально
 * повышает плотность. Рисуем на одном canvas, DPR-aware, без зависимостей.
 */
export default function HalftoneCanvas({
  cell = 4,
  gap = 8,
  color = '#3ddc84',
  className = '',
}: {
  cell?: number;      // размер пикселя в CSS-px
  gap?: number;       // шаг сетки в CSS-px
  color?: string;     // цвет пикселей
  className?: string;
}) {
  const ref = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const cv = ref.current;
    if (!cv) return;
    const ctx = cv.getContext('2d');
    if (!ctx) return;

    let raf = 0;
    let w = 0;
    let h = 0;
    const mouse = { x: -1e6, y: -1e6, on: false };

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const r = cv.getBoundingClientRect();
      w = Math.max(1, r.width);
      h = Math.max(1, r.height);
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(cv);

    const onMove = (e: PointerEvent) => {
      const r = cv.getBoundingClientRect();
      mouse.x = e.clientX - r.left;
      mouse.y = e.clientY - r.top;
      mouse.on = true;
    };
    const onLeave = () => { mouse.on = false; };
    cv.addEventListener('pointermove', onMove);
    cv.addEventListener('pointerleave', onLeave);

    const draw = (tms: number) => {
      const t = tms / 1000;
      ctx.clearRect(0, 0, w, h);
      ctx.fillStyle = color;
      for (let gy = gap / 2; gy < h; gy += gap) {
        for (let gx = gap / 2; gx < w; gx += gap) {
          // волны плотности: две бегущие синусоиды + лёгкий диагональный узор
          let d =
            0.5 +
            0.5 * Math.sin(gx * 0.012 + t * 0.7) *
            Math.cos(gy * 0.014 - t * 0.5);
          // реакция на курсор: радиальная «линза» до +0.8 плотности
          if (mouse.on) {
            const dx = gx - mouse.x;
            const dy = gy - mouse.y;
            const dist2 = dx * dx + dy * dy;
            d += 0.85 * Math.exp(-dist2 / (140 * 140));
          }
          if (d <= 0.08) continue;               // пусто — не рисуем вовсе
          const s = Math.min(cell, cell * d * 1.35);
          ctx.globalAlpha = Math.min(1, 0.12 + d * 0.55);
          ctx.fillRect(gx - s / 2, gy - s / 2, s, s);
        }
      }
      ctx.globalAlpha = 1;
      raf = requestAnimationFrame(draw);
    };
    raf = requestAnimationFrame(draw);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      cv.removeEventListener('pointermove', onMove);
      cv.removeEventListener('pointerleave', onLeave);
    };
  }, [cell, gap, color]);

  return <canvas ref={ref} className={`halftone ${className}`} aria-hidden="true" />;
}
