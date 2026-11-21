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

## 角度指定の校正

WebとMCPから映像と位置を取得できることを確認し、サーバーを停止して校正を行うワン。
校正ではチルトを下限まで動かし、上へ戻した位置を0度の基準にするワン。

```sh
./ipcam calibrate --host 192.168.1.123
```

既定では校正結果を `ipcam.json` に保存するワン。
サーバー設定で `calibration_path` を指定した場合は、校正コマンドでも `--config` に同じパスを指定するワン。
校正後はサーバーを起動し、MCPの `camera_get_info` と `camera_snapshot`、続けて小さな角度差の `camera_goto` を確認するワン。

## 自動テスト

サーバーの依存パッケージをインストールした環境で実行するワン。

```sh
.venv/bin/python -m unittest discover -s tests -v
```

[サーバー仕様](ipcam_mcp/SPEC.md)
