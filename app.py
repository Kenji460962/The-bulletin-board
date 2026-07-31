from flask import Flask, render_template, request, redirect, url_for, make_response, session

from datetime import datetime

import json
import html
import os
import hashlib
import uuid
import time
import re


# その他の既存インポート...








# Supabaseを使うためのライブラリを読み込み
from supabase import create_client, Client


import boto3  
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'super_secret_bbs_key_12345') # 必須: セッション暗号化用キー

# スリープ防止
@app.before_request
def response_to_uptimerobot():
    if request.method == 'HEAD':
        return make_response('', 200)



# Cloudflare R2 (S3互換) の設定
s3_client = boto3.client(
    's3',
    endpoint_url=os.environ.get('R2_ENDPOINT'),
    aws_access_key_id=os.environ.get('R2_ACCESS_KEY_ID'),
    aws_secret_access_key=os.environ.get('R2_SECRET_ACCESS_KEY'),
    region_name='auto'
)
R2_BUCKET_NAME = os.environ.get('R2_BUCKET_NAME', 'bbs-images')
R2_PUBLIC_URL = os.environ.get('R2_PUBLIC_URL')  







SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://mpzjidhuovorzvjhukmy.supabase.co')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1wemppZGh1b3Zvcnp2amh1a215Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODIwMDYzMjIsImV4cCI6MjA5NzU4MjMyMn0.Q11dCsMYX0LakWydaVD6EIKKJD2Wbv7qHV0GuAyxEeo')

# Supabaseに接続するロボットを起動
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

DATA_FILE = 'bbs_data.json'
ADMIN_PASSWORD = "setokoji114514810072"

# セキュリティ強化 ユーザーごとの最後の書き込み時間を記録する場所
LAST_POST_TIMES = {}
# ユーザーごとの最後の「スレ立て」「レス投稿」の時間を分けて記録
LAST_THREAD_TIMES = {}
LAST_REPLY_TIMES = {}

def auto_migrate_from_json():
    pass

# IPアドレスを元に毎日変わるIDを生成
def get_daily_user_id(ip_address):
    today_str = datetime.now().strftime('%Y-%m-%d')
    raw_str = f"{ip_address}_{today_str}"
    hashed = hashlib.md5(raw_str.encode('utf-8')).hexdigest()
    return hashed[:8]

def get_client_ip():
    # ユーザーIPを取得
    if request.headers.get('X-Forwarded-For'):
        # カンマ区切りの先頭がクライアントのIP
        ip = request.headers.get('X-Forwarded-For').split(',')[0].strip()
    elif request.headers.get('CF-Connecting-IP'):
        ip = request.headers.get('CF-Connecting-IP').strip()
    else:
        ip = request.remote_addr or ""
    return ip


#banされたIPかを確認
def is_banned_ip(ip):
    if not ip:
        return False
    try:
        # Supabaseの banned_ips テーブルと照合
        res = supabase.table('banned_ips').select('*').eq('ip_address', ip).execute()
        return len(res.data) > 0
    except Exception as e:
        print(f"BANチェックエラー: {e}")
        return False






def get_staff_role():
    """現在のログイン中の役職を取得"""
    return session.get('staff_role')

def can_manage_board():
    """削除・BAN・一言更新ができるのは admin と sub_admin のみ"""
    return session.get('staff_role') in ['admin', 'sub_admin']

# --- 運営用ログイン・ログアウトルート ---
@app.route('/login_secret_8823', methods=['GET', 'POST'])
def staff_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        try:
            # Supabaseの 'staff_users' テーブルから照合
            res = supabase.table('staff_users').select('*').eq('username', username).execute()
            if res.data:
                user = res.data[0]
                # 本来はハッシュ化推奨ですが、一旦は分かりやすく平文パスワードで比較します
                if user['password'] == password: 
                    session['staff_id'] = user['id']
                    session['staff_role'] = user['role']         # admin, sub_admin, pr など
                    session['staff_name'] = user['display_name'] # 掲示板に表示する名前
                    return redirect(url_for('index'))
        except Exception as e:
            print(f"Login error: {e}")
            
        return "ログイン失敗", 401
        
    return '''
        <form method="post">
            ID: <input type="text" name="username"><br>
            PW: <input type="password" name="password"><br>
            <input type="submit" value="Enter">
        </form>
    '''

