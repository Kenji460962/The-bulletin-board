import express, { Request, Response, NextFunction } from 'express';
import http from 'http';
import path from 'path';
import fs from 'fs';
import os from 'os';
import crypto from 'crypto';
import cookieParser from 'cookie-parser';
import session from 'express-session';
import multer from 'multer';
import { WebSocketServer, WebSocket } from 'ws';

const app = express();
const server = http.createServer(app);
const PORT = process.env.PORT ? parseInt(process.env.PORT, 10) : 3000;

// Ensure storage directories exist
const DATA_DIR = path.join(process.cwd(), 'data');
const UPLOAD_DIR = path.join(process.cwd(), 'static', 'uploads');
if (!fs.existsSync(DATA_DIR)) fs.mkdirSync(DATA_DIR, { recursive: true });
if (!fs.existsSync(UPLOAD_DIR)) fs.mkdirSync(UPLOAD_DIR, { recursive: true });

// Setup view engine
app.set('view engine', 'ejs');
app.set('views', path.join(process.cwd(), 'views'));

// Middlewares
app.use(express.json());
app.use(express.urlencoded({ extended: true }));
app.use(cookieParser());
app.use(session({
  secret: process.env.SESSION_SECRET || 'talk-ch-secret-key-12345',
  resave: false,
  saveUninitialized: true,
  cookie: { maxAge: 1000 * 60 * 60 * 24 * 7 }
}));

// Static files
app.use('/static', express.static(path.join(process.cwd(), 'static')));
app.use('/uploads', express.static(UPLOAD_DIR));

// Thread Categories
export interface ThreadCategory {
  id: string;
  name: string;
  icon: string;
  description: string;
  badge_color: string;
}

export const DEFAULT_CATEGORIES: ThreadCategory[] = [
  { id: 'notice', name: 'お知らせ', icon: '📢', description: '運営からのお知らせ・告知', badge_color: '#38bdf8' },
  { id: 'general', name: '雑談', icon: '💬', description: '日々の出来事やフリートーク', badge_color: '#0ea5e9' },
  { id: 'academic', name: '技術・学問', icon: '🔬', description: '物理・数学・開発・学問全般', badge_color: '#06b6d4' },
  { id: 'feedback', name: '運営・要望', icon: '🛡️', description: '新機能やUI改善リクエスト', badge_color: '#a855f7' },
  { id: 'anime', name: 'アニメ・漫画', icon: '🎨', description: 'アニメ、漫画、創作、サブカル', badge_color: '#ec4899' },
  { id: 'gadget', name: 'ガジェット', icon: '📱', description: 'スマホ、PC、周辺機器、ハードウェア', badge_color: '#10b981' },
  { id: 'tech', name: 'AI・IT', icon: '💻', description: '人工知能、Web技術、プログラミング', badge_color: '#3b82f6' },
  { id: 'gaming', name: 'ゲーム', icon: '🎮', description: 'ゲーム攻略、対戦募集、レトロゲーム', badge_color: '#f59e0b' }
];

// Multer for file uploads
const storage = multer.diskStorage({
  destination: (_req, _file, cb) => {
    cb(null, UPLOAD_DIR);
  },
  filename: (_req, file, cb) => {
    const ext = path.extname(file.originalname).toLowerCase() || '.png';
    const unique = `${Date.now()}_${crypto.randomBytes(6).toString('hex')}${ext}`;
    cb(null, unique);
  }
});
const upload = multer({
  storage,
  limits: { fileSize: 5 * 1024 * 1024 }
});

// Database Models & In-memory Store with JSON persistence
interface Reply {
  id: number;
  thread_id: number;
  post_num: number;
  author: string;
  content: string;
  image_url: string | null;
  user_id: string;
  is_admin: boolean;
  is_op: boolean;
  role: string | null;
  created_at: string;
  date: string;
}

interface Thread {
  id: number;
  title: string;
  category: string;
  op_user_id: string;
  is_pinned?: boolean;
  created_at: string;
  updated_at: string;
  replies: Reply[];
}

interface OthelloRoom {
  room_code: string;
  player1_name: string;
  player1_id: string;
  player2_name: string;
  player2_id: string;
  board: string;
  turn: 'B' | 'W';
  status: 'waiting' | 'playing' | 'finished';
  winner: string | null;
  created_at: string;
  updated_at: string;
}

interface ChessRoom {
  room_code: string;
  white_name: string;
  white_id: string;
  black_name: string;
  black_id: string;
  board: string[]; // 64 items
  turn: 'w' | 'b';
  status: 'waiting' | 'playing' | 'finished';
  winner: string | null;
  in_check: string | null;
  castling: string;
  en_passant: [number, number] | null;
  created_at: string;
  updated_at: string;
}

interface ActiveUser {
  token: string;
  location: string;
  last_seen: number;
}

interface DatabaseSchema {
  categories: ThreadCategory[];
  threads: Thread[];
  admin_message: string;
  othello_rooms: Record<string, OthelloRoom>;
  chess_rooms: Record<string, ChessRoom>;
  banned_users: string[];
}

const DB_FILE = path.join(DATA_DIR, 'db.json');

function createInitialBoard(): string {
  const b = new Array(64).fill('.');
  b[3 * 8 + 3] = 'W';
  b[3 * 8 + 4] = 'B';
  b[4 * 8 + 3] = 'B';
  b[4 * 8 + 4] = 'W';
  return b.join('');
}

function createInitialChessBoard(): string[] {
  return [
    'bR', 'bN', 'bB', 'bQ', 'bK', 'bB', 'bN', 'bR',
    'bP', 'bP', 'bP', 'bP', 'bP', 'bP', 'bP', 'bP',
    '', '', '', '', '', '', '', '',
    '', '', '', '', '', '', '', '',
    '', '', '', '', '', '', '', '',
    '', '', '', '', '', '', '', '',
    'wP', 'wP', 'wP', 'wP', 'wP', 'wP', 'wP', 'wP',
    'wR', 'wN', 'wB', 'wQ', 'wK', 'wB', 'wN', 'wR'
  ];
}

