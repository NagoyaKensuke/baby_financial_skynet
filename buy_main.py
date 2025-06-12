import multiprocessing
import time
import datetime
import urllib.request
import urllib.error
import json
import csv
import urllib.parse
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    NoSuchElementException,
)
from webdriver_manager.chrome import ChromeDriverManager
import os
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

# Kabus APIの認証キーを環境変数から取得
API_KEY = os.getenv("KABUSAPI_KEY", "test")  # KABUSAPI_KEYが設定されていない場合は"test"を使用

# Kabus APIの発注URLと板情報URL
# テスト環境用と本番用を分けたい場合は変数を分けるか、main_prod() で書き換えて使ってください
KABUSAPI_SENDORDER_URL = "http://localhost:18080/kabusapi/sendorder"
KABUSAPI_BOARD_URL = "http://localhost:18080/kabusapi/board"
# ★★★ 修正追加: 銘柄情報を取得して時価総額を確認するためのURL
KABUSAPI_SYMBOL_URL = "http://localhost:18080/kabusapi/symbol"

# 重複発注を防ぐために、発注済みの銘柄コードを記録する
ordered_symbols = set()

##########################
# 上限金額を材料により定数化（変更しやすいように）
##########################

# 上方修正 + 増配 は 200万円
BUDGET_UPPER_REVISION_PLUS_ZOHAI = 1_500_000

# 良好 は 150万円
BUDGET_RYOKO = 1_500_000

# 業務提携 は 100万円
BUDGET_BUSINESS_ALLIANCE = 1_000_000

# 資本提携 は 100万円（新規追加）
BUDGET_CAPITAL_ALLIANCE = 1_000_000

# 自己株式関連 単独: 100万円 (従来のもの)
BUDGET_SELF_STOCK = 500_000

# 優待(新設/導入/再開) は 100万円
BUDGET_NEW_YUTAI = 1_000_000

# 上方 単独: 100万円
BUDGET_UPPER_REVISION = 1_000_000

# 完成 は 100万円
BUDGET_KANSEI = 1_000_000

# 採択 100万円
BUDGET_SAITAKU = 1_000_000

# 株主優待 + 拡充: 50万円
BUDGET_YUTAI_KAKUCHU = 250_000

# 増配単独, 記念配当 等: 50万円
# BUDGET_ZOHAI_OR_KINEN = 250_000


# 自己株式の消却 単独 => 125万円
BUDGET_SELF_STOCK_CANCEL = 1_250_000

# 消却 + (自己株式取得, 自己株式の取得, 自己株式の買, 自己投資口) => 150万円
BUDGET_SELF_STOCK_ACQUIRE_AND_CANCEL = 1_500_000


def get_symbol_info(symbol: str, exchange: int = 1) -> dict:
    """
    Kabus APIの銘柄情報を取得し、時価総額など詳細情報を返す関数。
      - 'TotalMarketValue' が時価総額(円)
    取得に失敗した場合は空の dict を返す。
    """
    # 例: http://localhost:18080/kabusapi/symbol/5401@1?addinfo=true
    url = f"{KABUSAPI_SYMBOL_URL}/{symbol}@{exchange}"
    params = {
        "addinfo": "true"
    }
    req_url = f"{url}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(req_url, method="GET")
    req.add_header("X-API-KEY", API_KEY)
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req) as res:
            data = json.loads(res.read())
            return data
    except urllib.error.HTTPError as e:
        print(f"[get_symbol_info] HTTPError: {e}")
        try:
            err_data = json.loads(e.read())
            print(f"[get_symbol_info] Error detail: {err_data}")
        except:
            pass
    except Exception as e:
        print(f"[get_symbol_info] Exception: {e}")

    return {}


def get_current_price(symbol: str, exchange: int = 1) -> float:
    """
    kabusapi の板情報から現在値を取得する関数。
    取得できない場合は None を返す。

    Args:
        symbol (str): 銘柄コード (例: "4464")
        exchange (int): 市場コード。1 は東証を意味する。

    Returns:
        float: 現在値。取得できなければ None。
    """
    url = f"{KABUSAPI_BOARD_URL}/{symbol}@{exchange}"
    req = urllib.request.Request(url, method='GET')
    req.add_header('X-API-KEY', API_KEY)
    req.add_header('Content-Type', 'application/json')

    print(f"[現在値取得] 銘柄 {symbol} の現在値を取得します。URL: {url}")
    try:
        with urllib.request.urlopen(req) as res:
            data = json.loads(res.read())
            if 'CurrentPrice' in data and data['CurrentPrice'] is not None:
                current_price = float(data['CurrentPrice'])
                print(f"[現在値取得成功] 銘柄 {symbol} の現在値: {current_price} 円")
                return current_price
            else:
                print(f"[現在値取得失敗] 銘柄 {symbol} の現在値を取得できませんでした。レスポンス: {data}")
                return None
    except urllib.error.HTTPError as e:
        print(f"[現在値取得エラー] HTTPエラー: {e}, URL: {url}")
        try:
            content = json.loads(e.read())
            print(f"[現在値取得エラー] APIレスポンス詳細: {content}")
        except:
            pass
        return None
    except Exception as e:
        print(f"[現在値取得エラー] 予期せぬエラー: {e}, URL: {url}")
        return None


