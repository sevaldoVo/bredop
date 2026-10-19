import { useEffect, useRef } from 'react';

/**
 * ЭТАП 9: второй фон-режим для Home — дрейфующий граф.
 * ~20 нод плавно плывут по экрану; линии рисуются между ближайшими соседями.
 */
export default function GraphDriftCanvas({
  count = 20,
  linkDist = 220,
  color = '#3ddc84',
  lineColor = 'rgba(61, 220, 132, 0.25)',
  className = '',
}: {
  count?: number;
  linkDist?: number;   // максимальная дистанция для «нити»
  color?: string;
  lineColor?: string;
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

    type P = { x: number; y: number; vx: number; vy: number; r: number };
    let pts: P[] = [];

    const seed = () => {
      pts = Array.from({ length: count }, () => ({
        x: Math.random() * w,
        y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.25,   // медленный дрейф
        vy: (Math.random() - 0.5) * 0.25,
        r: 2 + Math.random() * 3,
      }));
    };

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      const rect = cv.getBoundingClientRect();
      w = Math.max(1, rect.width);
      h = Math.max(1, rect.height);
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      if (!pts.length) seed();
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(cv);

    const step = () => {
      ctx.clearRect(0, 0, w, h);
      // движение + заворот через края
      for (const p of pts) {
        p.x += p.vx; p.y += p.vy;
        if (p.x < -10) p.x = w + 10; else if (p.x > w + 10) p.x = -10;
        if (p.y < -10) p.y = h + 10; else if (p.y > h + 10) p.y = -10;
      }
      // нити между ближайшими
      ctx.strokeStyle = lineColor;
      ctx.lineWidth = 1;
      for (let i = 0; i < pts.length; i++) {
        for (let j = i + 1; j < pts.length; j++) {
          const dx = pts[i].x - pts[j].x;
          const dy = pts[i].y - pts[j].y;
          const d2 = dx * dx + dy * dy;
          if (d2 < linkDist * linkDist) {
            ctx.globalAlpha = 1 - Math.sqrt(d2) / linkDist;
            ctx.beginPath();
            ctx.moveTo(pts[i].x, pts[i].y);
            ctx.lineTo(pts[j].x, pts[j].y);
            ctx.stroke();
          }
        }
      }
      ctx.globalAlpha = 1;
      // ноды
      ctx.fillStyle = color;
      for (const p of pts) {
        ctx.beginPath();
        ctx.arc(p.x, p.y, p.r, 0, Math.PI * 2);
        ctx.fill();
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);

    return () => { cancelAnimationFrame(raf); ro.disconnect(); };
  }, [count, linkDist, color, lineColor]);

  return <canvas ref={ref} className={`halftone ${className}`} aria-hidden="true" />;
}