const initialDb: DatabaseSchema = {
  categories: DEFAULT_CATEGORIES,
  threads: [
    {
      id: 1,
      title: '【お知らせ】talk-chへようこそ！公式ガイドライン＆使い方',
      category: 'notice',
      op_user_id: 'ADMIN★ペンギン',
      is_pinned: true,
      created_at: '2026-08-10T10:00:00.000Z',
      updated_at: '2026-08-20T18:00:00.000Z',
      replies: [
        {
          id: 1,
          thread_id: 1,
          post_num: 1,
          author: 'ペンギン★',
          content: 'トークちゃんねるへようこそ！\n完全匿名・登録不要で、高速なリアルタイム更新、オセロ＆チェス対戦、ジャンル別スレッドを楽しめます。\n誰でも自由に新しいスレッドを作成して語り合ってください！',
          image_url: null,
          user_id: 'ADMIN_PENGUIN',
          is_admin: true,
          is_op: true,
          role: 'admin',
          created_at: '2026-08-10T10:00:00.000Z',
          date: '2026-08-10 10:00:00'
        }
      ]
    },
    {
      id: 2,
      title: '【ロビー】雑談・初めまして・なんでも語るスレ Part 42',
      category: 'general',
      op_user_id: '8a9f2b1c',
      is_pinned: true,
      created_at: '2026-08-15T14:00:00.000Z',
      updated_at: '2026-08-21T20:30:00.000Z',
      replies: [
        {
          id: 2,
          thread_id: 2,
          post_num: 1,
          author: '名無しさん',
          content: 'なんでも気楽に話す総合雑談スレです！\n挨拶や最近の出来事など自由に書き込んでください。',
          image_url: null,
          user_id: '8a9f2b1c',
          is_admin: false,
          is_op: true,
          role: null,
          created_at: '2026-08-15T14:00:00.000Z',
          date: '2026-08-15 14:00:00'
        },
        {
          id: 3,
          thread_id: 2,
          post_num: 2,
          author: '名無しさん',
          content: 'ダークモードのデザインすごく見やすくて気に入りました！',
          image_url: null,
          user_id: '3f7c1a9d',
          is_admin: false,
          is_op: false,
          role: null,
          created_at: '2026-08-15T14:10:00.000Z',
          date: '2026-08-15 14:10:00'
        }
      ]
    },
    {
      id: 3,
      title: '【技術部】物理・数学・プログラミング・Web開発 談話室',
      category: 'academic',
      op_user_id: 'DevTech99',
      is_pinned: true,
      created_at: '2026-08-18T09:30:00.000Z',
      updated_at: '2026-08-21T19:45:00.000Z',
      replies: [
        {
          id: 4,
          thread_id: 3,
          post_num: 1,
          author: '技術部員',
          content: '物理シミュレーション、数学の難問、TypeScript/Python開発、サーバーインフラについて語り合いましょう。',
          image_url: null,
          user_id: 'DevTech99',
          is_admin: false,
          is_op: true,
          role: null,
          created_at: '2026-08-18T09:30:00.000Z',
          date: '2026-08-18 09:30:00'
        }
      ]
    },
    {
      id: 4,
      title: '【提案・要望】新機能やUI改善リクエスト受付所',
      category: 'feedback',
      op_user_id: 'ADMIN_PENGUIN',
      is_pinned: true,
      created_at: '2026-08-19T12:00:00.000Z',
      updated_at: '2026-08-21T18:20:00.000Z',
      replies: [
        {
          id: 5,
          thread_id: 4,
          post_num: 1,
          author: 'ペンギン★',
          content: '「こんな機能が欲しい」「ここを改善してほしい」というアイデアがあれば気軽に書き込んでください！\n運営チームで定期的に確認・検討します。',
          image_url: null,
          user_id: 'ADMIN_PENGUIN',
          is_admin: true,
          is_op: true,
          role: 'admin',
          created_at: '2026-08-19T12:00:00.000Z',
          date: '2026-08-19 12:00:00'
        }
      ]
    },
    {
      id: 5,
      title: '🎮 オンラインオセロ・チェス対戦相手募集スレ',
      category: 'gaming',
      op_user_id: 'BoardGamer',
      is_pinned: false,
      created_at: '2026-08-20T15:00:00.000Z',
      updated_at: '2026-08-21T21:00:00.000Z',
      replies: [
        {
          id: 6,
          thread_id: 5,
          post_num: 1,
          author: '名無し棋士',
          content: '部屋を作ったらコードを貼ってください！誰でも歓迎です。',
          image_url: null,
          user_id: 'BoardGamer',
          is_admin: false,
          is_op: true,
          role: null,
          created_at: '2026-08-20T15:00:00.000Z',
          date: '2026-08-20 15:00:00'
        }
      ]
    }
  ],
  admin_message: '【お知らせ】talk-chがモダンUIリニューアルオープン！高速ストリーム閲覧、アンカーホバープレビュー、オセロ＆チェスを搭載しました。',
  othello_rooms: {},
  chess_rooms: {},
  banned_users: []
};

let db: DatabaseSchema = initialDb;

function loadDb() {
  try {
    if (fs.existsSync(DB_FILE)) {
      const data = fs.readFileSync(DB_FILE, 'utf-8');
      const parsed = JSON.parse(data);
      db = { ...initialDb, ...parsed };
      
      // Ensure categories exist
      if (!Array.isArray(db.categories) || db.categories.length === 0) {
        db.categories = DEFAULT_CATEGORIES;
      }
      
      // Ensure all threads have a valid category
      if (Array.isArray(db.threads)) {
        for (const t of db.threads) {
          if (!t.category) {
            t.category = 'general';
          }
        }
      }
    } else {
      saveDb();
    }
  } catch (e) {
    console.error('Error loading db.json:', e);
    db = initialDb;
  }
}

function saveDb() {
  try {
    fs.writeFileSync(DB_FILE, JSON.stringify(db, null, 2), 'utf-8');
  } catch (e) {
    console.error('Error saving db.json:', e);
  }
}

loadDb();

// Active users tracking
const activeUsers: Map<string, ActiveUser> = new Map();

function trackUser(req: Request, location: string): void {
  let token = req.cookies.user_bbs_token;
  if (!token) {
    token = crypto.randomUUID();
    req.res?.cookie('user_bbs_token', token, { maxAge: 1000 * 60 * 60 * 24 * 30, httpOnly: true });
  }
  activeUsers.set(token, {
    token,
    location,
    last_seen: Date.now()
  });
}

function getActiveCount(location?: string): number {
  const cutoff = Date.now() - 2 * 60 * 1000;
  let count = 0;
  for (const [token, user] of activeUsers.entries()) {
    if (user.last_seen < cutoff) {
      activeUsers.delete(token);
    } else if (!location || user.location === location) {
      count++;
    }
  }
  return Math.max(1, count);
}

// User ID generator
function getDailyUserId(req: Request): string {
  const ip = (req.headers['x-forwarded-for'] as string) || req.socket.remoteAddress || '127.0.0.1';
  const today = new Date().toISOString().slice(0, 10);
  const hash = crypto.createHash('md5').update(`${ip}_${today}`).digest('hex');
  return hash.substring(0, 8);
}

function formatDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  const h = String(d.getHours()).padStart(2, '0');
  const min = String(d.getMinutes()).padStart(2, '0');
  const s = String(d.getSeconds()).padStart(2, '0');
  return `${y}/${m}/${day} ${h}:${min}:${s}`;
}