def send_buy_order(symbol: str, limit_price: float, qty: int = 100):
    """
    kabusapi へ現物買い注文を「指値」で送信する関数。

    Args:
        symbol (str): 銘柄コード
        limit_price (float): 指値 (円)
        qty (int): 注文株数 (100株単位)
    """
    limit_price = int(round(limit_price))

    # --- 呼値「指値の丸めロジック」 ---
    # ほとんどの銘柄が１円で少し５円と１０円があり５０円は見たことない
    # 3,000円超の場合は「下1桁を5円単位(0 or 5)」で切り下げ
    # 5,000円超の場合は「下2桁を10円単位」で切り下げ
    if limit_price > 5000:
        # 下2桁を10円単位に揃える（切り下げ）
        limit_price = limit_price - (limit_price % 10)
    elif limit_price > 3000:
        # 切り下げだけなら limit_price - remainder % 5 といった方法もあるが、
        # 「下1桁が0か5」＝ 5円単位にまるめるなら
        #  limit_price = limit_price - (limit_price % 5)
        # でOK（整数値において最後の桁が 0 or 5 になる）
        limit_price = limit_price - (limit_price % 5)
    # --- ここまで指値丸め ---

    # 現物買い時のパラメータ例 (特定口座 "02" を想定)
    obj = {
        "Symbol": symbol,
        "Exchange": 1,        # 1: 東証
        "SecurityType": 1,    # 1: 株式
        "Side": "2",          # 2: 買い
        "CashMargin": 2,      # 1: 現物 2;　信用
        "MarginTradeType": 1, # 1:制度信用
        "DelivType": 0,       # 0: 指定なし 2: お預り金
        "AccountType": 4,     # 4: 特定 (各環境に合わせて設定)
        "Qty": qty,           # 発注株数
        "FrontOrderType": 20, # 20: 指値
        "Price": limit_price, # 指値(整数)
        "ExpireDay": 0        # 0: 当日
    }

    total_cost = limit_price * qty

    print(f"[発注準備] 銘柄 {symbol} 指値 {limit_price}円, 数量 {qty}株 (合計 {total_cost} 円)")
    print(f"[発注データ] {json.dumps(obj, ensure_ascii=False)}")

    json_data = json.dumps(obj).encode('utf-8')
    req = urllib.request.Request(KABUSAPI_SENDORDER_URL, json_data, method='POST')
    req.add_header('Content-Type', 'application/json')
    req.add_header('X-API-KEY', API_KEY)

    try:
        with urllib.request.urlopen(req) as res:
            content = json.loads(res.read())
            print(f"[発注成功] 銘柄 {symbol} 注文が成功: {json.dumps(content, ensure_ascii=False)}")
    except urllib.error.HTTPError as e:
        print(f"[発注失敗] 銘柄 {symbol} の注文に失敗。HTTPエラー: {e}")
        try:
            content = json.loads(e.read())
            print(f"[発注エラー詳細] {json.dumps(content, ensure_ascii=False)}")
        except:
            pass
    except Exception as e:
        print(f"[発注失敗] 銘柄 {symbol} の注文に失敗。予期せぬエラー: {e}")


def init_selenium_driver():
    """
    Seleniumドライバーを初期化する関数。
    webdriver_manager は非常に便利な一方、
    起動時に「最新版のドライバーを確認・ダウンロード」する処理が入るため、頻繁に呼ぶとその分時間がかかる場合があります。
    事前に手動インストール済みの chromedriver のパスを指定して利用することも可能です。
    """

    # 週に一回は、最新版のドライバーを確認・ダウンロードする。
    # print("[Selenium] Chromeドライバーを初期化します...")
    # chrome_options = Options()
    # chrome_options.add_argument("--headless")  # ヘッドレスモードで起動
    # chrome_options.add_argument("--no-sandbox")
    # chrome_options.add_argument("--disable-dev-shm-usage")
    #
    # service = Service(ChromeDriverManager().install())
    # driver = webdriver.Chrome(service=service, options=chrome_options)
    # print("[Selenium] Chromeドライバーの初期化が完了しました。")

    print("[Selenium] Chromeドライバーを初期化します...")
    chrome_options = Options()
    chrome_options.add_argument("--headless")  # ヘッドレスモードで起動
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    # 画像や通知等をブロックしたい場合
    # chrome_prefs = {
    #     "profile.default_content_setting_values": {
    #         "images": 2,
    #         "notifications": 2,
    #     }
    # }
    # chrome_options.add_experimental_option("prefs", chrome_prefs)

    # 手動インストール済みの chromedriver.exe のパスを指定
    # service = Service(r"C:\Users\nagoy\.wdm\drivers\chromedriver\win64\133.0.6943.141\chromedriver-win32\chromedriver.exe")
    service = Service(r"C:\Users\banko\.wdm\drivers\chromedriver\win64\134.0.6998.35\chromedriver-win32\chromedriver.exe")

    # webdriver を初期化
    driver = webdriver.Chrome(service=service, options=chrome_options)

    print("[Selenium] Chromeドライバーの初期化が完了しました。")
    return driver


def is_market_hours():
    """
    現在が市場時間内かどうかを判定 (8:55～11:30 or 12:30～15:40)
    """
    now = datetime.datetime.now()
    current_time = now.time()
    morning_start = datetime.time(8, 55)
    morning_end = datetime.time(11, 30)
    afternoon_start = datetime.time(12, 30)
    afternoon_end = datetime.time(15, 40)

    is_morning = morning_start <= current_time <= morning_end
    is_afternoon = afternoon_start <= current_time <= afternoon_end

    result = is_morning or is_afternoon
    status = "市場時間内" if result else "市場時間外"
    print(f"[市場時間チェック] 現在 {current_time.strftime('%H:%M:%S')} は {status} です。")
    return result


def is_market_hours_timestr(time_str):
    """
    指定された時刻 (例: "09:30") が 8:55～11:30 or 12:30～15:40 内かどうかを判定する。
    """
    if not time_str or len(time_str) < 5 or ":" not in time_str:
        return False

    try:
        hour, minute = map(int, time_str.split(":"))
        disclosure_time = datetime.time(hour, minute)

        morning_start = datetime.time(8, 55)
        morning_end = datetime.time(11, 30)
        afternoon_start = datetime.time(12, 30)
        afternoon_end = datetime.time(15, 40)

        return (morning_start <= disclosure_time <= morning_end) or \
               (afternoon_start <= disclosure_time <= afternoon_end)
    except (ValueError, TypeError):
        return False


def is_today_announcement(time_text):
    """
    開示時刻テキストが当日のものかどうかを判定。
    - "HH:MM" 形式のみの表示なら当日とみなす
    - "YYYY/MM/DD HH:MM" の場合は当日の日付かをチェック
    """
    now_date_str = datetime.datetime.now().strftime("%Y/%m/%d")
    if ":" in time_text:
        # "HH:MM"のみ → 当日とみなす
        if len(time_text) <= 5:
            return True
        else:
            # "YYYY/MM/DD HH:MM" の場合、日付が当日かどうかチェック
            if now_date_str in time_text:
                return True
    return False