@app.route('/staff_logout')
def staff_logout():
    session.clear()
    return redirect(url_for('index'))




# NGワード
NG_WORDS = {
    '死ね': '〇ね',
    'しね': '〇ね',
    'エロ': 'エ〇',
    'えろ': 'え〇',
    'まんこ': 'ま〇こ',
    'ちんこ': 'ち〇こ',
    'マンコ': 'マ〇コ',
    'チンコ': 'チ〇コ',
    'セックス': 'セ。〇ス',
    'せっくす': 'せ。〇す',
    'おっぱい': 'お。〇い',
    'オッパイ': 'オ。〇イ',
    'レイプ': 'レ〇プ',
    'れいぷ': 'れ〇ぷ',
    'バカ': 'バ*',
    'アホ': 'ア*',
    'シコシコ':'4545',
    'オナニー':'0721',
    '射精':'身寸米青',
    
}

def filter_ng_words(text):
    if not text:
        return text
    for ng_word, replaced_word in NG_WORDS.items():
        if ng_word in text:
            text = text.replace(ng_word, replaced_word)
    return text





ACTIVE_USERS = {}

def update_and_get_user_counts(current_token, location):
    now = time.time()
    if current_token:
        ACTIVE_USERS[current_token] = {
            "location": location,
            "last_time": now
        }
    expired_tokens = [token for token, info in ACTIVE_USERS.items() if now - info["last_time"] > 300]
    for token in expired_tokens:
        del ACTIVE_USERS[token]
    count = sum(1 for info in ACTIVE_USERS.values() if info["location"] == location)
    return count

@app.route('/privacy')
def privacy():
    return render_template('privacy.html')
    
@app.route('/roles')
def roles():
    return render_template('roles.html')



@app.route('/', methods=['GET', 'HEAD'])
def index():
    client_ip = get_client_ip()

    
    if is_banned_ip(client_ip):
        return "あなたはアクセス禁止（BAN）されています。", 403

    if request.method == 'HEAD':
        return make_response('', 200)

    # 現在のページ番号を取得
    page = request.args.get('page', default=1, type=int)
    per_page = 20  # 1ページあたりの表示件数
    start_index = (page - 1) * per_page
    end_index = start_index + per_page - 1

    try:
        # 全スレッドを最新順（IDの降順）で取得する
        threads_response = supabase.table('threads').select('*').order('id', desc=True).range(start_index, end_index).execute()
        threads = threads_response.data

        
        has_next = len(threads) == per_page

       
        pinned_ids = [3 ,2, 1]
        pinned_threads = []

        for pid in pinned_ids:
            for i, t in enumerate(threads):
                if int(t['id']) == pid:
                    pinned_threads.append(threads.pop(i))
                    break

        # もし現在のページに入っていなかった場合、個別にデータベースから取得する
        for pid in pinned_ids:
            # 既にリストから見つかっている場合はスキップ
            if any(int(pt['id']) == pid for pt in pinned_threads):
                continue
            try:
                pinned_res = supabase.table('threads').select('*').eq('id', pid).execute()
                if pinned_res.data:
                    pinned_threads.append(pinned_res.data[0])
            except Exception as pe:
                print(f"固定スレッド(ID:{pid})の個別取得エラー: {pe}")

        # 見つかった固定スレを、リストの「先頭」に1つずつ挿入していく
        for pt in pinned_threads:
            pt['is_pinned'] = True  
            pt['replies_count'] = None  
            threads.insert(0, pt)

        # 各スレッドのレス件数を取得
        for t in threads:
            # 雑談（ID=1 または is_pinned）の場合は、赤文字にしてレス数を表示しない
            if t.get('is_pinned') or int(t['id']) == 1 or int(t['id']) == 2 or int(t['id']) == 3:
                t['replies_count'] = None
                t['is_pinned'] = True
                continue

            try:
                replies_res = supabase.table('replies').select('id').eq('thread_id', int(t['id'])).execute()
                if replies_res.data:
                    t['replies_count'] = len(replies_res.data)
                else:
                    t['replies_count'] = 0
            except Exception as re:
                print(f"レス件数取得エラー (スレID {t['id']}): {re}")
                t['replies_count'] = 0

        # 管理者メッセージの取得
        try:
            admin_res = supabase.table('admin_messages').select('message').eq('id', 1).execute()
            admin_message = admin_res.data[0]['message'] if admin_res.data else "ここに管理者の一言が表示されます。"
        except Exception as ae:
            print(f"管理者メッセージ取得エラー: {ae}")
            admin_message = "管理者の一言の取得に失敗しました。"

    except Exception as e:
        print(f"スレッド一覧取得エラー: {e}")
        threads = []
        has_next = False

    user_token = request.cookies.get('user_bbs_token')
    is_new_user = False
    if not user_token:
        user_token = str(uuid.uuid4())
        is_new_user = True




    
    active_count = update_and_get_user_counts(user_token, "lobby")
   
    is_admin_user = can_manage_board()

    response = make_response(render_template(
        'index.html', 
        threads=threads, 
        admin_message=admin_message, 
        is_admin_user=is_admin_user, 
        active_count=active_count,
        current_page=page,      
        has_next=has_next       
    ))
    
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
        
    return response


