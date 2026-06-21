from datetime import datetime
import json
import os
from supabase import create_client, Client

# 🟢 あなたのSupabaseに接続するための情報です
SUPABASE_URL = 'https://mpzjidhuovorzvjhukmy.supabase.co'
SUPABASE_KEY = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1wemppZGh1b3Zvcnp2amh1a215Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODIwMDYzMjIsImV4cCI6MjA5NzU4MjMyMn0.Q11dCsMYX0LakWydaVD6EIKKJD2Wbv7qHV0GuAyxEeo'

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
DATA_FILE = 'bbs_data.json'

def migrate_data():
    if not os.path.exists(DATA_FILE):
        print(f"エラー: {DATA_FILE} が見つかりません。掲示板の正しいフォルダで実行してください。")
        return

    print("🔄 JSONファイルから大切なデータを読み込んでいます...")
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
        data = json.load(f)

    # 1. BANされたIPの引っ越し
    if "banned_ips" in data:
        print("🚫 BANリストを引っ越し中...")
        for ip in data["banned_ips"]:
            try:
                supabase.table('banned_ips').insert({'ip': ip}).execute()
            except Exception as e:
                pass

    # 2. 管理者メッセージの引っ越し
    if "admin_message" in data:
        print("💬 管理者メッセージを引っ越し中...")
        try:
            supabase.table('admin_messages').update({'message': data["admin_message"]}).eq('id', 1).execute()
        except Exception as e:
            print(f"管理者メッセージ引っ越しエラー: {e}")

    # 3. スレッドとレスの引っ越し（古い順にデータベースへ保存します）
    if "threads" in data:
        print("📝 スレッドと書き込み（レス）をすべて引っ越し中...")
        for thread in reversed(data["threads"]):
            try:
                # スレッドの作成
                supabase.table('threads').insert({
                    'id': thread['id'],  
                    'title': thread['title'],
                    'created_at': thread.get('created_at', datetime.now().isoformat())
                }).execute()
                
                print(f"✅ スレ移行完了【{thread['id']}】: {thread['title']}")

                # そのスレッドに紐づくすべてのレスをコピー
                if 'replies' in thread and thread['replies']:
                    for reply in thread['replies']:
                        reply_date = reply.get('date', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
                        iso_date = reply_date.replace(' ', 'T') + '+09:00' 

                        supabase.table('replies').insert({
                            'thread_id': thread['id'],
                            'author': reply['author'],
                            'content': reply['content'],
                            'user_id': reply['user_id'],
                            'is_admin': reply.get('is_admin', False),
                            'image_url': reply.get('image_url', ''),
                            'ip_address': reply.get('ip_address', ''),
                            'date': iso_date
                        }).execute()
            except Exception as e:
                print(f"⚠️ スレID {thread['id']} の移行中にエラー（既に移行済みの可能性があります）: {e}")

    print("✨ すべてのデータが頑丈なSupabaseの金庫へ引っ越し完了しました！")

if __name__ == '__main__':
    migrate_data()