def parse_announcement_datetime(time_text):
    """
    開示時刻を datetime型 に変換する関数。
    - "HH:MM"        → 今日の日付のその時刻
    - "YYYY/MM/DD HH:MM" → その日付 + 時刻
    それ以外は None を返す
    """
    now = datetime.datetime.now()
    try:
        if len(time_text) <= 5 and ":" in time_text:
            # "HH:MM"
            hour, minute = map(int, time_text.split(":"))
            return datetime.datetime(now.year, now.month, now.day, hour, minute)
        elif len(time_text) >= 16 and "/" in time_text and ":" in time_text:
            # "YYYY/MM/DD HH:MM"
            # 例: 2025/03/03 09:05
            date_part, hm_part = time_text.split(" ")
            y, mo, d = date_part.split("/")
            hour, minute = hm_part.split(":")
            return datetime.datetime(int(y), int(mo), int(d), int(hour), int(minute))
    except:
        pass

    return None


# def is_within_3_minutes_of_now(announcement_dt):
#     """
#     指定の日時が現在時刻から3分以内かどうかを判定。
#     """
#     if not announcement_dt:
#         return False
#     now = datetime.datetime.now()
#     diff = now - announcement_dt
#     diff_minutes = diff.total_seconds() / 60.0
#     return (0 <= diff_minutes <= 3)


def is_within_10_seconds_of_now(announcement_dt):
    """
    指定の日時が現在時刻から10秒以内かどうかを判定。

    Parameters:
    announcement_dt (datetime): 判定したい日時

    Returns:
    bool: 現在時刻から10秒以内であればTrue、そうでなければFalse
    """
    if not announcement_dt:
        return False

    now = datetime.datetime.now()
    diff = now - announcement_dt
    diff_seconds = diff.total_seconds()

    return (0 <= diff_seconds <= 25)


def get_budget_multiplier(market_cap: float) -> float:
    """
    時価総額に応じて買い付け上限金額を何倍にするかを決める。
    時価総額(円)の閾値に応じて以下の倍率を返す:

      - 10000億円以上: 5倍
      - 9000億円以上: 4.8倍
      - 8000億円以上: 4.6倍
      - 7000億円以上: 4.4倍
      - 6000億円以上: 4.2倍
      - 5000億円以上: 4倍
      - 4000億円以上: 3.5倍
      - 3000億円以上: 3倍
      - 2000億円以上: 2.5倍
      - 1000億円以上: 2倍
      - 600億円以上: 1.75倍
      - 300億円以上: 1.5倍
      - 上記未満: 1.0倍
    """
    if market_cap >= 10_000_000_000_000:    # 10兆円(=10000億円)
        return 6
    elif market_cap >= 9_000_000_000_000:   # 9兆
        return 5
    elif market_cap >= 8_000_000_000_000:   # 8兆
        return 5
    elif market_cap >= 7_000_000_000_000:   # 7兆
        return 5
    elif market_cap >= 6_000_000_000_000:   # 6兆
        return 4
    elif market_cap >= 5_000_000_000_000:   # 5兆
        return 4
    elif market_cap >= 4_000_000_000_000:   # 4兆
        return 4
    elif market_cap >= 3_000_000_000_000:   # 3兆
        return 3
    elif market_cap >= 2_000_000_000_000:   # 2兆
        return 3
    elif market_cap >= 1_000_000_000_000:   # 1兆
        return 3
    elif market_cap >= 900_000_000_000:   # 9000億円
        return 0.8
    elif market_cap >= 800_000_000_000:   # 8000億円
        return 0.7
    elif market_cap >= 700_000_000_000:   # 7000億円
        return 0.6
    elif market_cap >= 600_000_000_000:   # 6000億円
        return 0.5
    elif market_cap >= 500_000_000_000:   # 5000億円
        return 0.4
    elif market_cap >= 400_000_000_000:   # 4000億円
        return 0.3
    elif market_cap >= 300_000_000_000:   # 3000億円
        return 0.25
    elif market_cap >= 200_000_000_000:   # 2000億円
        return 0.25
    elif market_cap >= 100_000_000_000:   # 1000億円
        return 0.25
    elif market_cap >= 60_000_000_000:    # 600億円
        return 0.25
    elif market_cap >= 30_000_000_000:    # 300億円
        return 0.25
    elif market_cap >= 15_000_000_000:    # 150億円
        return 0.25
    else:
        return 0.2
    # if market_cap >= 1_000_000_000_000:    # 1兆円(=10000億円)
    #     return 5.0
    # elif market_cap >= 900_000_000_000:   # 9000億円
    #     return 4.8
    # elif market_cap >= 800_000_000_000:   # 8000億円
    #     return 4.6
    # elif market_cap >= 700_000_000_000:   # 7000億円
    #     return 4.4
    # elif market_cap >= 600_000_000_000:   # 6000億円
    #     return 4.2
    # elif market_cap >= 500_000_000_000:   # 5000億円
    #     return 4.0
    # elif market_cap >= 400_000_000_000:   # 4000億円
    #     return 3.5
    # elif market_cap >= 300_000_000_000:   # 3000億円
    #     return 3.0
    # elif market_cap >= 200_000_000_000:   # 2000億円
    #     return 2.5
    # elif market_cap >= 100_000_000_000:   # 1000億円
    #     return 2.0
    # elif market_cap >= 60_000_000_000:    # 600億円
    #     return 1.75
    # elif market_cap >= 30_000_000_000:    # 300億円
    #     return 1.5
    # else:
    #     return 1.0


