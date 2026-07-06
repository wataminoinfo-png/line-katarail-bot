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
    PushMessageRequest,
    TextMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]

handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

PDF_URL = "https://1drv.ms/b/c/e003a8697c0a8817/IQD-1YbBKffuQJndCRWHIMXMAcQq2qv2DvFlfW4qPyEVSpI?e=JCT9ws"

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

def get_response(key, dousa="その動作"):
    cta = R.get("CTA", "")
    cta_paid = R.get("CTA_PAID", "")
    text = R.get(key, "")
    return (
        text
        .replace("{CTA}", cta)
        .replace("{CTA_PAID}", cta_paid)
        .replace("{DOUSA}", dousa)
    )

# ── 状態定義 ──
STATE_START        = "start"
STATE_Q1           = "q1"
STATE_Q2           = "q2"
STATE_CONSULT      = "consult"       # 手動対応モード
STATE_POST_CONSULT = "post_consult"  # 有料誘導モード
STATE_MANUAL       = "manual"        # 自動解除モード（完全手動）

# 管理者のユーザーID
ADMIN_USER_ID = os.environ.get("ADMIN_USER_ID", "")

# ── 状態管理 ──
STATE_FILE   = "/tmp/user_state.json"
LAST_USER_FILE = "/tmp/last_user.json"

def load_last_user():
    try:
        with open(LAST_USER_FILE, "r", encoding="utf-8") as f:
            return json.load(f).get("id")
    except Exception:
        return None

def save_last_user(user_id):
    try:
        with open(LAST_USER_FILE, "w", encoding="utf-8") as f:
            json.dump({"id": user_id}, f)
    except Exception:
        pass

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

def detect_check_keyword(text):
    """「現在地」または「チェック」を含むか"""
    return any(kw in text for kw in ["現在地", "チェック", "check", "Check"])

def detect_yonda_keyword(text):
    """「読んだ」系キーワード（旧ユーザー対策）"""
    return any(kw in text for kw in ["読んだ", "読みました", "みた", "見た", "よんだ"])

def detect_consult_keyword(text):
    """「相談したい」系"""
    return any(kw in text for kw in ["相談", "したい", "話したい", "聞きたい"])

def detect_type_from_q1(text):
    """
    A/B/C タイプを判定。
    半角・全角・大文字小文字を許容。最初に出現した文字を採用。
    returns: ("a"/"b"/"c", dousa_str) or (None, None)
    """
    pattern = re.search(r'[AaＡａ]|[BbＢｂ]|[CcＣｃ]', text)
    if pattern is None:
        return None, None

    matched = pattern.group(0)
    if matched in ("A", "a", "Ａ", "ａ"):
        type_char = "a"
    elif matched in ("B", "b", "Ｂ", "ｂ"):
        type_char = "b"
    else:
        type_char = "c"

    # タイプ文字と直後の助詞・区切り文字を除去して動作部分を取り出す
    # 例: "Aで、服を着るとき" → "服を着るとき"
    # 例: "B。夜横になると" → "夜横になると"
    dousa = re.sub(
        r'[AaBbCcＡＢＣａｂｃ][でにはをもがとのやでも、。,\s　]*',
        '',
        text,
        count=1
    ).strip()

    if not dousa:
        dousa = "その動作"

    return type_char, dousa

TYPE_TO_RESPONSE_KEY = {
    "a": "R_TYPE_A",
    "b": "R_TYPE_B",
    "c": "R_TYPE_C",
}

