from flask import Flask, render_template, request, redirect, url_for, make_response
from datetime import datetime
import json
import html
import os
import hashlib
import uuid
import time
# Cloudinaryのライブラリを読み込み
import cloudinary
import cloudinary.uploader
# Supabaseを使うためのライブラリを読み込み
from supabase import create_client, Client

app = Flask(__name__)

# 無料プランのスリープを防ぎつつ、エラーログだけを完全に消し去る設定
@app.before_request
def response_to_uptimerobot():
    if request.method == 'HEAD':
        return make_response('', 200) # ←「生きてるよ！」と最速で返事をする

# Cloudinaryの設定
# Renderの環境変数
cloudinary.config(
    cloudinary_url = os.environ.get('cloudinary://413154997929334:1MWGTCiDlVZawKJWIm1aNpq_dhM@dpqh2ssnh'),
    secure = True
)

SUPABASE_URL = os.environ.get('SUPABASE_URL', 'https://mpzjidhuovorzvjhukmy.supabase.co')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY', 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1wemppZGh1b3Zvcnp2amh1a215Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODIwMDYzMjIsImV4cCI6MjA5NzU4MjMyMn0.Q11dCsMYX0LakWydaVD6EIKKJD2Wbv7qHV0GuAyxEeo')

# Supabaseに接続するロボットを起動
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

DATA_FILE = 'bbs_data.json'
ADMIN_PASSWORD = "setokoji114514"

# セキュリティ強化 ユーザーごとの最後の書き込み時間を記録する場所
LAST_POST_TIMES = {}
# ユーザーごとの最後の「スレ立て」「レス投稿」の時間を分けて記録
LAST_THREAD_TIMES = {}
LAST_REPLY_TIMES = {}

def auto_migrate_from_json():
    pass


# IPアドレスを元に毎日変わるIDを生成