def decide_order_plan(title_text: str, cost_per_100: int, factor: float = 1.0):
    """
    表題(title_text)から発注シナリオを判定して:
      - 発注すべきか否か(QTYが0なら発注なし)
      - 発注株数
      - ログ用の説明文
    を返す。

    Returns:
        (qty, reason, budget_label)
         qty (int): 0なら発注しない
         reason (str): ログ用の説明
         budget_label (str): どの材料扱いか
    """
    text = title_text

    # ---- まずは自己株式関連の追加要件を先に判定 ----
    #   - 「消却」 と ["自己株式取得", "自己株式の取得", "自己株式の買", "自己投資口"] の両方が含まれる => 150万円
    #   - 「自己株式の消却」が含まれる => 125万円
    #   - それ以外の自己株式取得系 => 従来どおり 100万円

    self_stock_words = ["自己株式取得", "自己株式の取得", "自己株式の買", "自己投資口"]

    # 1) 消却 + 自己株式取得系 => 150万円
    if ("消却" in text) and any(w in text for w in self_stock_words):
        base_budget = BUDGET_SELF_STOCK_ACQUIRE_AND_CANCEL
        budget_label = "自己株式取得 + 消却"
        adjusted_budget = int(base_budget * factor)
        if cost_per_100 > adjusted_budget:
            return (100, f"{budget_label} => 上限{adjusted_budget}円超 => 1単元のみ", budget_label)
        else:
            lots = adjusted_budget // cost_per_100
            if lots <= 0:
                return (0, f"{budget_label} => 調整後上限{adjusted_budget}円以内で買えない => 発注なし", budget_label)
            return (lots * 100, f"{budget_label} => 合計 {lots}単元 (adjusted_budget={adjusted_budget})", budget_label)

    # 2) 自己株式の消却 => 125万円
    if "自己株式の消却" in text:
        base_budget = BUDGET_SELF_STOCK_CANCEL
        budget_label = "自己株式の消却"
        adjusted_budget = int(base_budget * factor)
        if cost_per_100 > adjusted_budget:
            return (100, f"{budget_label} => 上限{adjusted_budget}円超 => 1単元のみ", budget_label)
        else:
            lots = adjusted_budget // cost_per_100
            if lots <= 0:
                return (0, f"{budget_label} => 調整後上限{adjusted_budget}円以内で買えない => 発注なし", budget_label)
            return (lots * 100, f"{budget_label} => 合計 {lots}単元 (adjusted_budget={adjusted_budget})", budget_label)

    # 3) 自己株式取得系（従来） => 100万円
    if any(w in text for w in self_stock_words):
        base_budget = BUDGET_SELF_STOCK
        budget_label = "自己株式取得系"
        adjusted_budget = int(base_budget * factor)
        if cost_per_100 > adjusted_budget:
            return (100, f"自己株式取得系 => 上限{adjusted_budget}円超 => 1単元のみ", budget_label)
        else:
            lots = adjusted_budget // cost_per_100
            if lots <= 0:
                return (0, f"自己株式取得系 => 調整後の上限{adjusted_budget}円以内=>発注なし", budget_label)
            return (lots * 100, f"自己株式取得系 => 合計 {lots}単元 (adjusted_budget={adjusted_budget})", budget_label)

    # ---------------------------
    # 以下は他材料の判定
    # ---------------------------

    # 4) 上方修正 + 増配 => 1,500,000円
    if ("上方" in text) and ("増配" in text):
        base_budget = BUDGET_UPPER_REVISION_PLUS_ZOHAI
        budget_label = "上方修正 + 増配"
        adjusted_budget = int(base_budget * factor)
        if cost_per_100 > adjusted_budget:
            return (100, f"上方+増配 => 上限{adjusted_budget}円超 => 1単元のみ", budget_label)
        else:
            lots = adjusted_budget // cost_per_100
            if lots <= 0:
                return (0, f"上方+増配 => 調整後の上限{adjusted_budget}円以内で買えない => 発注なし", budget_label)
            return (lots * 100, f"上方+増配 => 合計 {lots}単元 (adjusted_budget={adjusted_budget})", budget_label)

    # 2. 良好 => 上限 1,500,000円
    if "良好" in text:
        base_budget = BUDGET_RYOKO
        budget_label = "良好"
        adjusted_budget = int(base_budget * factor)
        if cost_per_100 > adjusted_budget:
            return (100, f"良好 => 上限{adjusted_budget}円超 => 1単元のみ", budget_label)
        else:
            lots = adjusted_budget // cost_per_100
            if lots <= 0:
                return (0, f"良好 => 調整後の上限{adjusted_budget}円以内で買えない => 発注なし", budget_label)
            return (lots * 100, f"良好 => 合計 {lots}単元 (adjusted_budget={adjusted_budget})", budget_label)

    # 3. 優待(新設/導入/再開) => 上限 1,000,000円
    if ("株主優待" in text) and (("新設" in text) or ("導入" in text) or ("再開" in text)):
        base_budget = BUDGET_NEW_YUTAI
        budget_label = "優待(新設/導入/再開)"
        adjusted_budget = int(base_budget * factor)
        if cost_per_100 > adjusted_budget:
            return (100, f"{budget_label} => 上限{adjusted_budget}円超 => 1単元のみ", budget_label)
        else:
            lots = adjusted_budget // cost_per_100
            if lots <= 0:
                return (0, f"{budget_label} => 調整後の上限{adjusted_budget}円以内=>発注なし", budget_label)
            return (lots * 100, f"{budget_label} => 合計 {lots}単元 (adjusted_budget={adjusted_budget})", budget_label)

    # 5. 業務提携 => 上限 1,000,000円
    if "業務提携" in text:
        base_budget = BUDGET_BUSINESS_ALLIANCE
        budget_label = "業務提携"
        adjusted_budget = int(base_budget * factor)
        if cost_per_100 > adjusted_budget:
            return (100, f"業務提携 => 上限{adjusted_budget}円超 => 1単元のみ", budget_label)
        else:
            lots = adjusted_budget // cost_per_100
            if lots <= 0:
                return (0, f"業務提携 => 上限{adjusted_budget}円以内=>発注なし", budget_label)
            return (lots * 100, f"業務提携 => 合計 {lots}単元 (adjusted_budget={adjusted_budget})", budget_label)

    # 6. 資本提携 => 上限 1,000,000円
    if "資本提携" in text:
        base_budget = BUDGET_CAPITAL_ALLIANCE
        budget_label = "資本提携"
        adjusted_budget = int(base_budget * factor)
        if cost_per_100 > adjusted_budget:
            return (100, f"資本提携 => 上限{adjusted_budget}円超 => 1単元のみ", budget_label)
        else:
            lots = adjusted_budget // cost_per_100
            if lots <= 0:
                return (0, f"資本提携 => 調整後の上限{adjusted_budget}円以内=>発注なし", budget_label)
            return (lots * 100, f"資本提携 => 合計 {lots}単元 (adjusted_budget={adjusted_budget})", budget_label)

    # 7. 完成 => 上限 1,000,000円
    if "完成" in text:
        base_budget = BUDGET_KANSEI
        budget_label = "完成"
        adjusted_budget = int(base_budget * factor)
        if cost_per_100 > adjusted_budget:
            return (100, f"完成 => 上限{adjusted_budget}円超 => 1単元のみ", budget_label)
        else:
            lots = adjusted_budget // cost_per_100
            if lots <= 0:
                return (0, f"完成 => 調整後の上限{adjusted_budget}円以内=>発注なし", budget_label)
            return (lots * 100, f"完成 => 合計 {lots}単元 (adjusted_budget={adjusted_budget})", budget_label)

    # 8. 採択 => 上限 1,000,000円
    if "採択" in text:
        base_budget = BUDGET_SAITAKU
        budget_label = "採択"
        adjusted_budget = int(base_budget * factor)
        if cost_per_100 > adjusted_budget:
            return (100, f"採択 => 上限{adjusted_budget}円超 => 1単元のみ", budget_label)
        else:
            lots = adjusted_budget // cost_per_100
            if lots <= 0:
                return (0, f"採択 => 調整後の上限{adjusted_budget}円以内=>発注なし", budget_label)
            return (lots * 100, f"採択 => 合計 {lots}単元 (adjusted_budget={adjusted_budget})", budget_label)

    # 9. 上方修正(単独) => 上限 1,000,000円
    if "上方" in text:
        base_budget = BUDGET_UPPER_REVISION
        budget_label = "上方修正(単独)"
        adjusted_budget = int(base_budget * factor)
        if cost_per_100 > adjusted_budget:
            return (100, f"上方単独 => 上限{adjusted_budget}円超 => 1単元のみ", budget_label)
        else:
            lots = adjusted_budget // cost_per_100
            if lots <= 0:
                return (0, f"上方単独 => 調整後の上限{adjusted_budget}円以内=>発注なし", budget_label)
            return (lots * 100, f"上方単独 => 合計 {lots}単元 (adjusted_budget={adjusted_budget})", budget_label)
    # 10. 株主優待 AND 拡充 => 上限 500,000円
    # if ("株主優待" in text) and ("拡充" in text):
    #     base_budget = BUDGET_YUTAI_KAKUCHU
    #     adjusted_budget = int(base_budget * factor)
    #     if cost_per_100 > adjusted_budget:
    #         return (100, f"優待拡充 => 上限{adjusted_budget}円超 => 1単元のみ")
    #     else:
    #         lots = adjusted_budget // cost_per_100
    #         if lots <= 0:
    #             return (0, f"優待拡充 => 調整後の上限{adjusted_budget}円以内で買えない => 発注なし")
    #         return (lots * 100, f"優待拡充 => 合計 {lots}単元 (adjusted_budget={adjusted_budget})")

    # 11. 増配(単独) or 記念配当 => 上限 500,000円
    # is_zohai_alone = ("増配" in text)
    # is_kinen = ("記念配当" in text) or (("記念" in text) and ("配当" in text))
    # if is_zohai_alone or is_kinen:
    #     base_budget = BUDGET_ZOHAI_OR_KINEN
    #     adjusted_budget = int(base_budget * factor)
    #     if cost_per_100 > adjusted_budget:
    #         return (0, f"増配or記念配当 => 調整後の上限{adjusted_budget}円超 => 発注なし")
    #     else:
    #         lots = adjusted_budget // cost_per_100
    #         if lots <= 0:
    #             return (0, f"増配or記念配当 => 調整後の上限{adjusted_budget}円以内で買えない => 発注なし")
    #         return (lots * 100, f"増配or記念配当 => 合計 {lots}単元 (adjusted_budget={adjusted_budget})")

    # 12. どれにも該当しない => 発注なし
    # 該当しない => 発注なし
    return (0, "ポジティブ条件該当なし => 発注なし", "未該当")


