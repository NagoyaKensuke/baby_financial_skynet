import os
import json
from pathlib import Path
import pypdf
from openai import OpenAI
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

# OpenAI APIキーの設定（環境変数から取得）
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise ValueError("環境変数 OPENAI_API_KEY が設定されていません。.envファイルを確認してください。")

client = OpenAI(api_key=api_key)

output_file = "財務分析結果o3-2025-04-16.json"

# PDFからテキストを抽出する関数
def extract_text_from_pdf(pdf_path):
    with open(pdf_path, 'rb') as file:
        reader = pypdf.PdfReader(file)
        text = ""
        for page_num in range(len(reader.pages)):
            page = reader.pages[page_num]
            text += page.extract_text()
    return text


# GPT-o3を使って財務分析を行う関数
def analyze_financials(company_name, financial_text):
    try:
        response = client.chat.completions.create(
            model="o3-2025-04-16",
            messages=[
                {"role": "system", "content": """
                あなたは財務分析のエキスパートです。企業の財務指標を分析し、以下の形式で回答してください：

                1. 財務状況の要約
                2. 強み（3つ）
                3. 懸念点（3つ）
                4. 投資判断（「強く推奨」「推奨」「中立」「非推奨」「強く非推奨」のいずれか）
                5. 投資点数（0-100点）
                6. 点数の根拠

                回答はJSON形式で返してください。
                """},
                {"role": "user",
                 "content": f"以下は{company_name}の財務情報です。分析してください：\n\n{financial_text[:16000]}"}
            ],
            temperature=1,
            response_format={"type": "json_object"}
        )

        # 結果をJSONとして解析
        analysis = json.loads(response.choices[0].message.content)
        return analysis

    except Exception as e:
        print(f"エラーが発生しました: {e}")
        return None


# メイン関数
def main():
    # PDFフォルダのパス
    pdf_folder = Path("pdf")

    # フォルダが存在するか確認
    if not pdf_folder.exists() or not pdf_folder.is_dir():
        print(f"エラー: {pdf_folder} フォルダが見つかりません。")
        return

    # 結果を保存するリスト
    results = []

    # PDFファイルを処理
    for pdf_file in pdf_folder.glob("*.pdf"):
        print(f"処理中: {pdf_file.name}")

        # ファイル名から会社名を抽出（拡張子を除く）
        company_name = pdf_file.stem

        # PDFからテキストを抽出
        financial_text = extract_text_from_pdf(pdf_file)

        # テキストが抽出できたか確認
        if not financial_text:
            print(f"警告: {pdf_file.name} からテキストを抽出できませんでした。")
            continue

        # 財務分析を実行
        analysis = analyze_financials(company_name, financial_text)

        if analysis:
            # 結果に会社名を追加
            analysis["company_name"] = company_name
            results.append(analysis)

            # 分析結果を表示
            print(f"\n===== {company_name} の分析結果 =====")
            print(f"投資判断: {analysis.get('投資判断', 'N/A')}")
            print(f"投資点数: {analysis.get('投資点数', 'N/A')}")
            print("財務状況の要約:")
            print(analysis.get('財務状況の要約', 'N/A'))
            print("\n")
        else:
            print(f"警告: {company_name} の分析に失敗しました。")

    # 全体の結果をファイルに保存
    if results:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"分析結果を {output_file} に保存しました。")

    # 点数順にランキング表示
    if results:
        ranked_results = sorted(results, key=lambda x: x.get('投資点数', 0), reverse=True)
        print("\n===== 企業ランキング（投資点数順）=====")
        for i, result in enumerate(ranked_results, 1):
            print(
                f"{i}. {result['company_name']} - {result.get('投資点数', 'N/A')}点 ({result.get('投資判断', 'N/A')})")


if __name__ == "__main__":
    main()
