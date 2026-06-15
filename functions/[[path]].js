import { Hono } from 'hono'
import { getCookie, setCookie } from 'hono/cookie'

const app = new Hono()
const ADMIN_PASSWORD = "kenji1228s00460962"

// 💡 2ちゃんねる風の今日限定ID生成
async function getDailyUserId(userToken) {
  const todayStr = new Date().toISOString().split('T')[0]
  const rawStr = `${userToken}_${todayStr}`
  const msgUint8 = new TextEncoder().encode(rawStr)
  const hashBuffer = await crypto.subtle.digest('MD5', msgUint8)
  const hashArray = Array.from(new Uint8Array(hashBuffer))
  return hashArray.map(b => b.toString(16).padStart(2, '0')).join('').substring(0, 8)
}

// 🏠 スレッド一覧の取得（API）
app.get('/api/threads', async (c) => {
  const { results: threads } = await c.env.DB.prepare("SELECT * FROM threads ORDER BY id DESC").all()
  const adminMsgRow = await c.env.DB.prepare("SELECT value FROM settings WHERE key = 'admin_message'").first()
  return c.json({ threads, admin_message: adminMsgRow ? adminMsgRow.value : "ここに管理者の一言が表示されます。" })
})

// 🧵 スレッド作成
app.post('/create_thread', async (c) => {
  const body = await c.req.parseBody()
  if (!body.title) return c.redirect('/')
  await c.env.DB.prepare("INSERT INTO threads (title, created_at) VALUES (?, ?)")
    .bind(body.title, new Date().toLocaleString('ja-JP', { timeZone: 'Asia/Tokyo' })).run()
  return c.redirect('/')
})

export default app