@app.route('/update_admin_message', methods=['POST'])
def update_admin_message():
    password = request.form.get('admin_password')
    message = request.form.get('message')
    if password == ADMIN_PASSWORD and message:
        try:
            supabase.table('admin_messages').update({'message': message}).eq('id', 1).execute()
        except Exception as e:
            print(f"メッセージ更新エラー: {e}")
    return redirect(url_for('index'))


@app.route('/create_thread', methods=['POST'])
def create_thread():
    client_ip = get_client_ip()
    if is_banned_ip(client_ip):
        return {"error": "あなたはアクセス禁止（BAN）されています。"}, 403

    title = request.form.get('title')
    if not title:
        return {"error": "タイトルが必要です"}, 400
        
    title = filter_ng_words(title)
    
    
    title = html.escape(title)    
    
    if len(title) > 30:
        return {"error": "スレッド名は30文字以内で入力してください"}, 400
    
    is_admin = can_manage_board()
    now = time.time()
    
    if not is_admin:
        if client_ip in LAST_THREAD_TIMES and now - LAST_THREAD_TIMES[client_ip] < 180:
            remaining_time = int(180 - (now - LAST_THREAD_TIMES[client_ip]))
            minutes = remaining_time // 60
            seconds = remaining_time % 60
            return {"error": f"スレッド作成は3分に1回までです。あと {minutes}分 {seconds}秒 お待ちください。"}, 429
            
    LAST_THREAD_TIMES[client_ip] = now 
    
    try:
        response = supabase.table('threads').insert({
            'title': title,
            'ip_address': client_ip
        }).execute()
        new_thread = response.data[0] if response.data else None
    except Exception as e:
        print(f"スレッド作成エラー: {e}")
        return {"error": "データベースエラーが発生しました"}, 500
        
    return {"success": True, "thread": new_thread}


