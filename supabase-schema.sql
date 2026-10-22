-- ============================================================
-- Схема базы Supabase для бота-собеседника (Олег) и его Mini App.
-- Куда вставлять: Supabase-проект → левое меню SQL → Query 1 →
-- вставить весь этот файл → кнопка RUN (Ctrl+Enter).
-- Повторный запуск безопасен (IF NOT EXISTS).
-- ============================================================

create table if not exists settings (
  chat_id    bigint primary key,
  data       text not null,                -- JSON настроек чата
  updated_at timestamptz default now()
);

create table if not exists history (
  chat_id bigint primary key,
  msgs    text not null                  -- JSON последних сообщений диалога
);

create table if not exists reminders (
  id      bigint generated always as identity primary key,
  chat_id bigint not null,
  due     bigint not null,               -- unix-время срабатывания
  text    text
);

create table if not exists bans (
  chat_id bigint not null,
  user_id bigint not null,
  primary key (chat_id, user_id)
);

create table if not exists daily (
  chat_id bigint not null,
  msg_id  bigint not null,
  ts      bigint not null,               -- unix-время сообщения
  user_id bigint not null,
  name    text,
  text    text,
  laughs  bigint default 0,
  primary key (chat_id, msg_id)
);
create index if not exists idx_daily_ts on daily (chat_id, ts);

create table if not exists stats (
  chat_id bigint not null,
  user_id bigint not null,
  name    text,
  msgs    bigint default 0,              -- активность
  laughs  bigint default 0,              -- реакции 😂 на сообщения
  wins    bigint default 0,              -- победы в играх
  points  bigint default 0,              -- общий счёт (рейтинг /top)
  warns   bigint default 0,
  primary key (chat_id, user_id)
);

create table if not exists dossier (
  chat_id bigint not null,
  user_id bigint not null,
  name    text,
  facts   text,                          -- JSON «досье» (/remember)
  primary key (chat_id, user_id)
);

-- Примечание: RLS (политики доступа) не настраиваем — доступ к базе
-- идёт только по секретному SERVICE KEY, который стоит в Secrets
-- (GitHub Actions и Vercel) и наружу не светится.
