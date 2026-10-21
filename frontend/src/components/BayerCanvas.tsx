/**
 * BayerCanvas — байесовский dithering-шейдер фона ВИЛКИ (ЭТАП 9, коммит 2).
 * Offscreen canvas 160x90, upscale через CSS image-rendering: pixelated.
 * 3 дрейфующих гауссовых блоба + репульсор от курсора + ripple на keydown.
 * Пауза при document.hidden; статика при prefers-reduced-motion.
 */
import { useEffect, useRef, type ReactElement } from 'react';

const W = 160;
const H = 90;

// Матрица Байера 4x4 (по ТЗ), нормализованная /16
const BAYER: number[][] = [
  [0, 8, 2, 10],
  [12, 4, 14, 6],
  [3, 11, 1, 9],
  [15, 7, 13, 5],
].map((row) => row.map((v) => v / 16));

interface Blob {
  x: number; y: number;       // центр в координатах полотна
  vx: number; vy: number;     // скорость дрейфа
  sigma: number;              // радиус (в пикселях offscreen)
  phase: number;              // фаза пульсации яркости
}

interface Ripple {
  t0: number;                 // время старта (ms)
  x: number; y: number;       // центр (обычно — центр полотна)
}

export interface BayerCanvasProps {
  className?: string;
  /** множитель интенсивности поля (1.0 = дефолт, 0.5 = слабее) */
  intensityScale?: number;
}

/** читает CSS-переменную из :root (учитывает активную тему) */
function readVar(el: HTMLElement | Window & typeof globalThis, name: string, fallback: string): string {
  try {
    const cs = getComputedStyle(el as Element);
    const v = cs.getPropertyValue(name).trim();
    return v.length > 0 ? v : fallback;
  } catch {
    return fallback;
  }
}