# ── 会話ハンドラ ──
def handle_conversation(user_id, user_text):
    if user_id not in user_state:
        user_state[user_id] = {"step": STATE_START}
        save_state(user_state)

    state = user_state[user_id]
    step = state["step"]

    # ── 全状態共通：「読んだ」系キーワード（旧ユーザー対策） ──
    if detect_yonda_keyword(user_text):
        user_state[user_id] = {"step": STATE_START}
        save_state(user_state)
        return "ありがとうございます。「現在地」とひとこと送ってもらえれば、30秒チェックの続き（残り2問）を始めます。"

    # ── 全状態共通：「現在地」または「チェック」→ Q1へ ──
    if detect_check_keyword(user_text):
        user_state[user_id] = {"step": STATE_Q1}
        save_state(user_state)
        return Q.get("Q1", "A・B・Cのタイプと、困っている動作を教えてください。")

    # 手動対応モード（3回メッセージ後に自動で支払いリンクを送信）
    if step == STATE_CONSULT:
        count = state.get("consult_count", 0) + 1
        user_state[user_id]["consult_count"] = count
        save_state(user_state)
        if count >= 3:
            user_state[user_id] = {"step": STATE_POST_CONSULT}
            save_state(user_state)
            return get_response("CTA_PAID")
        return None  # 手動対応中はボットは返信しない

    # 完全手動モード（自動返信なし）
    if step == STATE_MANUAL:
        return None

    # 有料誘導モード（何を送っても支払いリンクを案内）
    if step == STATE_POST_CONSULT:
        if detect_consult_keyword(user_text):
            return get_response("CTA_PAID")
        return get_response("R_NUDGE_PAID")

    # Q1：A/B/Cタイプ判定 ＋ 動作抽出
    if step == STATE_Q1:
        type_char, dousa = detect_type_from_q1(user_text)
        if type_char is None:
            # 判定失敗 → 再質問（STATE_Q1のまま）
            return Q.get("Q1_RETRY", "A・B・Cのどれか一文字だけでも大丈夫です。「Aで、服を着るとき」のように返信してください。")
        # 判定成功
        user_state[user_id]["type"] = type_char
        user_state[user_id]["dousa"] = dousa
        user_state[user_id]["step"] = STATE_Q2
        save_state(user_state)
        return Q.get("Q2", "今の肩の状態を、家族や友人に説明するとしたら、どう話しますか？")

    # Q2：どんなテキストでも受け付けてタイプ別最終返信を送信
    if step == STATE_Q2:
        type_char = state.get("type", "a")
        dousa = state.get("dousa", "その動作")
        response_key = TYPE_TO_RESPONSE_KEY.get(type_char, "R_TYPE_A")
        user_state[user_id] = {"step": STATE_START}
        save_state(user_state)
        return get_response(response_key, dousa=dousa)

    # どのステップでも「相談したい」→ 手動対応モードへ
    if detect_consult_keyword(user_text):
        user_state[user_id] = {"step": STATE_CONSULT}
        save_state(user_state)
        return R.get("R_CONSULT_START", "はじめまして、PTのるです。どんなことでお悩みですか？")

    # 想定外 → fallback & STATE_STARTにリセット
    user_state[user_id] = {"step": STATE_START}
    save_state(user_state)
    return (
        "メッセージありがとうございます。\n\n"
        "30秒チェックの続きは「現在地」、\n"
        "個別のご相談は「相談したい」と送ってください。\n\n"
        "このLINEでは、回復に役立つ話を\n"
        "不定期で配信していきます。"
    )


@handler.add(FollowEvent)
def handle_follow(event):
    user_id = event.source.user_id
    user_state[user_id] = {"step": STATE_START}
    save_state(user_state)

    pdf_msg = Q.get("PDF_MESSAGE", "").replace("{PDF_URL}", PDF_URL)
    if not pdf_msg:
        pdf_msg = (
            f"カタレール公式LINEへようこそ。\n"
            f"まず無料特典のPDFをお受け取りください👇\n{PDF_URL}\n\n"
            f"PDFの最後にある「30秒チェック」までできたら、\n"
            f"このLINEに「現在地」とひとこと送ってください。"
        )

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

    # 管理者コマンド処理
    parts = user_text.strip().split()

    def find_target(parts, prefer_states):
        """IDが指定されていればそのID、なければ指定ステートのユーザーを探す"""
        if len(parts) >= 2:
            return parts[1]
        for uid, st in user_state.items():
            if uid != user_id and st.get("step") in prefer_states:
                return uid
        return None

    if parts[0] == "自動解除":
        target = find_target(parts, [STATE_CONSULT, STATE_POST_CONSULT, STATE_Q1, STATE_Q2])
        if target:
            user_state[target] = {"step": STATE_MANUAL}
            save_state(user_state)
            reply_text = f"手動対応モードに切り替えました。\n対象: {target[:8]}..."
        else:
            reply_text = "対象ユーザーが見つかりませんでした。"
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)],
                )
            )
        return

    if parts[0] == "自動再開":
        target = find_target(parts, [STATE_MANUAL])
        if target:
            user_state[target] = {"step": STATE_POST_CONSULT}
            save_state(user_state)
            reply_text = f"自動対応モードに戻しました。\n対象: {target[:8]}..."
        else:
            reply_text = "対象ユーザーが見つかりませんでした。"
        with ApiClient(configuration) as api_client:
            line_bot_api = MessagingApi(api_client)
            line_bot_api.reply_message_with_http_info(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text)],
                )
            )
        return

    reply_text = handle_conversation(user_id, user_text)
    print(f"返信: {reply_text}")

    if reply_text is None:
        return

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
