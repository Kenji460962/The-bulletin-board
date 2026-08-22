import express from 'express';
import nunjucks from 'nunjucks';
import cookieParser from 'cookie-parser';
import session from 'express-session';
import multer from 'multer';
import path from 'path';
import fs from 'fs';
import os from 'os';
import crypto from 'crypto';
import { fileURLToPath } from 'url';
import 'dotenv/config';

import * as db from './db.js';
import * as r2 from './r2.js';

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const app = express();
const PORT = process.env.PORT || 3000;

// Initialize database tables if connected to D1
db.initDb().catch(err => console.warn('[DB Init Warning]:', err.message));

// Multer memory storage for direct R2 streaming / local fallback
const upload = multer({
  storage: multer.memoryStorage(),
  limits: { fileSize: 5 * 1024 * 1024 } // 5MB
});

// Configure Nunjucks
const nunjucksEnv = nunjucks.configure(path.join(__dirname, 'templates'), {
  autoescape: false,
  express: app,
  watch: false
});

// Jinja2 compatible globals & filters
nunjucksEnv.addGlobal('url_for', function (endpoint, kwargs) {
  if (endpoint === 'static') {
    const filename = kwargs && kwargs.filename ? kwargs.filename : (typeof kwargs === 'string' ? kwargs : '');
    return `/static/${filename}`;
  }
  if (endpoint === 'index') return '/';
  if (endpoint === 'game_lobby') return '/game';
  if (endpoint === 'chess_lobby') return '/chess';
  if (endpoint === 'games_hub') return '/games';
  if (endpoint === 'archive_list') return '/archive';
  if (endpoint === 'privacy') return '/privacy';
  if (endpoint === 'roles') return '/roles';
  if (endpoint === 'archive_view') {
    const id = kwargs && kwargs.thread_id ? kwargs.thread_id : '';
    return `/archive/${id}`;
  }
  if (endpoint === 'game_room') {
    const code = kwargs && kwargs.room_code ? kwargs.room_code : '';
    return `/game/${code}`;
  }
  if (endpoint === 'chess_room') {
    const code = kwargs && kwargs.room_code ? kwargs.room_code : '';
    return `/chess/${code}`;
  }
  if (endpoint === 'thread_view') {
    const id = kwargs && kwargs.thread_id ? kwargs.thread_id : '';
    return `/thread/${id}`;
  }
  return '/';
});

nunjucksEnv.addFilter('int', function (val) {
  return parseInt(val, 10) || 0;
});

nunjucksEnv.addFilter('urlencode', function (val) {
  return encodeURIComponent(val || '');
});

// Middleware
app.use(express.json());
app.use(express.urlencoded({ extended: true }));
app.use(cookieParser());
app.use(session({
  secret: process.env.FLASK_SECRET_KEY || 'talk_ch_session_secret_key_railway',
  resave: false,
  saveUninitialized: true,
  cookie: { maxAge: 1000 * 60 * 60 * 24 * 7 }
}));

// Static files
app.use('/static', express.static(path.join(__dirname, 'static')));

// ==========================================
// Helpers & Utilities
// ==========================================

const NG_WORDS = {
  'ﾀﾋ': 'タヒ',
  '死': 'タヒ',
  '死​ね': '〇ね',
  '死ね': '〇ね',
  'しね': '〇ね',
  'エロ': 'エ〇',
  'えろ': 'え〇',
  'まんこ': 'ま〇こ',
  'ちんこ': 'ち〇こ',
  'マンコ': 'マ〇こ',
  'チンコ': 'チ〇こ',
  'セックス': 'セ。〇ス',
  'せっくす': 'せ。〇す',
  'おっぱい': 'お。〇い',
  'オッパイ': 'オ。〇イ',
  'レイプ': 'レ〇プ',
  'れいぷ': 'れ〇ぷ',
  'バカ': 'バ*',
  'アホ': 'ア*',
  'シコシコ': '4545',
  'オナニー': '0721',
  '射精': '身寸米青',
  '精子': '米青子'
};

function filterNgWords(text) {
  if (!text) return '';
  let res = String(text);
  for (const [ng, rep] of Object.entries(NG_WORDS)) {
    res = res.replaceAll(ng, rep);
  }
  return res;
}