export function BayerCanvas({ className, intensityScale = 1.0 }: BayerCanvasProps): ReactElement {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const propsRef = useRef({ intensityScale });
  propsRef.current.intensityScale = intensityScale;

  useEffect(() => {
    const canvas = canvasRef.current;
    const wrap = wrapRef.current;
    if (!canvas || !wrap) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    canvas.width = W;
    canvas.height = H;

    const reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

    // цвета из токенов темы (--green как «--shader», --term-bg как фон)
    let shaderColor = '#3ddc84';
    let bgColor = '#060a07';
    const syncColors = () => {
      shaderColor = readVar(window, '--green', '#3ddc84');
      bgColor = readVar(document.documentElement, '--term-bg', '#060a07');
    };
    syncColors();
    const themeObserver = new MutationObserver(syncColors);
    themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme', 'class'] });

    // три блоба: случайные старт-позиции, медленный дрейф, разные сигмы
    const blobs: Blob[] = Array.from({ length: 3 }, (_, i) => ({
      x: W * (0.25 + 0.25 * i),
      y: H * (0.3 + 0.2 * ((i * 2) % 3)),
      vx: (Math.random() - 0.5) * 0.12,
      vy: (Math.random() - 0.5) * 0.08,
      sigma: 26 + i * 10,
      phase: Math.random() * Math.PI * 2,
    }));

    const ripples: Ripple[] = [];
    const mouse = { x: -9999, y: -9999, active: false };
    const REPEL_R = 80 / (W / 3); // 80 css-px примерно ≈ 24 px offscreen (полотно растянуто)

    const onMouseMove = (e: MouseEvent) => {
      const rect = canvas.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return;
      mouse.x = ((e.clientX - rect.left) / rect.width) * W;
      mouse.y = ((e.clientY - rect.top) / rect.height) * H;
      mouse.active = true;
    };
    const onMouseLeave = () => { mouse.active = false; mouse.x = -9999; mouse.y = -9999; };
    const onKeyDown = (e: KeyboardEvent) => {
      // волна от центра полотна на любую клавишу (не спамим: не чаще одной на 150ms)
      const now = performance.now();
      if (ripples.length > 0 && now - ripples[ripples.length - 1].t0 < 150) return;
      ripples.push({ t0: now, x: W / 2, y: H / 2 });
      if (ripples.length > 4) ripples.shift();
    };
    wrap.addEventListener('mousemove', onMouseMove);
    wrap.addEventListener('mouseleave', onMouseLeave);
    window.addEventListener('keydown', onKeyDown);

    const img = ctx.createImageData(W, H);

    const drawFrame = (timeMs: number) => {
      const scale = propsRef.current.intensityScale;
      const data = img.data;

      for (let y = 0; y < H; y++) {
        for (let x = 0; x < W; x++) {
          // сумма гауссиан трёх блобов
          let intensity = 0;
          for (const b of blobs) {
            const pulse = 0.75 + 0.25 * Math.sin(timeMs * 0.0006 + b.phase);
            const dx = x - b.x;
            const dy = y - b.y;
            intensity += pulse * Math.exp(-(dx * dx + dy * dy) / (2 * b.sigma * b.sigma));
          }

          // репульсор от курсора: гасим поле рядом с мышью
          if (mouse.active) {
            const mdx = x - mouse.x;
            const mdy = y - mouse.y;
            const mr2 = mdx * mdx + mdy * mdy;
            if (mr2 < REPEL_R * REPEL_R) {
              const k = 1 - Math.sqrt(mr2) / REPEL_R;
              intensity *= 1 - 0.9 * k;
            }
          }

          // ripple: бегущее кольцо синуса, затухание ~1.2s
          for (const r of ripples) {
            const age = (timeMs - r.t0) / 1200;
            if (age >= 0 && age <= 1) {
              const dist = Math.hypot(x - r.x, y - r.y);
              const waveR = age * (W * 0.7);
              const band = Math.exp(-((dist - waveR) * (dist - waveR)) / 90);
              intensity += band * (1 - age) * 0.9;
            }
          }

          intensity *= scale;

          // Bayer 4x4 thresholding
          const thr = BAYER[y & 3][x & 3];
          const on = intensity > thr;

          const p = (y * W + x) * 4;
          if (on) {
            data[p] = parseInt(shaderColor.slice(1, 3), 16) || 61;
            data[p + 1] = parseInt(shaderColor.slice(3, 5), 16) || 220;
            data[p + 2] = parseInt(shaderColor.slice(5, 7), 16) || 132;
          } else {
            data[p] = parseInt(bgColor.slice(1, 3), 16) || 6;
            data[p + 1] = parseInt(bgColor.slice(3, 5), 16) || 10;
            data[p + 2] = parseInt(bgColor.slice(5, 7), 16) || 7;
          }
          data[p + 3] = 255;
        }
      }
      ctx.putImageData(img, 0, 0);
    };

    const stepBlobs = () => {
      for (const b of blobs) {
        b.x += b.vx;
        b.y += b.vy;
        if (b.x < -10 || b.x > W + 10) b.vx = -b.vx;
        if (b.y < -10 || b.y > H + 10) b.vy = -b.vy;
      }
    };

    if (reduced) {
      // статика: один кадр без анимации
      drawFrame(0);
    } else {
      let raf = 0;
      let running = true;
      const loop = (t: number) => {
        if (!running) return;
        stepBlobs();
        drawFrame(t);
        raf = requestAnimationFrame(loop);
      };
      const onVis = () => {
        if (document.hidden) {
          running = false;
          cancelAnimationFrame(raf);
        } else if (!running) {
          running = true;
          raf = requestAnimationFrame(loop);
        }
      };
      document.addEventListener('visibilitychange', onVis);
      raf = requestAnimationFrame(loop);
      return () => {
        running = false;
        cancelAnimationFrame(raf);
        document.removeEventListener('visibilitychange', onVis);
        wrap.removeEventListener('mousemove', onMouseMove);
        wrap.removeEventListener('mouseleave', onMouseLeave);
        window.removeEventListener('keydown', onKeyDown);
        themeObserver.disconnect();
      };
    }

    return () => {
      wrap.removeEventListener('mousemove', onMouseMove);
      wrap.removeEventListener('mouseleave', onMouseLeave);
      window.removeEventListener('keydown', onKeyDown);
      themeObserver.disconnect();
    };
  }, []);

  return (
    <div ref={wrapRef} className={`bayer-wrap${className ? ' ' + className : ''}`}>
      <canvas ref={canvasRef} className="bayer-canvas" aria-hidden="true" />
      <span className="bayer-hud">SHADER: BAYER 60FPS</span>
    </div>
  );
}
