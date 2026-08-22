import crypto from 'crypto';
import 'dotenv/config';

// Cloudflare D1 Configuration from environment variables
const CF_ACCOUNT_ID = process.env.CF_D1_ACCOUNT_ID;
const CF_DATABASE_ID = process.env.CF_D1_DATABASE_ID;
const CF_API_TOKEN = process.env.CF_D1_API_TOKEN;

const isD1Configured = Boolean(CF_ACCOUNT_ID && CF_DATABASE_ID && CF_API_TOKEN);

console.log(`[Database] Cloudflare D1 is ${isD1Configured ? 'Configured (Active)' : 'Not configured (using in-memory fallback)'}`);

// ==========================================
// Cloudflare D1 REST API Client
// ==========================================
export async function queryD1(sql, params = []) {
  if (!isD1Configured) return null;
  try {
    const url = `https://api.cloudflare.com/client/v4/accounts/${CF_ACCOUNT_ID}/d1/database/${CF_DATABASE_ID}/query`;
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${CF_API_TOKEN}`,
        'Content-Type': 'application/json'
      },
      body: JSON.stringify({ sql, params })
    });

    if (!response.ok) {
      const errText = await response.text();
      console.warn(`[D1 Error ${response.status}]:`, errText);
      return null;
    }

    const data = await response.json();
    if (data.success && data.result && data.result.length > 0) {
      const queryResult = data.result[0];
      const results = queryResult.results || [];
      // Attach meta properties (last_row_id, changes) for non-RETURNING SQLite fallbacks
      if (queryResult.meta) {
        results.meta = queryResult.meta;
      }
      return results;
    }
    return [];
  } catch (err) {
    console.warn('[D1 Connection Error]:', err.message);
    return null;
  }
}

// In-Memory fallback store
const mem = {
  adminMessage: 'トークちゃんねるへようこそ！みんなで仲良く使ってね。',
  nextThreadId: 8,
  nextReplyId: 20,
  threads: [
    { id: 4, title: '運営からのお知らせ・アップデート情報', ip_address: '127.0.0.1', created_at: new Date(Date.now() - 1000 * 60 * 60 * 24 * 3).toISOString(), is_pinned: 1 },
    { id: 3, title: '管理人への要望・不具合報告スレ', ip_address: '127.0.0.1', created_at: new Date(Date.now() - 1000 * 60 * 60 * 24 * 4).toISOString(), is_pinned: 1 },
    { id: 2, title: '【総合】雑談スレッド Part 1', ip_address: '127.0.0.1', created_at: new Date(Date.now() - 1000 * 60 * 60 * 24 * 5).toISOString(), is_pinned: 1 },
    { id: 1, title: '【公式】トークちゃんねる利用規約・ルール', ip_address: '127.0.0.1', created_at: new Date(Date.now() - 1000 * 60 * 60 * 24 * 7).toISOString(), is_pinned: 1 },
    { id: 5, title: '今日の晩御飯なに食べる？', ip_address: '127.0.0.1', created_at: new Date(Date.now() - 1000 * 60 * 60 * 12).toISOString(), is_pinned: 0 },
    { id: 6, title: 'おすすめのゲーム・アニメ教えて', ip_address: '127.0.0.1', created_at: new Date(Date.now() - 1000 * 60 * 60 * 6).toISOString(), is_pinned: 0 },
    { id: 7, title: 'オセロ・チェス対戦募集スレ', ip_address: '127.0.0.1', created_at: new Date(Date.now() - 1000 * 60 * 60 * 2).toISOString(), is_pinned: 0 }
  ],
  replies: [
    { id: 1, thread_id: 1, author: 'ペンギン★', content: 'トークちゃんねるをご利用いただきありがとうございます。\n公序良俗に反する投稿、他者への誹謗中傷、荒らし行為は禁止です。\nマナーを守って楽しく交流しましょう！', user_id: 'STAFF', is_admin: 1, role: 'admin', image_url: '', ip_address: '127.0.0.1', date: new Date(Date.now() - 1000 * 60 * 60 * 24 * 7).toISOString() },
    { id: 2, thread_id: 1, author: 'Mino★', content: '健全で楽しいコミュニティづくりにご協力をお願いします✨', user_id: 'STAFF', is_admin: 1, role: 'sub_admin', image_url: '', ip_address: '127.0.0.1', date: new Date(Date.now() - 1000 * 60 * 60 * 24 * 6).toISOString() },
    { id: 3, thread_id: 2, author: '名無しさん', content: '雑談スレ立てました！なんでも自由にどうぞー', user_id: 'a1b2c3d4', is_admin: 0, role: null, image_url: '', ip_address: '127.0.0.1', date: new Date(Date.now() - 1000 * 60 * 60 * 24 * 5).toISOString() },
    { id: 4, thread_id: 2, author: '名無しさん', content: '>>1 乙です！\nよろしくね〜', user_id: 'e5f6g7h8', is_admin: 0, role: null, image_url: '', ip_address: '127.0.0.2', date: new Date(Date.now() - 1000 * 60 * 60 * 24 * 4).toISOString() },
    { id: 5, thread_id: 3, author: '名無しさん', content: '機能要望やバグ報告はこちらへどうぞ。', user_id: 'op_user_3', is_admin: 0, role: null, image_url: '', ip_address: '127.0.0.1', date: new Date(Date.now() - 1000 * 60 * 60 * 24 * 4).toISOString() },
    { id: 6, thread_id: 4, author: 'ペンギン★', content: 'オンラインオセロとチェス機能が追加されました！上部メニューの「ゲーム」から遊べます。', user_id: 'STAFF', is_admin: 1, role: 'admin', image_url: '', ip_address: '127.0.0.1', date: new Date(Date.now() - 1000 * 60 * 60 * 24 * 3).toISOString() },
    { id: 7, thread_id: 5, author: '名無しさん', content: 'カレー作ろうと思う！おすすめの隠し味ある？', user_id: '9988aabb', is_admin: 0, role: null, image_url: '', ip_address: '127.0.0.1', date: new Date(Date.now() - 1000 * 60 * 60 * 12).toISOString() },
    { id: 8, thread_id: 5, author: '名無しさん', content: '>>1 チョコレートかインスタントコーヒー少し入れるとコクが出るよ', user_id: '11223344', is_admin: 0, role: null, image_url: '', ip_address: '127.0.0.3', date: new Date(Date.now() - 1000 * 60 * 60 * 10).toISOString() },
    { id: 9, thread_id: 6, author: '名無しさん', content: '最近ハマってるおすすめ作品教えてください！', user_id: '55667788', is_admin: 0, role: null, image_url: '', ip_address: '127.0.0.1', date: new Date(Date.now() - 1000 * 60 * 60 * 6).toISOString() },
    { id: 10, thread_id: 7, author: '名無しさん', content: '誰かオセロかチェスやりませんか？部屋作ったら部屋コード貼ってください！', user_id: 'ccddeeff', is_admin: 0, role: null, image_url: '', ip_address: '127.0.0.1', date: new Date(Date.now() - 1000 * 60 * 60 * 2).toISOString() }
  ],
  bannedIps: new Set(),
  archivedThreads: [
    {
      thread_id: 100,
      title: '【過去ログ】第1回オンライントーナメント開催記念スレ',
      reply_count: 42,
      archived_at: new Date(Date.now() - 1000 * 60 * 60 * 24 * 30).toISOString()
    },
    {
      thread_id: 99,
      title: '【過去ログ】おすすめWebサービス・便利ツール紹介',
      reply_count: 18,
      archived_at: new Date(Date.now() - 1000 * 60 * 60 * 24 * 35).toISOString()
    }
  ],
  archivedData: {
    100: {
      thread: { id: 100, title: '【過去ログ】第1回オンライントーナメント開催記念スレ', created_at: '2026-06-01T12:00:00Z' },
      replies: [
        { id: 1, author: 'ペンギン★', content: 'オンライントーナメント開幕！熱い対局を期待しています。', user_id: 'STAFF', is_admin: 1, role: 'admin', date: '2026-06-01 12:05:00', image_url: '' },
        { id: 2, author: '名無しさん', content: '参加します！よろしくお願いします！', user_id: 'a8b9c0d1', is_admin: 0, role: null, date: '2026-06-01 12:10:00', image_url: '' }
      ],
      archived_at: new Date(Date.now() - 1000 * 60 * 60 * 24 * 30).toISOString()
    },
    99: {
      thread: { id: 99, title: '【過去ログ】おすすめWebサービス・便利ツール紹介', created_at: '2026-05-15T10:00:00Z' },
      replies: [
        { id: 1, author: '名無しさん', content: '日常で役立つ便利ツールを共有していきましょう。', user_id: '77665544', is_admin: 0, role: null, date: '2026-05-15 10:00:00', image_url: '' }
      ],
      archived_at: new Date(Date.now() - 1000 * 60 * 60 * 24 * 35).toISOString()
    }
  }
};

// Initialize Cloudflare D1 tables if needed
export async function initDb() {
  if (!isD1Configured) return;
  try {
    await queryD1(`
      CREATE TABLE IF NOT EXISTS threads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        ip_address TEXT,
        created_at TEXT NOT NULL
      );
    `);
    try {
      await queryD1(`ALTER TABLE threads ADD COLUMN is_pinned INTEGER DEFAULT 0;`);
    } catch (_) {}

    await queryD1(`
      CREATE TABLE IF NOT EXISTS replies (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        thread_id INTEGER NOT NULL,
        author TEXT NOT NULL,
        content TEXT,
        user_id TEXT,
        is_admin INTEGER DEFAULT 0,
        role TEXT,
        image_url TEXT,
        ip_address TEXT,
        date TEXT NOT NULL
      );
    `);
    await queryD1(`
      CREATE TABLE IF NOT EXISTS admin_meta (
        key TEXT PRIMARY KEY,
        value TEXT
      );
    `);
    await queryD1(`
      CREATE TABLE IF NOT EXISTS banned_ips (
        ip TEXT PRIMARY KEY,
        created_at TEXT
      );
    `);
    await queryD1(`
      CREATE TABLE IF NOT EXISTS archived_threads (
        thread_id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        reply_count INTEGER DEFAULT 0,
        archived_at TEXT NOT NULL,
        data TEXT
      );
    `);
    console.log('[Database] Cloudflare D1 tables verified/initialized.');
  } catch (err) {
    console.warn('[Database] D1 Table initialization warning:', err.message);
  }
}

// Database Helper Functions

export async function isBannedIp(ip) {
  if (mem.bannedIps.has(ip)) return true;
  if (isD1Configured) {
    const res = await queryD1(`SELECT ip FROM banned_ips WHERE ip = ? LIMIT 1`, [ip]);
    if (res && res.length > 0) {
      mem.bannedIps.add(ip);
      return true;
    }
  }
  return false;
}

export async function getAdminMessage() {
  if (isD1Configured) {
    try {
      const res = await queryD1(`SELECT message FROM admin_messages ORDER BY id DESC LIMIT 1`);
      if (res && res.length > 0 && res[0].message) return res[0].message;
    } catch (_) {}
    try {
      const res2 = await queryD1(`SELECT value FROM admin_meta WHERE key = 'admin_message' LIMIT 1`);
      if (res2 && res2.length > 0 && res2[0].value) return res2[0].value;
    } catch (_) {}
  }
  return mem.adminMessage;
}

export async function setAdminMessage(message) {
  mem.adminMessage = message;
  if (isD1Configured) {
    try {
      await queryD1(`UPDATE admin_messages SET message = ? WHERE id = 1`, [message]);
    } catch (_) {}
    try {
      await queryD1(`INSERT INTO admin_meta (key, value) VALUES ('admin_message', ?) ON CONFLICT(key) DO UPDATE SET value = ?`, [message, message]);
    } catch (_) {}
  }
}

export async function getThreads(page = 1, perPage = 20, searchQuery = '') {
  let d1Res = null;
  if (isD1Configured) {
    d1Res = await queryD1(`
      SELECT t.id, t.title, t.ip_address, t.created_at,
             (CASE WHEN t.id IN (1, 2, 3, 4) THEN 1 ELSE 0 END) as is_pinned,
             COUNT(r.id) as replies_count
      FROM threads t
      LEFT JOIN replies r ON t.id = r.thread_id
      ${searchQuery ? "WHERE t.title LIKE '%' || ? || '%'" : ""}
      GROUP BY t.id
      ORDER BY (CASE WHEN t.id IN (1, 2, 3, 4) THEN 1 ELSE 0 END) DESC, t.id DESC
    `, searchQuery ? [searchQuery] : []);
  }

  if (d1Res && Array.isArray(d1Res)) {
    const total = d1Res.length;
    const startIndex = (page - 1) * perPage;
    const paginated = d1Res.slice(startIndex, startIndex + perPage);
    return {
      threads: paginated,
      total,
      hasNext: total > startIndex + perPage
    };
  }

  // Memory fallback
  let filtered = [...mem.threads];
  if (searchQuery) {
    filtered = filtered.filter(t => t.title.toLowerCase().includes(searchQuery.toLowerCase()));
  }

  const pinnedIds = [4, 3, 2, 1];
  const pinnedList = [];
  const normalList = [];

  if (!searchQuery) {
    for (const t of filtered) {
      if (pinnedIds.includes(t.id) || t.is_pinned) {
        pinnedList.push({ ...t, is_pinned: true });
      } else {
        normalList.push(t);
      }
    }
    pinnedList.sort((a, b) => pinnedIds.indexOf(a.id) - pinnedIds.indexOf(b.id));
    normalList.sort((a, b) => b.id - a.id);
  } else {
    filtered.sort((a, b) => b.id - a.id);
  }

  const combined = searchQuery ? filtered : [...pinnedList, ...normalList];
  for (const t of combined) {
    t.replies_count = mem.replies.filter(r => r.thread_id === t.id).length;
  }

  const startIndex = (page - 1) * perPage;
  const paginated = combined.slice(startIndex, startIndex + perPage);

  return {
    threads: paginated,
    total: combined.length,
    hasNext: combined.length > startIndex + perPage
  };
}

export async function getThreadById(id) {
  if (isD1Configured) {
    const res = await queryD1(`SELECT * FROM threads WHERE id = ? LIMIT 1`, [id]);
    if (res && res.length > 0) return res[0];
  }
  return mem.threads.find(t => t.id === Number(id)) || null;
}

export async function createThread(title, ipAddress, isPinned = false) {
  const now = new Date().toISOString();
  if (isD1Configured) {
    const res = await queryD1(`
      INSERT INTO threads (title, ip_address, created_at, is_pinned)
      VALUES (?, ?, ?, ?)
      RETURNING *
    `, [title, ipAddress, now, isPinned ? 1 : 0]);
    if (res && res.length > 0 && res[0].id) return res[0];
    if (res && res.meta && res.meta.last_row_id) {
      return { id: res.meta.last_row_id, title, ip_address: ipAddress, created_at: now, is_pinned: isPinned ? 1 : 0 };
    }
  }

  const newThread = {
    id: mem.nextThreadId++,
    title,
    ip_address: ipAddress,
    created_at: now,
    is_pinned: isPinned ? 1 : 0
  };
  mem.threads.unshift(newThread);
  return newThread;
}

export async function getReplies(threadId) {
  if (isD1Configured) {
    const res = await queryD1(`SELECT * FROM replies WHERE thread_id = ? ORDER BY id ASC`, [threadId]);
    if (res) return res;
  }
  return mem.replies.filter(r => r.thread_id === Number(threadId));
}

export async function getRepliesAfter(threadId, afterId) {
  if (isD1Configured) {
    const res = await queryD1(`SELECT * FROM replies WHERE thread_id = ? AND id > ? ORDER BY id ASC`, [threadId, afterId]);
    if (res) return res;
  }
  return mem.replies.filter(r => r.thread_id === Number(threadId) && r.id > Number(afterId));
}

export async function getRepliesBefore(threadId, beforeId, limit = 300) {
  if (isD1Configured) {
    const res = await queryD1(`
      SELECT * FROM (
        SELECT * FROM replies WHERE thread_id = ? AND id < ? ORDER BY id DESC LIMIT ?
      ) ORDER BY id ASC
    `, [threadId, beforeId, limit]);
    if (res) return res;
  }
  const filtered = mem.replies.filter(r => r.thread_id === Number(threadId) && r.id < Number(beforeId));
  return filtered.slice(-limit);
}

export async function createReply({ threadId, author, content, userId, isAdmin, role, imageUrl, ipAddress }) {
  const now = new Date().toISOString();
  if (isD1Configured) {
    const res = await queryD1(`
      INSERT INTO replies (thread_id, author, content, user_id, is_admin, role, image_url, ip_address, date)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
      RETURNING *
    `, [threadId, author, content, userId, isAdmin ? 1 : 0, role || null, imageUrl || '', ipAddress, now]);
    if (res && res.length > 0 && res[0].id) return res[0];
    if (res && res.meta && res.meta.last_row_id) {
      return {
        id: res.meta.last_row_id,
        thread_id: Number(threadId),
        author,
        content,
        user_id: userId,
        is_admin: isAdmin ? 1 : 0,
        role: role || null,
        image_url: imageUrl || '',
        ip_address: ipAddress,
        date: now
      };
    }
  }

  const newReply = {
    id: mem.nextReplyId++,
    thread_id: Number(threadId),
    author,
    content,
    user_id: userId,
    is_admin: isAdmin ? 1 : 0,
    role: role || null,
    image_url: imageUrl || '',
    ip_address: ipAddress,
    date: now
  };
  mem.replies.push(newReply);
  return newReply;
}

export async function deleteThread(threadId) {
  if (isD1Configured) {
    await queryD1(`DELETE FROM replies WHERE thread_id = ?`, [threadId]);
    await queryD1(`DELETE FROM threads WHERE id = ?`, [threadId]);
  }
  const tIdx = mem.threads.findIndex(t => t.id === Number(threadId));
  if (tIdx !== -1) mem.threads.splice(tIdx, 1);
  mem.replies = mem.replies.filter(r => r.thread_id !== Number(threadId));
}

export async function deleteReply(threadId, replyId) {
  if (isD1Configured) {
    await queryD1(`
      UPDATE replies
      SET author = 'あぼーん',
          content = 'この書き込みは管理員によって削除されました。',
          user_id = '???',
          is_admin = 0,
          image_url = ''
      WHERE id = ? AND thread_id = ?
    `, [replyId, threadId]);
  }
  const reply = mem.replies.find(r => r.id === Number(replyId) && r.thread_id === Number(threadId));
  if (reply) {
    reply.author = 'あぼーん';
    reply.content = 'この書き込みは管理員によって削除されました。';
    reply.user_id = '???';
    reply.is_admin = 0;
    reply.image_url = '';
  }
}

export async function banUserByReply(replyId) {
  let ip = null;
  if (isD1Configured) {
    const r = await queryD1(`SELECT ip_address FROM replies WHERE id = ? LIMIT 1`, [replyId]);
    if (r && r.length > 0) ip = r[0].ip_address;
  } else {
    const r = mem.replies.find(x => x.id === Number(replyId));
    if (r) ip = r.ip_address;
  }

  if (ip) {
    mem.bannedIps.add(ip);
    if (isD1Configured) {
      await queryD1(`INSERT INTO banned_ips (ip, created_at) VALUES (?, ?) ON CONFLICT(ip) DO NOTHING`, [ip, new Date().toISOString()]);
      await queryD1(`
        UPDATE replies
        SET author = 'あぼーん',
            content = 'この書き込みは管理員によってBANされました。',
            user_id = '???',
            is_admin = 0,
            image_url = ''
        WHERE id = ?
      `, [replyId]);
    }
    const targetReply = mem.replies.find(x => x.id === Number(replyId));
    if (targetReply) {
      targetReply.author = 'あぼーん';
      targetReply.content = 'この書き込みは管理員によってBANされました。';
      targetReply.user_id = '???';
      targetReply.is_admin = 0;
      targetReply.image_url = '';
    }
  }
}

export async function banThreadOwner(threadId) {
  const thread = await getThreadById(threadId);
  if (thread && thread.ip_address) {
    const ip = thread.ip_address;
    mem.bannedIps.add(ip);
    if (isD1Configured) {
      await queryD1(`INSERT INTO banned_ips (ip, created_at) VALUES (?, ?) ON CONFLICT(ip) DO NOTHING`, [ip, new Date().toISOString()]);
      await queryD1(`UPDATE threads SET title = '【このスレッドは管理員によってBANされました】' WHERE id = ?`, [threadId]);
      await queryD1(`DELETE FROM replies WHERE thread_id = ?`, [threadId]);
      await queryD1(`
        INSERT INTO replies (thread_id, author, content, user_id, is_admin, role, image_url, ip_address, date)
        VALUES (?, 'あぼーん', 'このスレッドの作成者はBANされました。', '???', 0, NULL, '', ?, ?)
      `, [threadId, ip, new Date().toISOString()]);
    }
    const memThread = mem.threads.find(t => t.id === Number(threadId));
    if (memThread) memThread.title = '【このスレッドは管理員によってBANされました】';
    mem.replies = mem.replies.filter(r => r.thread_id !== Number(threadId));
    mem.replies.push({
      id: mem.nextReplyId++,
      thread_id: Number(threadId),
      author: 'あぼーん',
      content: 'このスレッドの作成者はBANされました。',
      user_id: '???',
      is_admin: 0,
      role: null,
      image_url: '',
      ip_address: ip,
      date: new Date().toISOString()
    });
  }
}

export async function getArchivedThreads(page = 1, perPage = 20) {
  if (isD1Configured) {
    const res = await queryD1(`SELECT thread_id, title, reply_count, archived_at FROM archived_threads ORDER BY archived_at DESC`);
    if (res && Array.isArray(res)) {
      const startIndex = (page - 1) * perPage;
      const paginated = res.slice(startIndex, startIndex + perPage);
      return {
        archived_threads: paginated,
        hasNext: res.length > startIndex + perPage
      };
    }
  }
  const startIndex = (page - 1) * perPage;
  const list = mem.archivedThreads.slice(startIndex, startIndex + perPage);
  return {
    archived_threads: list,
    hasNext: mem.archivedThreads.length > startIndex + perPage
  };
}

export async function getArchivedThread(id) {
  if (isD1Configured) {
    const res = await queryD1(`SELECT * FROM archived_threads WHERE thread_id = ? LIMIT 1`, [id]);
    if (res && res.length > 0 && res[0].data) {
      try {
        const parsed = JSON.parse(res[0].data);
        return {
          thread: parsed.thread,
          replies: parsed.replies,
          archived_at: res[0].archived_at
        };
      } catch (e) {
        console.error('Failed to parse archived thread JSON:', e);
      }
    }
  }
  return mem.archivedData[id] || null;
}

export async function authenticateStaff(username, password) {
  if (isD1Configured) {
    try {
      const res = await queryD1(`SELECT id, username, role, display_name FROM staff_users WHERE username = ? AND password = ? LIMIT 1`, [username, password]);
      if (res && res.length > 0) {
        return res[0];
      }
    } catch (_) {}
  }
  const fallbackUsers = [
    { id: 1, username: 'admin', password: 'password123', role: 'admin', display_name: 'ペンギン★' },
    { id: 2, username: 'mino', password: 'password123', role: 'sub_admin', display_name: 'Mino★' }
  ];
  return fallbackUsers.find(u => u.username === username && u.password === password) || null;
}