def process_disclosure_page(driver, include_keywords, exclude_keywords, target_disclosures, page_num=1):
    """
    1ページ分の開示情報を走査して、該当銘柄を target_disclosures リストに追加する。
    Args:
        driver (webdriver): SeleniumのWebDriver
        include_keywords (list): ポジティブ判定用の含むキーワード
        exclude_keywords (list): 除外用キーワード
        target_disclosures (list): 抽出した銘柄を追加する先
        page_num (int): ページ番号(ログ用)
    """
    print(f"[スクレイピング] ページ {page_num} の処理を開始します。")

    tables = driver.find_elements(By.TAG_NAME, "table")
    disclosure_table = None

    # テーブルを見つけるロジック（サンプル例: 行数が最も多いテーブルを探す）
    max_rows = 0
    for i, table in enumerate(tables):
        rows = table.find_elements(By.TAG_NAME, "tr")
        if len(rows) > max_rows:
            max_rows = len(rows)
            disclosure_table = table

    if disclosure_table is None:
        print(f"[スクレイピング] ページ {page_num} に開示テーブルが見当たりません。")
        return

    # tbody を取得 (なければそのままtableを扱う)
    try:
        tbody = disclosure_table.find_element(By.TAG_NAME, "tbody")
    except NoSuchElementException:
        tbody = disclosure_table

    rows = tbody.find_elements(By.TAG_NAME, "tr")
    if not rows:
        print(f"[スクレイピング] テーブル行が空 (ページ {page_num})")
        return

    # 先頭行がヘッダっぽければスキップ
    first_row_cells = rows[0].find_elements(By.TAG_NAME, "td")
    first_row_texts = [c.text.strip() for c in first_row_cells]
    maybe_header = any(k in " ".join(first_row_texts) for k in ["時刻", "コード", "会社", "表題"])
    start_index = 1 if maybe_header else 0

    for idx, tr in enumerate(rows[start_index:], start=start_index):
        try:
            cols = tr.find_elements(By.TAG_NAME, "td")
            if len(cols) < 4:
                continue

            time_text = cols[0].text.strip()
            code_text = cols[1].text.strip()
            company_name = cols[2].text.strip()
            title_text = cols[3].text.strip()

            # 今日の開示でかつ市場時間内かどうか
            if not is_today_announcement(time_text):
                continue
            if not is_market_hours_timestr(time_text):
                continue

            # コード整形
            normalized_code = code_text.replace(" ", "").replace("-", "")
            if len(normalized_code) > 4:
                normalized_code = normalized_code[:4]

            # 除外キーワードチェック
            if any(exc_kw in title_text for exc_kw in exclude_keywords):
                continue

            # ポジティブキーワードが含まれているか
            if any(inc_kw in title_text for inc_kw in include_keywords):
                target_disclosures.append({
                    "symbol": normalized_code,
                    "company_name": company_name,
                    "title_text": title_text,
                    "time_text": time_text,
                })

        except Exception as e:
            print(f"[スクレイピング] 行 {idx} でエラー: {e}")