function escapeHtml(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatJstDate(d = new Date()) {
  const dateObj = typeof d === 'string' ? new Date(d) : d;
  if (isNaN(dateObj.getTime())) return String(d);
  const jst = new Date(dateObj.getTime() + (9 * 60 + dateObj.getTimezoneOffset()) * 60000);
  const pad = (n) => String(n).padStart(2, '0');
  const Y = jst.getFullYear();
  const M = pad(jst.getMonth() + 1);
  const D = pad(jst.getDate());
  const h = pad(jst.getHours());
  const m = pad(jst.getMinutes());
  const s = pad(jst.getSeconds());
  return `${Y}-${M}-${D} ${h}:${m}:${s}`;
}

function formatReplyContent(rawContent) {
  if (!rawContent) return '';
  let content = escapeHtml(rawContent);
  // Auto-link URLs
  content = content.replace(/(https?:\/\/[^\s<>]+)/g, '<a href="$1" target="_blank" rel="noopener noreferrer" style="color: #38bdf8; text-decoration: underline;">$1</a>');
  // >>123 anchor links
  content = content.replace(/&gt;&gt;(\d+)|>>(\d+)/g, (match, p1, p2) => {
    const num = p1 || p2;
    return `<a href="#post-${num}" class="post-anchor" onclick="scrollToPost(${num}); return false;">&gt;&gt;${num}</a>`;
  });
  return content;
}

function getDailyUserId(ip) {
  const today = new Date().toISOString().slice(0, 10);
  const raw = `${ip || '127.0.0.1'}_${today}`;
  return crypto.createHash('md5').update(raw).digest('hex').slice(0, 8);
}

function getClientIp(req) {
  const cfIp = req.headers['cf-connecting-ip'];
  if (cfIp) return String(cfIp).trim();
  const forwarded = req.headers['x-forwarded-for'];
  if (forwarded) return String(forwarded).split(',')[0].trim();
  return req.socket.remoteAddress || '127.0.0.1';
}

function resolveRoleClass(author = '', role = '', isAdmin = false) {
  const name = String(author || '');
  const r = String(role || '').toLowerCase();
  if (r === 'admin' || r === 'sub_admin' || isAdmin || name.includes('ペンギン') || name.includes('Mino')) {
    return 'role-admin';
  }
  if (r === 'pr' || name.includes('鷹3an') || name.includes('タウ')) {
    return 'role-pr';
  }
  if (r === 'box' || r === 'moderator' || name.includes('車エビ') || name.includes('名有り') || name.includes('クラ急行')) {
    return 'role-box';
  }
  return '';
}

function canManageBoard(req) {
  const role = req.session && req.session.staff_role;
  return role === 'admin' || role === 'sub_admin';
}

const activeUsers = new Map(); // token -> { location, lastSeen }
const lastThreadTimes = new Map(); // ip -> timestamp
const lastReplyTimes = new Map(); // ip -> timestamp
const lastReplySignatures = new Map(); // signature -> timestamp

function updateAndGetUserCounts(token, location) {
  const now = Date.now();
  if (token) {
    activeUsers.set(token, { location, lastSeen: now });
  }
  const cutoff = now - 2 * 60 * 1000;
  let count = 0;
  for (const [t, data] of activeUsers.entries()) {
    if (data.lastSeen < cutoff) {
      activeUsers.delete(t);
    } else if (data.location === location) {
      count++;
    }
  }
  return Math.max(1, count);
}

function getThreadActiveCounts() {
  const now = Date.now();
  const cutoff = now - 5 * 60 * 1000;
  const counts = {};
  for (const [t, data] of activeUsers.entries()) {
    if (data.lastSeen >= cutoff && data.location && data.location.startsWith('thread_')) {
      counts[data.location] = (counts[data.location] || 0) + 1;
    }
  }
  return counts;
}

function ensureUserToken(req, res) {
  let token = req.cookies.user_bbs_token;
  if (!token) {
    token = crypto.randomUUID();
    res.cookie('user_bbs_token', token, { maxAge: 1000 * 60 * 60 * 24 * 365, httpOnly: true });
  }
  return token;
}

function ensureGameToken(req, res) {
  let token = req.cookies.game_player_token || req.cookies.user_bbs_token;
  if (!token) {
    token = crypto.randomUUID();
    res.cookie('game_player_token', token, { maxAge: 1000 * 60 * 60 * 24 * 365, httpOnly: true, sameSite: 'Lax' });
  }
  return token;
}

// Staff credentials
const staffUsers = [
  { id: 1, username: 'admin', password: 'password123', role: 'admin', display_name: 'ペンギン★' },
  { id: 2, username: 'mino', password: 'password123', role: 'sub_admin', display_name: 'Mino★' }
];

// ==========================================
// Game Engines: Othello & Chess
// ==========================================

const othelloRooms = new Map();
const chessRooms = new Map();

function newRoomCode() {
  const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
  for (let attempt = 0; attempt < 30; attempt++) {
    let code = '';
    for (let i = 0; i < 6; i++) {
      code += alphabet[Math.floor(Math.random() * alphabet.length)];
    }
    if (!othelloRooms.has(code) && !chessRooms.has(code)) {
      return code;
    }
  }
  return crypto.randomUUID().slice(0, 6).toUpperCase();
}

function initialOthello() {
  const b = Array(64).fill('.');
  b[3 * 8 + 3] = 'W';
  b[3 * 8 + 4] = 'B';
  b[4 * 8 + 3] = 'B';
  b[4 * 8 + 4] = 'W';
  return b.join('');
}

function othelloValid(board, player) {
  const opp = player === 'B' ? 'W' : 'B';
  const dirs = [[-1, -1], [-1, 0], [-1, 1], [0, -1], [0, 1], [1, -1], [1, 0], [1, 1]];
  const out = [];
  for (let r = 0; r < 8; r++) {
    for (let c = 0; c < 8; c++) {
      if (board[r * 8 + c] !== '.') continue;
      let ok = false;
      for (const [dr, dc] of dirs) {
        let rr = r + dr;
        let cc = c + dc;
        let seen = false;
        while (rr >= 0 && rr < 8 && cc >= 0 && cc < 8 && board[rr * 8 + cc] === opp) {
          seen = true;
          rr += dr;
          cc += dc;
        }
        if (seen && rr >= 0 && rr < 8 && cc >= 0 && cc < 8 && board[rr * 8 + cc] === player) {
          ok = true;
          break;
        }
      }
      if (ok) out.push([r, c]);
    }
  }
  return out;
}

function othelloApply(board, player, r, c) {
  const valids = othelloValid(board, player);
  if (!valids.some(([vr, vc]) => vr === r && vc === c)) return null;

  const a = board.split('');
  a[r * 8 + c] = player;
  const opp = player === 'B' ? 'W' : 'B';
  const dirs = [[-1, -1], [-1, 0], [-1, 1], [0, -1], [0, 1], [1, -1], [1, 0], [1, 1]];

  for (const [dr, dc] of dirs) {
    let rr = r + dr;
    let cc = c + dc;
    const flips = [];
    while (rr >= 0 && rr < 8 && cc >= 0 && cc < 8 && a[rr * 8 + cc] === opp) {
      flips.push([rr, cc]);
      rr += dr;
      cc += dc;
    }
    if (flips.length > 0 && rr >= 0 && rr < 8 && cc >= 0 && cc < 8 && a[rr * 8 + cc] === player) {
      for (const [fr, fc] of flips) {
        a[fr * 8 + fc] = player;
      }
    }
  }
  return a.join('');
}

function initialChess() {
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

function chessPseudo(board, r, c, castling = '', enPassant = null) {
  const p = board[r * 8 + c];
  if (!p) return [];
  const color = p[0];
  const typ = p[1];
  const out = [];

  let dirs = [];
  if (typ === 'N') dirs = [[-2, -1], [-2, 1], [-1, -2], [-1, 2], [1, -2], [1, 2], [2, -1], [2, 1]];
  else if (typ === 'K') dirs = [[-1, -1], [-1, 0], [-1, 1], [0, -1], [0, 1], [1, -1], [1, 0], [1, 1]];
  else if (['B', 'R', 'Q'].includes(typ)) {
    if (typ === 'B' || typ === 'Q') dirs.push([-1, -1], [-1, 1], [1, -1], [1, 1]);
    if (typ === 'R' || typ === 'Q') dirs.push([-1, 0], [1, 0], [0, -1], [0, 1]);
  }

  if (typ === 'N' || typ === 'K') {
    for (const [dr, dc] of dirs) {
      const rr = r + dr;
      const cc = c + dc;
      if (rr >= 0 && rr < 8 && cc >= 0 && cc < 8 && (!board[rr * 8 + cc] || board[rr * 8 + cc][0] !== color)) {
        out.push([rr, cc]);
      }
    }
  } else if (['B', 'R', 'Q'].includes(typ)) {
    for (const [dr, dc] of dirs) {
      let rr = r + dr;
      let cc = c + dc;
      while (rr >= 0 && rr < 8 && cc >= 0 && cc < 8) {
        const t = board[rr * 8 + cc];
        if (!t) {
          out.push([rr, cc]);
        } else {
          if (t[0] !== color) out.push([rr, cc]);
          break;
        }
        rr += dr;
        cc += dc;
      }
    }
  } else if (typ === 'P') {
    const d = color === 'w' ? -1 : 1;
    const start = color === 'w' ? 6 : 1;
    const rr = r + d;
    if (rr >= 0 && rr < 8 && !board[rr * 8 + c]) {
      out.push([rr, c]);
      const rr2 = r + 2 * d;
      if (r === start && !board[rr2 * 8 + c]) {
        out.push([rr2, c]);
      }
    }
    for (const dc of [-1, 1]) {
      const pr = r + d;
      const pc = c + dc;
      if (pr >= 0 && pr < 8 && pc >= 0 && pc < 8) {
        if (board[pr * 8 + pc] && board[pr * 8 + pc][0] !== color) {
          out.push([pr, pc]);
        } else if (enPassant && enPassant[0] === pr && enPassant[1] === pc) {
          out.push([pr, pc]);
        }
      }
    }
  }

  if (typ === 'K') {
    const row = color === 'w' ? 7 : 0;
    if (r === row && c === 4) {
      const kFlag = color === 'w' ? 'K' : 'k';
      const qFlag = color === 'w' ? 'Q' : 'q';
      const opp = color === 'w' ? 'b' : 'w';
      if (
        castling.includes(kFlag) &&
        !board[row * 8 + 5] &&
        !board[row * 8 + 6] &&
        board[row * 8 + 7] === color + 'R' &&
        !chessAttacked(board, row, 4, opp) &&
        !chessAttacked(board, row, 5, opp) &&
        !chessAttacked(board, row, 6, opp)
      ) {
        out.push([row, 6]);
      }
      if (
        castling.includes(qFlag) &&
        !board[row * 8 + 3] &&
        !board[row * 8 + 2] &&
        !board[row * 8 + 1] &&
        board[row * 8 + 0] === color + 'R' &&
        !chessAttacked(board, row, 4, opp) &&
        !chessAttacked(board, row, 3, opp) &&
        !chessAttacked(board, row, 2, opp)
      ) {
        out.push([row, 2]);
      }
    }
  }
  return out;
}

function chessFindKing(board, color) {
  const target = color + 'K';
  for (let i = 0; i < board.length; i++) {
    if (board[i] === target) {
      return [Math.floor(i / 8), i % 8];
    }
  }
  return null;
}

function chessAttacked(board, r, c, byColor) {
  const d = byColor === 'w' ? 1 : -1;
  for (const dc of [-1, 1]) {
    const pr = r + d;
    const pc = c + dc;
    if (pr >= 0 && pr < 8 && pc >= 0 && pc < 8 && board[pr * 8 + pc] === byColor + 'P') {
      return true;
    }
  }
  for (const [dr, dc] of [[-2, -1], [-2, 1], [-1, -2], [-1, 2], [1, -2], [1, 2], [2, -1], [2, 1]]) {
    const rr = r + dr;
    const cc = c + dc;
    if (rr >= 0 && rr < 8 && cc >= 0 && cc < 8 && board[rr * 8 + cc] === byColor + 'N') {
      return true;
    }
  }
  for (let dr = -1; dr <= 1; dr++) {
    for (let dc = -1; dc <= 1; dc++) {
      if (dr === 0 && dc === 0) continue;
      const rr = r + dr;
      const cc = c + dc;
      if (rr >= 0 && rr < 8 && cc >= 0 && cc < 8 && board[rr * 8 + cc] === byColor + 'K') {
        return true;
      }
    }
  }
  for (const [dr, dc] of [[-1, 0], [1, 0], [0, -1], [0, 1]]) {
    let rr = r + dr;
    let cc = c + dc;
    while (rr >= 0 && rr < 8 && cc >= 0 && cc < 8) {
      const p = board[rr * 8 + cc];
      if (p) {
        if (p[0] === byColor && ['R', 'Q'].includes(p[1])) return true;
        break;
      }
      rr += dr;
      cc += dc;
    }
  }
  for (const [dr, dc] of [[-1, -1], [-1, 1], [1, -1], [1, 1]]) {
    let rr = r + dr;
    let cc = c + dc;
    while (rr >= 0 && rr < 8 && cc >= 0 && cc < 8) {
      const p = board[rr * 8 + cc];
      if (p) {
        if (p[0] === byColor && ['B', 'Q'].includes(p[1])) return true;
        break;
      }
      rr += dr;
      cc += dc;
    }
  }
  return false;
}

function chessInCheck(board, color) {
  const pos = chessFindKing(board, color);
  if (!pos) return false;
  const [r, c] = pos;
  const opp = color === 'w' ? 'b' : 'w';
  return chessAttacked(board, r, c, opp);
}

function chessApply(board, r, c, tr, tc) {
  const nb = [...board];
  const piece = nb[r * 8 + c];
  if (!piece) return nb;
  const color = piece[0];
  const typ = piece[1];
  nb[r * 8 + c] = '';

  if (typ === 'K' && Math.abs(tc - c) === 2) {
    nb[tr * 8 + tc] = piece;
    const row = r;
    if (tc === 6) {
      nb[row * 8 + 7] = '';
      nb[row * 8 + 5] = color + 'R';
    } else if (tc === 2) {
      nb[row * 8 + 0] = '';
      nb[row * 8 + 3] = color + 'R';
    }
  } else if (typ === 'P' && c !== tc && !board[tr * 8 + tc]) {
    nb[tr * 8 + tc] = piece;
    nb[r * 8 + tc] = '';
  } else if (typ === 'P' && (tr === 0 || tr === 7)) {
    nb[tr * 8 + tc] = color + 'Q';
  } else {
    nb[tr * 8 + tc] = piece;
  }
  return nb;
}

function chessUpdateCastlingRights(castling, typ, color, r, c, tr, tc) {
  let newCastling = castling || '';
  if (typ === 'K') {
    newCastling = color === 'w' ? newCastling.replace('K', '').replace('Q', '') : newCastling.replace('k', '').replace('q', '');
  }
  const rooks = [
    [[7, 0], 'Q'],
    [[7, 7], 'K'],
    [[0, 0], 'q'],
    [[0, 7], 'k']
  ];
  for (const [[rr, cc], flag] of rooks) {
    if ((r === rr && c === cc) || (tr === rr && tc === cc)) {
      newCastling = newCastling.replace(flag, '');
    }
  }
  return newCastling;
}

function chessLegalMoves(board, color, castling = '', enPassant = null) {
  const moves = [];
  for (let i = 0; i < board.length; i++) {
    const p = board[i];
    if (p && p[0] === color) {
      const r = Math.floor(i / 8);
      const c = i % 8;
      for (const [tr, tc] of chessPseudo(board, r, c, castling, enPassant)) {
        const simulated = chessApply(board, r, c, tr, tc);
        if (!chessInCheck(simulated, color)) {
          moves.push([r, c, tr, tc]);
        }
      }
    }
  }
  return moves;
}

// ==========================================
// App Endpoints
// ==========================================

// Uptime robot & health check
app.head('*', (req, res) => res.status(200).end());

// Home / Lobby / Thread List
app.get('/', async (req, res) => {
  const clientIp = getClientIp(req);
  if (await db.isBannedIp(clientIp)) {
    return res.status(403).send('あなたはアクセス禁止（BAN）されています。');
  }

  const page = parseInt(req.query.page, 10) || 1;
  const perPage = 20;
  const searchQuery = (req.query.q || '').trim();

  const { threads, hasNext } = await db.getThreads(page, perPage, searchQuery);
  const adminMessage = await db.getAdminMessage();

  const threadActiveCounts = getThreadActiveCounts();
  for (const t of threads) {
    t.thread_active_count = threadActiveCounts[`thread_${t.id}`] || 0;
  }

  const userToken = ensureUserToken(req, res);
  const activeCount = updateAndGetUserCounts(userToken, 'lobby');
  const isAdminUser = canManageBoard(req);

  res.render('index.html', {
    threads,
    admin_message: adminMessage,
    is_admin_user: isAdminUser,
    active_count: activeCount,
    current_page: page,
    has_next: hasNext,
    search_query: searchQuery
  });
});

// Update Admin Message
app.post('/update_admin_message', async (req, res) => {
  if (!canManageBoard(req)) {
    return res.status(403).send('権限がありません');
  }
  const message = (req.body.message || '').trim();
  if (message) {
    await db.setAdminMessage(message);
  }
  res.redirect('/');
});

// Create Thread
app.post('/create_thread', async (req, res) => {
  const clientIp = getClientIp(req);
  if (await db.isBannedIp(clientIp)) {
    return res.status(403).json({ error: 'あなたはアクセス禁止（BAN）されています。' });
  }

  let title = (req.body.title || '').trim();
  if (!title) {
    return res.status(400).json({ error: 'タイトルが必要です' });
  }

  title = filterNgWords(title);
  title = escapeHtml(title);

  if (title.length > 50) {
    return res.status(400).json({ error: 'スレッド名は50文字以内で入力してください' });
  }

  const isAdmin = canManageBoard(req);
  const now = Date.now();
  const threadCooldown = 5 * 60 * 1000;

  if (!isAdmin) {
    const lastTime = lastThreadTimes.get(clientIp);
    if (lastTime && now - lastTime < threadCooldown) {
      const remainingSec = Math.ceil((threadCooldown - (now - lastTime)) / 1000);
      const minutes = Math.floor(remainingSec / 60);
      const seconds = remainingSec % 60;
      return res.status(429).json({
        error: `スレッド作成は5分に1回までです。あと ${minutes}分 ${seconds}秒 お待ちください。`
      });
    }
  }
  lastThreadTimes.set(clientIp, now);

  const newThread = await db.createThread(title, clientIp, false);
  res.json({ success: true, thread: newThread });
});

// Thread View
app.get('/thread/:id', async (req, res) => {
  const clientIp = getClientIp(req);
  if (await db.isBannedIp(clientIp)) {
    return res.status(403).send('あなたはアクセス禁止（BAN）されています。');
  }

  const threadId = parseInt(req.params.id, 10);
  const thread = await db.getThreadById(threadId);
  if (!thread) {
    return res.status(404).send('スレッドが見つかりません');
  }

  const allReplies = await db.getReplies(threadId);
  const totalReplyCount = allReplies.length;

  const RECENT_LIMIT = 300;
  const loadedReplies = allReplies.slice(-RECENT_LIMIT);
  const startNum = totalReplyCount - loadedReplies.length + 1;

  const opUserId = thread.ip_address ? getDailyUserId(thread.ip_address) : null;

  const formattedReplies = loadedReplies.map((r, i) => ({
    ...r,
    post_num: startNum + i,
    date: formatJstDate(r.date),
    content: formatReplyContent(r.content),
    is_op: Boolean(opUserId && r.user_id === opUserId),
    role_class: resolveRoleClass(r.author, r.role, r.is_admin)
  }));

  const threadData = {
    ...thread,
    replies: formattedReplies,
    total_reply_count: totalReplyCount,
    has_older: totalReplyCount > loadedReplies.length
  };

  const userToken = ensureUserToken(req, res);
  const locationKey = `thread_${threadId}`;
  const activeCount = updateAndGetUserCounts(userToken, locationKey);
  const isAdminUser = canManageBoard(req);

  res.render('thread.html', {
    thread: threadData,
    is_admin_user: isAdminUser,
    active_count: activeCount,
    back_to_board: '/?tab=threads',
    op_user_id: opUserId
  });
});

// Post Reply (with Cloudflare R2 image upload or local fallback)
app.post('/thread/:id', upload.single('image'), async (req, res) => {
  const clientIp = getClientIp(req);
  if (await db.isBannedIp(clientIp)) {
    return res.status(403).json({ success: false, error: 'あなたはアクセス禁止（BAN）されています。' });
  }

  const threadId = parseInt(req.params.id, 10);
  const thread = await db.getThreadById(threadId);
  if (!thread) {
    return res.status(404).json({ success: false, error: 'スレッドが見つかりません' });
  }

  let content = (req.body.content || '').trim();
  if (content.length > 500) {
    return res.status(400).json({ success: false, error: '500文字以内で入力してください。' });
  }

  let authorInput = (req.body.author || '名無しさん').trim();
  if (authorInput === 'あぼーん') authorInput = '名無しさん';
  authorInput = authorInput.slice(0, 20);

  content = filterNgWords(content);
  authorInput = filterNgWords(authorInput);

  const staffRole = req.session && req.session.staff_role;
  let isStaff = false;
  let roleToSave = null;
  let userId = '';

  if (staffRole) {
    authorInput = req.session.staff_name || '運営スタッフ★';
    isStaff = true;
    userId = 'STAFF';
    roleToSave = staffRole;
  } else {
    isStaff = false;
    roleToSave = null;
    if (authorInput.includes('#')) {
      const parts = authorInput.split('#');
      authorInput = escapeHtml(parts[0].slice(0, 20)) || '名無しさん';
    } else {
      authorInput = escapeHtml(authorInput);
    }
    userId = getDailyUserId(clientIp);
  }

  const now = Date.now();
  if (!staffRole) {
    const replyCooldown = 3000;
    const lastTime = lastReplyTimes.get(clientIp);
    if (lastTime && now - lastTime < replyCooldown) {
      return res.status(429).json({ success: false, error: '連続投稿はできません。3秒お待ちください。' });
    }
    lastReplyTimes.set(clientIp, now);
  }

  let imageUrl = '';
  if (req.file) {
    try {
      imageUrl = await r2.uploadImage(req.file.buffer, req.file.originalname, req.file.mimetype);
    } catch (err) {
      console.warn('Image upload error:', err.message);
    }
  }

  if (!content && !imageUrl) {
    return res.status(400).json({ success: false, error: '書き込み内容が空です。' });
  }

  // Duplicate prevention
  const signature = crypto.createHash('sha256').update(`${threadId}|${clientIp}|${authorInput}|${content}|${imageUrl}`).digest('hex');
  const prevSigTime = lastReplySignatures.get(signature);
  if (prevSigTime && now - prevSigTime < 5000) {
    return res.status(409).json({ success: false, duplicate: true, error: '同じ内容が連続して送信されたため、重複投稿を防止しました。' });
  }
  lastReplySignatures.set(signature, now);

  const newReply = await db.createReply({
    threadId,
    author: authorInput,
    content,
    userId,
    isAdmin: isStaff,
    role: roleToSave,
    imageUrl,
    ipAddress: clientIp
  });

  const allReplies = await db.getReplies(threadId);
  const opUserId = thread.ip_address ? getDailyUserId(thread.ip_address) : null;

  const formattedReply = {
    ...newReply,
    date: formatJstDate(newReply.date),
    content: formatReplyContent(newReply.content),
    is_op: Boolean(opUserId && newReply.user_id === opUserId),
    role_class: resolveRoleClass(newReply.author, newReply.role, newReply.is_admin),
    post_num: allReplies.length
  };

  res.json({ success: true, reply: formattedReply });
});

// Live Polling for New Replies
app.get('/thread/:id/get_new_replies', async (req, res) => {
  const clientIp = getClientIp(req);
  if (await db.isBannedIp(clientIp)) {
    return res.status(403).json({ success: false, error: 'Banned' });
  }

  const threadId = parseInt(req.params.id, 10);
  const afterId = parseInt(req.query.after_id, 10) || 0;

  const thread = await db.getThreadById(threadId);
  const allReplies = await db.getReplies(threadId);
  const newReplies = await db.getRepliesAfter(threadId, afterId);

  const opUserId = thread && thread.ip_address ? getDailyUserId(thread.ip_address) : null;
  const startNum = allReplies.length - newReplies.length + 1;

  const formattedReplies = newReplies.map((r, idx) => ({
    ...r,
    post_num: startNum + idx,
    date: formatJstDate(r.date),
    content: formatReplyContent(r.content),
    is_op: Boolean(opUserId && r.user_id === opUserId),
    role_class: resolveRoleClass(r.author, r.role, r.is_admin)
  }));

  res.json({ success: true, replies: formattedReplies });
});

// Load Older Replies
app.get('/thread/:id/get_older_replies', async (req, res) => {
  const clientIp = getClientIp(req);
  if (await db.isBannedIp(clientIp)) {
    return res.status(403).json({ success: false, error: 'Banned' });
  }

  const threadId = parseInt(req.params.id, 10);
  const beforeId = parseInt(req.query.before_id, 10);
  if (!beforeId) {
    return res.status(400).json({ success: false, error: 'before_idが必要です', replies: [], has_more: false });
  }

  const thread = await db.getThreadById(threadId);
  const olderReplies = await db.getRepliesBefore(threadId, beforeId, 300);

  const allReplies = await db.getReplies(threadId);
  const totalOlder = allReplies.filter(r => r.id < beforeId).length;
  const startNum = totalOlder - olderReplies.length + 1;

  const opUserId = thread && thread.ip_address ? getDailyUserId(thread.ip_address) : null;

  const formattedReplies = olderReplies.map((r, i) => ({
    ...r,
    post_num: startNum + i,
    date: formatJstDate(r.date),
    content: formatReplyContent(r.content),
    is_op: Boolean(opUserId && r.user_id === opUserId),
    role_class: resolveRoleClass(r.author, r.role, r.is_admin)
  }));

  res.json({
    success: true,
    replies: formattedReplies,
    has_more: totalOlder > olderReplies.length
  });
});

// Delete Thread (Admin)
app.post('/thread/:id/delete_thread', async (req, res) => {
  if (!canManageBoard(req)) {
    return res.status(403).send('権限がありません');
  }
  const threadId = parseInt(req.params.id, 10);
  await db.deleteThread(threadId);
  res.redirect('/');
});

// Delete Reply (Admin)
app.post('/thread/:id/delete/:reply_id', async (req, res) => {
  if (!canManageBoard(req)) {
    return res.status(403).send('権限がありません');
  }
  const threadId = parseInt(req.params.id, 10);
  const replyId = parseInt(req.params.reply_id, 10);
  await db.deleteReply(threadId, replyId);
  res.redirect(`/thread/${threadId}`);
});

// Ban User
app.post('/ban_user/:reply_id', async (req, res) => {
  if (!canManageBoard(req)) {
    return res.status(403).send('権限がありません');
  }
  const replyId = parseInt(req.params.reply_id, 10);
  await db.banUserByReply(replyId);
  res.redirect(req.headers.referer || '/');
});

// Ban Thread Owner
app.post('/ban_thread_owner/:thread_id', async (req, res) => {
  if (!canManageBoard(req)) {
    return res.status(403).send('権限がありません');
  }
  const threadId = parseInt(req.params.thread_id, 10);
  await db.banThreadOwner(threadId);
  res.redirect('/');
});

// Staff Authentication
app.get('/login_secret_8823', (req, res) => {
  res.send(`
    <!DOCTYPE html>
    <html lang="ja">
    <head>
      <meta charset="UTF-8">
      <title>スタッフログイン - talk-ch</title>
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <style>
        body { background: #0f172a; color: #fff; font-family: system-ui, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; padding: 20px; box-sizing: border-box; }
        .box { background: rgba(30, 41, 59, 0.85); backdrop-filter: blur(10px); padding: 30px; border-radius: 12px; border: 1px solid rgba(255,255,255,0.1); width: 100%; max-width: 360px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }
        input { width: 100%; box-sizing: border-box; padding: 10px 12px; margin: 8px 0 16px; border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: #fff; font-size: 14px; }
        button { width: 100%; padding: 12px; background: #0284c7; color: #fff; border: none; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 15px; transition: background 0.2s; }
        button:hover { background: #0369a1; }
      </style>
    </head>
    <body>
      <div class="box">
        <h2 style="margin-top:0; font-size: 20px;">🛡️ スタッフ管理ログイン</h2>
        <form method="POST" action="/login_secret_8823">
          <label style="font-size: 13px; color: #94a3b8;">ユーザー名:</label>
          <input type="text" name="username" placeholder="admin" required>
          <label style="font-size: 13px; color: #94a3b8;">パスワード:</label>
          <input type="password" name="password" placeholder="••••••••" required>
          <button type="submit">ログイン</button>
        </form>
      </div>
    </body>
    </html>
  `);
});

app.post('/login_secret_8823', async (req, res) => {
  const { username, password } = req.body;
  const staff = await db.authenticateStaff(username, password);
  if (staff) {
    req.session.staff_id = staff.id;
    req.session.staff_role = staff.role;
    req.session.staff_name = staff.display_name;
    return res.redirect('/');
  }
  res.status(401).send('ログイン失敗: ユーザー名またはパスワードが正しくありません');
});

app.get('/staff_logout', (req, res) => {
  req.session.destroy(() => {
    res.redirect('/');
  });
});

// Privacy & Roles Pages
app.get('/privacy', (req, res) => res.render('privacy.html'));
app.get('/roles', (req, res) => res.render('roles.html'));
app.get('/games', (req, res) => res.render('games_hub.html'));

// Archives
app.get('/archive', async (req, res) => {
  const page = parseInt(req.query.page, 10) || 1;
  const { archived_threads, hasNext } = await db.getArchivedThreads(page, 20);
  res.render('archive_list.html', {
    archived_threads,
    current_page: page,
    has_next: hasNext
  });
});

app.get('/archive/:id', async (req, res) => {
  const id = parseInt(req.params.id, 10);
  const data = await db.getArchivedThread(id);
  if (!data) {
    return res.status(404).send('過去ログが見つかりません');
  }
  const formattedReplies = (data.replies || []).map((r, i) => ({
    ...r,
    post_num: i + 1,
    date: formatJstDate(r.date),
    content: formatReplyContent(r.content),
    role_class: resolveRoleClass(r.author, r.role, r.is_admin)
  }));
  res.render('archive_view.html', {
    thread: data.thread,
    replies: formattedReplies,
    archived_at: data.archived_at
  });
});

// Live Active Counter API
app.get('/api/lobby/active_count', (req, res) => {
  const userToken = ensureUserToken(req, res);
  const activeCount = updateAndGetUserCounts(userToken, 'lobby');
  res.json({ success: true, active_count: activeCount });
});

// Server Telemetry / Stats API
let prevCpuTimes = os.cpus().map(c => c.times);
function getCpuUsage() {
  const currentTimes = os.cpus().map(c => c.times);
  let totalDiff = 0;
  let idleDiff = 0;
  for (let i = 0; i < currentTimes.length; i++) {
    const prev = prevCpuTimes[i] || currentTimes[i];
    const curr = currentTimes[i];
    const prevTotal = Object.values(prev).reduce((a, b) => a + b, 0);
    const currTotal = Object.values(curr).reduce((a, b) => a + b, 0);
    totalDiff += (currTotal - prevTotal);
    idleDiff += (curr.idle - prev.idle);
  }
  prevCpuTimes = currentTimes;
  if (totalDiff === 0) return 5.0;
  const usage = Math.max(1, Math.min(99, Math.round(((totalDiff - idleDiff) / totalDiff) * 100)));
  return usage;
}

app.get('/api/server_stats', (req, res) => {
  const totalMem = os.totalmem();
  const freeMem = os.freemem();
  const usedMem = totalMem - freeMem;
  const memUsagePercent = Math.round((usedMem / totalMem) * 100);
  const cpuPercent = getCpuUsage();

  res.json({
    success: true,
    cpu_usage_percent: cpuPercent,
    mem_usage_percent: memUsagePercent,
    mem_used_mb: Math.round(usedMem / (1024 * 1024)),
    mem_total_mb: Math.round(totalMem / (1024 * 1024)),
    net_rx_kb: Math.floor(Math.random() * 45) + 12,
    net_tx_kb: Math.floor(Math.random() * 60) + 18,
    timestamp: Date.now()
  });
});

// ==========================================
// Othello Routes
// ==========================================

app.get('/game', (req, res) => {
  const token = ensureGameToken(req, res);
  res.render('game.html', { room: null, my_color: null });
});

app.post('/game/create', (req, res) => {
  const token = ensureGameToken(req, res);
  const name = escapeHtml((req.body.name || req.cookies.bbs_saved_author || '名無しさん').trim()).slice(0, 20) || '名無しさん';
  const code = newRoomCode();
  const now = new Date().toISOString();

  const room = {
    room_code: code,
    black_token: token,
    black_name: name,
    white_token: null,
    white_name: null,
    board: initialOthello(),
    turn: 'B',
    status: 'waiting',
    winner: null,
    created_at: now,
    updated_at: now
  };
  othelloRooms.set(code, room);
  res.redirect(`/game/${code}`);
});

app.get('/game/:code', (req, res) => {
  const code = req.params.code.toUpperCase();
  const room = othelloRooms.get(code);
  if (!room) return res.redirect('/game');

  const token = ensureGameToken(req, res);
  let myColor = null;
  if (room.black_token === token) myColor = 'B';
  else if (room.white_token === token) myColor = 'W';

  res.render('game.html', { room, my_color: myColor });
});

app.post('/game/:code/join', (req, res) => {
  const code = req.params.code.toUpperCase();
  const room = othelloRooms.get(code);
  if (!room) return res.status(404).json({ success: false, error: '部屋が見つかりません' });

  const token = ensureGameToken(req, res);
  const name = escapeHtml((req.body.name || req.cookies.bbs_saved_author || '名無しさん').trim()).slice(0, 20) || '名無しさん';

  if (room.black_token === token || room.white_token === token) {
    return res.json({ success: true });
  }
  if (room.white_token) {
    return res.status(409).json({ success: false, error: 'この部屋は満員です' });
  }

  room.white_token = token;
  room.white_name = name;
  room.status = 'playing';
  room.updated_at = new Date().toISOString();
  res.json({ success: true });
});

app.get('/api/game/:code/state', (req, res) => {
  const code = req.params.code.toUpperCase();
  const r = othelloRooms.get(code);
  if (!r) return res.status(404).json({ error: 'not found' });

  const token = ensureGameToken(req, res);
  const my = r.black_token === token ? 'B' : (r.white_token === token ? 'W' : null);
  const b = r.board;
  const bc = (b.match(/B/g) || []).length;
  const wc = (b.match(/W/g) || []).length;
  const valid = r.status === 'playing' ? othelloValid(b, r.turn) : [];

  res.json({
    success: true,
    room_code: r.room_code,
    board: b,
    turn: r.turn,
    status: r.status,
    winner: r.winner,
    black_name: r.black_name || '名無しさん',
    white_name: r.white_name || '名無しさん',
    black_id: (r.black_token || '').slice(0, 4),
    white_id: (r.white_token || '').slice(0, 4),
    has_white: Boolean(r.white_token),
    my_color: my,
    black_count: bc,
    white_count: wc,
    valid_moves: valid
  });
});

app.post('/game/:code/move', (req, res) => {
  const code = req.params.code.toUpperCase();
  const r = othelloRooms.get(code);
  if (!r) return res.status(404).json({ success: false, error: '部屋が見つかりません' });

  const token = ensureGameToken(req, res);
  const player = r.black_token === token ? 'B' : (r.white_token === token ? 'W' : null);
  if (!player) return res.status(403).json({ success: false, error: '観戦者は着手できません' });
  if (r.status !== 'playing') return res.status(400).json({ success: false, error: '対局は終了しています' });
  if (r.turn !== player) return res.status(400).json({ success: false, error: '相手のターンです' });

  const rr = parseInt(req.body.row, 10);
  const cc = parseInt(req.body.col, 10);
  const newb = othelloApply(r.board, player, rr, cc);
  if (!newb) return res.status(400).json({ success: false, error: 'そこには置けません' });

  const opp = player === 'B' ? 'W' : 'B';
  let nextTurn = opp;
  let status = 'playing';
  let winner = null;

  if (othelloValid(newb, opp).length === 0) {
    if (othelloValid(newb, player).length > 0) {
      nextTurn = player; // pass
    } else {
      status = 'finished';
      const bc = (newb.match(/B/g) || []).length;
      const wc = (newb.match(/W/g) || []).length;
      winner = bc > wc ? 'B' : (wc > bc ? 'W' : 'draw');
    }
  }

  r.board = newb;
  r.turn = nextTurn;
  r.status = status;
  r.winner = winner;
  r.updated_at = new Date().toISOString();

  res.json({ success: true });
});

// ==========================================
// Chess Routes
// ==========================================

app.get('/chess', (req, res) => {
  const token = ensureGameToken(req, res);
  res.render('chess.html', { room: null, my_color: null });
});

app.post('/chess/create', (req, res) => {
  const token = ensureGameToken(req, res);
  const name = escapeHtml((req.body.name || req.cookies.bbs_saved_author || '名無しさん').trim()).slice(0, 20) || '名無しさん';
  const code = newRoomCode();
  const now = new Date().toISOString();

  const room = {
    room_code: code,
    white_token: token,
    white_name: name,
    black_token: null,
    black_name: null,
    board: initialChess(),
    turn: 'w',
    status: 'waiting',
    winner: null,
    in_check: null,
    castling: 'KQkq',
    en_passant: null,
    created_at: now,
    updated_at: now
  };
  chessRooms.set(code, room);
  res.redirect(`/chess/${code}`);
});

app.get('/chess/:code', (req, res) => {
  const code = req.params.code.toUpperCase();
  const room = chessRooms.get(code);
  if (!room) return res.redirect('/chess');

  const token = ensureGameToken(req, res);
  const myColor = room.white_token === token ? 'w' : (room.black_token === token ? 'b' : null);
  res.render('chess.html', { room, my_color: myColor });
});

app.post('/chess/:code/join', (req, res) => {
  const code = req.params.code.toUpperCase();
  const room = chessRooms.get(code);
  if (!room) return res.status(404).json({ success: false, error: '部屋が見つかりません' });

  const token = ensureGameToken(req, res);
  const name = escapeHtml((req.body.name || req.cookies.bbs_saved_author || '名無しさん').trim()).slice(0, 20) || '名無しさん';

  if (room.white_token === token || room.black_token === token) {
    return res.json({ success: true });
  }
  if (room.black_token) {
    return res.status(409).json({ success: false, error: 'この部屋は満員です' });
  }

  room.black_token = token;
  room.black_name = name;
  room.status = 'playing';
  room.updated_at = new Date().toISOString();
  res.json({ success: true });
});

app.get('/api/chess/:code/state', (req, res) => {
  const code = req.params.code.toUpperCase();
  const r = chessRooms.get(code);
  if (!r) return res.status(404).json({ error: 'not found' });

  const token = ensureGameToken(req, res);
  const my = r.white_token === token ? 'w' : (r.black_token === token ? 'b' : null);

  res.json({
    success: true,
    room_code: r.room_code,
    board: r.board,
    turn: r.turn,
    status: r.status,
    winner: r.winner,
    in_check: r.in_check,
    castling: r.castling || 'KQkq',
    en_passant: r.en_passant,
    white_name: r.white_name || '名無しさん',
    black_name: r.black_name || '名無しさん',
    white_id: (r.white_token || '').slice(0, 4),
    black_id: (r.black_token || '').slice(0, 4),
    has_black: Boolean(r.black_token),
    my_color: my
  });
});

app.post('/chess/:code/move', (req, res) => {
  const code = req.params.code.toUpperCase();
  const r = chessRooms.get(code);
  if (!r) return res.status(404).json({ success: false, error: '部屋が見つかりません' });

  const token = ensureGameToken(req, res);
  const color = r.white_token === token ? 'w' : (r.black_token === token ? 'b' : null);
  if (!color) return res.status(403).json({ success: false, error: '観戦者は着手できません' });
  if (r.status !== 'playing') return res.status(400).json({ success: false, error: '対局は終了しています' });
  if (r.turn !== color) return res.status(400).json({ success: false, error: '相手のターンです' });

  const { from_row, from_col, to_row, to_col } = req.body;
  const fr = parseInt(from_row, 10);
  const fc = parseInt(from_col, 10);
  const tr = parseInt(to_row, 10);
  const tc = parseInt(to_col, 10);

  if (![fr, fc, tr, tc].every(v => !isNaN(v) && v >= 0 && v < 8)) {
    return res.status(400).json({ success: false, error: '着手位置が不正です' });
  }

  const board = r.board;
  const castling = r.castling || 'KQkq';
  const enPassant = r.en_passant;

  const piece = board[fr * 8 + fc];
  if (!piece || piece[0] !== color) {
    return res.status(400).json({ success: false, error: '自分の駒を選んでください' });
  }

  const pseudos = chessPseudo(board, fr, fc, castling, enPassant);
  if (!pseudos.some(([pr, pc]) => pr === tr && pc === tc)) {
    return res.status(400).json({ success: false, error: 'その駒はそこへ動かせません' });
  }

  const simulated = chessApply(board, fr, fc, tr, tc);
  if (chessInCheck(simulated, color)) {
    return res.status(400).json({ success: false, error: 'キングが取られる手は指せません（王手放置）' });
  }

  // Update castling rights
  const newCastling = chessUpdateCastlingRights(castling, piece[1], color, fr, fc, tr, tc);

  // Update en passant
  let nextEnPassant = null;
  if (piece[1] === 'P' && Math.abs(tr - fr) === 2) {
    nextEnPassant = [(fr + tr) / 2, fc];
  }

  const opp = color === 'w' ? 'b' : 'w';
  const nextMoves = chessLegalMoves(simulated, opp, newCastling, nextEnPassant);
  const check = chessInCheck(simulated, opp);

  let status = 'playing';
  let winner = null;

  if (nextMoves.length === 0) {
    status = 'finished';
    winner = check ? color : 'draw'; // Checkmate or Stalemate
  }

  r.board = simulated;
  r.turn = opp;
  r.status = status;
  r.winner = winner;
  r.in_check = check ? opp : null;
  r.castling = newCastling;
  r.en_passant = nextEnPassant;
  r.updated_at = new Date().toISOString();

  res.json({ success: true });
});

// Start Server
app.listen(PORT, '0.0.0.0', () => {
  console.log(`[talk-ch] Server running on http://0.0.0.0:${PORT}`);
});
