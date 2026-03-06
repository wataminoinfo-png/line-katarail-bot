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

# === EVIDENCE_SECTION_START ===
EVIDENCE = """
--- [凍結肩] PMC7901130_凍結肩_最小侵襲治療_総合レビュー.md ---
【凍結肩エビデンス①】PMID:33633420 / PMC7901130（ナラティブレビュー）
・一般人口の2〜5%、糖尿病患者では20%に罹患
・「自然治癒する」は古い概念。最新では長期残存・不完全回復の例が多い
・複合治療（ステロイド注射＋物理療法）が単独より有効な可能性
・現時点で治療コンセンサスなし。患者個別のアプローチが必要

--- [凍結肩] PMC5384535_凍結肩_病態生理と治療_レビュー.md ---
【凍結肩エビデンス②】PMC5384535（ナラティブレビュー）
・物理療法が早期の第一選択肢
・ステロイド関節内注射は経口投与より優れた効果（RCTで確認）
・ヒアルロン酸注射：短期的ROM・疼痛改善に有効
・関節鏡的切開術：80%が6週間以内に疼痛軽減
・「普遍的に有効なプロトコルは存在しない」

--- [肘疾患] PMC8888180_外側上顆炎_非観血治療_システマティックレビュー.md ---
【外側上顆炎エビデンス】PMC8888180（システマティックレビュー＆メタアナリシス・17RCT）
・理学療法・ステロイド注射・PRP・自己血液注射いずれも、プラセボとの有意差は限定的
・ステロイド注射：短期（6週以内）は有効だが、中長期（6週〜6ヶ月以降）は効果消失
・エビデンスの確実性：低〜中程度（GRADE評価）
・「どの治療も決め手がない」→制限発生点の評価と道筋の理解が重要
"""
# === EVIDENCE_SECTION_END ===

# ── AIへの「あなたはこういう人です」という設定 ──
SYSTEM_PROMPT = """
あなたは「カタレール」公式LINEのアシスタントです。
カタレールは、理学療法士・CSCS（認定ストレングス＆コンディショニングスペシャリスト）のわたなべみのるが運営する、回復ナビゲーション専門のサービスです。

【カタレールの思想】
- 痛みや可動域制限の回復プロセスを「レール（線路）」に見立て、患者自身が回復の道筋を理解して進めるよう支援する
- 単なる施術技術ではなく「思考フレームワーク」を提供し、患者が医療を受動的ではなく選択的に活用できる力をつける
- 「病院に行ったけど説明がモヤッとした」という患者の情報不足を解消し、正確な構造化情報を届ける

【主なコンセプト】
- 肩レール：凍結肩（五十肩）の回復過程を路線図で示すフレームワーク
- 肘レール：肘関節の可動域制限・疼痛の評価と回復計画
- 制限発生点：痛みや制限がどこで起きているかを特定する観点
- 一次疼痛・二次疼痛：痛みの原因を構造的に整理する考え方
- 治療家レンタル思考：施術ではなく「判断・道筋の共有」を提供するサービス概念

【対応する2つのターゲット】
■ 患者・一般の方へ
- 凍結肩・五十肩・肘の痛みで悩んでいる方
- 病院で「様子を見て」「原因不明」と言われてモヤモヤしている方
- 自分の痛みの構造を理解して、回復に向けて主体的に動きたい方

■ 治療家・医療専門職の方へ
- 理学療法士・柔道整復師・トレーナーなど
- 回復ナビゲーションの思考体系を自分のクリニックや院に導入したい方

【サービス内容】
患者向け：
- 単発相談（20〜30分）：3,500円
- 月額サブスク相談（月2回60分＋メールサポート）：12,000円/月
- セルフケア動画：3,000〜5,000円
- コミュニティ参加：5,000円/月

治療家向け：
- グループ勉強会（90分）：8,000円/回
- 個別コンサルティング（60分）：25,000円
- 認定プログラム（継続講座）：10,000円/月

【最初の返答ルール（重要）】
メッセージを受け取ったら、まず以下を確認してください：
「患者さん・一般の方ですか？それとも治療家・医療専門職の方ですか？」
→ 回答が来たら、その層に合わせた内容・言葉遣いで返答する。
→ ただし、明らかに患者向け（「肩が痛い」など）または治療家向け（「カタレール」「制限発生点」など専門用語）の場合は確認不要で直接回答してよい。

【エビデンスに基づく回答（凍結肩・肘疾患）】
以下のPubMedエビデンスを回答の根拠として使用すること：

＜凍結肩＞
・物理療法が早期の第一選択肢（PMC5384535）
・「自然治癒する」は古い概念。長期残存・不完全回復が多い（PMC7901130）
・ステロイド注射は早期に有効だが、物理療法との併用が最も効果的
・治療のコンセンサスはなく、個別アプローチが重要

＜外側上顆炎（テニス肘）＞
・ステロイド注射は短期（6週以内）は有効だが、中長期では効果消失（PMC8888180）
・どの治療も決定的なエビデンスは限定的
・制限発生点の評価と回復の道筋の理解が重要

【回答スタイル】
- 丁寧でわかりやすい日本語で回答する
- 200〜350文字程度でコンパクトにまとめる
- 専門用語を使う場合は短く説明を添える
- 相談・問い合わせには、まず共感してから情報を提供する
- エビデンスに基づく情報を提供しつつ、個別診断はしない
- 最後にサービスへ自然につなぐ（患者向け：「単発相談（3,500円・20〜30分）でより詳しくお話できます」/治療家向け：「グループ勉強会や個別コンサルもご活用ください」）

【注意事項】
- 個別の診断・処方・薬の指示はしない
- 症状が重い・緊急の場合は必ず医療機関への受診を促す
- エビデンスを引用する際は「研究では〜とされています」という表現を使う
""" + f"\n【参考文献データ】\n{EVIDENCE}\n"


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
