import time
import datetime
import urllib.request
import urllib.parse
import json
import pprint
import os
import csv
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

# 環境変数から設定を読み込む
API_KEY = os.getenv("KABUS_API_KEY")
if not API_KEY:
    raise ValueError("環境変数 KABUS_API_KEY が設定されていません。.envファイルを確認してください。")

# APIエンドポイント（環境変数から読み込み、デフォルト値も設定）
KABUS_BASE_URL = os.getenv("KABUS_BASE_URL", "http://localhost:18080")
KABUSAPI_ORDERS_URL = f"{KABUS_BASE_URL}/kabusapi/orders"
KABUSAPI_SENDORDER_URL = f"{KABUS_BASE_URL}/kabusapi/sendorder"
KABUSAPI_SYMBOL_URL = f"{KABUS_BASE_URL}/kabusapi/symbol"

# ファイルパス設定（環境変数から読み込み可能）
PROCESSED_ORDERS_FILE = os.getenv("PROCESSED_ORDERS_FILE", "processed_orders.json")
ORDER_HISTORY_LOG = os.getenv("ORDER_HISTORY_LOG", "order_history.log")
PURCHASED_LOG_FILE = os.getenv("PURCHASED_LOG_FILE", "purchased_log.csv")

# すでに売り注文を出した「買い注文ID」（重複注文を防ぐため）
processed_order_ids = set()

# ▼ 追加: 材料別にさらに掛ける倍率の辞書
# ここで「上方修正(単独)」「自己株式取得系」など購入ロジックで使っている文字列を合わせて定義
# （存在しなければデフォルト 1.0 とする）
LABEL_RATIO_MAP = {
    "上方修正 + 増配": 1.02,  # 例: 上方修正 + 増配 => 1.02
    "自己株式取得 + 消却": 1.015,
    "自己株式の消却": 1.00,
    "良好": 1.03,
    "業務提携": 1.01,
    "資本提携": 1.01,
    "優待(新設/導入/再開)": 1.02,
    "自己株式取得系": 1.01,
    "上方修正(単独)": 1.01,  # 例: 上方 単独 => 1.0
    "完成": 1.03,
    "採択": 1.03,
    # もし増やしたい場合はここに追加
}


def get_additional_ratio_for_label(label: str) -> float:
    """
    材料ラベルに応じた追加倍率を返す。
    該当しない場合は 1.0 を返す。
    """
    return LABEL_RATIO_MAP.get(label, 1.0)


