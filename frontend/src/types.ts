export type NodeKind =
  | 'target'
  | 'person'
  | 'account'
  | 'handle'
  | 'phone'
  | 'email'
  | 'domain'
  | 'ip'
  | 'geo'
  | 'org'
  | 'event'
  | 'note'
  | 'image';

export interface KindMeta {
  label: string;
  short: string;
  glyph: string;
  hint: string;
}

export const KINDS: Record<NodeKind, KindMeta> = {
  target: { label: 'Цель',        short: 'цель',    glyph: '◎', hint: 'то, что расследуем' },
  person: { label: 'Персона',     short: 'персона', glyph: '☻', hint: 'человек' },
  account:{ label: 'Аккаунт',     short: 'аккаунт', glyph: '▣', hint: 'профиль на платформе' },
  handle: { label: 'Ник / хэндл', short: 'ник',     glyph: '@', hint: 'username, позывной' },
  phone:  { label: 'Телефон',     short: 'тел',     glyph: '☎', hint: 'номер, оператор' },
  email:  { label: 'Почта',       short: 'почта',   glyph: '✉', hint: 'e-mail, утечки' },
  domain: { label: 'Домен',       short: 'домен',   glyph: '⌘', hint: 'сайт, почтовый домен' },
  ip:     { label: 'IP / сеть',   short: 'ip',      glyph: '▪', hint: 'адрес, ASN' },
  geo:    { label: 'Геолокация',  short: 'гео',     glyph: '⚑', hint: 'место, адрес' },
  org:    { label: 'Организация', short: 'орг',     glyph: '⌂', hint: 'компания, группа' },
  event:  { label: 'Событие',     short: 'событие', glyph: '◷', hint: 'дата, таймлайн' },
  note:   { label: 'Заметка',     short: 'заметка', glyph: '¶', hint: 'мысль, гипотеза' },
  image:  { label: 'Изображение', short: 'фото', glyph: '🖼', hint: 'картинка, реверс-поиск' },
};

export const KIND_LIST = Object.keys(KINDS) as NodeKind[];

export type LinkStrength = 'strong' | 'medium' | 'weak';

export const STRENGTH_META: Record<LinkStrength, { label: string }> = {
  strong: { label: 'сильная' },
  medium: { label: 'средняя' },
  weak:   { label: 'слабая' },
};

/** Evidence reliability — from a rumor to confirmed. */
export type ConfLevel = 0 | 1 | 2 | 3;
export const CONF: Record<ConfLevel, { label: string }> = {
  0: { label: 'слух' },
  1: { label: 'гипотеза' },
  2: { label: 'подтверждено' },
  3: { label: 'железно' },
};

export interface SourceRef {
  id: string;
  title: string;
  url?: string;
  retrievedAt?: number;
}

export interface FactNode {
  id: string;
  kind: NodeKind;
  title: string;
  value?: string;
  notes?: string;
  tags: string[];
  sources: SourceRef[];
  conf: ConfLevel;
  x: number;
  y: number;
  createdAt: number;
  updatedAt: number;
  pinned?: boolean;
  /** ЭТАП 7: движок, выдавший факт (mock = симуляция, live = реальный источник). */
  engine?: 'mock' | 'live';
}

/** Тип связи, зеркаляет backend/models.py LinkKind (ЭТАП 1+). */
export type LinkKind =
  | 'belongs_to' | 'owned_by' | 'contact_of' | 'resolves_to' | 'hosted_at'
  | 'works_at' | 'located_in' | 'mentioned_in' | 'related_to' | 'leaked_with';

export interface FactLink {
  id: string;
  from: string;
  to: string;
  /** Свободная текстовая метка нити (UI) — см. также kind ниже. */
  relation: string;
  /** Машинный тип связи (проставляет бэкенд при обогащении). */
  kind?: LinkKind;
  /** Вес достоверности 0..1 (проставляет бэкенд). */
  weight?: number;
  strength: LinkStrength;
  note?: string;
  createdAt: number;
}

export type CaseStatus = 'active' | 'cold' | 'closed';

export const STATUS_META: Record<CaseStatus, { label: string }> = {
  active: { label: 'в работе' },
  cold:   { label: 'остыло' },
  closed: { label: 'закрыто' },
};

export interface InvestigationCase {
  id: string;
  name: string;
  codename?: string;
  summary?: string;
  status: CaseStatus;
  createdAt: number;
  updatedAt: number;
  nodes: Record<string, FactNode>;
  links: Record<string, FactLink>;
  view: { x: number; y: number; k: number };
}

export const uid = (): string =>
  Date.now().toString(36).slice(-4) + Math.random().toString(36).slice(2, 8);

export function newNode(partial: Partial<FactNode> & { kind: NodeKind; title: string }): FactNode {
  const now = Date.now();
  return {
    id: uid(),
    tags: [],
    sources: [],
    conf: 0,
    x: 0,
    y: 0,
    createdAt: now,
    updatedAt: now,
    ...partial,
  };
}

export function newLink(partial: Partial<FactLink> & { from: string; to: string }): FactLink {
  return {
    id: uid(),
    relation: '',
    strength: 'medium',
    createdAt: Date.now(),
    ...partial,
  };
}

export function emptyCase(name: string): InvestigationCase {
  const now = Date.now();
  return {
    id: uid(),
    name,
    status: 'active',
    createdAt: now,
    updatedAt: now,
    nodes: {},
    links: {},
    view: { x: 0, y: 0, k: 1 },
  };
}