@app.route('/thread/<int:thread_id>', methods=['GET', 'POST'])
def thread_view(thread_id):
    client_ip = get_client_ip()
    if is_banned_ip(client_ip):
        return "あなたはアクセス禁止（BAN）されています。", 403

    try:
        thread_res = supabase.table('threads').select('*').eq('id', thread_id).execute()
        if not thread_res.data:
            return "スレッドが見つかりません", 404
        thread = thread_res.data[0]

        replies_res = supabase.table('replies').select('*').eq('thread_id', thread_id).order('id', desc=False).execute()

        # Supabaseからスレッド内のレス一覧を取得した後の処理
        thread['replies'] = replies_res.data
        import re 
        for r in thread['replies']:
            if r.get('date'):
                dt_utc = datetime.fromisoformat(r['date'].replace('Z', '+00:00'))
                from datetime import timedelta
                dt_jst = dt_utc + timedelta(hours=9)
                r['date'] = dt_jst.strftime('%Y-%m-%d %H:%M:%S')
            
            #  URL自動リンク化 
            if r.get('content'):
                # https:// から始まる文字列を <a> タグに変換（別タブで開く安全な設定にしています）
                r['content'] = re.sub(r'(https?://[^\s<>]+)', r'<a href="\1" target="_blank" style="color: #38bdf8; text-decoration: underline;">\1</a>', r['content'])


    except Exception as e:
        print(f"スレッド読み込みエラー: {e}")
        return "データベースエラーが発生しました", 500

    is_admin_user = can_manage_board()







    if request.method == 'POST':
        content = request.form.get('content') or ""
        
        if len(content) > 500:
            return redirect(url_for('thread_view', thread_id=thread_id))
        
        author_input = request.form.get('author') or "名無しさん"

        # 🟢 1. トリップ（#）を崩さずに、名前部分だけを20文字制限にする
        if "#" in author_input:
            name_part, pass_part = author_input.split("#", 1)
            name_part = name_part[:20]  # 名前部分を先頭20文字までにカット
            author_input = f"{name_part}#{pass_part}"
        else:
            author_input = author_input[:20]  # 通常の名前も20文字までにカット

        # 🟢 2. 偽「あぼーん」の投稿を防止する
        if author_input.strip() == "あぼーん":
            author_input = "名無しさん"

        content = filter_ng_words(content)
        author_input = filter_ng_words(author_input)
        
        is_admin = False
        if "#" in author_input:
            name_part, pass_part = author_input.split("#", 1)
            if pass_part == ADMIN_PASSWORD:
                author_input = (html.escape(name_part) or "管理人") + " ★"
                is_admin = True
                user_id = "????"
            else:
                author_input = html.escape(name_part) or "名無しさん"
                user_id = get_daily_user_id(client_ip)
        else:
            author_input = html.escape(author_input)
            user_id = get_daily_user_id(client_ip)







        content = html.escape(content)

        # 「&gt;&gt;数字」を「>>数字」に復元（アンカーハック防止対策）
        content = re.sub(r'&gt;&gt;(\d+)', r'>>\1', content)

        now = time.time()
        if not is_admin:
            if client_ip in LAST_REPLY_TIMES and now - LAST_REPLY_TIMES[client_ip] < 3:
                return redirect(url_for('thread_view', thread_id=thread_id))
            
            LAST_REPLY_TIMES[client_ip] = now









        image_url = ""
        if 'image' in request.files:
            file = request.files['image']
            if file and file.filename != '':
                try:
                    # ファイル名が被らないようにユニークな名前（UUID + 拡張子）にする
                    orig_filename = secure_filename(file.filename)
                    ext = os.path.splitext(orig_filename)[1]
                    unique_filename = f"{uuid.uuid4()}{ext}"

                    # R2へアップロード
                    s3_client.upload_fileobj(
                        file,
                        R2_BUCKET_NAME,
                        unique_filename,
                        ExtraArgs={
                            'ContentType': file.content_type
                        }
                    )
                    
                    # 公開URLを組み立てる
                    image_url = f"{R2_PUBLIC_URL.rstrip('/')}/{unique_filename}"
                except Exception as e:
                    print(f"R2 Upload Error: {e}")





        

        

        if content.strip() or image_url:
            try:
                supabase.table('replies').insert({
                    'thread_id': thread_id,
                    'author': author_input,
                    'content': content,
                    'user_id': user_id,
                    'is_admin': is_admin,
                    'image_url': image_url,
                    'ip_address': client_ip
                }).execute()
            except Exception as e:
                print(f"レス保存エラー: {e}")

        response = redirect(url_for('thread_view', thread_id=thread_id))
        if is_admin:
            response.set_cookie('is_bbs_admin', 'true', max_age=60*60*24)
        return response
    user_token = request.cookies.get('user_bbs_token')
    is_new_user = False
    if not user_token:
        user_token = str(uuid.uuid4())
        is_new_user = True

    location_key = f"thread_{thread_id}"
    active_count = update_and_get_user_counts(user_token, location_key)

    response = make_response(render_template(
        'thread.html', 
        thread=thread, 
        is_admin_user=is_admin_user, 
        active_count=active_count, 
        back_to_board="/?tab=threads"
    ))
    
    if is_new_user:
        response.set_cookie('user_bbs_token', user_token, max_age=60*60*24*365, httponly=True)
    return response



@app.route('/delete_reply/<int:reply_id>', methods=['POST'])
def delete_reply(reply_id):
    if not can_manage_board(): # ← admin と sub_admin 以外はここで弾かれる
        return "管理者権限が必要です", 403

    try:
        supabase.table('threads').delete().eq('id', thread_id).execute()
    except Exception as e:
        print(f"スレッド削除エラー: {e}")
    return redirect(url_for('index'))


