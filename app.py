from flask import Flask, render_template, request, redirect
from datetime import datetime
import json
import os

app = Flask(__name__)

# データ保存ファイル
DATA_FILE = 'posts.json'

def load_posts():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return []

def save_posts(posts):
    with open(DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(posts, f, ensure_ascii=False, indent=2)

@app.route('/')
def index():
    posts = load_posts()
    return render_template('index.html', posts=posts)

@app.route('/post', methods=['GET', 'POST'])
def post():
    if request.method == 'POST':
        posts = load_posts()
        new_post = {
            'id': len(posts) + 1,
            'title': request.form['title'],
            'content': request.form['content'],
            'author': request.form['author'],
            'date': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        posts.append(new_post)
        save_posts(posts)
        return redirect('/')
    return render_template('post.html')

@app.route('/view/<int:post_id>')
def view(post_id):
    posts = load_posts()
    post = next((p for p in posts if p['id'] == post_id), None)
    return render_template('view.html', post=post)

if __name__ == '__main__':
    # 環境変数でdebugを制御
    debug_mode = os.environ.get('FLASK_DEBUG', 'False') == 'True'
    app.run(debug=debug_mode)