def scrape_tdnet_self_stock_acquisition(max_pages=10):
    """
    TDnet(適時開示)を複数ページにわたってスクレイピングし、
    ポジティブな銘柄情報を抽出して返す。
    Returns:
        list: [{ "symbol": str, "company_name": str, "title_text": str, "time_text": str }, ...]
    """
    print("[スクレイピング] TDnet適時開示を取得開始")
    driver = init_selenium_driver()
    target_disclosures = []

    # ポジティブ判定: 自己株式取得, 増配, 上方修正, 株主優待, 記念, 拡充, 新設, 再開, 業務提携, 資本提携, 完成, 採択 など
    include_keywords = [
        '自己株式取得', '自己株式の取得', '自己株式の買', '自己投資口',
        '増配', '上方', '株主優待', '新設', '導入', '再開',
        '業務提携', '資本提携', '完成', '採択', '良好',
        '消却', '自己株式の消却'
    ]
    # include_keywords = [
    #     '自己株式取得', '自己株式の取得', '自己株式の買', '自己投資口',
    #     '増配', '上方', '株主優待', '記念', '拡充', '新設', '導入', '再開',
    #     '業務提携', '資本提携', '完成', '採択', '良好'
    # ]
    # 除外キーワード(下方修正や減配、終了など)
    exclude_keywords = [
        '終了', '結果', '状況', '訂正', '中止', '無配', '廃止',
        '下方', '見送', '損失', '業績目標', '補足', '減配', '解消', '完了', '一部変更'
    ]

    base_url = "https://www.release.tdnet.info"
    main_page_path = "/inbs/I_main_00.html"
    url = urllib.parse.urljoin(base_url, main_page_path)

    try:
        driver.get(url)
        # iframe読み込み完了を待機
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.ID, "main_list")))
        iframe = driver.find_element(By.ID, "main_list")
        iframe_src = iframe.get_attribute("src")
        iframe_url = urllib.parse.urljoin(base_url, iframe_src)
        driver.get(iframe_url)

        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.TAG_NAME, "table")))

        #1ページのみ取得
        process_disclosure_page(
            driver,
            include_keywords,
            exclude_keywords,
            target_disclosures,
            page_num=1
        )
        #
        # current_page = 1
        # while current_page <= max_pages:
        #     # 1ページ分のスクレイピング処理
        #     process_disclosure_page(
        #         driver,
        #         include_keywords,
        #         exclude_keywords,
        #         target_disclosures,
        #         page_num=current_page
        #     )
        #
        #     # ページネーション (「次へ」or「Next」、ページ番号リンクなど)
        #     next_page_found = False
        #     try:
        #         next_page_elements = driver.find_elements(By.XPATH, "//*[contains(text(), '次へ') or contains(text(), 'Next')]")
        #         if not next_page_elements:
        #             # ページ番号リンク(2,3,...)も探してみる
        #             next_page_num = current_page + 1
        #             next_num_links = driver.find_elements(By.XPATH, f"//a[text()='{next_page_num}']")
        #             if next_num_links:
        #                 next_page_elements = next_num_links
        #
        #         for link in next_page_elements:
        #             if link.is_enabled() and link.is_displayed():
        #                 driver.execute_script("arguments[0].click();", link)
        #                 # time.sleep(1)  # ページ切り替えの待ち(適宜調整)
        #                 WebDriverWait(driver, 10).until(EC.staleness_of(link))
        #                 WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.TAG_NAME, "table")))
        #                 current_page += 1
        #                 next_page_found = True
        #                 break
        #
        #         if not next_page_found:
        #             print(f"[スクレイピング] 次ページが見当たらないので終了 (現在ページ {current_page})")
        #             break
        #
        #     except Exception as e:
        #         print(f"[スクレイピング] ページ切り替え時エラー: {e}")
        #         break

        # 重複銘柄をまとめる (既に見つかったものは上書きしない: 先着優先)
        unique_map = {}
        for d in target_disclosures:
            sym = d["symbol"]
            if sym not in unique_map:
                unique_map[sym] = d
            else:
                # 既にあればスキップ(必要に応じて時刻比較で上書きするなど)
                pass

        results = list(unique_map.values())
        return results

    except Exception as e:
        print(f"[スクレイピング] 予期せぬエラー: {e}")
        return []
    finally:
        driver.quit()