const NG_WORDS: Record<string, string> = {
  '死ね': '〇ね',
  'しね': '〇ね',
  '殺す': '〇す',
  'ころす': '〇す',
  'エロ': 'エ〇',
  'えろ': 'え〇',
  'まんこ': 'ま〇こ',
  'ちんこ': 'ち〇こ',
  'セックス': 'セ〇クス',
  'せっくす': 'せ〇くす',
  'バカ': 'バ*',
  'アホ': 'ア*'
};

function filterNgWords(text: string): string {
  let res = text;
  for (const [ng, rep] of Object.entries(NG_WORDS)) {
    res = res.replaceAll(ng, rep);
  }
  return res;
}

function escapeHtml(str: string): string {
  return str
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatPostContent(content: string): string {
  const escaped = escapeHtml(content);
  // Convert >>1 or >>123 to anchor
  return escaped.replace(/&gt;&gt;(\d+)/g, '<a href="#post-$1" class="post-anchor" onclick="scrollToPost($1); return false;">&gt;&gt;$1</a>');
}

// System Stats Tracking
let prevCpuTimes: { user: number; nice: number; sys: number; idle: number; irq: number } | null = null;
let prevNetRx = 0;
let prevNetTx = 0;
let prevNetTime = Date.now();

function getCpuUsage(): number {
  const cpus = os.cpus();
  let totalUser = 0, totalNice = 0, totalSys = 0, totalIdle = 0, totalIrq = 0;
  for (const cpu of cpus) {
    totalUser += cpu.times.user;
    totalNice += cpu.times.nice;
    totalSys += cpu.times.sys;
    totalIdle += cpu.times.idle;
    totalIrq += cpu.times.irq;
  }

  if (!prevCpuTimes) {
    prevCpuTimes = { user: totalUser, nice: totalNice, sys: totalSys, idle: totalIdle, irq: totalIrq };
    return 12.5;
  }

  const deltaUser = totalUser - prevCpuTimes.user;
  const deltaNice = totalNice - prevCpuTimes.nice;
  const deltaSys = totalSys - prevCpuTimes.sys;
  const deltaIdle = totalIdle - prevCpuTimes.idle;
  const deltaIrq = totalIrq - prevCpuTimes.irq;
  const totalDelta = deltaUser + deltaNice + deltaSys + deltaIdle + deltaIrq;

  prevCpuTimes = { user: totalUser, nice: totalNice, sys: totalSys, idle: totalIdle, irq: totalIrq };
  if (totalDelta <= 0) return 10.0;
  const usage = ((totalDelta - deltaIdle) / totalDelta) * 100;
  return Math.max(0, Math.min(100, Math.round(usage * 10) / 10));
}

function getNetworkSpeed(): { rx: number; tx: number } {
  try {
    if (fs.existsSync('/proc/net/dev')) {
      const data = fs.readFileSync('/proc/net/dev', 'utf-8');
      const lines = data.split('\n').slice(2);
      let rxTotal = 0, txTotal = 0;
      for (const line of lines) {
        if (!line.includes(':')) continue;
        const [iface, rest] = line.split(':');
        if (iface.trim() === 'lo') continue;
        const parts = rest.trim().split(/\s+/);
        rxTotal += parseInt(parts[0], 10) || 0;
        txTotal += parseInt(parts[8], 10) || 0;
      }
      const now = Date.now();
      const dt = (now - prevNetTime) / 1000;
      let rxSpeed = 0, txSpeed = 0;
      if (prevNetRx > 0 && dt > 0) {
        rxSpeed = Math.max(0, Math.round(((rxTotal - prevNetRx) / 1024 / dt) * 10) / 10);
        txSpeed = Math.max(0, Math.round(((txTotal - prevNetTx) / 1024 / dt) * 10) / 10);
      }
      prevNetRx = rxTotal;
      prevNetTx = txTotal;
      prevNetTime = now;
      return { rx: rxSpeed, tx: txSpeed };
    }
  } catch (e) {
    // fallback
  }
  return { rx: 4.2, tx: 6.8 };
}

// WebSockets Server
const wss = new WebSocketServer({ noServer: true });
const wsRooms = new Map<string, Set<WebSocket>>();

server.on('upgrade', (request, socket, head) => {
  const url = new URL(request.url || '', `http://${request.headers.host}`);
  const pathname = url.pathname;

  if (pathname.startsWith('/ws/')) {
    wss.handleUpgrade(request, socket, head, (ws) => {
      wss.emit('connection', ws, request);
    });
  } else {
    socket.destroy();
  }
});

wss.on('connection', (ws, request) => {
  const url = new URL(request.url || '', `http://${request.headers.host}`);
  const roomKey = url.pathname.replace(/^\/ws\//, '');

  if (!wsRooms.has(roomKey)) {
    wsRooms.set(roomKey, new Set());
  }
  wsRooms.get(roomKey)!.add(ws);

  ws.on('close', () => {
    const clients = wsRooms.get(roomKey);
    if (clients) {
      clients.delete(ws);
      if (clients.size === 0) wsRooms.delete(roomKey);
    }
  });
});

function broadcastWs(roomKey: string, payload: any) {
  const clients = wsRooms.get(roomKey);
  if (clients) {
    const msg = JSON.stringify(payload);
    for (const client of clients) {
      if (client.readyState === WebSocket.OPEN) {
        client.send(msg);
      }
    }
  }
}

// -------------------------------------------------------------
// ROUTES
// -------------------------------------------------------------

// Health Check for Railway & Cloud hosting
app.get('/health', (_req: Request, res: Response) => {
  res.status(200).json({ status: 'ok', time: new Date().toISOString() });
});

app.get('/api/health', (_req: Request, res: Response) => {
  res.status(200).json({ status: 'ok', uptime: Math.round(process.uptime()), memory: process.memoryUsage() });
});

// Admin Login & Session Management
app.post('/api/admin/login', (req: Request, res: Response) => {
  const { password } = req.body;
  if (password === 'penguin8823' || password === 'talk8823admin' || password === 'admin') {
    (req.session as any).staff_role = 'admin';
    (req.session as any).staff_name = 'ペンギン★';
    (req.session as any).is_penguin = true;
    return res.json({ success: true, user: { name: 'ペンギン★', role: 'admin' } });
  }
  return res.status(401).json({ error: 'パスワードが正しくありません' });
});

app.post('/api/admin/logout', (req: Request, res: Response) => {
  req.session.destroy(() => {
    res.json({ success: true });
  });
});

app.get('/api/admin/me', (req: Request, res: Response) => {
  const isAdminUser = Boolean((req.session as any)?.staff_role === 'admin' || (req.session as any)?.staff_role === 'sub_admin');
  res.json({
    is_admin: isAdminUser,
    role: (req.session as any)?.staff_role || null,
    name: (req.session as any)?.staff_name || null
  });
});

// Categories API
app.get('/api/categories', (_req: Request, res: Response) => {
  const categories = db.categories || DEFAULT_CATEGORIES;
  const categoryCounts: Record<string, number> = {
    all: db.threads.length
  };
  for (const cat of categories) {
    categoryCounts[cat.id] = db.threads.filter(t => (t.category || 'general') === cat.id).length;
  }
  res.json({ categories, counts: categoryCounts });
});

// Admin: Add Category
app.post('/api/admin/categories', (req: Request, res: Response) => {
  const isAdminUser = Boolean((req.session as any)?.staff_role === 'admin' || (req.session as any)?.staff_role === 'sub_admin');
  const adminPassword = req.body.admin_password;
  const isAuth = isAdminUser || adminPassword === 'talk8823admin' || adminPassword === 'penguin8823';

  if (!isAuth) {
    return res.status(403).json({ error: '管理者権限が必要です。' });
  }

  const { id, name, icon, description, badge_color } = req.body;
  if (!name || !name.trim()) {
    return res.status(400).json({ error: 'ジャンル名を入力してください。' });
  }

  const catId = (id || name.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '') || `cat_${Date.now()}`).trim();
  
  if (!db.categories) db.categories = [...DEFAULT_CATEGORIES];
  
  // Check duplicate
  const exists = db.categories.find(c => c.id === catId);
  if (exists) {
    exists.name = name.trim();
    exists.icon = icon?.trim() || '🏷️';
    exists.description = description?.trim() || '';
    exists.badge_color = badge_color?.trim() || '#38bdf8';
  } else {
    db.categories.push({
      id: catId,
      name: name.trim(),
      icon: icon?.trim() || '🏷️',
      description: description?.trim() || '',
      badge_color: badge_color?.trim() || '#38bdf8'
    });
  }

  saveDb();
  res.json({ success: true, categories: db.categories });
});

// Admin: Delete Category
app.delete('/api/admin/categories/:id', (req: Request, res: Response) => {
  const isAdminUser = Boolean((req.session as any)?.staff_role === 'admin' || (req.session as any)?.staff_role === 'sub_admin');
  const adminPassword = req.headers['x-admin-password'] || req.query.admin_password;
  const isAuth = isAdminUser || adminPassword === 'talk8823admin' || adminPassword === 'penguin8823';

  if (!isAuth) {
    return res.status(403).json({ error: '管理者権限が必要です。' });
  }

  const catId = req.params.id;
  if (catId === 'general' || catId === 'notice') {
    return res.status(400).json({ error: '基本ジャンルは削除できません。' });
  }

  if (!db.categories) db.categories = [...DEFAULT_CATEGORIES];
  db.categories = db.categories.filter(c => c.id !== catId);

  // Re-assign threads with deleted category to general
  for (const t of db.threads) {
    if (t.category === catId) {
      t.category = 'general';
    }
  }

  saveDb();
  res.json({ success: true, categories: db.categories });
});

// Admin: Toggle Thread Pin
app.post('/api/admin/thread/:id/toggle_pin', (req: Request, res: Response) => {
  const isAdminUser = Boolean((req.session as any)?.staff_role === 'admin' || (req.session as any)?.staff_role === 'sub_admin');
  const adminPassword = req.body.admin_password;
  const isAuth = isAdminUser || adminPassword === 'talk8823admin' || adminPassword === 'penguin8823';

  if (!isAuth) {
    return res.status(403).json({ error: '管理者権限が必要です。' });
  }

  const threadId = parseInt(req.params.id, 10);
  const thread = db.threads.find(t => t.id === threadId);
  if (!thread) {
    return res.status(404).json({ error: 'スレッドが見つかりません。' });
  }

  thread.is_pinned = !thread.is_pinned;
  saveDb();
  res.json({ success: true, is_pinned: thread.is_pinned });
});

// Threads API for dynamic filtering and sorting
app.get('/api/threads', (req: Request, res: Response) => {
  const q = (req.query.q as string || '').trim().toLowerCase();
  const category = (req.query.category as string || 'all').trim().toLowerCase();
  const sort = (req.query.sort as string || 'latest').trim();

  const categories = db.categories || DEFAULT_CATEGORIES;
  let list = [...db.threads];

  if (category && category !== 'all') {
    list = list.filter(t => (t.category || 'general') === category);
  }
  if (q) {
    list = list.filter(t => t.title.toLowerCase().includes(q) || t.id.toString() === q);
  }

  // Sort options:
  // - latest: 最終更新順（最新レスまたは作成が新しい順）
  // - number_desc: スレッド番号が大きい順（最新作成順）
  // - number_asc: スレッド番号が小さい順（#1から順）
  // - replies: レス数が多い順
  // - active: 閲覧人数が多い順
  list.sort((a, b) => {
    // Pinned threads always on top unless specific sorting chosen
    if (a.is_pinned && !b.is_pinned) return -1;
    if (!a.is_pinned && b.is_pinned) return 1;

    if (sort === 'number_desc') {
      return b.id - a.id;
    } else if (sort === 'number_asc') {
      return a.id - b.id;
    } else if (sort === 'replies') {
      return b.replies.length - a.replies.length;
    } else if (sort === 'active') {
      return getActiveCount(`thread:${b.id}`) - getActiveCount(`thread:${a.id}`);
    } else {
      // Default: latest updated
      const timeA = new Date(a.updated_at || a.created_at).getTime();
      const timeB = new Date(b.updated_at || b.created_at).getTime();
      return timeB - timeA;
    }
  });

  const pagedThreads = list.map(t => {
    const cat = categories.find(c => c.id === (t.category || 'general')) || categories[0];
    return {
      id: t.id,
      title: t.title,
      category: t.category || 'general',
      category_name: cat?.name || '雑談',
      category_icon: cat?.icon || '💬',
      category_color: cat?.badge_color || '#38bdf8',
      is_pinned: Boolean(t.is_pinned),
      replies_count: t.replies.length,
      created_at: t.created_at,
      updated_at: t.updated_at,
      thread_active_count: getActiveCount(`thread:${t.id}`)
    };
  });

  res.json({ threads: pagedThreads, total: list.length });
});

// 1. Home / Index
app.get('/', (req: Request, res: Response) => {
  trackUser(req, 'lobby');
  const page = Math.max(1, parseInt(req.query.page as string, 10) || 1);
  const q = (req.query.q as string || '').trim().toLowerCase();
  const selectedCategory = (req.query.category as string || 'all').trim().toLowerCase();
  const sort = (req.query.sort as string || 'latest').trim();
  const activeTab = (req.query.tab as string || 'lobby').trim().toLowerCase();
  const perPage = 25;

  const categories = db.categories || DEFAULT_CATEGORIES;
  let list = [...db.threads];

  if (selectedCategory && selectedCategory !== 'all') {
    list = list.filter(t => (t.category || 'general') === selectedCategory);
  }
  if (q) {
    list = list.filter(t => t.title.toLowerCase().includes(q) || t.id.toString() === q);
  }

  // Calculate counts per category
  const categoryCounts: Record<string, number> = {
    all: db.threads.length
  };
  for (const cat of categories) {
    categoryCounts[cat.id] = db.threads.filter(t => (t.category || 'general') === cat.id).length;
  }

  // Apply sorting
  list.sort((a, b) => {
    if (a.is_pinned && !b.is_pinned) return -1;
    if (!a.is_pinned && b.is_pinned) return 1;

    if (sort === 'number_desc') {
      return b.id - a.id;
    } else if (sort === 'number_asc') {
      return a.id - b.id;
    } else if (sort === 'replies') {
      return b.replies.length - a.replies.length;
    } else if (sort === 'active') {
      return getActiveCount(`thread:${b.id}`) - getActiveCount(`thread:${a.id}`);
    } else {
      // Default: latest updated
      const timeA = new Date(a.updated_at || a.created_at).getTime();
      const timeB = new Date(b.updated_at || b.created_at).getTime();
      return timeB - timeA;
    }
  });

  const total = list.length;
  const start = (page - 1) * perPage;
  const pagedThreads = list.slice(start, start + perPage).map(t => {
    const cat = categories.find(c => c.id === (t.category || 'general')) || categories[0];
    return {
      id: t.id,
      title: t.title,
      category: t.category || 'general',
      category_name: cat?.name || '雑談',
      category_icon: cat?.icon || '💬',
      category_color: cat?.badge_color || '#38bdf8',
      is_pinned: Boolean(t.is_pinned),
      replies_count: t.replies.length,
      created_at: t.created_at,
      updated_at: t.updated_at,
      thread_active_count: getActiveCount(`thread:${t.id}`)
    };
  });

  // Featured threads: Top 4 threads sorted by online active user count
  const featuredThreads = db.threads
    .map(t => {
      const active = getActiveCount(`thread:${t.id}`) || Math.max(1, ((t.id * 5) % 17) + 2);
      return {
        thread: t,
        active_count: active
      };
    })
    .sort((a, b) => {
      // Primary: Most online users
      if (b.active_count !== a.active_count) {
        return b.active_count - a.active_count;
      }
      // Secondary: Reply count
      return b.thread.replies.length - a.thread.replies.length;
    })
    .slice(0, 4)
    .map(item => {
      const t = item.thread;
      const cat = categories.find(c => c.id === (t.category || 'general')) || categories[0];
      return {
        id: t.id,
        title: t.title,
        category: t.category || 'general',
        category_name: cat?.name || '雑談',
        category_icon: cat?.icon || '💬',
        category_color: cat?.badge_color || '#38bdf8',
        is_pinned: Boolean(t.is_pinned),
        replies_count: t.replies.length,
        created_at: t.created_at,
        updated_at: t.updated_at,
        thread_active_count: item.active_count
      };
    });

  const hasNext = start + perPage < total;
  const isAdminUser = Boolean((req.session as any)?.staff_role === 'admin' || (req.session as any)?.staff_role === 'sub_admin');

  res.render('index', {
    threads: pagedThreads,
    featured_threads: featuredThreads,
    categories,
    category_counts: categoryCounts,
    selected_category: selectedCategory,
    selected_sort: sort,
    active_tab: activeTab,
    total_threads: total,
    active_count: getActiveCount() || 49,
    admin_message: db.admin_message,
    current_page: page,
    has_next: hasNext,
    search_query: q,
    is_admin_user: isAdminUser
  });
});

// API Active Count
app.get('/api/lobby/active_count', (_req: Request, res: Response) => {
  res.json({ active_count: getActiveCount() });
});

// API Server Stats
app.get('/api/server_stats', (_req: Request, res: Response) => {
  const cpu = getCpuUsage();
  const totalMem = Math.round(os.totalmem() / 1024 / 1024);
  const freeMem = Math.round(os.freemem() / 1024 / 1024);
  const usedMem = totalMem - freeMem;
  const memPercent = Math.round((usedMem / totalMem) * 100);
  const net = getNetworkSpeed();

  res.json({
    cpu_percent: cpu,
    memory_percent: memPercent,
    memory_used_mb: usedMem,
    memory_total_mb: totalMem,
    net_rx_kbps: net.rx,
    net_tx_kbps: net.tx
  });
});

// Update Admin Message
app.post('/update_admin_message', (req: Request, res: Response) => {
  const pwd = req.body.admin_password;
  const msg = req.body.message;
  if (pwd === 'admin123' || (req.session as any)?.staff_role === 'admin') {
    db.admin_message = msg;
    saveDb();
  }
  res.redirect('/');
});

// Create Thread
app.post('/create_thread', (req: Request, res: Response) => {
  const categories = db.categories || DEFAULT_CATEGORIES;
  const title = (req.body.title || '').trim().slice(0, 50);
  const rawCategory = (req.body.category || 'general').trim().toLowerCase();
  const category = categories.some(c => c.id === rawCategory) ? rawCategory : 'general';

  if (!title) {
    if (req.xhr || req.headers.accept?.includes('application/json')) {
      return res.status(400).json({ error: 'スレッドタイトルを入力してください' });
    }
    return res.redirect('/?tab=threads');
  }

  const userId = getDailyUserId(req);
  const newId = db.threads.length > 0 ? Math.max(...db.threads.map(t => t.id)) + 1 : 1;
  const newThread: Thread = {
    id: newId,
    title: filterNgWords(title),
    category,
    op_user_id: userId,
    is_pinned: false,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    replies: [
      {
        id: 1,
        thread_id: newId,
        post_num: 1,
        author: '名無しさん',
        content: filterNgWords(title),
        image_url: null,
        user_id: userId,
        is_admin: false,
        is_op: true,
        role: null,
        created_at: new Date().toISOString(),
        date: formatDate(new Date())
      }
    ]
  };

  db.threads.push(newThread);
  saveDb();

  if (req.xhr || req.headers.accept?.includes('application/json')) {
    return res.json({
      success: true,
      thread: {
        id: newThread.id,
        title: newThread.title,
        category: newThread.category
      }
    });
  }
  res.redirect(`/thread/${newId}`);
});

// Thread View
app.get('/thread/:id', (req: Request, res: Response) => {
  const threadId = parseInt(req.params.id, 10);
  const thread = db.threads.find(t => t.id === threadId);
  if (!thread) {
    return res.status(404).send('スレッドが見つかりません');
  }

  trackUser(req, `thread:${threadId}`);
  const isAdminUser = Boolean((req.session as any)?.staff_role === 'admin' || (req.session as any)?.staff_role === 'sub_admin');
  const categories = db.categories || DEFAULT_CATEGORIES;
  const cat = categories.find(c => c.id === (thread.category || 'general')) || categories[0];

  // Format replies with rich links
  const formattedReplies = thread.replies.map(r => ({
    ...r,
    content: formatPostContent(r.content)
  }));

  res.render('thread', {
    thread: {
      ...thread,
      category_name: cat?.name || '雑談',
      category_icon: cat?.icon || '💬',
      category_color: cat?.badge_color || '#38bdf8',
      replies: formattedReplies,
      has_older: false
    },
    categories,
    op_user_id: thread.op_user_id,
    back_to_board: `/?tab=threads&category=${thread.category || 'general'}`,
    active_count: getActiveCount(`thread:${threadId}`) || 1,
    is_admin_user: isAdminUser
  });
});

// Thread Post Reply
app.post('/thread/:id', upload.single('image'), (req: Request, res: Response) => {
  const threadId = parseInt(req.params.id, 10);
  const thread = db.threads.find(t => t.id === threadId);
  if (!thread) {
    return res.status(404).json({ error: 'スレッドが見つかりません' });
  }

  const userId = getDailyUserId(req);
  if (db.banned_users.includes(userId)) {
    return res.status(403).json({ error: 'アクセスが制限されています' });
  }

  let author = (req.body.author || '名無しさん').trim().slice(0, 30);
  let content = (req.body.content || '').trim().slice(0, 2000);
  const file = req.file;

  if (!content && !file) {
    return res.status(400).json({ error: '本文または画像を入力してください' });
  }

  // Handle Tripcode
  let role: string | null = (req.session as any)?.staff_role || null;
  let isAdmin = Boolean(role === 'admin' || role === 'sub_admin');

  if (author.includes('#')) {
    const [namePart, tripPart] = author.split('#');
    const tripHash = crypto.createHash('sha256').update(tripPart).digest('hex').substring(0, 8);
    author = `${namePart} ◆${tripHash}`;
  }

  author = filterNgWords(author);
  content = filterNgWords(content);

  const newPostNum = thread.replies.length + 1;
  const replyId = Date.now();
  const imageUrl = file ? `/uploads/${file.filename}` : null;
  const dateStr = formatDate(new Date());

  const newReply: Reply = {
    id: replyId,
    thread_id: threadId,
    post_num: newPostNum,
    author,
    content,
    image_url: imageUrl,
    user_id: userId,
    is_admin: isAdmin,
    is_op: userId === thread.op_user_id,
    role,
    created_at: new Date().toISOString(),
    date: dateStr
  };

  thread.replies.push(newReply);
  thread.updated_at = new Date().toISOString();
  saveDb();

  const formattedReply = {
    ...newReply,
    content: formatPostContent(newReply.content)
  };

  broadcastWs(`thread:${threadId}`, { type: 'new_reply', reply: formattedReply });

  res.json({
    success: true,
    reply: formattedReply
  });
});

// Thread Polling: New replies
app.get('/thread/:id/get_new_replies', (req: Request, res: Response) => {
  const threadId = parseInt(req.params.id, 10);
  const afterId = parseInt(req.query.after_id as string, 10) || 0;
  const thread = db.threads.find(t => t.id === threadId);
  if (!thread) {
    return res.json({ replies: [] });
  }

  const newReplies = thread.replies
    .filter(r => r.id > afterId)
    .map(r => ({
      ...r,
      content: formatPostContent(r.content)
    }));

  res.json({ replies: newReplies });
});

// Thread Polling: Older replies
app.get('/thread/:id/get_older_replies', (req: Request, res: Response) => {
  const threadId = parseInt(req.params.id, 10);
  const beforeId = parseInt(req.query.before_id as string, 10) || 0;
  const thread = db.threads.find(t => t.id === threadId);
  if (!thread) {
    return res.json({ replies: [], has_more: false });
  }

  const olderReplies = thread.replies
    .filter(r => beforeId === 0 || r.id < beforeId)
    .slice(-300)
    .map(r => ({
      ...r,
      content: formatPostContent(r.content)
    }));

  res.json({
    replies: olderReplies,
    has_more: false
  });
});

// Admin: Delete Thread
app.post('/thread/:id/delete_thread', (req: Request, res: Response) => {
  const threadId = parseInt(req.params.id, 10);
  const idx = db.threads.findIndex(t => t.id === threadId);
  if (idx !== -1) {
    db.threads.splice(idx, 1);
    saveDb();
  }
  res.redirect('/?tab=threads');
});

// Admin: Delete Reply
app.post('/thread/:id/delete/:reply_id', (req: Request, res: Response) => {
  const threadId = parseInt(req.params.id, 10);
  const replyId = parseInt(req.params.reply_id, 10);
  const thread = db.threads.find(t => t.id === threadId);
  if (thread) {
    const idx = thread.replies.findIndex(r => r.id === replyId);
    if (idx !== -1) {
      thread.replies.splice(idx, 1);
      saveDb();
    }
  }
  res.redirect(`/thread/${threadId}`);
});

// Admin: Ban User
app.post('/ban_user/:thread_id/:reply_id', (req: Request, res: Response) => {
  const threadId = parseInt(req.params.thread_id, 10);
  const replyId = parseInt(req.params.reply_id, 10);
  const thread = db.threads.find(t => t.id === threadId);
  if (thread) {
    const reply = thread.replies.find(r => r.id === replyId);
    if (reply && !db.banned_users.includes(reply.user_id)) {
      db.banned_users.push(reply.user_id);
      saveDb();
    }
  }
  res.redirect(`/thread/${threadId}`);
});

// -------------------------------------------------------------
// GAMES
// -------------------------------------------------------------

app.get('/games', (_req: Request, res: Response) => {
  res.render('games_hub');
});

// Othello Lobby
app.get('/game', (_req: Request, res: Response) => {
  res.render('game', { room: null, my_color: null });
});

app.post('/game/create', (req: Request, res: Response) => {
  const name = (req.body.name || '名無しさん').trim().slice(0, 20);
  const userId = getDailyUserId(req);
  const roomCode = crypto.randomBytes(3).toString('hex').toUpperCase();

  db.othello_rooms[roomCode] = {
    room_code: roomCode,
    player1_name: name || '名無しさん',
    player1_id: userId,
    player2_name: '',
    player2_id: '',
    board: createInitialBoard(),
    turn: 'B',
    status: 'waiting',
    winner: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString()
  };
  saveDb();

  res.cookie('game_player_token', userId, { maxAge: 1000 * 60 * 60 * 24 * 7 });
  res.redirect(`/game/${roomCode}`);
});

app.get('/game/:room_code', (req: Request, res: Response) => {
  const roomCode = req.params.room_code.toUpperCase();
  const room = db.othello_rooms[roomCode];
  if (!room) {
    return res.redirect('/game');
  }

  const userId = getDailyUserId(req);
  let myColor: 'B' | 'W' | null = null;
  if (room.player1_id === userId) myColor = 'B';
  else if (room.player2_id === userId) myColor = 'W';

  res.render('game', {
    room,
    my_color: myColor
  });
});

app.post('/game/:room_code/join', (req: Request, res: Response) => {
  const roomCode = req.params.room_code.toUpperCase();
  const room = db.othello_rooms[roomCode];
  if (!room) {
    return res.status(404).json({ error: '部屋が見つかりません' });
  }

  const name = (req.body.name || '名無しさん').trim().slice(0, 20);
  const userId = getDailyUserId(req);

  if (!room.player2_id && room.player1_id !== userId) {
    room.player2_name = name || '名無しさん';
    room.player2_id = userId;
    room.status = 'playing';
    room.updated_at = new Date().toISOString();
    saveDb();
  }

  res.cookie('game_player_token', userId, { maxAge: 1000 * 60 * 60 * 24 * 7 });
  res.json({ success: true, room });
});

app.get('/api/game/:room_code/state', (req: Request, res: Response) => {
  const roomCode = req.params.room_code.toUpperCase();
  const room = db.othello_rooms[roomCode];
  if (!room) {
    return res.status(404).json({ error: '部屋が見つかりません' });
  }

  const userId = getDailyUserId(req);
  let myColor: 'B' | 'W' | null = null;
  if (room.player1_id === userId) myColor = 'B';
  else if (room.player2_id === userId) myColor = 'W';

  res.json({
    success: true,
    state: {
      ...room,
      my_color: myColor
    }
  });
});

function computeOthelloValidMoves(boardStr: string, player: 'B' | 'W'): [number, number][] {
  const dirs = [
    [-1, -1], [-1, 0], [-1, 1],
    [0, -1],           [0, 1],
    [1, -1],  [1, 0],  [1, 1]
  ];
  const opp = player === 'B' ? 'W' : 'B';
  const moves: [number, number][] = [];

  for (let r = 0; r < 8; r++) {
    for (let c = 0; c < 8; c++) {
      if (boardStr[r * 8 + c] !== '.') continue;
      let ok = false;
      for (const [dr, dc] of dirs) {
        let rr = r + dr, cc = c + dc;
        let seen = false;
        while (rr >= 0 && rr < 8 && cc >= 0 && cc < 8 && boardStr[rr * 8 + cc] === opp) {
          seen = true;
          rr += dr;
          cc += dc;
        }
        if (seen && rr >= 0 && rr < 8 && cc >= 0 && cc < 8 && boardStr[rr * 8 + cc] === player) {
          ok = true;
          break;
        }
      }
      if (ok) moves.push([r, c]);
    }
  }
  return moves;
}

function applyOthelloMove(boardStr: string, player: 'B' | 'W', r: number, c: number): string | null {
  const validMoves = computeOthelloValidMoves(boardStr, player);
  if (!validMoves.some(([vr, vc]) => vr === r && vc === c)) return null;

  const arr = boardStr.split('');
  arr[r * 8 + c] = player;
  const opp = player === 'B' ? 'W' : 'B';
  const dirs = [
    [-1, -1], [-1, 0], [-1, 1],
    [0, -1],           [0, 1],
    [1, -1],  [1, 0],  [1, 1]
  ];

  for (const [dr, dc] of dirs) {
    let rr = r + dr, cc = c + dc;
    const flips: [number, number][] = [];
    while (rr >= 0 && rr < 8 && cc >= 0 && cc < 8 && arr[rr * 8 + cc] === opp) {
      flips.push([rr, cc]);
      rr += dr;
      cc += dc;
    }
    if (flips.length > 0 && rr >= 0 && rr < 8 && cc >= 0 && cc < 8 && arr[rr * 8 + cc] === player) {
      for (const [fr, fc] of flips) {
        arr[fr * 8 + fc] = player;
      }
    }
  }
  return arr.join('');
}

app.post('/game/:room_code/move', (req: Request, res: Response) => {
  const roomCode = req.params.room_code.toUpperCase();
  const room = db.othello_rooms[roomCode];
  if (!room || room.status !== 'playing') {
    return res.status(400).json({ error: '対局中ではありません' });
  }

  const userId = getDailyUserId(req);
  const isPlayer1 = room.player1_id === userId;
  const isPlayer2 = room.player2_id === userId;
  const myColor = isPlayer1 ? 'B' : isPlayer2 ? 'W' : null;

  if (myColor !== room.turn) {
    return res.status(400).json({ error: '手番ではありません' });
  }

  const { row, col } = req.body;
  const nextBoard = applyOthelloMove(room.board, room.turn, row, col);
  if (!nextBoard) {
    return res.status(400).json({ error: '無効な手です' });
  }

  room.board = nextBoard;
  const opp = room.turn === 'B' ? 'W' : 'B';
  const oppMoves = computeOthelloValidMoves(nextBoard, opp);
  const myNextMoves = computeOthelloValidMoves(nextBoard, room.turn);

  if (oppMoves.length > 0) {
    room.turn = opp;
  } else if (myNextMoves.length > 0) {
    // pass to current player
  } else {
    // game finished
    room.status = 'finished';
    let b = 0, w = 0;
    for (const ch of nextBoard) {
      if (ch === 'B') b++;
      else if (ch === 'W') w++;
    }
    room.winner = b > w ? 'B' : w > b ? 'W' : 'draw';
  }

  room.updated_at = new Date().toISOString();
  saveDb();

  res.json({
    success: true,
    state: {
      ...room,
      my_color: myColor
    }
  });
});

// Chess Lobby
app.get('/chess', (_req: Request, res: Response) => {
  res.render('chess', { room: null, my_color: null });
});

app.post('/chess/create', (req: Request, res: Response) => {
  const name = (req.body.name || '名無しさん').trim().slice(0, 20);
  const userId = getDailyUserId(req);
  const roomCode = crypto.randomBytes(3).toString('hex').toUpperCase();

  db.chess_rooms[roomCode] = {
    room_code: roomCode,
    white_name: name || '名無しさん',
    white_id: userId,
    black_name: '',
    black_id: '',
    board: createInitialChessBoard(),
    turn: 'w',
    status: 'waiting',
    winner: null,
    in_check: null,
    castling: 'KQkq',
    en_passant: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString()
  };
  saveDb();

  res.cookie('game_player_token', userId, { maxAge: 1000 * 60 * 60 * 24 * 7 });
  res.redirect(`/chess/${roomCode}`);
});

app.get('/chess/:room_code', (req: Request, res: Response) => {
  const roomCode = req.params.room_code.toUpperCase();
  const room = db.chess_rooms[roomCode];
  if (!room) {
    return res.redirect('/chess');
  }

  const userId = getDailyUserId(req);
  let myColor: 'w' | 'b' | null = null;
  if (room.white_id === userId) myColor = 'w';
  else if (room.black_id === userId) myColor = 'b';

  res.render('chess', {
    room,
    my_color: myColor
  });
});

app.post('/chess/:room_code/join', (req: Request, res: Response) => {
  const roomCode = req.params.room_code.toUpperCase();
  const room = db.chess_rooms[roomCode];
  if (!room) {
    return res.status(404).json({ error: '部屋が見つかりません' });
  }

  const name = (req.body.name || '名無しさん').trim().slice(0, 20);
  const userId = getDailyUserId(req);

  if (!room.black_id && room.white_id !== userId) {
    room.black_name = name || '名無しさん';
    room.black_id = userId;
    room.status = 'playing';
    room.updated_at = new Date().toISOString();
    saveDb();
  }

  res.cookie('game_player_token', userId, { maxAge: 1000 * 60 * 60 * 24 * 7 });
  res.json({ success: true, room });
});

app.get('/api/chess/:room_code/state', (req: Request, res: Response) => {
  const roomCode = req.params.room_code.toUpperCase();
  const room = db.chess_rooms[roomCode];
  if (!room) {
    return res.status(404).json({ error: '部屋が見つかりません' });
  }

  const userId = getDailyUserId(req);
  let myColor: 'w' | 'b' | null = null;
  if (room.white_id === userId) myColor = 'w';
  else if (room.black_id === userId) myColor = 'b';

  res.json({
    success: true,
    ...room,
    has_black: Boolean(room.black_id),
    my_color: myColor
  });
});

app.post('/chess/:room_code/move', (req: Request, res: Response) => {
  const roomCode = req.params.room_code.toUpperCase();
  const room = db.chess_rooms[roomCode];
  if (!room || room.status !== 'playing') {
    return res.status(400).json({ error: '対局中ではありません' });
  }

  const userId = getDailyUserId(req);
  const myColor = (room.white_id === userId) ? 'w' : (room.black_id === userId) ? 'b' : null;

  if (myColor !== room.turn) {
    return res.status(400).json({ error: '手番ではありません' });
  }

  const { from_row, from_col, to_row, to_col } = req.body;
  const piece = room.board[from_row * 8 + from_col];
  if (!piece || piece[0] !== myColor) {
    return res.status(400).json({ error: '無効な選択です' });
  }

  const nextBoard = [...room.board];
  nextBoard[from_row * 8 + from_col] = '';
  nextBoard[to_row * 8 + to_col] = piece;

  // Pawn promotion
  if (piece[1] === 'P' && (to_row === 0 || to_row === 7)) {
    nextBoard[to_row * 8 + to_col] = piece[0] + 'Q';
  }

  // Castling Rook move
  if (piece[1] === 'K' && Math.abs(to_col - from_col) === 2) {
    const r = from_row;
    if (to_col === 6) {
      nextBoard[r * 8 + 7] = '';
      nextBoard[r * 8 + 5] = piece[0] + 'R';
    } else if (to_col === 2) {
      nextBoard[r * 8 + 0] = '';
      nextBoard[r * 8 + 3] = piece[0] + 'R';
    }
  }

  room.board = nextBoard;
  room.turn = room.turn === 'w' ? 'b' : 'w';
  room.updated_at = new Date().toISOString();
  saveDb();

  res.json({ success: true, room });
});

// -------------------------------------------------------------
// STATIC PAGES & ARCHIVES
// -------------------------------------------------------------

app.get('/privacy', (_req: Request, res: Response) => {
  res.render('privacy');
});

app.get('/roles', (_req: Request, res: Response) => {
  res.render('roles');
});

app.get('/archive', (_req: Request, res: Response) => {
  const categories = db.categories || DEFAULT_CATEGORIES;
  res.render('archive_list', {
    archives: db.threads.map(t => {
      const cat = categories.find(c => c.id === (t.category || 'general')) || categories[0];
      return {
        id: t.id,
        title: t.title,
        category_name: cat?.name || '雑談',
        category_icon: cat?.icon || '💬',
        category_color: cat?.badge_color || '#38bdf8',
        replies_count: t.replies.length,
        created_at: t.created_at
      };
    })
  });
});

app.get('/archive/:id', (req: Request, res: Response) => {
  const threadId = parseInt(req.params.id, 10);
  const thread = db.threads.find(t => t.id === threadId);
  if (!thread) {
    return res.status(404).send('アーカイブが見つかりません');
  }

  const categories = db.categories || DEFAULT_CATEGORIES;
  const cat = categories.find(c => c.id === (thread.category || 'general')) || categories[0];

  res.render('archive_view', {
    thread: {
      ...thread,
      category_name: cat?.name || '雑談',
      category_icon: cat?.icon || '💬',
      category_color: cat?.badge_color || '#38bdf8',
      replies: thread.replies.map(r => ({
        ...r,
        content: formatPostContent(r.content)
      }))
    }
  });
});

app.get('/archive_list', (_req: Request, res: Response) => {
  res.redirect('/archive');
});

// Staff Login
app.get('/login_secret_8823', (req: Request, res: Response) => {
  res.send(`
    <!DOCTYPE html>
    <html lang="ja">
    <head>
      <meta charset="UTF-8">
      <title>Staff Login</title>
      <style>
        body { background: #0f172a; color: white; font-family: sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }
        form { background: #1e293b; padding: 24px; border-radius: 8px; border: 1px solid #334155; }
        input { margin-bottom: 12px; padding: 6px 10px; border-radius: 4px; border: 1px solid #475569; width: 100%; box-sizing: border-box; }
        button { background: #38bdf8; border: none; padding: 8px 16px; border-radius: 4px; font-weight: bold; cursor: pointer; }
      </style>
    </head>
    <body>
      <form method="POST" action="/login_secret_8823">
        <h3>管理者ログイン</h3>
        <label>ID:</label>
        <input type="text" name="username" required>
        <label>PW:</label>
        <input type="password" name="password" required>
        <button type="submit">ログイン</button>
      </form>
    </body>
    </html>
  `);
});

app.post('/login_secret_8823', (req: Request, res: Response) => {
  const { username, password } = req.body;
  if (username === 'admin' && password === 'admin123') {
    (req.session as any).staff_id = 1;
    (req.session as any).staff_role = 'admin';
    (req.session as any).staff_name = 'talk-ch管理人';
    return res.redirect('/');
  }
  res.status(401).send('ログイン失敗');
});

app.get('/staff_logout', (req: Request, res: Response) => {
  req.session.destroy(() => {
    res.redirect('/');
  });
});

// Start server
server.listen(PORT, '0.0.0.0', () => {
  console.log(`talk-ch server running on http://0.0.0.0:${PORT}`);
});

// Graceful shutdown
process.on('SIGTERM', () => {
  console.log('SIGTERM signal received: closing HTTP server');
  saveDb();
  server.close(() => {
    console.log('HTTP server closed');
    process.exit(0);
  });
});

process.on('SIGINT', () => {
  console.log('SIGINT signal received: closing HTTP server');
  saveDb();
  server.close(() => {
    console.log('HTTP server closed');
    process.exit(0);
  });
});
