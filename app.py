import os
import json
import re
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

PDF_URL = "https://drive.google.com/file/d/1OaYExf0Bvn7HA-ZC4UE3mdSnD3Ot4au8/view?usp=sharing"

# ── コンテンツ読み込み ──
CONTENT_DIR = os.path.join(os.path.dirname(__file__), "content")

def load_content(filename):
    path = os.path.join(CONTENT_DIR, filename)
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except Exception as e:
        print(f"コンテンツ読み込みエラー: {e}")
        return {}
    result = {}
    matches = re.findall(r'\[(\w+)\]\s*---内容---\s*(.*?)\s*---ここまで---', text, re.DOTALL)
    for key, value in matches:
        result[key] = value.strip()
    return result

Q = load_content("questions.txt")
R = load_content("responses.txt")

def get_response(key):
    cta = R.get("CTA", "")
    cta_paid = R.get("CTA_PAID", "")
    text = R.get(key, "")
    return text.replace("{CTA}", cta).replace("{CTA_PAID}", cta_paid)

# ── 状態定義 ──
STATE_WAIT_READ = "wait_read"
STATE_BRANCH    = "branch"
STATE_Q1        = "q1"
STATE_Q2        = "q2"
STATE_Q3        = "q3"
STATE_START     = "start"

# ── 状態管理 ──
STATE_FILE = "/tmp/user_state.json"

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)
    except Exception as e:
        print(f"状態保存エラー: {e}")

user_state = load_state()

# ── 検出関数 ──
def detect_branch(text):
    if any(kw in text for kw in ["現在地", "チェック", "check", "Check"]):
        return "check"
    if any(kw in text for kw in ["相談", "したい", "話したい", "聞きたい"]):
        return "consult"
    return None

def detect_hospital(text):
    if any(kw in text for kw in ["行った", "行きました", "はい", "受診", "した", "Yes", "yes"]):
        return "yes"
    if any(kw in text for kw in ["まだ", "いいえ", "行ってない", "行っていない", "ない", "no", "No"]):
        return "no"
    return None

def detect_concern(text):
    if any(kw in text for kw in ["治る気", "治らない", "希望がない", "先が見えない"]):
        return "hopeless"
    if any(kw in text for kw in ["正解", "わからない", "矛盾", "違う", "どれが", "混乱"]):
        return "confused"
    if any(kw in text for kw in ["変化なし", "変わらない", "変化がない", "効かない", "治療した", "変化なかった"]):
        return "no_change"
    if any(kw in text for kw in ["その他", "other", "別", "該当しない"]):
        return "other"
    return None

RESPONSE_MAP = {
    ("yes", "hopeless"):  "R_HOSPITAL_HOPELESS",
    ("yes", "confused"):  "R_HOSPITAL_CONFUSED",
    ("yes", "no_change"): "R_HOSPITAL_NO_CHANGE",
    ("no",  "hopeless"):  "R_NO_HOSPITAL_HOPELESS",
    ("no",  "confused"):  "R_NO_HOSPITAL_CONFUSED",
    ("no",  "no_change"): "R_NO_HOSPITAL_NO_CHANGE",
}

# ── 会話ハンドラ ──
def handle_conversation(user_id, user_text):
    if user_id not in user_state:
        user_state[user_id] = {"step": STATE_START}
        save_state(user_state)

    state = user_state[user_id]
    step = state["step"]

    # 「読んだ」待ち
    if step == STATE_WAIT_READ:
        if any(kw in user_text for kw in ["読んだ", "読みました", "みた", "見た", "ok", "OK", "はい", "よんだ"]):
            user_state[user_id]["step"] = STATE_BRANCH
            save_state(user_state)
            return Q.get("AFTER_PDF", "ありがとうございます。\n「現在地チェック」または「相談したい」と送ってください")
        return "PDFを読み終わったら「読んだ」と返信してください。"

    # 分岐
    if step == STATE_BRANCH:
        branch = detect_branch(user_text)
        if branch == "check":
            user_state[user_id]["step"] = STATE_Q1
            save_state(user_state)
            return Q.get("Q1", "肩の痛みはいつ頃から始まりましたか？（自由に書いてください）")
        if branch == "consult":
            user_state[user_id] = {"step": STATE_START}
            save_state(user_state)
            return get_response("R_OTHER")
        return "「現在地チェック」または「相談したい」と送ってください"

    # Q1（自由記載 → 自動でQ2へ）
    if step == STATE_Q1:
        user_state[user_id]["q1"] = user_text
        user_state[user_id]["step"] = STATE_Q2
        save_state(user_state)
        return Q.get("Q2", "整形外科などの病院には行きましたか？\n「行った」または「まだ」と返信してください")

    # Q2
    if step == STATE_Q2:
        hospital = detect_hospital(user_text)
        if hospital:
            user_state[user_id]["hospital"] = hospital
            user_state[user_id]["step"] = STATE_Q3
            save_state(user_state)
            return Q.get("Q3", "今一番困っていることを教えてください。\n「治る気がしない」\n「何が正解かわからない」\n「治療したが変化なし」\n「その他」")
        return "「行った」または「まだ」と返信してください"

    # Q3
    if step == STATE_Q3:
        concern = detect_concern(user_text)
        hospital = state.get("hospital", "no")
        user_state[user_id] = {"step": STATE_START}
        save_state(user_state)
        if concern is None or concern == "other":
            return get_response("R_OTHER")
        key = RESPONSE_MAP.get((hospital, concern))
        return get_response(key) if key else get_response("R_OTHER")

    # 想定外 → リセット
    user_state[user_id] = {"step": STATE_START}
    save_state(user_state)
    return "カタレール公式LINEです。\nお気軽にメッセージをどうぞ。"


@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    user_state[user_id] = {"step": STATE_WAIT_READ}
    save_state(user_state)

    pdf_msg = Q.get("PDF_MESSAGE", "").replace("{PDF_URL}", PDF_URL)
    if not pdf_msg:
        pdf_msg = f"カタレール公式LINEへようこそ。\nまず無料特典のPDFをお受け取りください👇\n{PDF_URL}\n\n読み終わったら「読んだ」と返信してください。"

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=pdf_msg)],
            )
        )


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    user_text = event.message.text
    user_id = event.source.user_id
    print(f"受信: {user_id} → {user_text}")

    reply_text = handle_conversation(user_id, user_text)
    print(f"返信: {reply_text}")

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply_text)],
            )
        )


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=False)
