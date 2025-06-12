import time
import datetime
import urllib.request
import urllib.parse
import json
import pprint
import os
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

# 環境変数からAPIキーを取得
API_KEY = os.getenv("KABUSAPI_KEY")
if not API_KEY:
    raise ValueError("環境変数 KABUSAPI_KEY が設定されていません。.envファイルを確認してください。")

# 環境変数からAPIエンドポイントを取得（デフォルト値付き）
API_BASE_URL = os.getenv("KABUSAPI_BASE_URL", "http://localhost:18080/kabusapi")

# APIエンドポイントの定義
KABUSAPI_ORDERS_URL = f"{API_BASE_URL}/orders"
KABUSAPI_POSITIONS_URL = f"{API_BASE_URL}/positions"
KABUSAPI_SENDORDER_URL = f"{API_BASE_URL}/sendorder"
KABUSAPI_CANCELORDER_URL = f"{API_BASE_URL}/cancelorder"


#---------------------------------------------------
# 呼値(きざみ)に合わせて価格を切り下げる
#---------------------------------------------------
def adjust_price_to_tick(price: float) -> int:
    """
    呼値の単位にあわせて丸め(切り捨て)を行う。
    対象価格帯の下限・上限は「以下」「超～以下」で分岐させる。
    ほとんどの銘柄が１円で少し５円と１０円があり５０円は見たことない
    """
    # float で渡ってくる前提のため、念のため int 化
    p = int(price)

    if p <= 3000:             # 3,000円以下 → 1円刻み
        tick = 1
    elif p <= 5000:           # 3,000円超～5,000円以下 → 5円刻み
        tick = 5
    else:                     # 5,000円超～→ 10円刻み
        tick = 10


    # 切り捨て: 余りを除去
    return (p // tick) * tick


#---------------------------------------------------
# ポジション情報を取得する（買値/現在値などを取得するため）
#---------------------------------------------------
def get_positions():
    """
    現在保有しているポジション一覧を取得する。
    product=0 (すべて) & addinfo=true で
    - Price(建単価)
    - CurrentPrice(現在値)
    などを含む情報が返る。
    戻り値: リスト(各要素はポジション情報のdict)
    """
    params = {
        "product": 0,
        "addinfo": "true"  # 現在値など追加情報を取得
    }
    url = "{}?{}".format(KABUSAPI_POSITIONS_URL, urllib.parse.urlencode(params))
    req = urllib.request.Request(url, method="GET")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-API-KEY", API_KEY)

    try:
        with urllib.request.urlopen(req) as res:
            content = json.loads(res.read())
            return content  # list
    except urllib.error.HTTPError as e:
        print("[ERROR] get_positions HTTPError:", e)
        error_content = json.loads(e.read())
        pprint.pprint(error_content)
    except Exception as e:
        print("[ERROR] get_positions Exception:", e)
    return []


#---------------------------------------------------
# 注文一覧を取得する
#---------------------------------------------------
def get_orders():
    """
    現在の注文一覧を取得する。product=0 (すべて)。
    戻り値: リスト(各要素は注文情報のdict)
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
        print("[ERROR] get_orders HTTPError:", e)
        error_content = json.loads(e.read())
        pprint.pprint(error_content)
    except Exception as e:
        print("[ERROR] get_orders Exception:", e)
    return []


#---------------------------------------------------
# 売り注文をキャンセルする
#---------------------------------------------------
def cancel_order(order_id):
    """
    引数で指定した注文IDをキャンセルする。
    """
    print(f"[DEBUG] cancel_order: OrderID={order_id} をキャンセルします。")
    obj = {
        'OrderID': order_id
    }
    json_data = json.dumps(obj).encode('utf8')

    req = urllib.request.Request(KABUSAPI_CANCELORDER_URL, json_data, method='PUT')
    req.add_header('Content-Type', 'application/json')
    req.add_header('X-API-KEY', API_KEY)

    try:
        with urllib.request.urlopen(req) as res:
            print("cancel_order:", res.status, res.reason)
            content = json.loads(res.read())
            pprint.pprint(content)
    except urllib.error.HTTPError as e:
        print("[ERROR] cancel_order HTTPError:", e)
        content = json.loads(e.read())
        pprint.pprint(content)
    except Exception as e:
        print("[ERROR] cancel_order Exception:", e)


#---------------------------------------------------
# 指値の現物売り注文を新規発注する
#---------------------------------------------------
def send_cash_sell_order(symbol, qty, price):
    """
    現物売り注文(指値)を発注する。
    """
    print(f"[DEBUG] send_cash_sell_order: symbol={symbol}, qty={qty}, price={price}")
    #キャンセル後1秒開けないと建玉が選択されていませんエラーが出るので
    time.sleep(1)
    obj = {
        "Symbol": symbol,
        "Exchange": 1,       # 1: 東証
        "SecurityType": 1,   # 1:株式
        "Side": "1",         # "1": 売
        "CashMargin": 3,     # 1: 現物 3:返済
        "MarginTradeType": 1,# 1: 制度信用
        "DelivType": 2,      # 0: （特に指定しない/自動設定）2: お預かり
        "AccountType": 4,    # 4: 特定
        "Qty": qty,
        "ClosePositionOrder": 1, #決済順序
        "FrontOrderType": 20, # 20:指値
        "Price": price,
        "ExpireDay": 0,      # 0:本日
    }
    print(obj)

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
        print("[ERROR] send_cash_sell_order HTTPError:", e)
        error_content = json.loads(e.read())
        pprint.pprint(error_content)
    except Exception as e:
        print("[ERROR] send_cash_sell_order Exception:", e)

    # ログ出力
    log_order(symbol, price, qty)


#---------------------------------------------------
# 注文発注のログを出す
#---------------------------------------------------
def log_order(symbol, price, qty):
    """
    発注時のログをファイル(order_history_loss-cutting.log)と標準出力に出す。
    """
    log_line = f"{datetime.datetime.now()} [ORDER] Symbol={symbol}, Price={price}, Qty={qty}"
    print("[ORDER LOG]", log_line)
    with open("order_history_loss-cutting.log", "a", encoding="utf-8") as f:
        f.write(log_line + "\n")


#---------------------------------------------------
# メイン処理
#---------------------------------------------------
def main():
    print("[INFO] ----- Start Stop-Loss Monitor -----")

    while True:
        try:
            # 1) 保有ポジションを取得して、銘柄ごとに「買値」「現在値」などを管理
            positions_data = get_positions()

            # key: symbol, value: dict( buy_price=..., current_price=..., side=... )
            position_dict = {}
            for pos in positions_data:
                # pos["Side"] = "1"(売) or "2"(買)
                # 現物買い/信用買いなどが混在する可能性があるので、必要に応じてフィルタ
                symbol = pos.get("Symbol")
                side = pos.get("Side")       # '1'=売建玉, '2'=買建玉
                buy_price = pos.get("Price") # 建単価(=建てた価格)
                curr_price = pos.get("CurrentPrice") # 現在値 (addinfo=true指定時)
                if not symbol or buy_price is None or curr_price is None:
                    continue

                # ここでは単純に「最後のposを保存」か、または「もし同じ銘柄が複数あれば加重平均を計算」など実装をカスタマイズ。
                # 例: ここでは1つだけ想定で上書きします。
                position_dict[symbol] = {
                    "buy_price": float(buy_price),
                    "current_price": float(curr_price),
                    "side": side
                }

            # 2) 注文一覧を取得して、約定前の売り注文を探す
            orders = get_orders()

            # 「State != 5 (終了)」かつ「Side='1'(売)」の注文を対象とする
            # さらに、全部約定していない( cumQty < orderQty )場合も未完了の可能性あり
            for od in orders:
                order_id = od.get("ID")
                side = od.get("Side")               # '1'=売, '2'=買
                state = od.get("State")             # 1～5
                cum_qty = od.get("CumQty", 0.0)     # 約定数量
                order_qty = od.get("OrderQty", 0.0) # 発注数量
                symbol = od.get("Symbol")
                symbol_name = od.get("SymbolName")

                # 売り注文 && まだ全量約定してない && state != 5 のものを対象にする
                # (state==5 でも部分約定済みかもしれないが、一応発注全体が終了扱いならスキップ)
                if side == "1":
                    if state != 5 or (cum_qty < order_qty):
                        # 対応する買いポジション情報がなければスキップ
                        if symbol not in position_dict:
                            continue

                        pos_info = position_dict[symbol]
                        buy_price = pos_info["buy_price"]
                        current_price = pos_info["current_price"]

                        # 「現在値」が「買った値段の3%下回る」 => current_price <= buy_price * 0.99
                        # になったらストップロス発動: キャンセル -> 新しい売り注文
                        #3％損切
                        threshold = buy_price * 0.97

                        if current_price <= threshold:
                            print(f"[INFO] 損切発動条件達成: "
                                  f"Symbol={symbol}({symbol_name}), "
                                  f"買値={buy_price}, 現値={current_price}, "
                                  f"損切ライン={threshold:.2f}, OrderID={order_id}")

                            # (1) まず既存の売り注文をキャンセル
                            cancel_order(order_id)

                            # (2) 新しい売り指値注文 (例: 買値の 0.95倍 など)
                            new_sell_price = buy_price * 0.95
                            # ★呼値で切り捨てる
                            new_sell_price = adjust_price_to_tick(new_sell_price)

                            # 未約定数量(= order_qty - cum_qty)を再注文する想定
                            remain_qty = order_qty - cum_qty
                            remain_qty = int(remain_qty)

                            if remain_qty > 0:
                                print(f"[INFO] 新規ストップロス売注文発注: Price={new_sell_price}, Qty={remain_qty}")
                                send_cash_sell_order(symbol, remain_qty, new_sell_price)
                            else:
                                print(f"[WARN] remain_qtyが0以下のため、新規発注をスキップ: remain_qty={remain_qty}")
                        else:
                            print(f"[DEBUG] 損切発動なし: Symbol={symbol}, "
                                  f"現値={current_price} > 損切ライン={threshold:.2f}")
                    else:
                        print(f"[DEBUG] 売注文だが完了(State=5)または全量約定済み => スキップ: OrderID={order_id}")
                else:
                    # 買い注文ならスキップ
                    continue

            # 監視間隔(適宜調整)
            time.sleep(2)

        except Exception as e:
            print("[ERROR] main loop Exception:", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
