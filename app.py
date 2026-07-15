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
        # 1. 普通に全スレッドを最新順（IDの降順）で取得する
        threads_response = supabase.table('threads').select('*').order('id', desc=True).range(start_index, end_index).execute()
        threads = threads_response.data

        # 🟢 先に「次のページがあるか」を純粋な取得件数（割り込み前）で判定しておく（これでバグが直ります！）
        has_next = len(threads) == per_page

        # 2. 【修正】ページ数に関係なく、IDが1のスレを常に先頭に移動させる
        pinned_thread = None
        
        # 取得したリストの中に ID=1 のスレがあるか探して取り出す
        for i, t in enumerate(threads):
            if int(t['id']) == 1:
                pinned_thread = threads.pop(i)
                break
        
        # もし現在のページのリストに入っていなかった場合、個別にデータベースから取得する
        if not pinned_thread:
            try:
                pinned_res = supabase.table('threads').select('*').eq('id', 1).execute()
                if pinned_res.data:
                    pinned_thread = pinned_res.data[0]
            except Exception as pe:
                print(f"固定スレッドの個別取得エラー: {pe}")
        
        # ID=1 のスレが見つかったら、リストの一番最初（先頭）に挿入する
        if pinned_thread:
            pinned_thread['is_pinned'] = True  # HTML側で装飾するための目印
            threads.insert(0, pinned_thread)

        # 各スレッドのレス件数を取得
        for t in threads:
            # 雑談（ID=1 または is_pinned）の場合は、赤文字にしてレス数を表示しない
            if t.get('is_pinned') or int(t['id']) == 1:
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
    is_admin_user = check_is_admin_cookie(request)

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
