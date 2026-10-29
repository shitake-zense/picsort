#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
if [[ "$(uname -s)" != "Darwin" ]]; then
  echo 'このビルドはMacで実行してください。' >&2; exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Python 3.12をインストールし、ターミナルから再実行してください。' >&2; exit 1
fi
python3 -c 'import sys; assert sys.version_info >= (3, 10), "Python 3.10以上が必要です。3.12を推奨します。"'
if [[ -e "dist/PixSort.app" || -e "dist/PixSort" || -e "dist/PixSort-Mac" || -e "dist/PixSort-Mac.zip" ]]; then
  echo 'dist に既存のビルド結果があります。上書きせず終了します。新しいフォルダに展開して実行してください。' >&2; exit 1
fi
echo 'ピクソートのMac版をビルドします。開発用ライブラリの取得にはネット接続が必要です。'
echo '完成したアプリの使用時にはネット接続は不要です。'
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python build.py
release="dist/PixSort-Mac"
if [[ -e "$release" || -e "dist/PixSort-Mac.zip" ]]; then
  echo '既存の配布用フォルダまたはZIPがあるため、上書きせず終了します。dist/PixSort.app はビルド済みです。' >&2; exit 1
fi
mkdir -p "$release"
ditto "dist/PixSort.app" "$release/PixSort.app"
cp README.md LICENSE DEPENDENCIES.md Macでのビルド手順.txt "$release/"
ditto THIRD_PARTY_LICENSES "$release/THIRD_PARTY_LICENSES"
mkdir "$release/Source"
cp app.py core.py build.py requirements.txt README.md LICENSE DEPENDENCIES.md Macでのビルド手順.txt "$release/Source/"
ditto assets "$release/Source/assets"
ditto tests "$release/Source/tests"
cp Build-Mac.command "$release/Source/"
ditto THIRD_PARTY_LICENSES "$release/Source/THIRD_PARTY_LICENSES"
ditto -c -k --sequesterRsrc --keepParent "$release" "dist/PixSort-Mac.zip"
echo '完成: dist/PixSort.app と dist/PixSort-Mac.zip'
echo '実機で操作とごみ箱への移動を確認してから配布してください。'
echo 'このビルドにはAppleの開発者署名・公証は含まれていません。'
