from flask import Flask, render_template, request, redirect, url_for
from datetime import datetime
import json
import os

app = Flask(__name__)

# データ保存ファイル
DATA_FILE = 'bbs_data.json'

def load_data():
    """データを読み込む（ファイルがない場合は初期構造を返す）"""
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {"threads": []}

def save_data(data):
    """データをJSONファイルに保存する"""
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

@app.route('/')
def index():
    """トップページ：板（スレッド）の一覧を表示"""
    data = load_data()
    return render_template('index.html', threads=data['threads'])

@app.route('/create_thread', methods=['POST'])
def create_thread():
    """新しい板（スレッド）を作成する"""
    title = request.form.get('title')
    if not title:
        return redirect(url_for('index'))
        
    data = load_data()
    
    # 新しい板のオブジェクト
    new_thread = {
        'id': len(data['threads']) + 1,
        'title': title,
        'created_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'replies': [] # この中に書き込み（レス）が溜まっていく
    }
    
    data['threads'].append(new_thread)
    save_data(data)
    return redirect(url_for('index'))

@app.route('/thread/<int:thread_id>', methods=['GET', 'POST'])
def thread_view(thread_id):
    """特定の板の表示 ＆ その板への書き込み（レス）"""
    data = load_data()
    # 該当する板を探す
    thread = next((t for t in data['threads'] if t['id'] == thread_id), None)
    
    if not thread:
        return "スレッドが見つかりません", 404

    if request.method == 'POST':
        # 名前が空なら「名無しさん」にする
        author = request.form.get('author') or "名無しさん"
        content = request.form.get('content')
        
        if content:
            new_reply = {
                'id': len(thread['replies']) + 1,
                'author': author,
                'content': content,
                'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            }
            thread['replies'].append(new_reply)
            save_data(data)
            
        return redirect(url_for('thread_view', thread_id=thread_id))
        
    return render_template('thread.html', thread=thread)

if __name__ == '__main__':
    debug_mode = os.environ.get('FLASK_DEBUG', 'False') == 'True'
    app.run(debug=debug_mode)
