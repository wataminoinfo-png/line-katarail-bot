import os
import anthropic
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
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# === EVIDENCE_SECTION_START ===
EVIDENCE = """
--- [凍結肩] PMC7901130 ---
・一般人口の2〜5%、糖尿病患者では20%に罹患
・「自然治癒する」は古い概念。長期残存・不完全回復の例が多い
・複合治療（ステロイド注射＋物理療法）が単独より有効な可能性

--- [凍結肩] PMC5384535 ---
・物理療法が早期の第一選択肢
・ステロイド関節内注射は経口投与より優れた効果（RCTで確認）
・「普遍的に有効なプロトコルは存在しない」

--- [肘疾患] PMC8888180 ---
・ステロイド注射：短期（6週以内）は有効だが、中長期では効果消失
・理学療法・PRP・自己血液注射いずれも、プラセボとの有意差は限定的
"""
# === EVIDENCE_SECTION_END ===

SYSTEM_PROMPT = """
あなたは「回復ナビゲーター」公式LINEのアシスタントです。
運営者は理学療法士・CSCSのわたなべみのるです。

【基本思想】
- 痛みの回復過程を「レール（路線図）」に見立て、患者が今どこにいるかを整理する
- 医療と患者の間の「翻訳者」として、構造的に分かりやすく伝える
- 治療を否定せず、医師を否定せず、補完する立ち位置を崩さない
- 診断・処方はしない。「研究では〜とされています」という表現でエビデンスを引用する

【回答スタイル】
- 200〜350文字でコンパクトに
- まず共感、次に構造的な説明、最後にサービス誘導（1文）
- 患者向け誘導：「単発相談（3,500円・20〜30分）で道筋をお話しできます」
- 治療家向け誘導：「勉強会や個別コンサルもご活用ください」

【エビデンス】
""" + EVIDENCE


# ── 状態管理（ファイル永続化） ──
import json

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

STATE_START = "start"
STATE_ASK_BODY_PART = "ask_body_part"
STATE_ASK_DURATION = "ask_duration"
STATE_ASK_DOCTOR = "ask_doctor"
STATE_ASK_CLINICIAN = "ask_clinician"


def detect_type(text):
    clinician_kw = ["治療家", "理学療法士", "柔道整復師", "トレーナー", "セラピスト",
                    "PT", "制限発生点", "操作体系", "臨床", "施術", "医療者", "医療職", "勉強会"]
    patient_kw = ["患者", "痛い", "痛み", "肩", "肘", "五十肩", "凍結肩", "テニス肘",
                  "リハビリ", "病院", "しびれ", "動かない", "固い", "硬い"]
    for kw in clinician_kw:
        if kw in text:
            return "clinician"
    if text.strip() in ["2", "②", "２"]:
        return "clinician"
    if text.strip() in ["1", "①", "１"]:
        return "patient"
    for kw in patient_kw:
        if kw in text:
            return "patient"
    return None


def detect_body_part(text):
    if any(kw in text for kw in ["肩", "五十肩", "凍結肩", "1", "①", "１"]):
        return "shoulder"
    if any(kw in text for kw in ["肘", "テニス肘", "2", "②", "２", "内側", "外側"]):
        return "elbow"
    return None


def detect_duration(text):
    if any(kw in text for kw in ["1", "①", "１", "1ヶ月", "1か月", "先月", "最近", "急に", "急"]):
        return "acute"
    if any(kw in text for kw in ["2", "②", "２", "2ヶ月", "3ヶ月", "4ヶ月", "5ヶ月", "6ヶ月", "半年"]):
        return "subacute"
    if any(kw in text for kw in ["3", "③", "３", "7ヶ月", "8ヶ月", "9ヶ月", "1年", "2年", "長い", "ずっと", "何年"]):
        return "chronic"
    return None


def detect_doctor(text):
    if any(kw in text for kw in ["1", "①", "１", "行った", "行きました", "はい", "受診", "診て"]):
        return True
    if any(kw in text for kw in ["2", "②", "２", "まだ", "いいえ", "行ってない", "行っていない", "ない"]):
        return False
    return None


def detect_clinician_interest(text):
    if any(kw in text for kw in ["1", "①", "１", "患者", "説明", "ツール"]):
        return "patient_education"
    if any(kw in text for kw in ["2", "②", "２", "臨床", "思考", "学び", "体系", "技術"]):
        return "clinical_thinking"
    return None


def ask_claude_direct(prompt):
    try:
        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception as e:
        print(f"Claude APIエラー: {e}")
        return "申し訳ありません、現在回答を生成できませんでした。少し時間をおいて再度お試しください。"


