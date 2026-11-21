# WTW-IPW308TTW/BのLANサーバー

Ubuntu上のサーバーから、ブラウザーでの映像表示とパン・チルト操作、Streamable HTTPでのMCP接続を利用できるワン。
対象機の内部モデル識別子は `IPC-G3B2D0-TY` ワン。
通信はローカルLAN内で行い、クラウドAPIは使わないワン。

## カメラ用SDカード

純正のセットアップ用アクセスポイントを起動する状態のカメラと、WPA/WPA2-PSKで接続できるWi-Fiを用意するワン。
カメラとUbuntuを同じIPv4 LANに置き、ルーター側でカメラのインターネット通信を遮断するワン。
SD側のDHCP処理はIPv4アドレスとネットマスクだけを適用するワン。

以下の `SD` はUbuntu上のSDカードのマウント先に置き換えるワン。
既存の `update_script.sh` がある場合は、作業前にカードの内容をバックアップするワン。
`--probe-ip` には同じLANでpingに応答する機器のIPv4を指定するワン。

```sh
SD=/media/"$USER"/IPCAM
cp -i sd-card/update_script.sh sd-card/sd_wifi_boot.sh sd-card/sd_http_watchdog.sh "$SD/"
python3 sd-card/configure.py "$SD" --ssid '自宅のSSID' --probe-ip 192.168.1.1
sync
```

生成した `wpa_supplicant.conf` には、Wi-Fi接続に使えるPSKが入るワン。
SDカードをカメラに挿して起動し、ルーターのDHCP割り当て一覧でカメラのIPを確認するワン。
以下は割り当てられたアドレスが `192.168.1.123` の例ワン。

```sh
curl --fail http://192.168.1.123:10081/device_model.txt
```

カメラのTCP 10081は、サーバーが読む機種情報とモーター位置を配信するワン。
工場初期化直後など、カメラが位置を保存するまでは `/motorPosition` がHTTP 404を返すことがあるワン。
HTTP版は接続確認時の404に対し、短い上下移動を各1回試して再取得するため、起動前に可動範囲を空けるワン。
SDの起動処理が失敗した場合は、電源を切ってSDカードを取り外し、起動し直せるワン。

## Ubuntuでの起動

Python 3.13、FFmpeg、curlを利用するワン。
設定例のIPv4アドレスとCIDRは自分のLANに合わせて変更するワン。

```sh
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
mkdir -p .config
cp ipcam-server.example.json .config/ipcam-server.json
.venv/bin/python -m ipcam_mcp --config .config/ipcam-server.json --port 9928
```

同じUbuntu上のブラウザーで `http://127.0.0.1:9928/` を開くワン。
MCPクライアントの接続先は `http://127.0.0.1:9928/mcp` ワン。
別PCから使う場合は、サーバーのLAN上のIPv4を `--host` に指定するワン。
ホスト名でアクセスする場合は `--allowed-host` も指定するワン。

systemdのユーザーサービスを使う場合、同梱のユニットは `~/ipcam-mcp` に配置した場合の例ワン。
別の場所で使う場合は、ユニットの `WorkingDirectory` と `ExecStart` を変更するワン。

```sh
mkdir -p ~/.config/systemd/user
cp service/ipcam-server.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now ipcam-server.service
```

## 原点復帰

操作画面の「原点復帰」を押すと、カメラが電源投入時と同じ動作を行い、パンとチルトの両軸が動くワン。
原点復帰の直後に右と左へ各0.1秒動かし、停止後のパンとチルトのステップ値を表示するワン。

## ステップと角度を指定して移動

操作画面で「ステップ」または「度」を選び、パンとチルトの目標を入力するワン。
片方を空欄にすると、その軸は開始時の位置を目標とするワン。
MCPではステップ指定に `camera_goto_steps`、角度指定に `camera_goto` を使うワン。

サーバーは移動後のステップを読み、誤差が20ステップを超える軸を最大3回補正するワン。
同じ軸で方向を反転する指定移動では、パンは300 ms、チルトは390 msを指令時間に足すワン。
移動速度はソースの初期値を使い、方向パッドまたは指定移動の初回で同じ方向のずれが3回続いたときに更新するワン。
初回で許容差に入った移動と、指定移動の補正パルスは速度を変えないワン。
更新値は接続先IPごとにサーバーのメモリーで保持し、再起動時に初期値に戻るワン。

## 音声再生

`GET /audio` をSSEで開き、最初の `session` イベントの値を後続のPOSTの `X-Audio-Session` に指定するワン。
`POST /audio` に8 kHzのモノラルPCMを送ると再生IDが返り、開始、区切り到達、終了をSSEで受け取れるワン。
ヘッダーと一時停止・再開・キャンセルの呼び出し方はサーバー仕様に記載しているワン。[音声再生API](ipcam_mcp/SPEC.md#音声再生)

WAVファイルを再生するには、サーバーの起動後に次を実行するワン。

```sh
python3 tools/play_wav.py --server http://127.0.0.1:9928 speech.wav
```

CLIは最長60秒のWAVをFFmpegで変換し、サーバーの終了通知を待つワン。
Ctrl+CでCLIを終了するとSSE接続が切れ、サーバーが再生を中止するワン。

## 自動テスト

サーバーの依存パッケージをインストールした環境で実行するワン。

```sh
.venv/bin/python -m unittest discover -s tests -v
```

[サーバー仕様](ipcam_mcp/SPEC.md)