def log_symbols_to_csv(disclosures):
    """
    抽出した銘柄を CSV に記録する。
    該当銘柄なしの場合は "該当銘柄なし" とだけ出力。
    """
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_file = "scraping_log_tdnet.csv"
    print(f"[ログ] {log_file} へ記録します。")

    with open(log_file, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if disclosures:
            for d in disclosures:
                writer.writerow([now_str, d["symbol"], d["company_name"], d["title_text"], d["time_text"]])
                print(f"[ログ] {now_str}, {d['symbol']}, {d['company_name']}, {d['title_text']}, {d['time_text']}")
        else:
            writer.writerow([now_str, "該当銘柄なし"])
            print(f"[ログ] {now_str}, 該当銘柄なし")

###############################################
# 追加: 買付完了後のログをまとめて出力する関数
###############################################

def log_purchased_orders(purchased_list, filename="purchased_log.csv"):
    """
    今回買いを入れた銘柄・材料・発注金額などをまとめてCSVログ出力する。
    ここで出力した情報を「売り注文側のコード」に渡しても良いし、
    別の処理に取り込んでも良いように疎結合化。

    purchased_list: [
       {
         "symbol": str,
         "title_text": str,
         "budget_label": str,   # "上方修正 + 増配" 等
         "limit_price": int,
         "qty": int,
         "total_cost": int,
         "reason": str,        # decide_order_plan でのコメント
         "market_cap": int
       }, ...
    ]
    """
    if not purchased_list:
        print("[購入ログ] 今回の買付銘柄はありません。")
        return

    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[購入ログ] {filename} へ記録します。")

    with open(filename, mode="a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        # ヘッダを付けたい場合は if 文でファイルサイズを見て0なら...などで対応
        # ここでは簡易例として常にヘッダを出さず追記のみ
        for info in purchased_list:
            row = [
                now_str,
                info["symbol"],
                info["budget_label"],   # どの材料で買ったか
                info["title_text"],
                info["reason"],
                info["limit_price"],
                info["qty"],
                info["total_cost"],
                info["market_cap"],
            ]
            writer.writerow(row)
            print(f"[購入ログ] 記録: {row}")
#
# ここから追加：材料ごとの倍率を判定する関数
# #
def calc_material_multiplier(title_text: str) -> float:
    """
    表題(title_text)内の材料に応じて、指値用の追加倍率を返す。
    複合条件と単独条件が重なる場合は、意図がかぶらないよう if/elif で優先度をつけている。
    """
    text = title_text
    multiplier = 1.0

    # 1) 上方修正 + 増配 => 1.02
    #   （もし同時に "良好" などあれば下でさらに掛かる）
    if "上方" in text and "増配" in text:
        multiplier *= 1.03
    # else:
    #     # 2) 増配単独 or 記念配当 => 0.995
    #     #    "上方+増配" と重複しないよう else 側に配置
    #     if "増配" in text or "記念配当" in text:
    #         multiplier *= 0.995

    # if "増配" in text or "記念配当" in text:
    #     multiplier *= 0.98
    # if ("株主優待" in text) and ("拡充" in text):
    #     multiplier *= 0.98

    # 3) 良好 => 1.02
    elif "良好" in text:
        multiplier *= 1.03

    # 4) 業務提携 => 1.01
    elif "業務提携" in text:
        multiplier *= 1.03

    # 5) 資本提携 => 1.01
    elif "資本提携" in text:
        multiplier *= 1.03

    # 6) 株主優待 + 拡充 => 0.995
    #   優待(新設/導入/再開) => 1.01
    #   どちらも含まれる場合、拡充を優先したいなら先に if で判定
    # if ("株主優待" in text) and ("拡充" in text):
    #     multiplier *= 0.995
    # else:
    #     # 新設/導入/再開 が含まれる場合は 1.01
    #     # （"拡充" と重複しないよう else 側に記載）
    #     if ("株主優待" in text or "優待" in text) and \
    #        ("新設" in text or "導入" in text or "再開" in text):
    #         multiplier *= 1.01

    # 7) 完成 => 1.02
    elif "完成" in text:
        multiplier *= 1.03

    # 8) 採択 => 1.02
    elif "採択" in text:
        multiplier *= 1.03

    # 9) 自己株式関連 or 上方(単独) => 1.0 (実質影響なし)
    #    一応枠だけ用意し、今後の拡張に対応しやすくする
    # if "自己株" in text:
    #     multiplier *= 1.0
    # if "上方" in text:
    #     multiplier *= 1.0

    return multiplier


def main_prod():
    """
    実際の処理 (スクレイピング→発注判定→発注) を行うメイン関数。
      - 市場時間外なら処理をスキップするなどの判定を入れることも可能
      - TDnetをスクレイピング
      - 開示から数十秒以内の銘柄を判定し KabusAPIへ指値注文
    """
    print("=== (本番) 自己株式取得・消却・増配・上方修正などポジティブ材料監視プログラム 開始 ===")
    print(f"[開始時刻] {datetime.datetime.now()}")

    # 市場時間外ならスキップしたい場合は下記コメント解除
    # if not is_market_hours():
    #     print("[スキップ] 現在は市場時間外のため今回は処理しません。")
    #     return

    print("\n=== スクレイピング & 発注判定サイクル開始 ===")
    try:
        disclosures = scrape_tdnet_self_stock_acquisition()
        if disclosures:
            print(f"[取得銘柄] { [d['symbol'] for d in disclosures] }")
        else:
            print("[取得銘柄] 該当なし")
    except Exception as e:
        print(f"[スクレイピング中エラー] {e}")
        disclosures = []

    # ログ出力
    # log_symbols_to_csv(disclosures)

    # 今回実際に買付を行った銘柄の情報をまとめる
    purchased_orders = []

    # KabusAPIでの注文処理
    for info in disclosures:
        symbol = info["symbol"]
        title_text = info["title_text"]
        time_text = info["time_text"]

        # 重複発注防止
        if symbol in ordered_symbols:
            print(f"[発注スキップ] {symbol} は既に発注済み")
            continue

        # 開示時刻が25秒以内か確認
        ann_dt = parse_announcement_datetime(time_text)
        if not is_within_10_seconds_of_now(ann_dt):
            print(f"[発注スキップ] {symbol}: 開示から25秒以上経過({time_text})")
            continue

        # 現在値取得
        current_price = get_current_price(symbol)
        if current_price is None or current_price <= 0:
            print(f"[発注スキップ] 銘柄 {symbol} の現在値取得失敗")
            continue

        # 指値(現在値 + 3%)
        # limit_price = round(current_price * 1.03)
        # 速度で勝てないので　指値(現在値 - 0.7%)
        # limit_price = round(current_price * 0.993)
        # 1％
        # limit_price = round(current_price * 1.01)


        # ★★★ 修正箇所: 銘柄情報から時価総額を取得し、250億円以上かどうかで指値を変える
        symbol_info = get_symbol_info(symbol)
        market_cap = symbol_info.get("TotalMarketValue", 0)

        # 時価総額に応じた指値

        # if market_cap >= 1_000_000_000_000:     # 10000億円以上
        #     raw_limit_price = current_price * 1.01
        # elif market_cap >= 900_000_000_000:    # 9000億円以上
        #     raw_limit_price = current_price * 1.01
        # elif market_cap >= 800_000_000_000:    # 8000億円以上
        #     raw_limit_price = current_price * 1.01
        # elif market_cap >= 700_000_000_000:    # 7000億円以上
        #     raw_limit_price = current_price * 1.01
        # elif market_cap >= 600_000_000_000:    # 6000億円以上
        #     raw_limit_price = current_price * 1.01
        # elif market_cap >= 500_000_000_000:    # 5000億円以上
        #     raw_limit_price = current_price * 1.01
        # elif market_cap >= 400_000_000_000:    # 4000億円以上
        #     raw_limit_price = current_price * 1.01
        # elif market_cap >= 300_000_000_000:    # 3000億円以上
        #     raw_limit_price = current_price * 1.01
        # elif market_cap >= 200_000_000_000:    # 2000億円以上
        #     raw_limit_price = current_price * 1.01
        # elif market_cap >= 100_000_000_000:    # 1000億円以上
        #     raw_limit_price = current_price * 1.01
        # elif market_cap >= 60_000_000_000:     # 600億円以上
        #     raw_limit_price = current_price * 1.01
        # elif market_cap >= 30_000_000_000: # 300億円以上
        #     raw_limit_price = current_price * 1.01
        # else:
        #     # 時価総額300億円未満: -3%
        #     raw_limit_price = current_price * 0.98

        if market_cap >= 30_000_000_000:  # 300億円以上
            raw_limit_price = current_price * 1.01
        else:
            # 時価総額300億円未満: -3%
            raw_limit_price = current_price * 0.98

        # 時価総額によって指値倍率変更
        # if market_cap >= 30_000_000_000:
        #     raw_limit_price = current_price * 1.015
        # else:
        #     # 時価総額300億円未満: -3%
        #     raw_limit_price = current_price * 0.99
        # raw_limit_price = current_price * 1.015

        #
        # ★★★ ここが今回の改修ポイント：材料に応じて更に multiplier を掛ける
        #
        material_factor = calc_material_multiplier(title_text)
        raw_limit_price *= material_factor

        limit_price = round(raw_limit_price)

        # 時価総額に応じた 上限金額の倍率 を求める
        factor = get_budget_multiplier(market_cap)

        # 100株あたりの目安コスト
        cost_per_100 = limit_price * 100

        # 発注プランを決定
        qty, reason, budget_label = decide_order_plan(title_text, cost_per_100, factor=factor)
        if qty <= 0:
            print(f"[発注なし] {symbol} ({title_text}) => {reason}")
            continue

        total_cost = limit_price * qty
        print(f"[発注判定] 銘柄:{symbol}, 表題:'{title_text}', 指値:{limit_price}円, 株数:{qty}, "
              f"予定金額:{total_cost}円, 判定:{reason}, 時価総額:{market_cap}, factor:{factor}")

        # 実際にkabusapiへ注文送信
        send_buy_order(symbol, limit_price, qty)

        # 発注済みリストに登録
        ordered_symbols.add(symbol)

        # 買付情報を蓄積 (最後にまとめてログ出力する用)
        purchased_orders.append({
            "symbol": symbol,
            "title_text": title_text,
            "budget_label": budget_label,
            "limit_price": limit_price,
            "qty": qty,
            "total_cost": total_cost,
            "reason": reason,
            "market_cap": market_cap,
        })


    # 速度重視のため、発注系がすべて終わった「最後」にログ出力をまとめて実行
    log_purchased_orders(purchased_orders)

    print("[完了] main_prod() の処理が完了しました。")


def main_logic():
    """
    main_prod()を呼び出すラッパー関数。
    実際に行いたい処理をここにまとめることで、
    multiprocessingによるタイムアウト管理との切り分けをしやすくする。
    """
    main_prod()


def run_with_timeout():
    """
    multiprocessing.Processで main_logic() を実行し、
    16秒以内に完了しなければプロセスを強制終了する。
    戻り値: True => 正常終了, False => タイムアウトで強制終了
    """
    p = multiprocessing.Process(target=main_logic)
    p.start()
    p.join(timeout=40)
    # p.join(timeout=30)
    if p.is_alive():
        print(">>> 16秒以内に処理が終わらなかったので強制終了し、やり直します。")
        p.terminate()
        p.join()
        return False
    else:
        print(">>> 16秒以内に全処理が正常終了しました。")
        return True


if __name__ == "__main__":
    """
    「毎分ちょうどの 'mm:00:01', 'mm:01:01', 'mm:02:01' ... のように
     1秒のタイミングで処理を実行し、
     さらに 1秒の20秒前からは調整（停止）してよい」
    という要件を満たすように、待機ロジックを追加する例。

    - 次の実行ターゲット時刻 (例: [今の分+1分]:01) を計算
    - もし 20秒以上先なら、その差 -20秒を先に sleep
    - そこから残りは細かくループしながら待ち、
      ぴったり次のターゲット時刻になったら run_with_timeout() を実行
    """
    while True:
        # 現在時刻
        now = datetime.datetime.now()
        print(f"現在時刻: {now}")

        # 次の「mm分 1秒」の実行ターゲットを計算
        # ( 今+1分 ) にして second=1, microsecond=0 にする
        next_run = (now + datetime.timedelta(minutes=1)).replace(second=1, microsecond=500000)

        # 残り秒数
        wait_seconds = (next_run - now).total_seconds()
        if wait_seconds <= 0:
            # もし既にターゲット時間を過ぎてしまっていればスキップ(念のため)
            continue

        # 20秒以上先なら (残り - 20秒) だけ先に寝る
        if wait_seconds > 20:
            time.sleep(wait_seconds - 20)
            # ここで「20秒前」まで来た

        # 20秒を切ったら細かく待ち合わせ (秒精度で合わせたい場合)
        while True:
            now_loop = datetime.datetime.now()
            if now_loop >= next_run:
                # ターゲット時刻になったのでブレイク
                break

        # ちょうど next_run になったので本処理実行
        finished = run_with_timeout()

        # finished が True でも False でも、またすぐ次の分で同様に実行
        # (永久ループ)
        print(">>> 次の分の 1秒になったら再度実行します...\n")
