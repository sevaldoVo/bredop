import { useEffect, useMemo, useState } from 'react';
import { emptyCase, newNode, STATUS_META, type InvestigationCase } from '../types';
import { loadIndex, loadCase, saveCase, deleteCase, exportCases, importCases, type CaseMeta } from '../storage';
import { Modal, useConfirm, relTime, fmtDate } from '../ui';
import { DriftGraphCanvas } from './DriftGraphCanvas';

const NODE_W = 208;
const NODE_H = 96;

const CODENAMES = ['СЕРОКРЫЛ', 'КОРШУН', 'ГЛУБИННАЯ', 'ОПЁНКОВЫЙ', 'ТИХИЙ-7', 'КРЕМЕНЬ', 'ПОЛЫНЬ', 'ЖЕЛЕЗО'];
const randomCodename = () => CODENAMES[Math.floor(Math.random() * CODENAMES.length)];

export default function Home({
  onOpenCase, toast,
}: {
  onOpenCase: (c: InvestigationCase) => void;
  toast: (m: string) => void;
}) {
  const [metas, setMetas] = useState<CaseMeta[] | null>(null);
  const [creating, setCreating] = useState(false);
  const [name, setName] = useState('');
  const [codename, setCodename] = useState(randomCodename());
  const [summary, setSummary] = useState('');
  const [filter, setFilter] = useState<'all' | 'active' | 'cold' | 'closed'>('all');
  const confirm = useConfirm();

  const refresh = () => loadIndex().then(setMetas);
  useEffect(() => { refresh(); }, []);

  const open = async (id: string) => {
    const c = await loadCase(id);
    if (!c) { toast('Дело не нашлось в хранилище'); refresh(); return; }
    onOpenCase(c);
  };

  const create = async () => {
    const c = emptyCase(name.trim() || 'Без названия');
    c.codename = codename.trim() || undefined;
    c.summary = summary.trim() || undefined;

    // seed the board with the case's target pinned in the middle
    const target = newNode({ kind: 'target', title: c.name, conf: 3, x: -NODE_W / 2, y: -NODE_H / 2 });
    c.nodes[target.id] = target;

    await saveCase(c);
    setCreating(false);
    setName(''); setSummary(''); setCodename(randomCodename());
    onOpenCase(c);
  };

  const remove = async (m: CaseMeta) => {
    const ok = await confirm.ask(`Удалить дело «${m.name}» вместе со всей доской? Это необратимо.`);
    if (!ok) return;
    await deleteCase(m.id);
    refresh();
    toast('Дело удалено');
  };

  const doExport = async () => {
    const ids = metas ?? [];
    const cases: InvestigationCase[] = [];
    for (const m of ids) {
      const c = await loadCase(m.id);
      if (c) cases.push(c);
    }
    if (!cases.length) { toast('Нечего выгружать — дел нет'); return; }
    const blob = new Blob([exportCases(cases)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `vilka-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    toast(`Выгружено дел: ${cases.length}`);
  };

  const doImport = async (file: File) => {
    try {
      const text = await file.text();
      const n = await importCases(text);
      refresh();
      toast(`Загружено дел: ${n}`);
    } catch (e) {
      toast('Не получилось: ' + (e as Error).message);
    }
  };

  const list = useMemo(() => {
    if (!metas) return [];
    const sorted = [...metas].sort((a, b) => b.updatedAt - a.updatedAt);
    return filter === 'all' ? sorted : sorted.filter((m) => m.status === filter);
  }, [metas, filter]);

  return (
    <div className="app">
      <div className="topbar">
        <div className="brand">Вилка<small>v0.1 · локально</small></div>
        <div className="spacer" />
        {/* ЭТАП9 коммит7: Импорт/Экспорт убраны из шапки — живут только в tool-rail доски */}
      </div>

      <div className="home">
        {/* ЭТАП 9: фон-режим — дрейфующий граф из 20 нод под крупным заголовком */}
        <div className="home-hero-wrap">
        <div className="halftone-layer"><DriftGraphCanvas /></div>
        <section className="hero">
          <h1>Доска расследований<br />для тех, кто ищет <em>следы</em>, а не лайки.</h1>
          <p className="lede">
            ВИЛКА — это пробковая доска из детективных фильмов, только без пыли: карточки улик,
            красные нити между ними, достоверность каждой находки и ссылка на источник.
            Всё живёт у тебя на устройстве — ничего никуда не улетает. Работает на телефоне и на компе.
          </p>
          <div className="actions">
            <button className="btn primary" onClick={() => setCreating(true)}>＋ Новое дело</button>
            <button className="btn" onClick={() => document.getElementById('cases')?.scrollIntoView({ behavior: 'smooth' })}>Открыть последнее</button>
          </div>
        </section>
        </div>

        <section>
          <div className="section-title"><h2>Что внутри</h2><span className="rule" /><span className="stamp">только нужное</span></div>
          <div className="feature-grid">
            {[
              ['01', 'Карточки фактов', '12 типов: персона, ник, телефон, почта, домен, IP, гео, организация, событие, заметка. Каждая — с значением и комментарием.'],
              ['02', 'Красные нити', 'Тянешь связь от карточки к карточке, подписываешь («владеет», «один EXIF») и задаёшь силу: сильная / средняя / слабая.'],
              ['03', 'Достоверность', 'Шкала «слух → гипотеза → подтверждено → железно». Видно сразу, где уже факт, а где пока фантазия.'],
              ['04', 'Источники', 'К каждой карточке — ссылки и дата, когда это было найдено. Без источника это слух, а не улика.'],
              ['05', 'Поиск по делу', 'Мгновенно находит карточку по нику, номеру, тегу или тексту заметки и подлетает к ней на доске.'],
              ['06', 'Локальность', 'Данные лежат в IndexedDB твоего браузера. Экспорт/импорт одним JSON-файлом — можно перенести на другое устройство.'],
            ].map(([n, t, d]) => (
              <article className="feature" key={n}>
                <div className="num">{n}</div>
                <h3>{t}</h3>
                <p>{d}</p>
              </article>
            ))}
          </div>
        </section>

        <section id="cases">
          <div className="section-title">
            <h2>Дела</h2><span className="rule" />
            <div className="seg" style={{ flex: 'none' }}>
              {(['all', 'active', 'cold', 'closed'] as const).map((f) => (
                <button key={f} className={filter === f ? 'on alt' : ''} onClick={() => setFilter(f)}>
                  {f === 'all' ? 'все' : STATUS_META[f].label}
                </button>
              ))}
            </div>
          </div>

          {!metas && <div className="muted">грузим…</div>}
          {metas && !list.length && (
            <div className="empty-state">
              <span className="big-glyph">◎</span>
              {filter === 'all'
                ? <>Пусто. Создай первое дело — <b style={{ color: 'var(--bone)' }}>«Новое дело»</b> выше.</>
                : 'В этом статусе дел нет.'}
            </div>
          )}

          <div className="case-list">
            {list.map((m, i) => (
              <div key={m.id} className={`case-card ${m.status}`} onClick={() => open(m.id)}>
                <div className="idx">{String(i + 1).padStart(2, '0')}</div>
                <div style={{ minWidth: 0 }}>
                  <div className="name">{m.name}{m.codename ? <span className="mono muted"> «{m.codename}»</span> : null}</div>
                  <div className="meta">
                    <span>{m.nodeCount} карточек · {m.linkCount} нитей</span>
                    <span>обновлено {relTime(m.updatedAt)}</span>
                    <span>заведено {fmtDate(m.createdAt)}</span>
                  </div>
                </div>
                <div className="right">
                  <span className="chip">{STATUS_META[m.status].label}</span>
                  <button
                    className="btn ghost small"
                    onClick={(e) => { e.stopPropagation(); remove(m); }}
                    title="удалить дело">✕</button>
                </div>
              </div>
            ))}
          </div>
        </section>

        <footer style={{ marginTop: 56, borderTop: '1px solid var(--hairline-soft)', paddingTop: 18 }} className="muted">
          <div className="stamp" style={{ marginBottom: 6 }}>коротко о горячих клавишах на доске</div>
          <span className="kbd">N</span> новая карточка &nbsp;·&nbsp;
          <span className="kbd">L</span> нить &nbsp;·&nbsp;
          <span className="kbd">F</span> поиск &nbsp;·&nbsp;
          <span className="kbd">Del</span> удалить выбранное &nbsp;·&nbsp;
          <span className="kbd">Esc</span> отмена. На телефоне — те же кнопки слева сверху, жесты: один палец — двигать, два — зум.
        </footer>
      </div>

      {creating && (
        <Modal title="Новое дело" sub="название можно поменять позже" onClose={() => setCreating(false)}>
          <div className="field"><label>Название *</label>
            <input autoFocus value={name} placeholder="например: поиск владельца @serkryl"
              onChange={(e) => setName(e.target.value)} onKeyDown={(e) => { if (e.key === 'Enter') create(); }} /></div>
          <div className="field"><label>Позывной</label>
            <div className="row">
              <input className="mono" value={codename} onChange={(e) => setCodename(e.target.value.toUpperCase())} />
              <button className="btn small" onClick={() => setCodename(randomCodename())}>другой</button>
            </div></div>
          <div className="field"><label>Суть (что ищем)</label>
            <textarea value={summary} placeholder="коротко: цель, рамки, что считаем успехом"
              onChange={(e) => setSummary(e.target.value)} /></div>
          <div className="actions">
            <button className="btn" onClick={() => setCreating(false)}>Отмена</button>
            <button className="btn primary" onClick={create}>Открыть доску</button>
          </div>
        </Modal>
      )}

      {confirm.node}
    </div>
  );
}