def load_processed_symbols_for_today():
    """
    当日発注済みの銘柄リストをファイルから読み込む。
    システム再起動後でも同日に一度発注した銘柄は再発注しないようにするため。
    """
    today_str = str(datetime.date.today())
    if not os.path.exists(PROCESSED_ORDERS_FILE):
        return set()
    try:
        with open(PROCESSED_ORDERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # data は {"YYYY-MM-DD": ["銘柄1", "銘柄2", ...], ...} の構造を想定
        symbols_today = data.get(today_str, [])
        return set(symbols_today)
    except Exception as e:
        print(f"[WARN] ファイル読み込み失敗: {PROCESSED_ORDERS_FILE}, エラー: {e}")
        return set()


def save_processed_symbol_for_today(symbol):
    """
    当日に発注済みの銘柄をファイルに追記保存する。
    """
    today_str = str(datetime.date.today())
    data = {}
    if os.path.exists(PROCESSED_ORDERS_FILE):
        try:
            with open(PROCESSED_ORDERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            print(f"[WARN] ファイル読み込み失敗(新規作成します): {PROCESSED_ORDERS_FILE}, エラー: {e}")
            data = {}

    if today_str not in data:
        data[today_str] = []
    if symbol not in data[today_str]:
        data[today_str].append(symbol)

    # JSONファイル上書き保存
    try:
        with open(PROCESSED_ORDERS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ERROR] ファイル書き込み失敗: {PROCESSED_ORDERS_FILE}, エラー: {e}")


def log_order(symbol, price, qty):
    """
    発注時のログをファイルと標準出力に出す。
    ログ内容: 日時, 銘柄, 金額, 数量
    """
    log_line = f"{datetime.datetime.now()} 発注銘柄: {symbol}, 金額: {price}, 数量: {qty}\n"
    # コンソール出力
    print("[ORDER LOG]", log_line.strip())
    # ファイル出力 (追記)
    with open(ORDER_HISTORY_LOG, "a", encoding="utf-8") as f:
        f.write(log_line)


def get_orders():
    """
    kabusapi_orders.py 相当: 現在の注文一覧を取得する
    product=0 (すべて) で注文情報を取得
    """
    params = {
        "product": 0
    }
    url = "{}?{}".format(KABUSAPI_ORDERS_URL, urllib.parse.urlencode(params))
    req = urllib.request.Request(url, method="GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-KEY", API_KEY)

    try:
        with urllib.request.urlopen(req) as res:
            content = json.loads(res.read())
            return content
    except urllib.error.HTTPError as e:
        print("HTTPError:", e)
        error_content = json.loads(e.read())
        pprint.pprint(error_content)
    except Exception as e:
        print("Exception:", e)

    return []  # 失敗時は空配列


def send_cash_sell_order(symbol, qty, price):
    """
    kabusapi_sendorder_cash_sell.py 相当:
    現物売り注文(指値)を発注する。
    フロント注文種別は 20 (指値) or 30 (逆指値) など
    """
    # 指値売り (FrontOrderType=20)

    obj = {
        "Symbol": symbol,
        "Exchange": 1,  # 1: 東証
        "SecurityType": 1,  # 1:株式
        "Side": "1",  # "1": 売
        "CashMargin": 3,  # 1: 現物 3返済
        'MarginTradeType': 1,  # 1 制度信用
        "DelivType": 2,  # 0: （特に指定しない/自動設定）2 お預かり
        "AccountType": 4,  # 4: 特定
        "Qty": qty,
        'ClosePositionOrder': 1,  # 決済順序
        "FrontOrderType": 20,  # 20:指値
        "Price": price,
        "ExpireDay": 0,  # 0:本日
    }

    json_data = json.dumps(obj).encode("utf-8")

    req = urllib.request.Request(KABUSAPI_SENDORDER_URL, json_data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-KEY", API_KEY)

    try:
        with urllib.request.urlopen(req) as res:
            print("send_cash_sell_order:", res.status, res.reason)
            content = json.loads(res.read())
            pprint.pprint(content)
    except urllib.error.HTTPError as e:
        print("HTTPError:", e)
        error_content = json.loads(e.read())
        pprint.pprint(error_content)
    except Exception as e:
        print("Exception:", e)


def round_sell_price_for_rules(price: float) -> int:
    """
    3,000円超の場合は最終1桁が0または5になるように切り下げ（5円単位）、
    5,000円超の場合は下2桁が10円単位になるように切り下げる。
    ※それ以下は特に調整せず int 変換のみ。
    """
    # まずは int へ（小数点以下切り捨て）
    p = int(price)

    if p > 5000:
        # 下2桁を10円単位に切り捨て: 例 5012 -> 5010, 5999 -> 5990
        p = (p // 10) * 10
    elif p > 3000:
        # 最終1桁を 0 or 5 に切り捨て: 例 4323 -> 4320, 4327 -> 4325
        # → 5円刻みにするので (p // 5)*5
        p = (p // 5) * 5

    return p


def get_symbol_info(symbol):
    """
    銘柄の追加情報(時価総額など)を取得する。
    例: http://localhost:18080/kabusapi/symbol/5401@1?addinfo=true
    """
    url = f"{KABUSAPI_SYMBOL_URL}/{symbol}@1"
    params = {
        'addinfo': 'true'
    }
    req_url = "{}?{}".format(url, urllib.parse.urlencode(params))
    req = urllib.request.Request(req_url, method='GET')
    req.add_header('Content-Type', 'application/json')
    req.add_header('X-API-KEY', API_KEY)

    try:
        with urllib.request.urlopen(req) as res:
            content = json.loads(res.read())
            return content
    except urllib.error.HTTPError as e:
        print("[ERROR] get_symbol_info HTTPError:", e)
        error_content = json.loads(e.read())
        pprint.pprint(error_content)
    except Exception as e:
        print("[ERROR] get_symbol_info Exception:", e)
    # 取得失敗した場合は空の辞書を返す
    return {}


def load_purchased_info_for_today(csv_filename=None):
    """
    purchased_log.csv から当日分の「symbol -> budget_label」をマッピングして返す。
    当日以外はスキップ。
    ログ形式例:
      2025-03-24 23:59:33,8601,業務提携,・・・
    カラム構造: [日時, symbol, budget_label, title_text, reason, limit_price, qty, total_cost, market_cap]
    ※予期しない形式の行は読み飛ばす。
    """
    if csv_filename is None:
        csv_filename = PURCHASED_LOG_FILE

    today_str = str(datetime.date.today())  # "YYYY-MM-DD"
    symbol_to_label = {}

    if not os.path.exists(csv_filename):
        return symbol_to_label

    try:
        with open(csv_filename, mode="r", encoding="utf-8") as f:
            reader = csv.reader(f)
            for row in reader:
                # row[0] => 日時 (例: "2025-03-24 23:59:33")
                # row[1] => symbol
                # row[2] => budget_label
                if len(row) < 3:
                    continue
                dt_str = row[0]  # "YYYY-MM-DD HH:MM:SS" 形式想定
                symbol = row[1]
                budget_label = row[2]

                # 日付だけ比較するために先頭10文字を取り出す
                # "2025-03-24"
                if len(dt_str) >= 10:
                    date_part = dt_str[:10]
                    if date_part == today_str:
                        symbol_to_label[symbol] = budget_label
    except Exception as e:
        print(f"[WARN] {csv_filename} 読み込み中にエラー: {e}")

    return symbol_to_label


def main():
    # 修正ポイント: 現在の日付を保持しておき、ループ内で変わったら当日用リストや重複チェック用IDをリセットする
    current_date = datetime.date.today()

    # 当日既に売り注文を出した銘柄をロード
    processed_symbols_today = load_processed_symbols_for_today()

    while True:
        # ▼ 追加: 当日の purchased_log.csv を読み込む (symbol -> budget_label)
        purchased_labels_today = load_purchased_info_for_today()
        # 日付が変わったかチェック
        new_date = datetime.date.today()
        if new_date != current_date:
            print(f"[INFO] 日付が変更されました。{current_date} -> {new_date} のためリセットします。")
            processed_symbols_today = load_processed_symbols_for_today()
            processed_order_ids.clear()  # 当日の買い注文ID管理セットも初期化
            current_date = new_date

        orders = get_orders()
        # orders は注文一覧 (配列)
        for order in orders:
            order_id = order.get("ID")
            side = order.get("Side")  # '1'=売, '2'=買
            state = order.get("State")  # 1～5
            cum_qty = order.get("CumQty")  # 約定数量
            order_qty = order.get("OrderQty")
            symbol = order.get("Symbol")

            # 今回探したいのは「買い注文 (side='2') が正常に全数量約定 (state=5 かつ cum_qty=order_qty)」のもの
            # かつ、まだ売り注文を出していないもの。
            if side == "2" and state == 5 and cum_qty == order_qty:
                # 既に売り発注済みのOrderIDかどうかチェック
                if order_id in processed_order_ids:
                    continue

                # 当日既に同銘柄を売り注文出していればスキップ
                # if symbol in processed_symbols_today:
                #     print(f"[INFO] 当日既に売り注文済み銘柄のためスキップ: {symbol}")
                #     continue

                # 明細から「約定価格」(複数約定がある場合は加重平均) を計算する
                details = order.get("Details", [])
                total_price = 0.0
                total_fill_qty = 0.0

                for d in details:
                    rec_type = d.get("RecType")
                    exec_price = d.get("Price")
                    exec_qty = d.get("Qty")
                    if rec_type == 8 and exec_price is not None and exec_qty is not None:
                        # RecType=8 => 約定
                        total_price += exec_price * exec_qty
                        total_fill_qty += exec_qty

                if total_fill_qty == 0:
                    # 何らかの理由で約定明細が0の場合はスキップ
                    continue

                avg_fill_price = total_price / total_fill_qty
                # --- ここで銘柄の時価総額を取得し、売り指値の割合を決定する ---
                symbol_info = get_symbol_info(symbol)
                market_cap = symbol_info.get("TotalMarketValue", 0)  # 時価総額(円)

                # ### 修正ポイント: 合計金額をもとに売り指値(%)を変える ###
                total_position = avg_fill_price * total_fill_qty

                ratio = 1.03

                # ▼ 追加: 購入時の budget_label に応じてさらに倍率を掛ける
                # （なければデフォルト1.0）
                budget_label = purchased_labels_today.get(symbol)
                if budget_label:
                    add_factor = get_additional_ratio_for_label(budget_label)
                    ratio *= add_factor

                # 価格を最終丸め
                tmp_price = avg_fill_price * ratio
                limit_price = round_sell_price_for_rules(tmp_price)
                qty_to_sell = int(cum_qty)

                print(f"[INFO] Buy約定検出: OrderID={order_id}, Symbol={symbol}, "
                      f"約定平均価格={avg_fill_price:.2f}, 時価総額={market_cap}, "
                      f"売り指値(丸め前)={tmp_price:.2f}, 最終売り指値={limit_price}, 数量={qty_to_sell}, "
                      f"材料={budget_label if budget_label else '不明'}")

                # 売り注文送信
                send_cash_sell_order(symbol, qty_to_sell, limit_price)

                # 発注ログを出力（銘柄, 金額, 数量）
                log_order(symbol, limit_price, qty_to_sell)

                # 重複発注を避けるため、処理済IDに登録
                processed_order_ids.add(order_id)

                # 当日発注済み銘柄に登録してファイルにも保存
                processed_symbols_today.add(symbol)
                save_processed_symbol_for_today(symbol)

        # 2秒待機
        print(f"{datetime.datetime.now()} 発注済みOrderID: {processed_order_ids}")
        time.sleep(2)


if __name__ == "__main__":
    main()
