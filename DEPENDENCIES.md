# 同梱ライブラリとソース

ピクソートのソースはMITライセンスです。依存ライブラリのライセンスは独立しています。
Windows版はQtを共有DLLとして同梱し、`_internal`内のライブラリを置き換えられる構成です。
LGPLで認められたライブラリの改変・デバッグのためのリバースエンジニアリングを制限しません。

| コンポーネント | バージョン | ライセンス・上流ソース |
|---|---|---|
| PySide6 Essentials / Shiboken6 | 6.12.0 | LGPL v3。https://code.qt.io/cgit/pyside/pyside-setup.git/ （v6.12.0） |
| Qt Core / Gui / Widgets / Network / Svg | 6.12.0 | LGPL v3。https://code.qt.io/cgit/qt/qtbase.git/ 、https://code.qt.io/cgit/qt/qtsvg.git/ （v6.12.0） |
| CPython | 3.12.14 | PSFライセンス。https://github.com/python/cpython/tree/v3.12.14 |
| Pillow | 12.3.0 | HPND。https://github.com/python-pillow/Pillow/tree/12.3.0 |
| SQLite | Python付属 | Public domain。https://sqlite.org/copyright.html |
| PyInstaller bootloader | 6.22.3 | GPL＋配布例外。https://github.com/pyinstaller/pyinstaller/tree/v6.22.3 |

第三者コードの通知とライセンスは、Windows配布物の`THIRD_PARTY_LICENSES`にまとめています。
Qtの第三者コードには、画像コーデック・フォント処理・圧縮などのコンポーネントが含まれます。
公式のモジュール別通知のコピーを添付しています。

Qt/PySide6の対応するソースは、上記の公式リポジトリのv6.12.0タグにあります。

ビルド手順はREADME.mdとbuild.pyを参照してください。ピクソート本体に外部通信処理はありません。