@app.route('/thread/<int:thread_id>/delete/<int:reply_id>', methods=['POST'])
def delete_reply(thread_id, reply_id):
    if not check_is_admin_cookie(request):
        return "権限がありません", 403
    try:
        supabase.table('replies').update({
            'author': 'あぼーん',
            'content': 'この書き込みは管理員によって削除されました。',
            'user_id': '???',
            'is_admin': False,
            'image_url': ''
        }).eq('id', reply_id).execute()
    except Exception as e:
        print(f"レス削除エラー: {e}")
    return redirect(url_for('thread_view', thread_id=thread_id))






@app.route('/thread/<int:thread_id>/ban/<int:reply_id>', methods=['POST'])
def ban_user(thread_id, reply_id):
    if not check_is_admin_cookie(request):
        return "権限がありません", 403
    
    try:
        reply_res = supabase.table('replies').select('ip_address').eq('id', reply_id).execute()
        
        # 1. IPがあればBANリストに登録する
        if reply_res.data and reply_res.data[0].get('ip_address'):
            b_ip = reply_res.data[0]['ip_address']
            supabase.table('banned_ips').insert({'ip_address': b_ip}).execute()
            
        # 2. IPが無くても、対象のレスは必ず「あぼーん」化する
        supabase.table('replies').update({
            'author': 'あぼーん',
            'content': 'この書き込みは管理員によってBANされました。',
            'user_id': '???',
            'is_admin': False,
            'image_url': ''
        }).eq('id', reply_id).execute()
        
    except Exception as e:
        # 🚨 何かエラーが起きたら画面に直接原因を表示する！
        return f"【BAN失敗】エラーの原因: {e}", 500
            
    return redirect(url_for('thread_view', thread_id=thread_id))


@app.route('/thread/<int:thread_id>/ban_owner', methods=['POST'])
def ban_thread_owner(thread_id):
    if not check_is_admin_cookie(request):
        return "権限がありません", 403
    
    try:
        thread_res = supabase.table('threads').select('ip_address').eq('id', thread_id).execute()
        
        # 1. IPがあればBANリストに登録する
        if thread_res.data and thread_res.data[0].get('ip_address'):
            owner_ip = thread_res.data[0]['ip_address']
            supabase.table('banned_ips').insert({'ip_address': owner_ip}).execute()
                
        # 2. スレッド自体は必ず「あぼーん」化する
        supabase.table('threads').update({'title': '【このスレッドは管理員によってBANされました】'}).eq('id', thread_id).execute()
        supabase.table('replies').delete().eq('thread_id', thread_id).execute()
        supabase.table('replies').insert({
            'thread_id': thread_id,
            'author': 'あぼーん',
            'content': 'このスレッドの作成者はBANされました。',
            'user_id': '???',
            'is_admin': False,
            'image_url': ''
        }).execute()
        
    except Exception as e:
        # 🚨 何かエラーが起きたら画面に直接原因を表示する！
        return f"【スレ主BAN失敗】エラーの原因: {e}", 500
        
    return redirect(url_for('index'))









@app.route('/api/thread/<int:thread_id>/updates')
def thread_updates(thread_id):
    last_id = request.args.get('last_id', type=int, default=0)
    user_token = request.cookies.get('user_bbs_token')
    location_key = f"thread_{thread_id}"
    active_count = update_and_get_user_counts(user_token, location_key)

    try:



        new_replies_res = supabase.table('replies').select('*').eq('thread_id', thread_id).gt('id', last_id).order('id', desc=False).execute()
        new_replies = new_replies_res.data
        for r in new_replies:
            if r.get('date'):
                dt_utc = datetime.fromisoformat(r['date'].replace('Z', '+00:00'))
                from datetime import timedelta
                dt_jst = dt_utc + timedelta(hours=9)
                r['date'] = dt_jst.strftime('%Y-%m-%d %H:%M:%S')

           
            if r.get('content'):
                r['content'] = re.sub(r'(https?://[^\s<>]+)', r'<a href="\1" target="_blank" style="color: #38bdf8; text-decoration: underline;">\1</a>', r['content'])

    except Exception as e:
        print(f"自動更新APIエラー: {e}")
        new_replies = []
        
    

    is_admin_user = can_manage_board()
    return {
        "replies": new_replies, 
        "is_admin_user": is_admin_user, 
        "active_count": active_count
    }







if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
