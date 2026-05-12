# vivado-bridge (日本語版)

[English README](README.md)

Vivado を Claude Code から自動制御するための SKILL です。Vivado 側で Tcl によるソケットサーバーを動かしておくことで、Claude Code から各種 Tcl コマンドで Vivado を操作できるようになります。

Vivado はプロジェクトモード (GUI 表示) での動作になります。HDL ファイルなどのファイル操作については Claude Code から直接編集することを想定しています。

**この SKILL はClaudeCodeを使って開発しています。使用においては自己責任にてお願いします。**

## 特徴

- **SKILL の柔軟性と透明性** — SKILL として実装しているので、`SKILL.md` やコードをユーザー側で直接確認できます。ユーザー自身で編集・拡張も可能です。
- **シンプルな通信方式** — Tcl サーバー側 (Vivado) は受信したコマンドを実行するだけのシンプルな仕組みで、コマンドは Python スクリプト側で生成しています。そのため自由に Tcl スクリプトを組み合わせて実行できます。
- **セキュリティへの配慮** — `.env` で接続相手をローカル固定にしており、さらに Tcl サーバー側で `exec` など一部コマンドの実行を制限しています。

## できること

- Vivado の基本操作（HDL 作成から bit 生成、VIO / ILA を使った検証）
- テストベンチの作成・実行・結果確認
- HDL ファイルの検証・改善提案
- タイミングエラーなど各種レポートの解析と問題点の改善
- Git 管理

ClaudeCodeの機能活用により応用範囲はアイデア次第で無限大です。

## 主な機能

SKILL は **45 種類の高レベル operation** を、単一の CLI エントリポイント `scripts/vivado_op.py` から呼び出せる形で提供しています。標準入力に JSON リクエストを渡すと、標準出力に JSON レスポンスが返ってきます。カテゴリ一覧:

| カテゴリ | 主な機能 |
|---|---|
| `project.*`  | プロジェクト情報の取得 |
| `build.*`    | 合成・実装の起動と監視、bitstream 取得、タイミング解析 |
| `hardware.*` | Hardware Manager 操作、JTAG ターゲット制御、デバイス書き込み |
| `debug.*`    | VIO probe の列挙・読み書き、複数 probe の atomic 書き込み、ビルド時の VIO/ILA core helper |
| `ila.*`      | ILA の configure / set_triggers / arm / wait / CSV エクスポート・パース |
| `sim.*`      | xsim による testbench 実行、ログ要約 |
| `bridge.*`   | Vivado ログファイルのパス特定 |

詳細は `references/op_*.md` を参照してください。登録 op の一覧は `python scripts/vivado_op.py --list` で取得できます。

operation でカバーされていない任意の Tcl コマンドを実行したい場合は、`scripts/exec_tcl.py` を escape hatch として使えます。

## 動作確認環境

- **Claude Code**    
  Max プランで動作確認しています。フリープランやProプランではすぐに上限に達してしまうかもしれません。
  自律動作にはOpus 4.7のAutoModeが快適です。

- **Python 3.9 以降**    
  標準ライブラリのみで動作します。スクリーンショット機能を使う場合のみ追加パッケージが必要です（後述）。

- **Vivado 2024.1 / 2021.1**  
  どちらでも動作確認済みです。他のバージョンでも動くと思いますが、細かいパラメータなどに違いがあるかもしれません。ClaudeCodeに依頼すれば修正してくれると思います。
  なお、基本的な Vivado での FPGA 開発の経験があるユーザーを想定しており、初心者向けではありません。

- **Windows 11**  
  Linux / WSL でも動くかもしれませんが未確認です。
  スクリーンショット機能は Windows 限定です。

- **FPGA ボード: PYNQ-Z1**  
  他の環境でも問題ないはずです。ボード設定は Claude Code に依頼すれば OK です。

## 導入方法

### インストール

SKILL は単純なファイル群です。`vivado-bridge` ディレクトリを Claude Code の skills フォルダに置いてください。通常下記になると思います。

  ```text
  <project>/.claude/skills/vivado-bridge/
  ```

**git で取得する場合**:

```bash
cd <project>/.claude/skills/
git clone https://github.com/manahiyo831/vivado-bridge.git
```

**手動で配置する場合**: GitHub から ZIP をダウンロードして、中身を `<project>/.claude/skills/vivado-bridge/` に展開してください。

デフォルトの `.env` (`127.0.0.1:53729`) は同一マシンで Vivado を 1 本動かすケースに合わせており、基本的にそのままで動きます。

スクリーンショット機能はオプションで、`pywin32` と `Pillow` の追加が必要です。Vivado メイン画面のキャプチャを Claude Code から行いたい場合のみインストールしてください。:

```bash
pip install -r requirements-screenshot.txt
```

### 使い方

1. Vivado を起動して、何かしらプロジェクトを開いた状態にしてください。既存プロジェクトでも新規プロジェクトでも構いません。
2. Claude Code の作業フォルダは Vivado プロジェクトと同じにしておくと、Vivado のファイルを見つけやすくなります。Claude Code を起動したのち、 `/vivado-bridge` と打って SKILL を起動します。
3. Tcl サーバーへの通信確認が実施されます。未接続なら「Tcl サーバーを起動してほしい」という依頼が出るので、指示に従って Vivado の Tcl Console にコマンドを貼り付けて実行してください。
4. Tcl サーバーが正常に起動すると、次のようなバナーが Tcl Console に表示されます:

   ```
   ============================================================
   vivado-bridge v0.1.0 started
     Listening on : 127.0.0.1:53729
     Working dir  : ...
   ============================================================
   ```

**初回起動時の注意**: TCP 通信を行うため、OS のセキュリティ警告が出る場合があります。許可してください。

これで準備完了です。あとは Claude Code に「通信確認して」「プロジェクトの状態を確認して」など、自然言語で頼めば動いてくれます。


## 既知の問題

ビルド時にエラーなどが発生してダイアログが出たときの対処ができておりません。実際はその状態でもTCLサーバーは通信してClaudeCodeは作業を進められるので、それで処理が止まることはないようです。その場合はユーザー側で閉じてもらえれば問題ないです。

まれに使用中にVIVADOのTCLが動作しない状態となり、サーバーの応答がなくなる現象が見られています。原因調査中ですが解決に至っておりません。すみませんが、この場合はタスクマネージャーからVIVADOを強制終了するしかありません。

