import { useEffect, useState } from 'react';
import Home from './components/Home';
import Board from './components/Board';
import { useToast } from './ui';
import { useAuth } from './useAuth';
import { BayerCanvas } from './components/BayerCanvas';
import { THEMES, useTheme } from './theme';
import { fetchHealth, loadToken } from './api';
import type { AuthUser } from './api';
import type { InvestigationCase } from './types';

/** Свотчи тем для точек-переключателя (совпадают с [data-theme] в styles.css). */
const THEME_SWATCH: Record<string, string> = {
  phantom: '#3ddc84',
  axis: '#22d3ee',
  tactical: '#f59e0b',
  zeroday: '#ef4444',
  ghost: '#9ca3af',
};

/** ЭТАП 9 коммит 3 (VORTEX): сплит — слева шейдер + лого, справа карточка инициализации сессии. */
function AuthGate({ auth, onDemoEnter }: { auth: ReturnType<typeof useAuth>; onDemoEnter: () => void }) {
  const [mode, setMode] = useState<'login' | 'register'>('login');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [demoMode, setDemoMode] = useState(false);
  const { theme, setTheme } = useTheme();

  // Честный демо-режим: бэкенд при AUTH_ENFORCED=0 отдаёт auth_enforced:false.
  useEffect(() => {
    let alive = true;
    fetchHealth()
      .then((h) => { if (alive && h.auth_enforced === false) setDemoMode(true); })
      .catch(() => { /* бэк недоступен — показываем только обычную форму */ });
    return () => { alive = false; };
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!email || password.length < 6) return;
    await (mode === 'login' ? auth.login(email, password) : auth.register(email, password));
  };

  /** Демо-вход без аккаунта: НЕ трогаем JWT (иначе useAuth решит, что мы залогинены, и api.ts начнёт слать "Bearer demo" -> 401).
   *  Просто скрываем gate через локальный стейт App (onDemoEnter). */

  return (
    <div className="auth-gate" role="dialog" aria-modal="true">
      {/* левая колонка: Bayer-шейдер на всю высоту + лого Unbounded + бренд-чип */}
      <div className="auth-left">
        <div className="halftone-layer"><BayerCanvas /></div>
        <div className="auth-brand-chip lbl">ВИЛКА // OSINT TERM OS</div>
        <div className="auth-wordmark">ВИЛКА</div>
      </div>

      {/* правая колонка: карточка 420px */}
      <div className="auth-right">
        <form className="auth-card" onSubmit={(e) => void submit(e)}>
          <div className="auth-chips">
            <span className="chip ok">ГОТОВ К РУКОПОЖАТИЮ</span>
            <span className="chip">JWT HS256</span>
            <span className="chip">AUDIT LOG ON</span>
          </div>

          <div className="auth-title">ИНИЦИАЛИЗАЦИЯ СЕССИИ</div>

          <div className="seg auth-tabs" role="tablist">
            <button type="button" className={mode === 'login' ? 'on' : ''} onClick={() => setMode('login')}>ВХОД</button>
            <button type="button" className={mode === 'register' ? 'on' : ''} onClick={() => setMode('register')}>РЕГИСТРАЦИЯ</button>
          </div>

          <label><span className="lbl">EMAIL</span>
            <input
              type="email" autoComplete="email" placeholder="agent@vilka.app"
              value={email} onChange={(e) => setEmail(e.target.value)} required
            />
          </label>
          <label><span className="lbl">ПАРОЛЬ</span>
            <input
              type="password" autoComplete={mode === 'login' ? 'current-password' : 'new-password'}
              placeholder="••••••••" minLength={6}
              value={password} onChange={(e) => setPassword(e.target.value)} required
            />
          </label>

          {auth.error && <div className="auth-error">{String(auth.error).slice(0, 120)}</div>}

          <button className="btn primary block" type="submit" disabled={auth.isBusy}>
            {auth.isBusy ? 'УСТАНОВКА СОЕДИНЕНИЯ…' : 'ОТКРЫТЬ ДОСКУ →'}
          </button>

          {mode === 'login' ? (
            <button className="btn ghost small" type="button" onClick={() => setMode('register')}>
              Нет аккаунта? Регистрация
            </button>
          ) : (
            <button className="btn ghost small" type="button" onClick={() => setMode('login')}>
              Уже есть аккаунт? Вход
            </button>
          )}

          {demoMode && !loadToken() && (
            <a href="#" className="auth-demo-link" onClick={(e) => { e.preventDefault(); onDemoEnter(); }}>
              демо-вход без аккаунта
            </a>
          )}

          <div className="auth-footer">
            <span className="lbl">ДЕЙСТВИЯ ЛОГИРУЮТСЯ // AUDIT_LOG ENABLED</span>
            <span className="theme-dots" aria-label="тема">
              {THEMES.map((t) => (
                <button
                  key={t} type="button" title={t.toUpperCase()}
                  className={`theme-dot${t === theme ? ' active' : ''}`}
                  style={{ ['--swatch' as never]: THEME_SWATCH[t] }}
                  onClick={() => setTheme(t)}
                />
              ))}
            </span>
          </div>
        </form>
      </div>
    </div>
  );
}

export default function App() {
  const [route, setRoute] = useState<{ name: 'home' } | { name: 'board'; c: InvestigationCase }>({ name: 'home' });
  const toast = useToast();
  // ЭТАП 6: JWT-авторизация — прокидываем юзера и обработчик 402 в Board.
  const auth = useAuth();
  // ЭТАП 9: демо-режим (AUTH_ENFORCED=0) — скрываем gate без токена.
  const [demoEntered, setDemoEntered] = useState(false);

  useEffect(() => {
    const onVilka = (e: Event) => {
      const d = (e as CustomEvent).detail ?? {};
      if (d.type === 'unauthorized') auth.logout();
    };
    window.addEventListener('vilka-api', onVilka);
    return () => window.removeEventListener('vilka-api', onVilka);
  }, [auth]);

  const handlePaymentRequired = (u: AuthUser) => {
    toast.show(`Кредиты: ${u.credits_balance}. Пополните баланс.`);
  };

  /** ЭТАП 7: «Пополнить» — создаёт крипто-инвойс и открывает pay_url. */
  const handleTopUp = async () => {
    const res = await auth.checkout(25);
    if (res?.pay_url) {
      window.open(res.pay_url, '_blank', 'noopener');
      toast.show(`Инвойс на ${res.credits} кредитов создан`);
    } else {
      toast.show(auth.error ? String(auth.error).slice(0, 90) : 'Не удалось создать инвойс');
    }
  };

  // ЭТАП 7: нет JWT -> форма входа/регистрации поверх всего приложения.
  // ЭТАП 9: в демо-режиме (AUTH_ENFORCED=0) пользователь может войти без аккаунта — gate скрывается по demoEntered.
  const showAuthGate = !auth.isAuthed && !demoEntered;

  return (
    <>
      {route.name === 'home' ? (
        <Home
          toast={toast.show}
          onOpenCase={(c) => setRoute({ name: 'board', c })}
        />
      ) : (
        <Board
          key={route.c.id}
          initial={route.c}
          onExit={() => setRoute({ name: 'home' })}
          user={auth.user}
          onPaymentRequired={handlePaymentRequired}
          onTopUp={() => void handleTopUp()}
          onLogout={() => { auth.logout(); setDemoEntered(false); }}
        />
      )}
      {showAuthGate && <AuthGate auth={auth} onDemoEnter={() => setDemoEntered(true)} />}
      {toast.node}
    </>
  );
}
