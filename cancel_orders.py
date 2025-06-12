import os
import time
import datetime
import urllib.request
import urllib.parse
import json
from typing import Any, Dict, List
import logging
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

# -----------------------------
# 環境変数から設定を読み込む
# -----------------------------
API_KEY = os.getenv("KABU_API_KEY")
if not API_KEY:
    raise ValueError("環境変数 KABU_API_KEY が設定されていません。.envファイルを確認してください。")

# KabuステーションのIPとポート（デフォルト値を設定）
BASE_URL = os.getenv("KABU_BASE_URL", "http://localhost:18080")

# 取り消しまでの待機時間（秒）
CANCEL_THRESHOLD_SECONDS = int(os.getenv("CANCEL_THRESHOLD_SECONDS", "30"))

# -----------------------------
# ログの設定
# -----------------------------
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ログフォーマット（APIキーなどの機密情報を含まないように注意）
formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s')

# ファイル出力用ハンドラー
file_handler = logging.FileHandler("trade_cancellations.log", "a", encoding="utf-8")
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

# コンソール出力用ハンドラー
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)


def fetch_orders() -> List[Dict[str, Any]]:
    """
    kabusapi/orders から注文一覧を取得し、パースして返す
    """
    url = f"{BASE_URL}/kabusapi/orders"
    # product=0 で「すべて」
    params = {"product": 0}
    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(full_url, method="GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-KEY", API_KEY)

    try:
        with urllib.request.urlopen(req) as res:
            content = res.read()
            orders_json = json.loads(content)
            return orders_json
    except urllib.error.HTTPError as e:
        # APIキーをログに含めないよう注意
        err_content = e.read().decode("utf-8", errors="replace")
        # 機密情報を含む可能性があるエラー内容は最小限に
        logger.error(f"HTTPError in fetch_orders: status={e.code}")
    except Exception as e:
        logger.exception(f"Exception in fetch_orders: {type(e).__name__}")

    # 失敗した場合は空リストを返す
    return []


def cancel_order(order_id: str):
    """
    kabusapi/cancelorder を呼び出して注文を取消する
    """
    url = f"{BASE_URL}/kabusapi/cancelorder"

    # PUTメソッドで取り消したいオーダーIDをJSONボディで送信
    obj = {"OrderID": order_id}
    json_data = json.dumps(obj).encode("utf-8")

    req = urllib.request.Request(url, json_data, method="PUT")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-KEY", API_KEY)

    try:
        with urllib.request.urlopen(req) as res:
            content = res.read().decode("utf-8", errors="replace")
            logger.info(
                f"注文取り消し成功: OrderID={order_id}, status={res.status}"
            )
    except urllib.error.HTTPError as e:
        # APIキーやその他の機密情報をログに含めない
        logger.error(
            f"HTTPError in cancel_order: OrderID={order_id}, status={e.code}"
        )
    except Exception as e:
        logger.exception(f"Exception in cancel_order: OrderID={order_id}, {type(e).__name__}")


def main_loop():
    """
    5秒ごとに永遠に注文一覧をチェックし、
    ・買い注文(Side='2')
    ・約定数量(CumQty)=0のまま
    ・指定時間以上経過
    のものを取消する
    """
    logger.info(f"自動キャンセル処理を開始します。閾値: {CANCEL_THRESHOLD_SECONDS}秒")

    while True:
        try:
            current_time = datetime.datetime.now()
            logger.info(f"---- チェック開始 ({current_time}) ----")

            orders = fetch_orders()
            # Python 3.7 以上なら fromisoformat が使用可能
            # kabuステーションの時刻例: "2025-03-10T09:00:30.17592+09:00"
            now = datetime.datetime.now(datetime.timezone.utc).astimezone()

            for order in orders:
                # 注文サイドが '2' = 買い注文
                if order.get("Side") == "2":
                    # まだ約定していない (CumQty == 0) かつ 注文が終了状態(5)でない
                    # ※ State=5 は既に終了状態(約定済 / キャンセル済 / エラー等)
                    cum_qty = order.get("CumQty", 0.0)
                    order_state = order.get("State", 0)

                    if cum_qty == 0 and order_state != 5:
                        recv_time_str = order.get("RecvTime", None)
                        if not recv_time_str:
                            continue  # 受付時刻がなければスキップ

                        try:
                            recv_time = datetime.datetime.fromisoformat(recv_time_str)
                        except ValueError:
                            # fromisoformatでパース失敗したらスキップ
                            continue

                        diff_sec = (now - recv_time).total_seconds()
                        if diff_sec >= CANCEL_THRESHOLD_SECONDS:
                            order_id = order.get("ID")
                            logger.info(
                                f"{CANCEL_THRESHOLD_SECONDS}秒以上約定なしの買い注文をキャンセル: "
                                f"OrderID={order_id}, 経過時間={int(diff_sec)}秒"
                            )
                            cancel_order(order_id)

        except Exception as e:
            logger.exception(f"Exception in main_loop: {type(e).__name__}")

        # 5秒待機
        time.sleep(5)


if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        logger.info("プログラムを終了します。")
    except Exception as e:
        logger.critical(f"予期しないエラーが発生しました: {type(e).__name__}")
        raise