def generate_patient_response(state):
    body_part = "肩（凍結肩・五十肩）" if state["body_part"] == "shoulder" else "肘（テニス肘・上顆炎など）"
    duration_map = {
        "acute": "1ヶ月以内（急性期）",
        "subacute": "1〜6ヶ月（亜急性期・拘縮期）",
        "chronic": "6ヶ月以上（慢性期・回復期）"
    }
    duration = duration_map[state["duration"]]
    doctor = "受診済み" if state["doctor"] else "未受診"

    prompt = f"""
患者情報：
- 部位：{body_part}
- 期間：{duration}
- 受診状況：{doctor}

この患者さんに対して回復ナビゲーターとして回答してください。
①現在の病期・状態を構造的に説明
②この時期に大切なこと・注意点
③次のステップ
④最後に単発相談への自然な誘導（1文のみ）

200〜350文字でまとめてください。
"""
    return ask_claude_direct(prompt)


def generate_clinician_response(state):
    interest_map = {
        "patient_education": "患者への説明ツール・カタレールの活用",
        "clinical_thinking": "制限発生点・操作体系の臨床統合"
    }
    interest = interest_map[state["interest"]]

    prompt = f"""
治療家からの問い合わせ：
- 関心：{interest}

回復ナビゲーターとして回答してください。
①その関心に対する具体的な価値・活用方法
②次のステップとして何ができるか
③勉強会や個別コンサルへの自然な誘導（1文のみ）

200〜350文字でまとめてください。
"""
    return ask_claude_direct(prompt)


def handle_conversation(user_id, user_text):
    if user_id not in user_state:
        user_state[user_id] = {"step": STATE_START}
        save_state(user_state)

    state = user_state[user_id]
    step = state["step"]

    # START
    if step == STATE_START:
        user_type = detect_type(user_text)

        if user_type == "patient":
            state["type"] = "patient"
            body_part = detect_body_part(user_text)
            if body_part:
                state["body_part"] = body_part
                state["step"] = STATE_ASK_DURATION
                save_state(user_state)
                return "痛みはいつ頃から始まりましたか？\n①1ヶ月以内\n②1〜6ヶ月\n③6ヶ月以上"
            state["step"] = STATE_ASK_BODY_PART
            save_state(user_state)
            return "肩と肘、どちらのお悩みですか？\n①肩（五十肩・凍結肩など）\n②肘（テニス肘・内側・外側の痛みなど）"

        elif user_type == "clinician":
            state["type"] = "clinician"
            state["step"] = STATE_ASK_CLINICIAN
            save_state(user_state)
            return "ありがとうございます。どちらのご関心ですか？\n①患者さんへの説明ツールとして使いたい\n②臨床の思考体系として学びたい"

        else:
            return "はじめまして！カタレール公式LINEです。\nまず教えてください。\n①患者さん・一般の方\n②治療家・医療専門職の方"

    # 患者：部位確認
    elif step == STATE_ASK_BODY_PART:
        body_part = detect_body_part(user_text)
        if body_part:
            state["body_part"] = body_part
            state["step"] = STATE_ASK_DURATION
            save_state(user_state)
            return "痛みはいつ頃から始まりましたか？\n①1ヶ月以内\n②1〜6ヶ月\n③6ヶ月以上"
        return "肩と肘、どちらのお悩みですか？\n①肩（五十肩・凍結肩など）\n②肘（テニス肘・内側・外側の痛みなど）"

    # 患者：期間確認
    elif step == STATE_ASK_DURATION:
        duration = detect_duration(user_text)
        if duration:
            state["duration"] = duration
            state["step"] = STATE_ASK_DOCTOR
            save_state(user_state)
            return "病院には行きましたか？\n①行った\n②まだ行っていない"
        return "痛みはいつ頃から始まりましたか？\n①1ヶ月以内\n②1〜6ヶ月\n③6ヶ月以上"

    # 患者：受診確認 → 最終回答
    elif step == STATE_ASK_DOCTOR:
        doctor = detect_doctor(user_text)
        if doctor is not None:
            state["doctor"] = doctor
            reply = generate_patient_response(state)
            user_state[user_id] = {"step": STATE_START}
            save_state(user_state)
            return reply
        return "病院には行きましたか？\n①行った\n②まだ行っていない"

    # 治療家：関心確認 → 最終回答
    elif step == STATE_ASK_CLINICIAN:
        interest = detect_clinician_interest(user_text)
        if interest:
            state["interest"] = interest
            reply = generate_clinician_response(state)
            user_state[user_id] = {"step": STATE_START}
            save_state(user_state)
            return reply
        return "どちらのご関心ですか？\n①患者さんへの説明ツールとして使いたい\n②臨床の思考体系として学びたい"

    # 想定外 → リセット
    user_state[user_id] = {"step": STATE_START}
    save_state(user_state)
    return "はじめまして！カタレール公式LINEです。\nまず教えてください。\n①患者さん・一般の方\n②治療家・医療専門職の方"


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
