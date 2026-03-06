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

# ── 環境変数から読み込み（.envに設定する） ──
LINE_CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
LINE_CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

handler = WebhookHandler(LINE_CHANNEL_SECRET)
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ── AIへの「あなたはこういう人です」という設定 ──
SYSTEM_PROMPT = """
あなたは理学療法士（PT）かつCSCS（認定ストレングス＆コンディショニングスペシャリスト）の専門家アシスタントです。
以下のスタンスで回答してください：

【専門領域】
- リハビリテーション・運動療法・姿勢改善
- レジスタンストレーニング・ストレングス＆コンディショニング
- スポーツ傷害の予防・回復
- 痛みのメカニズムと自己管理

【回答スタイル】
- 丁寧でわかりやすい日本語で回答する
- 専門用語は使う場合に簡単な説明を添える
- 個別の診断・治療方針は行わず、「専門家への相談」を促す
- 200〜400文字程度でコンパクトにまとめる
- 最後に「何かご不明な点があればお気軽にどうぞ！」などの一言を添える

【注意事項】
- 診断・処方・具体的な薬の指示はしない
- 症状が重い場合は必ず医療機関への受診を勧める
"""


def ask_claude(user_message: str) -> str:
    """ユーザーのメッセージをClaudeに投げて回答を得る"""
    try:
        response = anthropic_client.messages.create(
            model="claude-haiku-4-5-20251001",  # 高速・低コストモデル
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        return response.content[0].text
    except Exception as e:
        print(f"Claude APIエラー: {e}")
        return "申し訳ありません、現在回答を生成できませんでした。少し時間をおいて再度お試しください。"


@app.route("/callback", methods=["POST"])
def callback():
    """LINEからのWebhookを受け取るエンドポイント"""
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    """テキストメッセージを受信したときの処理"""
    user_text = event.message.text
    print(f"受信メッセージ: {user_text}")

    # Claudeに質問して回答を生成
    reply_text = ask_claude(user_text)
    print(f"返信内容: {reply_text}")

    # LINEに返信を送る
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
