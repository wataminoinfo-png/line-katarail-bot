import os
import json
import re
from flask import Flask, request, abort, send_from_directory
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

PDF_URL = "https://line-katarail-bot.onrender.com/tokuten.pdf"
PDF_FILENAME = "katarail_tokuten_v5.pdf"

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
    """「読んだ」系キーワード（旧ユーザー対策）
    ※「みた」「見た」は自由回答文に頻出するため含めない（誤発火防止）"""
    return any(kw in text for kw in ["読んだ", "読みました", "よんだ"])

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

    # タイプ文字の前後から動作部分を取り出す
    # 例: "Aで、服を着るとき" → "服を着るとき"（前置き型）
    # 例: "夜、横になると痛い。Aです" → "夜、横になると痛い"（後置き型）
    before = text[:pattern.start()]
    after = text[pattern.end():]
    after = re.sub(r'^(?:です|だ)?[でにはをもがとのや、。,．\s　]*', '', after)
    before = before.strip('、。,．（(「 　')
    dousa = (before + after).strip().strip('、。,．()（）「」 　')

    if not dousa:
        dousa = "その動作"

    return type_char, dousa

# ── 会話ハンドラ ──
def handle_conversation(user_id, user_text):
    if user_id not in user_state:
        user_state[user_id] = {"step": STATE_START}
        save_state(user_state)

    state = user_state[user_id]
    step = state["step"]

    # ── 完全手動モードは何より優先で沈黙（手動対応の横取り防止） ──
    if step == STATE_MANUAL:
        return None

    # ── 「読んだ」系（旧ユーザー対策）：初期状態のときだけ反応 ──
    if step == STATE_START and detect_yonda_keyword(user_text):
        return "ありがとうございます。「現在地」とひとこと送ってもらえれば、30秒チェックの続き（残り2問）を始めます。"

    # ── 「現在地」「チェック」→ Q1へ（初期状態・有料誘導モードからのみ。
    #    相談モード・Q1/Q2回答中の自由文に含まれる語での誤発火を防ぐ） ──
    if step in (STATE_START, STATE_POST_CONSULT) and detect_check_keyword(user_text):
        user_state[user_id] = {"step": STATE_Q1}
        save_state(user_state)
        return Q.get("Q1", "A・B・Cのタイプと、困っている動作を教えてください。")

    # ── チェック結果の直接送信（「Aで、服を着るとき」形式）→ Q1を飛ばしてQ2へ ──
    #    文頭がA/B/C＋区切りのときだけ反応（誤発火防止）
    if step == STATE_START and re.match(r'^\s*[AaＡａBbＢｂCcＣｃ]([でにはがとも、。,\s　]|$)', user_text):
        type_char, dousa = detect_type_from_q1(user_text)
        user_state[user_id] = {"step": STATE_Q2, "type": type_char, "dousa": dousa}
        save_state(user_state)
        return Q.get("Q2", "今の肩の状態を、家族や友人に説明するとしたら、どう話しますか？")

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

    # 有料誘導モード（何を送っても支払いリンクを案内）
    if step == STATE_POST_CONSULT:
        if detect_consult_keyword(user_text):
            return get_response("CTA_PAID")
        return get_response("R_NUDGE_PAID")

    # Q1：どんな回答でも受け付ける（A/B/Cは取れたら記録するだけ。強制しない）
    if step == STATE_Q1:
        type_char, dousa = detect_type_from_q1(user_text)
        user_state[user_id]["type"] = type_char or "free"
        user_state[user_id]["dousa"] = dousa if type_char else user_text
        user_state[user_id]["step"] = STATE_Q2
        save_state(user_state)
        return Q.get("Q2", "今の肩の状態を、家族や友人に説明するとしたら、どう話しますか？")

    # Q2：どんなテキストでも受け付けて共通の完了メッセージを送信
    # （「一文に整理して返す」仕上げは、みのるが会話履歴を読んで手動で行う）
    if step == STATE_Q2:
        user_state[user_id] = {"step": STATE_START}
        save_state(user_state)
        return get_response("R_CHECK_DONE")

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
        "チェックの結果は「Aで、服を着るとき」のように、\n"
        "個別のご相談は「相談したい」と送ってください。\n\n"
        "肩に役立つ話を、ときどきこのLINEで配信します。"
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


@app.route("/tokuten.pdf")
def tokuten_pdf():
    return send_from_directory(
        os.path.dirname(os.path.abspath(__file__)),
        PDF_FILENAME,
        mimetype="application/pdf",
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
